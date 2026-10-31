from datetime import datetime, time, timedelta, timezone
import hashlib
import logging
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone as dj_timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import CursorPagination
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse, OpenApiExample
from drf_spectacular.types import OpenApiTypes

from .http_cache import etag_matches, make_etag
from .models import InboundWebhookEvent, Payment, WebhookDelivery
from .permissions import HasAPIKey
from .providers import (
    ChapaProvider,
    ArifPayProvider,
    ProviderError,
    ProviderSignatureError,
    UnsupportedProviderStatus,
    get_provider,
)
from .routing import build_provider_chain, start_checkout
from .serializers import CheckoutSessionSerializer, PaymentListItemSerializer, PaymentSerializer
from .services import notify_status_change, resend_delivery, schedule_first_check, sync_payment_from_provider
from .tasks import log_payment_event
from .throttling import PaymentRateThrottle, PaymentReadRateThrottle, WebhookResendThrottle

logger = logging.getLogger(__name__)


def parse_iso_datetime(val: str, is_end_of_day: bool = False) -> datetime:
    if not isinstance(val, str) or not val.strip():
        raise ValueError("Invalid ISO 8601 date or datetime.")
    val = val.strip()
    d = parse_date(val)
    if d is not None:
        t = time.max.replace(microsecond=999999) if is_end_of_day else time.min
        return datetime.combine(d, t, tzinfo=timezone.utc)
    dt = parse_datetime(val)
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    try:
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    raise ValueError(f"Invalid ISO 8601 date or datetime: {val}")


class PaymentCursorPagination(CursorPagination):
    page_size = 20
    page_size_query_param = "limit"
    max_page_size = 100
    ordering = ("-created_at", "-id")

    def get_page_size(self, request):
        if self.page_size_query_param in request.query_params:
            raw_limit = request.query_params[self.page_size_query_param]
            try:
                val = int(raw_limit)
            except (ValueError, TypeError):
                raise ValidationError({"detail": f"Invalid limit: '{raw_limit}'. Must be an integer."})
            if val <= 0:
                raise ValidationError({"detail": f"Invalid limit: {val}. Must be greater than 0."})
            if self.max_page_size and val > self.max_page_size:
                return self.max_page_size
            return val
        return self.page_size

    def paginate_queryset(self, queryset, request, view=None):
        try:
            return super().paginate_queryset(queryset, request, view)
        except (NotFound, Exception) as exc:
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError({"detail": "Invalid or tampered cursor."})


