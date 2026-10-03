from django.contrib import admin, messages

from .models import (
    InvalidStateTransition,
    Payment,
    PaymentStatusHistory,
    WebhookDelivery,
)
from .tasks import deliver_webhook_attempt


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
        "amount",
        "sender",
        "receiver",
        "status",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("idempotency_key", "sender", "receiver")
    # status is read-only: editing it here would skip transition_to(),
    # bypass the state rules, and create no history row
    readonly_fields = ("status", "created_at", "updated_at")
    inlines = [PaymentStatusHistoryInline]
    actions = ["mark_processing", "mark_succeeded"]

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
        "payment",
        "target_url",
        "status",
        "attempt_count",
        "max_attempts",
        "last_response_code",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "last_response_code", "created_at")
    search_fields = ("payment__idempotency_key", "target_url")
    readonly_fields = ("created_at", "updated_at")
    actions = [retry_webhook_deliveries]