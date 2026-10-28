from datetime import timedelta
import hashlib
import hmac
import json
import logging
import time

import requests
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

import urllib.parse

from .models import Payment, WebhookDelivery
from .webhook_secrets import derive_merchant_secret
from .webhook_urls import is_target_safe

logger = logging.getLogger(__name__)


@shared_task
def log_payment_event(payment_id, event_description):
    logger.info(f"[Celery task ran] Payment {payment_id}: {event_description}")
    return f"Logged: {event_description}"


def _sign_payload(timestamp: str, payload_bytes: bytes, secret: str = None) -> str:
    if secret is None:
        secret = settings.WEBHOOK_SECRET
    secret_bytes = secret.encode("utf-8")
    to_sign = f"{timestamp}.".encode("utf-8") + payload_bytes
    return hmac.new(secret_bytes, to_sign, hashlib.sha256).hexdigest()


@shared_task
def deliver_webhook_attempt(webhook_delivery_id):
    try:
        webhook = WebhookDelivery.objects.select_related("payment__merchant").get(id=webhook_delivery_id)
    except WebhookDelivery.DoesNotExist:
        logger.error(f"WebhookDelivery {webhook_delivery_id} not found")
        return

    if webhook.status != "pending":
        return

    # Delivery-time safety recheck
    if not is_target_safe(webhook.target_url):
        webhook.status = "failed"
        webhook.last_error = "blocked_target"
        webhook.save(update_fields=["status", "last_error", "updated_at"])
        parsed_url = urllib.parse.urlsplit(webhook.target_url)
        safe_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
        logger.warning(
            "Webhook delivery %s blocked: target %s failed safety checks",
            webhook.id,
            safe_url,
        )
        return

    merchant = getattr(webhook.payment, "merchant", None)
    if merchant:
        secret = derive_merchant_secret(
            merchant,
            livemode=getattr(webhook.payment, "livemode", False),
        )
    else:
        secret = settings.WEBHOOK_SECRET

    payload_bytes = json.dumps(webhook.payload).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = _sign_payload(timestamp, payload_bytes, secret=secret)

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
            allow_redirects=False,
        )
        webhook.last_response_code = response.status_code

        if 200 <= response.status_code < 300:
            webhook.status = "succeeded"
            webhook.save()
            logger.info(f"Webhook {webhook.id} delivered successfully")
            return

        if 300 <= response.status_code < 400:
            webhook.last_error = f"Redirect rejected: {response.status_code}"
        else:
            webhook.last_error = f"Non-2xx response: {response.status_code}"

    except requests.RequestException as exc:
        webhook.last_error = exc.__class__.__name__

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

    # Heartbeat: signal a monitoring service that the sweep completed.
    heartbeat_url = getattr(settings, "RECONCILE_HEARTBEAT_URL", "")
    if heartbeat_url:
        try:
            requests.get(heartbeat_url, timeout=3)
        except Exception:
            pass  # silently ignore — never raise or log the URL

    # Record completion timestamp for the readiness health check
    try:
        cache.set("safepay:health:reconcile_sweep_ts", time.time(), timeout=86400)
    except Exception:
        pass

    return {"due_count": due_count, "dispatched_count": dispatched_count}


@shared_task
def expire_pending_payments():
    from .services import expire_one

    now = timezone.now()
    batch_size = getattr(settings, "EXPIRY_BATCH_SIZE", 50)
    grace_seconds = getattr(settings, "EXPIRY_GRACE_SECONDS", 900)
    cutoff = now - timedelta(seconds=grace_seconds)

    # Pick candidate IDs inside a short atomic transaction with select_for_update(skip_locked=True).
    # This immediately commits/releases batch row locks so provider calls never happen inside a long
    # transaction holding multiple row locks.
    with transaction.atomic():
        candidate_ids = list(
            Payment.objects.select_for_update(skip_locked=True)
            .filter(
                status="pending",
                expires_at__isnull=False,
                expires_at__lte=cutoff,
                needs_review=False,
            )
            .order_by("expires_at")
            .values_list("id", flat=True)[:batch_size]
        )

    counts = {"expired": 0, "settled": 0, "unknown": 0, "skipped": 0}

    for payment_id in candidate_ids:
        try:
            outcome = expire_one(payment_id, now=now)
            if outcome in counts:
                counts[outcome] += 1
            else:
                counts["unknown"] += 1
        except Exception:
            logger.exception("Unexpected error expiring payment %s", payment_id)
            counts["unknown"] += 1

    logger.info(
        "Expiry sweep: expired=%d, settled=%d, unknown=%d, skipped=%d",
        counts["expired"],
        counts["settled"],
        counts["unknown"],
        counts["skipped"],
    )

    return counts


@shared_task
def alert_new_reviews():
    """Send one grouped alert for all un-alerted review-queue payments."""
    from .alerts import format_age, send_alert_message

    now = timezone.now()

    # Step 1: lock rows to prevent duplicate alerts from concurrent workers.
    # select_related("merchant") is NOT allowed here because merchant is a
    # nullable FK and PostgreSQL rejects FOR UPDATE on outer-join sides.
    with transaction.atomic():
        locked_ids = list(
            Payment.objects.select_for_update(skip_locked=True)
            .filter(needs_review=True, review_alerted_at__isnull=True)
            .order_by("needs_review_at", "id")
            .values_list("id", flat=True)
        )

    if not locked_ids:
        return

    # Step 2: fetch full objects with merchant for message formatting.
    payments = list(
        Payment.objects.filter(id__in=locked_ids)
        .select_related("merchant")
        .order_by("needs_review_at", "id")
    )

    if not payments:
        return

    max_lines = 10
    shown = payments[:max_lines]
    extra = len(payments) - max_lines

    lines = [f"\u26a0\ufe0f {len(payments)} payment(s) need review:\n"]
    for p in shown:
        merchant_name = p.merchant.name if p.merchant else "—"
        age = format_age(now - p.needs_review_at) if p.needs_review_at else "?"
        lines.append(
            f"  payment {p.id} | {merchant_name} | {p.provider or '—'}"
            f" | {p.review_reason} | age {age}"
        )
    if extra > 0:
        lines.append(f"  +{extra} more")

    message = "\n".join(lines)
    ok = send_alert_message(message)

    if ok:
        shown_ids = [p.id for p in shown]
        Payment.objects.filter(id__in=shown_ids).update(review_alerted_at=now)


@shared_task
def review_queue_digest():
    """Send a daily digest of the review queue. Does nothing if the queue is empty."""
    from .alerts import format_age, send_alert_message

    now = timezone.now()
    qs = Payment.objects.filter(needs_review=True)
    total = qs.count()

    if total == 0:
        return

    # Counts per reason
    reason_counts = {}
    for row in qs.values("review_reason").annotate(count=models.Count("id")):
        reason_counts[row["review_reason"] or "unknown"] = row["count"]

    oldest = qs.order_by("needs_review_at").values_list("needs_review_at", flat=True).first()
    oldest_age = format_age(now - oldest) if oldest else "?"

    lines = [f"\U0001f4cb Review queue digest: {total} item(s)\n"]
    for reason, count in sorted(reason_counts.items()):
        lines.append(f"  {reason}: {count}")
    lines.append(f"\n  Oldest item age: {oldest_age}")

    send_alert_message("\n".join(lines))