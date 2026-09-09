from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.db import IntegrityError

from .models import Payment
from .serializers import PaymentSerializer


@api_view(["POST"])
def create_payment(request):
    idempotency_key = request.data.get("idempotency_key")

    existing_payment = Payment.objects.filter(
        idempotency_key=idempotency_key
    ).first()

    if existing_payment is not None:
        return Response(
            PaymentSerializer(existing_payment).data,
            status=status.HTTP_200_OK,
        )

    serializer = PaymentSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        payment = serializer.save()
    except IntegrityError:
        # Another request with the same key won the race between our
        payment = Payment.objects.get(idempotency_key=idempotency_key)
        return Response(
            PaymentSerializer(payment).data,
            status=status.HTTP_200_OK,
        )

    return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)