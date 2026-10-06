import uuid
from django.conf import settings

from .models import WebhookDelivery
from .tasks import deliver_webhook_attempt

STATUS_TO_EVENT_TYPE = {
    "succeeded": "payment.succeeded",
    "failed": "payment.failed",
    "cancelled": "payment.cancelled",
}


def notify_status_change(payment):
    event_type = STATUS_TO_EVENT_TYPE.get(payment.status)
    if not event_type:
        raise ValueError(
            f"Unsupported payment status '{payment.status}' for webhook notification. "
            f"Allowed statuses: {list(STATUS_TO_EVENT_TYPE.keys())}"
        )

    event_id = uuid.uuid4()
    payload = {
        "id": str(event_id),
        "type": event_type,
        "data": {
            "payment_id": payment.id,
            "idempotency_key": payment.idempotency_key,
            "status": payment.status,
            "amount": str(payment.amount),
        },
    }

    webhook = WebhookDelivery.objects.create(
        payment=payment,
        event_id=event_id,
        event_type=event_type,
        target_url=settings.WEBHOOK_TARGET_URL,
        payload=payload,
    )
    deliver_webhook_attempt.delay(webhook.id)
    return webhook