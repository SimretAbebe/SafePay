from decimal import Decimal
from urllib.parse import urlparse
from rest_framework import serializers

from .models import Payment
from .providers import get_provider, ProviderError


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
        extra_kwargs = {"idempotency_key": {"validators": []}}


class CheckoutSessionSerializer(serializers.Serializer):
    amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=True,
    )
    reference = serializers.CharField(
        max_length=100,
        required=False,
        allow_null=True,
        allow_blank=True,
        default=None,
    )
    return_url = serializers.CharField(
        max_length=500,
        required=False,
        allow_null=True,
        allow_blank=True,
        default=None,
    )
    provider = serializers.CharField(
        max_length=30,
        required=False,
        allow_null=True,
        allow_blank=True,
        default=None,
    )
    customer_email = serializers.EmailField(
        required=False,
        allow_null=True,
        allow_blank=True,
        default=None,
    )
    customer_first_name = serializers.CharField(
        max_length=100,
        required=False,
        allow_null=True,
        allow_blank=True,
        default=None,
    )
    customer_last_name = serializers.CharField(
        max_length=100,
        required=False,
        allow_null=True,
        allow_blank=True,
        default=None,
    )

    def validate_amount(self, value):
        if value <= Decimal("0.00"):
            raise serializers.ValidationError("Amount must be greater than zero.")
        # Ensure at most 2 decimal places
        if value.as_tuple().exponent < -2:
            raise serializers.ValidationError("Amount can have at most 2 decimal places.")
        return value

    def validate_return_url(self, value):
        if value:
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise serializers.ValidationError("return_url must be a valid http or https URL.")
        return value

    def validate_provider(self, value):
        if value:
            try:
                get_provider(value)
            except ProviderError as e:
                raise serializers.ValidationError(str(e))
        return value