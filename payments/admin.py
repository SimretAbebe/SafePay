from django import forms
from django.contrib import admin, messages
from django.utils import timezone

from django.core.exceptions import ValidationError

from .models import (
    InboundWebhookEvent,
    InvalidStateTransition,
    Merchant,
    Payment,
    PaymentStatusHistory,
    ProviderAttempt,
    WebhookDelivery,
)
from .tasks import deliver_webhook_attempt, reconcile_payment
from .webhook_secrets import derive_merchant_secret
from .webhook_urls import validate_webhook_url


class MerchantAdminForm(forms.ModelForm):
    """Validate default_provider, fallback_providers, and webhook_url before saving."""

    class Meta:
        model = Merchant
        fields = "__all__"

    def clean_webhook_url(self):
        url = self.cleaned_data.get("webhook_url")
        if url:
            try:
                validate_webhook_url(url)
            except ValidationError as exc:
                raise forms.ValidationError("Invalid or disallowed webhook URL.") from exc
        return url

    def clean(self):
        from .providers.registry import _registry as _provider_registry

        cleaned = super().clean()
        default_provider = cleaned.get("default_provider") or ""
        fallback_providers = cleaned.get("fallback_providers") or []

        errors = {}

        # Validate default_provider
        if default_provider and default_provider not in _provider_registry:
            errors["default_provider"] = (
                f"'{default_provider}' is not a registered provider. "
                f"Available: {sorted(_provider_registry.keys())}"
            )

        # Validate fallback_providers
        if not isinstance(fallback_providers, list):
            errors["fallback_providers"] = "Must be a JSON list of provider name strings."
        else:
            seen = set()
            for name in fallback_providers:
                if not isinstance(name, str):
                    errors.setdefault("fallback_providers", [])
                    errors["fallback_providers"] = "Each entry must be a string."
                    break
                if name not in _provider_registry:
                    errors["fallback_providers"] = (
                        f"'{name}' is not a registered provider. "
                        f"Available: {sorted(_provider_registry.keys())}"
                    )
                    break
                if name in seen:
                    errors["fallback_providers"] = f"Duplicate provider name: '{name}'."
                    break
                seen.add(name)

            # The default must not also appear in the fallback list.
            if default_provider and default_provider in seen:
                errors["fallback_providers"] = (
                    f"'{default_provider}' is already set as the default provider — "
                    "it must not appear in the fallback list."
                )

        if errors:
            raise forms.ValidationError(errors)

        return cleaned


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    form = MerchantAdminForm
    list_display = (
        "name",
        "key_prefix",
        "is_active",
        "default_provider",
        "webhook_configured",
        "created_at",
    )
    readonly_fields = ("api_key_hash", "key_prefix", "webhook_secret_version", "created_at")
    actions = ["show_webhook_secret", "rotate_webhook_secret"]

    @admin.display(description="Webhook configured", boolean=True)
    def webhook_configured(self, obj):
        return bool(obj.webhook_url)

    @admin.action(description="Show webhook secret (once)")
    def show_webhook_secret(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request,
                "Please select exactly one merchant to show the webhook secret.",
                level=messages.ERROR,
            )
            return
        merchant = queryset.first()
        secret = derive_merchant_secret(merchant)
        self.message_user(
            request,
            f"Webhook signing secret for '{merchant.name}': {secret}",
            level=messages.SUCCESS,
        )

    @admin.action(description="Rotate webhook secret")
    def rotate_webhook_secret(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request,
                "Please select exactly one merchant to rotate the webhook secret.",
                level=messages.ERROR,
            )
            return
        merchant = queryset.first()
        # TODO: Add a grace period with dual-signing during secret rotation to avoid dropped webhooks.
        merchant.webhook_secret_version += 1
        merchant.save(update_fields=["webhook_secret_version"])
        new_secret = derive_merchant_secret(merchant)
        self.message_user(
            request,
            f"Rotated webhook secret for '{merchant.name}' (v{merchant.webhook_secret_version}): {new_secret}",
            level=messages.SUCCESS,
        )

    def save_model(self, request, obj, form, change):
        if not change:
            raw_key, api_key_hash, key_prefix = Merchant.generate_key_pair()
            obj.api_key_hash = api_key_hash
            obj.key_prefix = key_prefix
            super().save_model(request, obj, form, change)
            self.message_user(
                request,
                f"Merchant '{obj.name}' created. API Key: {raw_key} (Save this key now; it will never be displayed again.)",
                level=messages.SUCCESS,
            )
        else:
            super().save_model(request, obj, form, change)


