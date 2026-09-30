import hashlib
import hmac
import json
import logging

import requests
from celery import shared_task
from django.conf import settings

from .models import WebhookDelivery

logger = logging.getLogger(__name__)


@shared_task
def log_payment_event(payment_id, event_description):
    logger.info(f"[Celery task ran] Payment {payment_id}: {event_description}")
    return f"Logged: {event_description}"


def _sign_payload(payload_bytes):
    secret = settings.WEBHOOK_SECRET.encode()
    return hmac.new(secret, payload_bytes, hashlib.sha256).hexdigest()


@shared_task
def deliver_webhook_attempt(webhook_delivery_id):
    try:
        webhook = WebhookDelivery.objects.get(id=webhook_delivery_id)
    except WebhookDelivery.DoesNotExist:
        logger.error(f"WebhookDelivery {webhook_delivery_id} not found")
        return

    if webhook.status != "pending":
        return

    payload_bytes = json.dumps(webhook.payload).encode()
    signature = _sign_payload(payload_bytes)

    webhook.attempt_count += 1

    try:
        response = requests.post(
            webhook.target_url,
            data=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-SafePay-Signature": f"sha256={signature}",
            },
            timeout=5,
        )
        webhook.last_response_code = response.status_code

        if 200 <= response.status_code < 300:
            webhook.status = "succeeded"
            webhook.save()
            logger.info(f"Webhook {webhook.id} delivered successfully")
            return

        webhook.last_error = f"Non-2xx response: {response.status_code}"

    except requests.RequestException as exc:
        webhook.last_error = str(exc)

    # If  reach here, this attempt failed one way or another.
    if webhook.attempt_count >= webhook.max_attempts:
        webhook.status = "failed"
        webhook.save()
        logger.warning(
            f"Webhook {webhook.id} exhausted all {webhook.max_attempts} attempts, dead-lettered"
        )
    else:
        webhook.save()  # persist attempt_count/last_error before rescheduling
        backoff_seconds = 2 ** webhook.attempt_count  # 2, 4, 8, 16...
        logger.info(
            f"Webhook {webhook.id} attempt {webhook.attempt_count} failed, "
            f"retrying in {backoff_seconds}s"
        )
        deliver_webhook_attempt.apply_async(
            args=[webhook_delivery_id],
            countdown=backoff_seconds,
        )