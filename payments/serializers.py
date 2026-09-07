from rest_framework import serializers

from .models import Payment


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = [
            "id",
            "idempotency_key",
            "amount",
            "sender",
            "receiver",
            "status",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "status", "created_at", "updated_at"]