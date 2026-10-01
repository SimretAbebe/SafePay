from django.contrib import admin
from .models import Payment, WebhookDelivery
from .tasks import deliver_webhook_attempt


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "idempotency_key",
        "amount",
        "sender",
        "receiver",
        "status",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("idempotency_key", "sender", "receiver")
    readonly_fields = ("created_at", "updated_at")


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
