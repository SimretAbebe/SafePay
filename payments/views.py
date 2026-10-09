import logging
from django.conf import settings
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.response import Response

from .models import Payment
from .permissions import HasAPIKey
from .providers import ProviderError, get_provider
from .serializers import CheckoutSessionSerializer, PaymentSerializer
from .services import notify_status_change
from .tasks import log_payment_event
from .throttling import PaymentRateThrottle

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([HasAPIKey])
@throttle_classes([PaymentRateThrottle])
def create_payment(request):
    idempotency_key = request.data.get("idempotency_key")

    existing_payment = Payment.objects.filter(
        merchant=request.merchant,
        idempotency_key=idempotency_key,
    ).first()
    if existing_payment is not None:
        log_payment_event.delay(existing_payment.id, "payment created")
        return Response(
            PaymentSerializer(existing_payment).data,
            status=status.HTTP_200_OK,
        )

    serializer = PaymentSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        with transaction.atomic():
            payment = serializer.save(merchant=request.merchant)
    except IntegrityError:
        payment = Payment.objects.get(
            merchant=request.merchant,
            idempotency_key=idempotency_key,
        )
        return Response(
            PaymentSerializer(payment).data,
            status=status.HTTP_200_OK,
        )

    return Response(
        PaymentSerializer(payment).data,
        status=status.HTTP_201_CREATED,
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

    provider_name = validated_data.get("provider") or getattr(settings, "DEFAULT_PROVIDER", "chapa")
    try:
        provider = get_provider(provider_name)
    except ProviderError:
        return Response(
            {"detail": f"Unknown or unavailable provider: '{provider_name}'"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Check if payment already exists for this merchant and idempotency key
    existing_payment = Payment.objects.filter(
        merchant=request.merchant,
        idempotency_key=idempotency_key,
    ).first()

    # If it already has a checkout_url, return 200 with the stored session
    if existing_payment and existing_payment.checkout_url:
        return Response(
            {
                "id": existing_payment.id,
                "status": existing_payment.status,
                "provider": existing_payment.provider,
                "reference": existing_payment.merchant_reference,
                "checkout_url": existing_payment.checkout_url,
                "created_at": existing_payment.created_at,
            },
            status=status.HTTP_200_OK,
        )

    customer_email = validated_data.get("customer_email")
    sender = customer_email or "customer"
    receiver = request.merchant.name
    amount = validated_data["amount"]
    reference = validated_data.get("reference")
    return_url = validated_data.get("return_url")

    if not existing_payment:
        try:
            with transaction.atomic():
                payment = Payment.objects.create(
                    merchant=request.merchant,
                    idempotency_key=idempotency_key,
                    amount=amount,
                    sender=sender,
                    receiver=receiver,
                    merchant_reference=reference,
                    return_url=return_url,
                    provider=provider.name,
                    status="pending",
                )
        except IntegrityError:
            payment = Payment.objects.get(
                merchant=request.merchant,
                idempotency_key=idempotency_key,
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

    try:
        with transaction.atomic():
            locked_payment = Payment.objects.select_for_update().get(pk=payment.pk)
            if locked_payment.checkout_url:
                return Response(
                    {
                        "id": locked_payment.id,
                        "status": locked_payment.status,
                        "provider": locked_payment.provider,
                        "reference": locked_payment.merchant_reference,
                        "checkout_url": locked_payment.checkout_url,
                        "created_at": locked_payment.created_at,
                    },
                    status=status.HTTP_200_OK,
                )

            init_result = provider.initialize(locked_payment, customer=customer or None)
            locked_payment.provider = provider.name
            locked_payment.provider_reference = init_result.provider_reference
            locked_payment.checkout_url = init_result.checkout_url
            locked_payment.save(
                update_fields=["provider", "provider_reference", "checkout_url", "updated_at"]
            )
    except ProviderError as exc:
        logger.warning("Checkout session provider failure: %s", exc)
        return Response(
            {"detail": "Payment provider unavailable. Retry with the same Idempotency-Key."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(
        {
            "id": locked_payment.id,
            "status": locked_payment.status,
            "provider": locked_payment.provider,
            "reference": locked_payment.merchant_reference,
            "checkout_url": locked_payment.checkout_url,
            "created_at": locked_payment.created_at,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([HasAPIKey])
def payment_detail(request, pk):
    payment = get_object_or_404(Payment, pk=pk, merchant=request.merchant)
    return Response(
        PaymentSerializer(payment).data,
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([HasAPIKey])
def verify_payment(request, pk):
    payment = get_object_or_404(Payment, pk=pk, merchant=request.merchant)
    return Response(
        {
            "id": payment.id,
            "status": payment.status,
            "updated_at": payment.updated_at,
        },
        status=status.HTTP_200_OK,
    )


# Statuses that cannot be cancelled because they are already terminal
_NON_CANCELLABLE_STATUSES = {"processing", "succeeded", "failed"}


@api_view(["POST"])
@permission_classes([HasAPIKey])
def cancel_payment(request, pk):
    with transaction.atomic():
        payment = get_object_or_404(
            Payment.objects.select_for_update(),
            pk=pk,
            merchant=request.merchant,
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