@extend_schema(
    summary="List payments",
    description=(
        "Returns a cursor-paginated list of payments belonging to the authenticated merchant.\n\n"
        "Filter by `status`, `provider`, `reference`, `created_after`, and `created_before`.\n"
        "Use `limit` to control page size (max 100) and `cursor` for pagination."
    ),
    parameters=[
        OpenApiParameter("limit", OpenApiTypes.INT, OpenApiParameter.QUERY, description="Page size (1–100, default 20)."),
        OpenApiParameter("cursor", OpenApiTypes.STR, OpenApiParameter.QUERY, description="Opaque cursor returned in `next`/`previous` links."),
        OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY, description="Filter by payment status (pending, processing, succeeded, failed, cancelled, expired)."),
        OpenApiParameter("provider", OpenApiTypes.STR, OpenApiParameter.QUERY, description="Filter by provider name (e.g. `chapa`, `arifpay`, `fake`)."),
        OpenApiParameter("reference", OpenApiTypes.STR, OpenApiParameter.QUERY, description="Filter by merchant reference."),
        OpenApiParameter("created_after", OpenApiTypes.STR, OpenApiParameter.QUERY, description="ISO 8601 date or datetime. Only payments created at or after this value."),
        OpenApiParameter("created_before", OpenApiTypes.STR, OpenApiParameter.QUERY, description="ISO 8601 date or datetime. Only payments created at or before this value."),
    ],
    responses={
        200: OpenApiResponse(description="Paginated list of payments."),
        400: OpenApiResponse(description="Invalid filter parameter or cursor."),
        403: OpenApiResponse(description="Invalid or missing API key."),
        429: OpenApiResponse(description="Rate limit exceeded."),
    },
    tags=["Payments"],
)
@api_view(["GET"])
@permission_classes([HasAPIKey])
@throttle_classes([PaymentReadRateThrottle])
def list_payments(request):
    # Base queryset scoped by merchant and livemode
    queryset = Payment.objects.filter(
        merchant=request.merchant,
        livemode=request.livemode,
    )

    # status filter
    status_param = request.query_params.get("status")
    if status_param is not None:
        valid_statuses = {choice[0] for choice in Payment.STATUS_CHOICES}
        if status_param not in valid_statuses:
            return Response(
                {"detail": f"Invalid status: '{status_param}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(status=status_param)

    # provider filter
    provider_param = request.query_params.get("provider")
    if provider_param is not None:
        queryset = queryset.filter(provider=provider_param)

    # reference filter
    reference_param = request.query_params.get("reference")
    if reference_param is not None:
        queryset = queryset.filter(merchant_reference=reference_param)

    # created_after and created_before filters
    created_after_param = request.query_params.get("created_after")
    created_before_param = request.query_params.get("created_before")

    parsed_after = None
    parsed_before = None

    if created_after_param is not None:
        try:
            parsed_after = parse_iso_datetime(created_after_param, is_end_of_day=False)
        except ValueError:
            return Response(
                {"detail": f"Invalid created_after date or datetime: '{created_after_param}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if created_before_param is not None:
        try:
            parsed_before = parse_iso_datetime(created_before_param, is_end_of_day=True)
        except ValueError:
            return Response(
                {"detail": f"Invalid created_before date or datetime: '{created_before_param}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if parsed_after is not None and parsed_before is not None:
        if parsed_after > parsed_before:
            return Response(
                {"detail": "created_after cannot be later than created_before."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if parsed_after is not None:
        queryset = queryset.filter(created_at__gte=parsed_after)
    if parsed_before is not None:
        queryset = queryset.filter(created_at__lte=parsed_before)

    # Optimize column selection: only columns needed for response and ordering
    queryset = queryset.only(
        "id",
        "status",
        "amount",
        "provider",
        "merchant_reference",
        "checkout_url",
        "livemode",
        "created_at",
        "updated_at",
    )

    paginator = PaymentCursorPagination()
    page = paginator.paginate_queryset(queryset, request)
    serializer = PaymentListItemSerializer(page, many=True)
    return paginator.get_paginated_response(serializer.data)



@extend_schema(
    methods=['POST'],
    summary='Create a payment',
    description='Creates a new payment. Returns 200 with Idempotent-Replayed header if the Idempotency-Key was already used.',
    request=PaymentSerializer,
    responses={
        201: OpenApiResponse(response=PaymentSerializer, description='Payment created.'),
        200: OpenApiResponse(response=PaymentSerializer, description='Idempotent replay - existing payment returned. Includes `Idempotent-Replayed` header.'),
        400: OpenApiResponse(description='Validation error.'),
        403: OpenApiResponse(description='Invalid or missing API key.'),
    },
    tags=['Payments'],
)


@api_view(["GET", "POST"])
def payments_dispatch(request, *args, **kwargs):
    """
    Router that delegates to list_payments (GET) or create_payment (POST).
    Auth and throttling are handled by each sub-view so that they fire only
    once and use the correct per-method throttle bucket.
    """
    if request.method == "GET":
        return list_payments(request._request, *args, **kwargs)
    elif request.method == "POST":
        return create_payment(request._request, *args, **kwargs)
    return HttpResponseNotAllowed(["GET", "POST"])


@extend_schema(
    summary="Create a payment",
    description=(
        "Creates a new payment record. Uses the `idempotency_key` field in the request body \n"
        "for idempotency. If a payment with the same key already exists the existing payment \n"
        "is returned with status 200 and the response header `Idempotent-Replayed: true`.\n\n"
        "This is the low-level endpoint. For hosted checkout, use *Create checkout session* instead."
    ),
    request=PaymentSerializer,
    responses={
        201: OpenApiResponse(response=PaymentSerializer, description="Payment created."),
        200: OpenApiResponse(
            response=PaymentSerializer,
            description="Idempotent replay — existing payment returned. Includes `Idempotent-Replayed` header set to `true`.",
        ),
        400: OpenApiResponse(description="Validation error."),
        403: OpenApiResponse(description="Invalid or missing API key."),
        429: OpenApiResponse(description="Rate limit exceeded."),
    },
    examples=[
        OpenApiExample(
            "Create payment request",
            value={"idempotency_key": "order-1001", "amount": "150.00", "sender": "customer@example.com", "receiver": "Acme Corp"},
            request_only=True,
        ),
        OpenApiExample(
            "Create payment response",
            value={"id": 42, "idempotency_key": "order-1001", "amount": "150.00", "sender": "customer@example.com", "receiver": "Acme Corp", "status": "pending", "checkout_url": None, "expires_at": "2026-10-27T18:00:00Z", "livemode": False, "created_at": "2026-10-26T18:00:00Z", "updated_at": "2026-10-26T18:00:00Z"},
            response_only=True,
            status_codes=["201"],
        ),
    ],
    tags=["Payments"],
)
@api_view(["POST"])
@permission_classes([HasAPIKey])
@throttle_classes([PaymentRateThrottle])
def create_payment(request):
    idempotency_key = request.data.get("idempotency_key")
    livemode = getattr(request, "livemode", False)

    existing_payment = Payment.objects.filter(
        merchant=request.merchant,
        livemode=livemode,
        idempotency_key=idempotency_key,
    ).first()
    if existing_payment is not None:
        log_payment_event.delay(existing_payment.id, "payment created")
        response = Response(
            PaymentSerializer(existing_payment).data,
            status=status.HTTP_200_OK,
        )
        response["Idempotent-Replayed"] = "true"
        return response

    serializer = PaymentSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    expiry_seconds = getattr(settings, "PAYMENT_EXPIRY_SECONDS", 86400)
    expires_at = dj_timezone.now() + timedelta(seconds=expiry_seconds)

    try:
        with transaction.atomic():
            payment = serializer.save(
                merchant=request.merchant,
                livemode=livemode,
                expires_at=expires_at,
            )
    except IntegrityError:
        payment = Payment.objects.get(
            merchant=request.merchant,
            livemode=livemode,
            idempotency_key=idempotency_key,
        )
        response = Response(
            PaymentSerializer(payment).data,
            status=status.HTTP_200_OK,
        )
        response["Idempotent-Replayed"] = "true"
        return response

    return Response(
        PaymentSerializer(payment).data,
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    summary="Create a checkout session",
    description=(
        "Initiates a hosted checkout session with a payment provider.\n\n"
        "The `Idempotency-Key` request header is **required** (max 100 chars). "
        "Repeating a request with the same key returns the existing session with status 200 "
        "and the response header `Idempotent-Replayed: true`.\n\n"
        "On success the response contains a `checkout_url` that the customer should be "
        "redirected to. If all providers fail, a 502 is returned and the request can be retried "
        "with the same idempotency key."
    ),
    parameters=[
        OpenApiParameter(
            "Idempotency-Key",
            OpenApiTypes.STR,
            OpenApiParameter.HEADER,
            required=True,
            description="Unique key for idempotent session creation (max 100 characters).",
        ),
    ],
    request=CheckoutSessionSerializer,
    responses={
        201: OpenApiResponse(
            description="Checkout session created.",
            examples=[
                OpenApiExample(
                    "Checkout session created",
                    value={"id": 42, "status": "pending", "provider": "chapa", "reference": "ORDER-1001", "checkout_url": "https://checkout.chapa.co/pay/test-ref", "expires_at": "2026-10-27T18:00:00Z", "livemode": False, "created_at": "2026-10-26T18:00:00Z"},
                ),
            ],
        ),
        200: OpenApiResponse(
            description="Idempotent replay — existing session returned. Includes `Idempotent-Replayed` header set to `true`.",
        ),
        400: OpenApiResponse(description="Validation error or missing/invalid Idempotency-Key."),
        403: OpenApiResponse(description="Invalid or missing API key."),
        429: OpenApiResponse(description="Rate limit exceeded."),
        502: OpenApiResponse(description="Payment provider unavailable."),
    },
    examples=[
        OpenApiExample(
            "Checkout session request",
            value={"amount": "150.00", "reference": "ORDER-1001", "return_url": "https://example.com/complete", "provider": "chapa", "customer_email": "customer@example.com"},
            request_only=True,
        ),
    ],
    tags=["Checkout"],
)
@api_view(["POST"])
@permission_classes([HasAPIKey])
@throttle_classes([PaymentRateThrottle])
def create_checkout_session(request):
    idempotency_key = request.headers.get("Idempotency-Key") or request.META.get("HTTP_IDEMPOTENCY_KEY")
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()

    if not idempotency_key or len(idempotency_key) > 100:
        return Response(
            {"detail": "Idempotency-Key header is required and must not exceed 100 characters."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = CheckoutSessionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    validated_data = serializer.validated_data

    # Validate the explicitly requested provider now (before touching the DB).
    requested_provider_name = validated_data.get("provider") or None
    if requested_provider_name:
        try:
            get_provider(requested_provider_name)
        except ProviderError:
            return Response(
                {"detail": f"Unknown or unavailable provider: '{requested_provider_name}'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

    allow_fallback = validated_data.get("allow_fallback", True)
    livemode = getattr(request, "livemode", False)

    expires_in_minutes = validated_data.get("expires_in_minutes")
    if expires_in_minutes is not None:
        lifetime_seconds = expires_in_minutes * 60
    else:
        lifetime_seconds = getattr(settings, "PAYMENT_EXPIRY_SECONDS", 86400)
    expires_at = dj_timezone.now() + timedelta(seconds=lifetime_seconds)

    # --- Idempotency: return 200 if session already exists ---
    existing_payment = Payment.objects.filter(
        merchant=request.merchant,
        livemode=livemode,
        idempotency_key=idempotency_key,
    ).first()

    if existing_payment and (existing_payment.checkout_url or existing_payment.status != "pending"):
        response = Response(
            {
                "id": existing_payment.id,
                "status": existing_payment.status,
                "provider": existing_payment.provider,
                "reference": existing_payment.merchant_reference,
                "checkout_url": existing_payment.checkout_url if existing_payment.status == "pending" else None,
                "expires_at": existing_payment.expires_at.isoformat() if existing_payment.expires_at else None,
                "livemode": existing_payment.livemode,
                "created_at": existing_payment.created_at,
            },
            status=status.HTTP_200_OK,
        )
        response["Idempotent-Replayed"] = "true"
        return response

    customer_email = validated_data.get("customer_email")
    sender = customer_email or "customer"
    receiver = request.merchant.name
    amount = validated_data["amount"]
    reference = validated_data.get("reference")
    return_url = validated_data.get("return_url")

    # Build provider candidate chain (considering merchant defaults and prior attempts)
    if not existing_payment:
        class _NewPayment:
            provider_attempts = type('qs', (), {'filter': staticmethod(lambda **_: type('qs2', (), {'first': staticmethod(lambda: None)})())})()  # noqa: E501

        chain = build_provider_chain(
            request.merchant,
            requested_provider_name,
            allow_fallback,
            _NewPayment(),
            livemode=livemode,
        )
    else:
        chain = build_provider_chain(
            request.merchant,
            requested_provider_name,
            allow_fallback,
            existing_payment,
            livemode=livemode,
        )

    if livemode and not chain:
        return Response(
            {"detail": "No live-capable provider is configured."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    primary_provider_name = chain[0]

    if not existing_payment:
        try:
            with transaction.atomic():
                payment = Payment.objects.create(
                    merchant=request.merchant,
                    livemode=livemode,
                    idempotency_key=idempotency_key,
                    amount=amount,
                    sender=sender,
                    receiver=receiver,
                    merchant_reference=reference,
                    return_url=return_url,
                    provider=primary_provider_name,
                    status="pending",
                    expires_at=expires_at,
                )
        except IntegrityError:
            payment = Payment.objects.get(
                merchant=request.merchant,
                livemode=livemode,
                idempotency_key=idempotency_key,
            )
            if payment.checkout_url or payment.status != "pending":
                response = Response(
                    {
                        "id": payment.id,
                        "status": payment.status,
                        "provider": payment.provider,
                        "reference": payment.merchant_reference,
                        "checkout_url": payment.checkout_url if payment.status == "pending" else None,
                        "expires_at": payment.expires_at.isoformat() if payment.expires_at else None,
                        "livemode": payment.livemode,
                        "created_at": payment.created_at,
                    },
                    status=status.HTTP_200_OK,
                )
                response["Idempotent-Replayed"] = "true"
                return response
            # Re-build chain for the existing payment's prior attempts.
            chain = build_provider_chain(
                request.merchant,
                requested_provider_name,
                allow_fallback,
                payment,
                livemode=livemode,
            )
    else:
        payment = existing_payment

    customer = {}
    if customer_email:
        customer["email"] = customer_email
    if validated_data.get("customer_first_name"):
        customer["first_name"] = validated_data["customer_first_name"]
    if validated_data.get("customer_last_name"):
        customer["last_name"] = validated_data["customer_last_name"]

    with transaction.atomic():
        locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if locked_payment.checkout_url or locked_payment.status != "pending":
            response = Response(
                {
                    "id": locked_payment.id,
                    "status": locked_payment.status,
                    "provider": locked_payment.provider,
                    "reference": locked_payment.merchant_reference,
                    "checkout_url": locked_payment.checkout_url if locked_payment.status == "pending" else None,
                    "expires_at": locked_payment.expires_at.isoformat() if locked_payment.expires_at else None,
                    "livemode": locked_payment.livemode,
                    "created_at": locked_payment.created_at,
                },
                status=status.HTTP_200_OK,
            )
            response["Idempotent-Replayed"] = "true"
            return response

        # start_checkout never raises; attempt rows are committed with the transaction.
        routing_result = start_checkout(
            locked_payment,
            customer=customer or None,
            chain=chain,
        )

        if not routing_result.success:
            resp = Response(
                {"detail": "Payment provider unavailable. Retry with the same Idempotency-Key."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
            if getattr(routing_result, "retry_after", None) is not None:
                resp["Retry-After"] = str(routing_result.retry_after)
            return resp

        locked_payment.provider = routing_result.provider_name
        locked_payment.provider_reference = routing_result.provider_reference
        locked_payment.checkout_url = routing_result.checkout_url
        locked_payment.save(
            update_fields=["provider", "provider_reference", "checkout_url", "updated_at"]
        )
        schedule_first_check(locked_payment)

    return Response(
        {
            "id": locked_payment.id,
            "status": locked_payment.status,
            "provider": locked_payment.provider,
            "reference": locked_payment.merchant_reference,
            "checkout_url": locked_payment.checkout_url if locked_payment.status == "pending" else None,
            "expires_at": locked_payment.expires_at.isoformat() if locked_payment.expires_at else None,
            "livemode": locked_payment.livemode,
            "created_at": locked_payment.created_at,
        },
        status=status.HTTP_201_CREATED,
    )


@extend_schema(
    summary="Retrieve a payment",
    description="Returns full details of a single payment by its ID, scoped to the authenticated merchant.",
    parameters=[
        OpenApiParameter(
            name="If-None-Match",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.HEADER,
            required=False,
            description="ETag from a previous response to allow conditional GET (304 Not Modified).",
        ),
        OpenApiParameter(
            name="ETag",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.HEADER,
            description="Strong entity tag representing the payment resource state.",
            response=[200, 304],
        ),
        OpenApiParameter(
            name="Cache-Control",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.HEADER,
            description="Caching directives (private, no-cache).",
            response=[200, 304],
        ),
    ],
    responses={
        200: OpenApiResponse(response=PaymentSerializer, description="Payment details."),
        304: OpenApiResponse(description="Payment not modified (conditional GET match)."),
        403: OpenApiResponse(description="Invalid or missing API key."),
        404: OpenApiResponse(description="Payment not found."),
        429: OpenApiResponse(description="Rate limit exceeded."),
    },
    tags=["Payments"],
)
@api_view(["GET"])
@permission_classes([HasAPIKey])
@throttle_classes([PaymentReadRateThrottle])
def payment_detail(request, pk):
    payment = get_object_or_404(
        Payment,
        pk=pk,
        merchant=request.merchant,
        livemode=getattr(request, "livemode", False),
    )
    serializer_data = PaymentSerializer(payment).data
    body_bytes = JSONRenderer().render(serializer_data)
    etag = make_etag(body_bytes)

    if_none_match = request.headers.get("If-None-Match") or request.META.get("HTTP_IF_NONE_MATCH")
    if etag_matches(if_none_match, etag):
        return Response(
            status=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": "private, no-cache"},
        )

    return Response(
        serializer_data,
        status=status.HTTP_200_OK,
        headers={"ETag": etag, "Cache-Control": "private, no-cache"},
    )


@extend_schema(
    summary="Verify a payment",
    description=(
        "Queries the payment provider for the latest status and synchronises it locally.\n\n"
        "Safe to call repeatedly. Use this to confirm a webhook notification before "
        "fulfilling an order."
    ),
    responses={
        200: OpenApiResponse(
            description="Current payment status.",
            examples=[
                OpenApiExample(
                    "Verified payment",
                    value={"id": 42, "status": "succeeded", "updated_at": "2026-10-26T18:05:00Z"},
                ),
            ],
        ),
        403: OpenApiResponse(description="Invalid or missing API key."),
        404: OpenApiResponse(description="Payment not found."),
        502: OpenApiResponse(description="Payment provider unavailable."),
    },
    tags=["Payments"],
)
@api_view(["GET"])
@permission_classes([HasAPIKey])
def verify_payment(request, pk):
    """
    Verify payment status against the provider.
    Asks the provider and is safe to repeat.
    """
    payment = get_object_or_404(
        Payment,
        pk=pk,
        merchant=request.merchant,
        livemode=getattr(request, "livemode", False),
    )

    if payment.provider:
        try:
            sync_result = sync_payment_from_provider(payment.pk)
            payment = sync_result.payment
        except ProviderError:
            return Response(
                {"detail": "Payment provider unavailable. Try again."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

    return Response(
        {
            "id": payment.id,
            "status": payment.status,
            "updated_at": payment.updated_at,
        },
        status=status.HTTP_200_OK,
    )


# Statuses that cannot be cancelled because they are already terminal
_NON_CANCELLABLE_STATUSES = {"processing", "succeeded", "failed", "expired"}


@extend_schema(
    summary="Cancel a payment",
    description=(
        "Cancels a pending payment that has **no** provider checkout session.\n\n"
        "Provider-backed payments cannot be cancelled through this endpoint (returns 409). "
        "Already-cancelled payments return 200 idempotently."
    ),
    request=None,
    responses={
        200: OpenApiResponse(
            description="Payment cancelled (or was already cancelled).",
            examples=[
                OpenApiExample(
                    "Cancelled payment",
                    value={"id": 42, "status": "cancelled"},
                ),
            ],
        ),
        403: OpenApiResponse(description="Invalid or missing API key."),
        404: OpenApiResponse(description="Payment not found."),
        409: OpenApiResponse(description="Payment cannot be cancelled (provider-backed or terminal state)."),
    },
    tags=["Payments"],
)
@api_view(["POST"])
@permission_classes([HasAPIKey])
def cancel_payment(request, pk):
    with transaction.atomic():
        payment = get_object_or_404(
            Payment.objects.select_for_update(),
            pk=pk,
            merchant=request.merchant,
            livemode=getattr(request, "livemode", False),
        )

        # Cancel guard: reject cancellation for provider-backed payments
        if payment.provider:
            # TODO: Late-payment risk: cancelling a payment in SafePay while a provider
            # checkout session is still active creates a race where the customer might
            # complete payment at the provider after SafePay marked it cancelled.
            # Cancelling provider payments safely requires provider-side void/cancel API
            # support and webhook reconciliation.
            return Response(
                {"detail": "Cancelling provider payments is not supported yet."},
                status=status.HTTP_409_CONFLICT,
            )

        if payment.status == "cancelled":
            # Already cancelled — idempotent, no history row, no webhook.
            return Response(
                {"id": payment.id, "status": "cancelled"},
                status=status.HTTP_200_OK,
            )

        if payment.status in _NON_CANCELLABLE_STATUSES:
            return Response(
                {"detail": "Payment can no longer be cancelled."},
                status=status.HTTP_409_CONFLICT,
            )

        # status is "pending" — the only state that allows cancellation.
        payment.transition_to("cancelled")

    # Notify outside the atomic block so the DB row is visible before the
    # Celery task runs, but we call it regardless because cancelled is terminal.
    notify_status_change(payment)

    return Response(
        {"id": payment.id, "status": "cancelled"},
        status=status.HTTP_200_OK,
    )


@csrf_exempt
@require_POST
@extend_schema(exclude=True)
def chapa_webhook(request):
    # Existing Chapa webhook – unchanged
    body = request.body
    body_sha256 = hashlib.sha256(body).hexdigest()

    provider = ChapaProvider()
    try:
        event = provider.parse_webhook(request.headers, body)
    except ProviderSignatureError:
        InboundWebhookEvent.objects.create(
            provider="chapa",
            signature_valid=False,
            outcome="signature_rejected",
            body_sha256=body_sha256,
        )
        return JsonResponse({"detail": "Invalid signature."}, status=401)
    except UnsupportedProviderStatus as exc:
        InboundWebhookEvent.objects.create(
            provider="chapa",
            signature_valid=True,
            provider_reference=getattr(exc, "provider_reference", None),
            event_type=getattr(exc, "event_type", None),
            outcome="ignored_unsupported",
            body_sha256=body_sha256,
        )
        return JsonResponse({"status": "ignored"}, status=200)
    except ProviderError as exc:
        if "Chapa webhook secret is not configured" in str(exc):
            InboundWebhookEvent.objects.create(
                provider="chapa",
                signature_valid=False,
                outcome="not_configured",
                body_sha256=body_sha256,
            )
            return JsonResponse(
                {"detail": "Webhook receiver is not configured."},
                status=503,
            )

        InboundWebhookEvent.objects.create(
            provider="chapa",
            signature_valid=True,
            outcome="invalid_payload",
            body_sha256=body_sha256,
        )
        return JsonResponse({"detail": "Invalid webhook payload."}, status=400)
    except Exception:
        InboundWebhookEvent.objects.create(
            provider="chapa",
            signature_valid=False,
            outcome="signature_rejected",
            body_sha256=body_sha256,
        )
        return JsonResponse({"detail": "Invalid signature."}, status=401)

    tx_ref = event.provider_reference
    event_type = None
    if isinstance(event.raw, dict):
        event_type = event.raw.get("event") or event.raw.get("type")
        if not event_type and isinstance(event.raw.get("data"), dict):
            event_type = event.raw["data"].get("event") or event.raw["data"].get("type")

    payment = Payment.objects.filter(provider="chapa", provider_reference=tx_ref).first()
    if not payment:
        InboundWebhookEvent.objects.create(
            provider="chapa",
            signature_valid=True,
            provider_reference=tx_ref,
            event_type=event_type,
            outcome="unknown_reference",
            body_sha256=body_sha256,
        )
        return JsonResponse({"status": "ignored"}, status=200)

    try:
        sync_result = sync_payment_from_provider(payment.id)
    except ProviderError:
        InboundWebhookEvent.objects.create(
            provider="chapa",
            signature_valid=True,
            provider_reference=tx_ref,
            event_type=event_type,
            outcome="verify_failed",
            body_sha256=body_sha256,
        )
        return JsonResponse(
            {"detail": "Temporarily unable to confirm. Retry later."},
            status=503,
        )

    if sync_result.outcome == "updated":
        event_outcome = "processed_updated"
    elif sync_result.outcome == "unchanged":
        event_outcome = "processed_unchanged"
    else:
        event_outcome = "mismatch"

    InboundWebhookEvent.objects.create(
        provider="chapa",
        signature_valid=True,
        provider_reference=tx_ref,
        event_type=event_type,
        outcome=event_outcome,
        body_sha256=body_sha256,
    )
    return JsonResponse({"status": "ok"}, status=200)


@csrf_exempt
@require_POST
@extend_schema(exclude=True)
def arifpay_webhook(request):
    """Handle ArifPay webhook callbacks.

    Since ArifPay does not provide a signature verification mechanism, we consider the webhook
    payload as trusted after basic parsing. The function mirrors the behaviour of the existing
    ``chapa_webhook`` view, creating ``InboundWebhookEvent`` records and synchronising the payment
    state via ``sync_payment_from_provider``.
    """
    body = request.body
    body_sha256 = hashlib.sha256(body).hexdigest()

    provider = ArifPayProvider()
    try:
        event = provider.parse_webhook(request.headers, body)
    except UnsupportedProviderStatus as exc:
        InboundWebhookEvent.objects.create(
            provider="arifpay",
            signature_valid=True,
            provider_reference=getattr(exc, "provider_reference", None),
            event_type=getattr(exc, "event_type", None),
            outcome="ignored_unsupported",
            body_sha256=body_sha256,
        )
        return JsonResponse({"status": "ignored"}, status=200)
    except ProviderError as exc:
        # Record malformed payloads; no signature verification available.
        InboundWebhookEvent.objects.create(
            provider="arifpay",
            signature_valid=False,
            outcome="invalid_payload",
            body_sha256=body_sha256,
        )
        return JsonResponse({"detail": "Invalid webhook payload."}, status=400)

    tx_ref = event.provider_reference
    event_type = None
    if isinstance(event.raw, dict):
        event_type = event.raw.get("event") or event.raw.get("type")
        if not event_type and isinstance(event.raw.get("data"), dict):
            event_type = event.raw["data"].get("event") or event.raw["data"].get("type")

    payment = Payment.objects.filter(provider="arifpay", provider_reference=tx_ref).first()
    if not payment:
        InboundWebhookEvent.objects.create(
            provider="arifpay",
            signature_valid=True,
            provider_reference=tx_ref,
            event_type=event_type,
            outcome="unknown_reference",
            body_sha256=body_sha256,
        )
        return JsonResponse({"status": "ignored"}, status=200)

    try:
        sync_result = sync_payment_from_provider(payment.id)
    except ProviderError:
        InboundWebhookEvent.objects.create(
            provider="arifpay",
            signature_valid=True,
            provider_reference=tx_ref,
            event_type=event_type,
            outcome="verify_failed",
            body_sha256=body_sha256,
        )
        return JsonResponse({"detail": "Temporarily unable to confirm. Retry later."}, status=503)

    if sync_result.outcome == "updated":
        event_outcome = "processed_updated"
    elif sync_result.outcome == "unchanged":
        event_outcome = "processed_unchanged"
    else:
        event_outcome = "mismatch"

    InboundWebhookEvent.objects.create(
        provider="arifpay",
        signature_valid=True,
        provider_reference=tx_ref,
        event_type=event_type,
        outcome=event_outcome,
        body_sha256=body_sha256,
    )
    return JsonResponse({"status": "ok"}, status=200)


# ---------------------------------------------------------------------------
# Webhook Delivery Log
# ---------------------------------------------------------------------------

import urllib.parse as _urlparse

# Ordered list of (pattern, code) for error_code derivation.
# Patterns are checked in order; the first match wins.
_LAST_ERROR_PATTERNS = [
    ("blocked_target", "blocked_target"),
    ("Timeout", "timeout"),
    ("timeout", "timeout"),
    ("ConnectionError", "connection_error"),
    ("connection_error", "connection_error"),
    ("Redirect rejected", "redirect"),
    ("redirect", "redirect"),
]


def _derive_error_code(last_error: str) -> str:
    """
    Map a raw last_error string to a short public error_code.

    Rules (in precedence order):
    - None / empty string → ""
    - Exact "blocked_target" → "blocked_target"
    - Contains "Timeout" or "timeout" → "timeout"
    - Contains "ConnectionError" or "connection_error" → "connection_error"
    - Contains "Redirect rejected" or "redirect" → "redirect"
    - Starts with "Non-2xx response: 4" → "http_4xx"
    - Starts with "Non-2xx response: 5" → "http_5xx"
    - Otherwise → "unknown"
    """
    if not last_error:
        return ""
    err = last_error
    # Exact match for blocked_target
    if err == "blocked_target":
        return "blocked_target"
    # Timeout variants (class names or messages)
    if "Timeout" in err or "timeout" in err:
        return "timeout"
    # Connection error variants
    if "ConnectionError" in err or "connection_error" in err:
        return "connection_error"
    # Redirect
    if "Redirect rejected" in err or "redirect" in err:
        return "redirect"
    # HTTP 4xx / 5xx via the "Non-2xx response: NNN" message
    if err.startswith("Non-2xx response: "):
        try:
            code = int(err.split("Non-2xx response: ", 1)[1].strip())
            if 400 <= code < 500:
                return "http_4xx"
            if 500 <= code < 600:
                return "http_5xx"
        except (ValueError, IndexError):
            pass
    return "unknown"


def _delivery_to_dict(delivery) -> dict:
    """
    Serialize a WebhookDelivery to the allowed field set.
    Never exposes raw last_error, full target_url, payload or signatures.
    """
    # target_host: hostname only, strip scheme/path/query/port/credentials
    try:
        parsed = _urlparse.urlsplit(delivery.target_url)
        # netloc may contain port (host:port); split it off
        target_host = parsed.hostname or ""
    except Exception:
        target_host = ""

    return {
        "id": delivery.id,
        "event_id": str(delivery.event_id),
        "event_type": delivery.event_type,
        "payment_id": delivery.payment_id,
        "status": delivery.status,
        "attempt_count": delivery.attempt_count,
        "max_attempts": delivery.max_attempts,
        "last_response_code": delivery.last_response_code,
        "error_code": _derive_error_code(delivery.last_error or ""),
        "target_host": target_host,
        "livemode": delivery.payment.livemode,
        "created_at": delivery.created_at,
        "updated_at": delivery.updated_at,
    }


class WebhookDeliveryCursorPagination(CursorPagination):
    """Cursor pagination for webhook deliveries: newest first, id tie-break."""
    page_size = 20
    page_size_query_param = "limit"
    max_page_size = 100
    ordering = ("-created_at", "-id")

    def get_page_size(self, request):
        if self.page_size_query_param in request.query_params:
            raw_limit = request.query_params[self.page_size_query_param]
            try:
                val = int(raw_limit)
            except (ValueError, TypeError):
                raise ValidationError({"detail": f"Invalid limit: '{raw_limit}'. Must be an integer."})
            if val <= 0:
                raise ValidationError({"detail": f"Invalid limit: {val}. Must be greater than 0."})
            if self.max_page_size and val > self.max_page_size:
                return self.max_page_size
            return val
        return self.page_size

    def paginate_queryset(self, queryset, request, view=None):
        try:
            return super().paginate_queryset(queryset, request, view)
        except (NotFound, Exception) as exc:
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError({"detail": "Invalid or tampered cursor."})


_VALID_DELIVERY_STATUSES = {choice[0] for choice in WebhookDelivery.STATUS_CHOICES}

_DELIVERY_LIST_EXAMPLE = {
    "next": "https://api.safepay.example/api/webhook-deliveries/?cursor=abc123",
    "previous": None,
    "results": [
        {
            "id": 7,
            "event_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "event_type": "payment.cancelled",
            "payment_id": 42,
            "status": "failed",
            "attempt_count": 5,
            "max_attempts": 5,
            "last_response_code": None,
            "error_code": "connection_error",
            "target_host": "hooks.example.com",
            "livemode": False,
            "created_at": "2026-10-27T15:00:00Z",
            "updated_at": "2026-10-27T15:05:00Z",
        }
    ],
}

_DELIVERY_DETAIL_EXAMPLE = {
    "id": 7,
    "event_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "event_type": "payment.cancelled",
    "payment_id": 42,
    "status": "failed",
    "attempt_count": 5,
    "max_attempts": 5,
    "last_response_code": None,
    "error_code": "connection_error",
    "target_host": "hooks.example.com",
    "livemode": False,
    "created_at": "2026-10-27T15:00:00Z",
    "updated_at": "2026-10-27T15:05:00Z",
}


@extend_schema(
    summary="List webhook deliveries",
    description=(
        "Returns a cursor-paginated list of outbound webhook delivery attempts belonging "
        "to the authenticated merchant's payments, filtered by livemode.\n\n"
        "**Safe fields only**: `error_code` is a short public code; raw error messages, "
        "signatures, the full target URL (path/query/credentials) and the payload are "
        "never returned.\n\n"
        "Filter parameters (all optional, AND logic):\n"
        "- `status` — one of `pending`, `succeeded`, `failed`\n"
        "- `event_type` — exact match (e.g. `payment.cancelled`)\n"
        "- `payment_id` — integer payment primary key\n"
        "- `created_after` / `created_before` — ISO 8601 date or datetime"
    ),
    parameters=[
        OpenApiParameter("limit", OpenApiTypes.INT, OpenApiParameter.QUERY,
                         description="Page size (1–100, default 20)."),
        OpenApiParameter("cursor", OpenApiTypes.STR, OpenApiParameter.QUERY,
                         description="Opaque cursor returned in next/previous links."),
        OpenApiParameter("status", OpenApiTypes.STR, OpenApiParameter.QUERY,
                         description="Filter by delivery status: pending, succeeded, or failed."),
        OpenApiParameter("event_type", OpenApiTypes.STR, OpenApiParameter.QUERY,
                         description="Filter by event type, e.g. payment.cancelled."),
        OpenApiParameter("payment_id", OpenApiTypes.INT, OpenApiParameter.QUERY,
                         description="Filter by payment ID (integer)."),
        OpenApiParameter("created_after", OpenApiTypes.STR, OpenApiParameter.QUERY,
                         description="ISO 8601 date or datetime lower bound."),
        OpenApiParameter("created_before", OpenApiTypes.STR, OpenApiParameter.QUERY,
                         description="ISO 8601 date or datetime upper bound."),
    ],
    responses={
        200: OpenApiResponse(
            description="Paginated list of webhook deliveries.",
            examples=[OpenApiExample("List response", value=_DELIVERY_LIST_EXAMPLE)],
        ),
        400: OpenApiResponse(description="Invalid filter parameter or cursor."),
        403: OpenApiResponse(description="Invalid or missing API key."),
        429: OpenApiResponse(description="Rate limit exceeded."),
    },
    tags=["Webhook Deliveries"],
)
@api_view(["GET"])
@permission_classes([HasAPIKey])
@throttle_classes([PaymentReadRateThrottle])
def list_webhook_deliveries(request):
    """List webhook deliveries for the authenticated merchant (scoped by livemode)."""
    queryset = WebhookDelivery.objects.select_related("payment").filter(
        payment__merchant=request.merchant,
        payment__livemode=request.livemode,
    )

    # --- status filter ---
    status_param = request.query_params.get("status")
    if status_param is not None:
        if status_param not in _VALID_DELIVERY_STATUSES:
            return Response(
                {"detail": f"Invalid status: '{status_param}'. Valid values: {sorted(_VALID_DELIVERY_STATUSES)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(status=status_param)

    # --- event_type filter ---
    event_type_param = request.query_params.get("event_type")
    if event_type_param is not None:
        queryset = queryset.filter(event_type=event_type_param)

    # --- payment_id filter ---
    payment_id_param = request.query_params.get("payment_id")
    if payment_id_param is not None:
        try:
            payment_id_int = int(payment_id_param)
        except (ValueError, TypeError):
            return Response(
                {"detail": f"Invalid payment_id: '{payment_id_param}'. Must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(payment_id=payment_id_int)

    # --- date filters (same rules as payments list) ---
    created_after_param = request.query_params.get("created_after")
    created_before_param = request.query_params.get("created_before")

    parsed_after = None
    parsed_before = None

    if created_after_param is not None:
        try:
            parsed_after = parse_iso_datetime(created_after_param, is_end_of_day=False)
        except ValueError:
            return Response(
                {"detail": f"Invalid created_after date or datetime: '{created_after_param}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if created_before_param is not None:
        try:
            parsed_before = parse_iso_datetime(created_before_param, is_end_of_day=True)
        except ValueError:
            return Response(
                {"detail": f"Invalid created_before date or datetime: '{created_before_param}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if parsed_after is not None and parsed_before is not None:
        if parsed_after > parsed_before:
            return Response(
                {"detail": "created_after cannot be later than created_before."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if parsed_after is not None:
        queryset = queryset.filter(created_at__gte=parsed_after)
    if parsed_before is not None:
        queryset = queryset.filter(created_at__lte=parsed_before)

    paginator = WebhookDeliveryCursorPagination()
    page = paginator.paginate_queryset(queryset, request)
    data = [_delivery_to_dict(d) for d in page]
    return paginator.get_paginated_response(data)


@extend_schema(
    summary="Retrieve a webhook delivery",
    description=(
        "Returns details of a single webhook delivery attempt. "
        "Returns 404 if the delivery does not belong to the authenticated merchant "
        "and livemode, or does not exist."
    ),
    responses={
        200: OpenApiResponse(
            description="Webhook delivery detail.",
            examples=[OpenApiExample("Detail response", value=_DELIVERY_DETAIL_EXAMPLE)],
        ),
        403: OpenApiResponse(description="Invalid or missing API key."),
        404: OpenApiResponse(description="Delivery not found."),
        429: OpenApiResponse(description="Rate limit exceeded."),
    },
    tags=["Webhook Deliveries"],
)
@api_view(["GET"])
@permission_classes([HasAPIKey])
@throttle_classes([PaymentReadRateThrottle])
def webhook_delivery_detail(request, pk):
    """Return a single webhook delivery scoped to the authenticated merchant."""
    delivery = get_object_or_404(
        WebhookDelivery.objects.select_related("payment"),
        pk=pk,
        payment__merchant=request.merchant,
        payment__livemode=request.livemode,
    )
    return Response(_delivery_to_dict(delivery), status=status.HTTP_200_OK)


@extend_schema(
    summary="Resend a webhook delivery",
    description=(
        "Re-queues a **failed** (dead-lettered) webhook delivery for a new delivery attempt. "
        "The event_id, event_type, target_url and payload are preserved. "
        "Attempt count is reset to 0.\n\n"
        "- Returns **202** with `{\"id\": N, \"status\": \"pending\"}` when successfully queued.\n"
        "- Returns **409** if the delivery is not in `failed` state.\n"
        "- Returns **404** if the delivery does not belong to the authenticated merchant / livemode."
    ),
    request=None,
    responses={
        202: OpenApiResponse(
            description="Delivery re-queued.",
            examples=[OpenApiExample("Resend queued", value={"id": 7, "status": "pending"})],
        ),
        403: OpenApiResponse(description="Invalid or missing API key."),
        404: OpenApiResponse(description="Delivery not found or belongs to another merchant."),
        409: OpenApiResponse(
            description="Delivery is not in failed state.",
            examples=[OpenApiExample("Not failed", value={"detail": "Only failed deliveries can be resent."})],
        ),
        429: OpenApiResponse(description="Rate limit exceeded (webhook_resend bucket)."),
    },
    tags=["Webhook Deliveries"],
)
@api_view(["POST"])
@permission_classes([HasAPIKey])
@throttle_classes([WebhookResendThrottle])
def webhook_delivery_resend(request, pk):
    """Re-queue a failed webhook delivery."""
    # 404 guard: must belong to the authenticated merchant and livemode
    delivery = get_object_or_404(
        WebhookDelivery.objects.select_related("payment"),
        pk=pk,
        payment__merchant=request.merchant,
        payment__livemode=request.livemode,
    )

    result = resend_delivery(delivery.id)

    if result == "queued":
        # Refresh to get updated status from the service
        delivery.refresh_from_db()
        return Response(
            {"id": delivery.id, "status": "pending"},
            status=status.HTTP_202_ACCEPTED,
        )

    # result == "not_failed"
    return Response(
        {"detail": "Only failed deliveries can be resent."},
        status=status.HTTP_409_CONFLICT,
    )
