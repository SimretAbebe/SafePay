from datetime import timedelta
import hashlib
import hmac
import json
import logging
import time

import requests
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Payment, WebhookDelivery

logger = logging.getLogger(__name__)


@shared_task
def log_payment_event(payment_id, event_description):
    logger.info(f"[Celery task ran] Payment {payment_id}: {event_description}")
    return f"Logged: {event_description}"


def _sign_payload(timestamp: str, payload_bytes: bytes) -> str:
    secret = settings.WEBHOOK_SECRET.encode("utf-8")
    to_sign = f"{timestamp}.".encode("utf-8") + payload_bytes
    return hmac.new(secret, to_sign, hashlib.sha256).hexdigest()


@shared_task
def deliver_webhook_attempt(webhook_delivery_id):
    try:
        webhook = WebhookDelivery.objects.get(id=webhook_delivery_id)
    except WebhookDelivery.DoesNotExist:
        logger.error(f"WebhookDelivery {webhook_delivery_id} not found")
        return

    if webhook.status != "pending":
        return

    payload_bytes = json.dumps(webhook.payload).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = _sign_payload(timestamp, payload_bytes)

    webhook.attempt_count += 1

    try:
        response = requests.post(
            webhook.target_url,
            data=payload_bytes,
            headers={
                "Content-Type": "application/json",
                "X-SafePay-Event-Id": str(webhook.event_id),
                "X-SafePay-Timestamp": timestamp,
                "X-SafePay-Signature": signature,
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


@shared_task
def reconcile_payment(payment_id):
    from .services import reconcile_one

    result = reconcile_one(payment_id)
    payment = result.payment
    sync_attempts = getattr(payment, "sync_attempts", None) if payment else None
    logger.info(
        "Reconciled payment %s: outcome=%s, sync_attempts=%s",
        payment_id,
        result.outcome,
        sync_attempts,
    )
    return result.outcome


@shared_task
def reconcile_pending_payments():
    now = timezone.now()
    first_check_delay = getattr(settings, "RECONCILE_FIRST_CHECK_DELAY_SECONDS", 300)
    lease_seconds = getattr(settings, "RECONCILE_LEASE_SECONDS", 120)
    batch_size = getattr(settings, "RECONCILE_BATCH_SIZE", 50)

    first_check_cutoff = now - timedelta(seconds=first_check_delay)
    lease_until = now + timedelta(seconds=lease_seconds)

    due_filter = (
        Q(status="pending")
        & Q(provider__isnull=False)
        & ~Q(provider="")
        & Q(provider_reference__isnull=False)
        & ~Q(provider_reference="")
        & Q(needs_review=False)
        & (
            Q(next_sync_at__lte=now)
            | (Q(next_sync_at__isnull=True) & Q(created_at__lte=first_check_cutoff))
        )
    )

    dispatched_ids = []
    with transaction.atomic():
        due_qs = (
            Payment.objects.select_for_update(skip_locked=True)
            .filter(due_filter)
            .order_by("next_sync_at")[:batch_size]
        )
        selected_payments = list(due_qs)
        for payment in selected_payments:
            payment.next_sync_at = lease_until
            payment.save(update_fields=["next_sync_at", "updated_at"])
            dispatched_ids.append(payment.id)

    # After the transaction commits, call reconcile_payment.delay(id) for each
    for payment_id in dispatched_ids:
        reconcile_payment.delay(payment_id)

    due_count = len(selected_payments)
    dispatched_count = len(dispatched_ids)
    logger.info(
        "Reconciliation sweep: %d due payments found, %d dispatched",
        due_count,
        dispatched_count,
    )
    return {"due_count": due_count, "dispatched_count": dispatched_count}