class PaymentStatusHistoryInline(admin.TabularInline):
    model = PaymentStatusHistory
    readonly_fields = ("from_status", "to_status", "changed_at")
    extra = 0  # history is auto-generated, never added by hand
    can_delete = False  # an audit trail must not be deleted from the admin

    def has_add_permission(self, request, obj=None):
        return False


class ProviderAttemptInline(admin.TabularInline):
    """Read-only inline showing all provider call attempts for a payment."""
    model = ProviderAttempt
    readonly_fields = ("provider", "outcome", "error_type", "created_at")
    extra = 0
    can_delete = False
    ordering = ("created_at", "id")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "idempotency_key",
        "merchant",
        "provider",
        "provider_reference",
        "provider_status",
        "amount",
        "sender",
        "receiver",
        "status",
        "sync_attempts",
        "next_sync_at",
        "last_synced_at",
        "needs_review",
        "review_reason",
        "created_at",
    )
    list_filter = ("status", "merchant", "provider", "needs_review")
    search_fields = ("idempotency_key", "sender", "receiver", "provider_reference")
    # status is read-only: editing it here would skip transition_to(),
    # bypass the state rules, and create no history row
    readonly_fields = (
        "status",
        "provider",
        "provider_reference",
        "provider_status",
        "checkout_url",
        "merchant_reference",
        "return_url",
        "sync_attempts",
        "next_sync_at",
        "last_synced_at",
        "needs_review",
        "review_reason",
        "created_at",
        "updated_at",
    )
    inlines = [PaymentStatusHistoryInline, ProviderAttemptInline]
    actions = [
        "mark_processing",
        "mark_succeeded",
        "check_with_provider_now",
        "clear_review_and_recheck",
    ]

    def _move(self, request, queryset, new_status):
        for payment in queryset:
            try:
                payment.transition_to(new_status)
            except InvalidStateTransition as e:
                self.message_user(request, str(e), level=messages.ERROR)

    @admin.action(description="Move selected payments to processing")
    def mark_processing(self, request, queryset):
        self._move(request, queryset, "processing")

    @admin.action(description="Move selected payments to succeeded")
    def mark_succeeded(self, request, queryset):
        self._move(request, queryset, "succeeded")

    @admin.action(description="Check with provider now")
    def check_with_provider_now(self, request, queryset):
        count = 0
        for payment in queryset:
            reconcile_payment.delay(payment.id)
            count += 1
        self.message_user(request, f"{count} payment(s) queued for provider check.", level=messages.SUCCESS)

    @admin.action(description="Clear review flag and re-check")
    def clear_review_and_recheck(self, request, queryset):
        now = timezone.now()
        count = queryset.update(
            needs_review=False,
            review_reason="",
            sync_attempts=0,
            next_sync_at=now,
        )
        self.message_user(request, f"{count} payment(s) review flag cleared and queued for check.", level=messages.SUCCESS)


@admin.action(description="Retry selected webhook deliveries")
def retry_webhook_deliveries(modeladmin, request, queryset):
    count = 0
    for delivery in queryset:
        delivery.status = "pending"
        delivery.attempt_count = 0
        delivery.last_error = None
        delivery.save(update_fields=["status", "attempt_count", "last_error", "updated_at"])
        deliver_webhook_attempt.delay(delivery.id)
        count += 1
    modeladmin.message_user(request, f"{count} webhook delivery/deliveries queued for retry.")


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "event_id",
        "event_type",
        "payment",
        "target_url",
        "status",
        "attempt_count",
        "max_attempts",
        "last_response_code",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "event_type", "last_response_code", "created_at")
    search_fields = ("event_id", "event_type", "payment__idempotency_key", "target_url")
    readonly_fields = ("event_id", "event_type", "created_at", "updated_at")
    actions = [retry_webhook_deliveries]


@admin.register(InboundWebhookEvent)
class InboundWebhookEventAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "provider",
        "provider_reference",
        "event_type",
        "outcome",
        "signature_valid",
        "body_sha256",
        "received_at",
    )
    list_filter = ("outcome", "signature_valid")
    search_fields = ("provider_reference", "event_type", "body_sha256")
    readonly_fields = (
        "provider",
        "received_at",
        "signature_valid",
        "provider_reference",
        "event_type",
        "outcome",
        "body_sha256",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False