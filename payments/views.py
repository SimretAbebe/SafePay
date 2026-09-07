from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Payment
from .serializers import PaymentSerializer


@api_view(["POST"])
def create_payment(request):
    serializer = PaymentSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    payment = serializer.save()
    return Response(
        PaymentSerializer(payment).data, 
        status=status.HTTP_201_CREATED
        )
