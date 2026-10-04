from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.response import Response

from .models import Payment
from .permissions import HasAPIKey
from .serializers import PaymentSerializer
from .tasks import log_payment_event
from .throttling import PaymentRateThrottle


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


@api_view(["GET"])
@permission_classes([HasAPIKey])
def payment_detail(request, pk):
    payment = get_object_or_404(Payment, pk=pk, merchant=request.merchant)
    return Response(
        PaymentSerializer(payment).data,
        status=status.HTTP_200_OK,
    )