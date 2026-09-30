from django.conf import settings

from .models import WebhookDelivery
from .tasks import deliver_webhook_attempt


def notify_status_change(payment):
    webhook = WebhookDelivery.objects.create(
        payment=payment,
        target_url=settings.WEBHOOK_TARGET_URL,
        payload={
            "payment_id": payment.id,
            "idempotency_key": payment.idempotency_key,
            "status": payment.status,
            "amount": str(payment.amount),
        },
    )
    deliver_webhook_attempt.delay(webhook.id)
    return webhook