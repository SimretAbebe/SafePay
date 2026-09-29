from .tasks import log_payment_event
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Payment
from .serializers import PaymentSerializer


@api_view(["POST"])
def create_payment(request):
    idempotency_key = request.data.get("idempotency_key")

    existing_payment = Payment.objects.filter(
        idempotency_key=idempotency_key
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
            payment = serializer.save()
    except IntegrityError:
        payment = Payment.objects.get(idempotency_key=idempotency_key)
        return Response(
            PaymentSerializer(payment).data,
            status=status.HTTP_200_OK,
        )

    return Response(
        PaymentSerializer(payment).data,
        status=status.HTTP_201_CREATED,
    )