from django.contrib import admin, messages
from django.utils import timezone

from .models import (
    InboundWebhookEvent,
    InvalidStateTransition,
    Merchant,
    Payment,
    PaymentStatusHistory,
    WebhookDelivery,
)
from .tasks import deliver_webhook_attempt, reconcile_payment


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ("name", "key_prefix", "is_active", "created_at")
    readonly_fields = ("api_key_hash", "key_prefix", "created_at")

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
    inlines = [PaymentStatusHistoryInline]
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