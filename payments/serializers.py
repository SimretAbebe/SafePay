from decimal import Decimal
from urllib.parse import urlparse
from django.conf import settings
from rest_framework import serializers

from .models import Payment
from .providers import get_provider, ProviderError


class PaymentSerializer(serializers.ModelSerializer):
    checkout_url = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            "id",
            "idempotency_key",
            "amount",
            "sender",
            "receiver",
            "status",
            "checkout_url",
            "expires_at",
            "livemode",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "checkout_url",
            "expires_at",
            "livemode",
            "created_at",
            "updated_at",
        ]
        extra_kwargs = {"idempotency_key": {"validators": []}}

    def get_checkout_url(self, obj):
        if obj.status != "pending":
            return None
        return obj.checkout_url


class PaymentListItemSerializer(serializers.ModelSerializer):
    reference = serializers.CharField(source="merchant_reference", allow_null=True)

    class Meta:
        model = Payment
        fields = [
            "id",
            "status",
            "amount",
            "provider",
            "reference",
            "checkout_url",
            "livemode",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class CheckoutSessionSerializer(serializers.Serializer):
    """Validates checkout session request payload and routing overrides."""
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
    # Provider fallback preference (strict boolean)
    allow_fallback = serializers.BooleanField(
        required=False,
        default=True,
    )
    expires_in_minutes = serializers.IntegerField(
        required=False,
        allow_null=False,
        default=None,
    )

    def validate_expires_in_minutes(self, value):
        raw = self.initial_data.get("expires_in_minutes")
        if raw is not None:
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise serializers.ValidationError("expires_in_minutes must be an integer.")
            min_sec = getattr(settings, "PAYMENT_EXPIRY_MIN_SECONDS", 900)
            max_sec = getattr(settings, "PAYMENT_EXPIRY_MAX_SECONDS", 604800)
            lifetime_sec = value * 60
            if lifetime_sec < min_sec or lifetime_sec > max_sec:
                raise serializers.ValidationError(
                    f"expires_in_minutes must result in a lifetime between {min_sec} and {max_sec} seconds."
                )
        return value

    def validate_allow_fallback(self, value):
        """Enforce strict boolean types (reject truthy/falsy strings like 'yes')."""
        raw = self.initial_data.get("allow_fallback")
        if raw is not None and not isinstance(raw, bool):
            raise serializers.ValidationError("allow_fallback must be a boolean.")
        return value

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