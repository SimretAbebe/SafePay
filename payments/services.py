import logging
import random
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Payment, WebhookDelivery
from .providers import ProviderError, UnsupportedProviderStatus, get_provider
from .tasks import deliver_webhook_attempt

logger = logging.getLogger(__name__)


def _schedule_review_alert():
    """Called via transaction.on_commit after a payment is flagged for review."""
    from .alerts import schedule_review_alert
    schedule_review_alert()


STATUS_TO_EVENT_TYPE = {
    "succeeded": "payment.succeeded",
    "failed": "payment.failed",
    "cancelled": "payment.cancelled",
    "expired": "payment.expired",
}

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "expired"})


@dataclass
class SyncResult:
    outcome: str
    payment: Any

    def __getitem__(self, item):
        if item == "outcome":
            return self.outcome
        if item == "payment":
            return self.payment
        raise KeyError(item)


def notify_status_change(payment):
    event_type = STATUS_TO_EVENT_TYPE.get(payment.status)
    if not event_type:
        raise ValueError(
            f"Unsupported payment status '{payment.status}' for webhook notification. "
            f"Allowed statuses: {list(STATUS_TO_EVENT_TYPE.keys())}"
        )

    if payment.merchant is None:
        target_url = settings.WEBHOOK_TARGET_URL
    else:
        if payment.livemode:
            target_url = payment.merchant.live_webhook_url
        else:
            target_url = payment.merchant.webhook_url

        if not target_url:
            logger.info(
                "No webhook URL configured for merchant %s (livemode=%s); skipping webhook notification",
                payment.merchant.id,
                payment.livemode,
            )
            return None

    event_id = uuid.uuid4()
    payload = {
        "id": str(event_id),
        "type": event_type,
        "livemode": payment.livemode,
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
        target_url=target_url,
        payload=payload,
    )
    deliver_webhook_attempt.delay(webhook.id)
    return webhook


def resend_delivery(delivery_id: int) -> str:
    """
    Re-queue a failed WebhookDelivery for delivery.

    Must be called outside an existing transaction (it opens its own).

    Returns:
        "not_failed" – the delivery was not in the "failed" (dead-letter) state;
                       nothing was changed.
        "queued"     – delivery was reset to pending and the task was queued
                       via transaction.on_commit so it only fires after the
                       DB row is durable.
    """
    with transaction.atomic():
        try:
            delivery = WebhookDelivery.objects.select_for_update().get(pk=delivery_id)
        except WebhookDelivery.DoesNotExist:
            return "not_failed"

        if delivery.status != "failed":
            return "not_failed"

        # Reset to pending; preserve event_id, event_type, target_url, created_at, payload
        delivery.status = "pending"
        delivery.attempt_count = 0
        delivery.last_error = ""
        delivery.save(update_fields=["status", "attempt_count", "last_error", "updated_at"])

        # Queue the task AFTER the transaction commits so the worker always
        # sees the updated row.
        transaction.on_commit(lambda: deliver_webhook_attempt.delay(delivery.id))

    return "queued"


def sync_payment_from_provider(payment_id) -> SyncResult:
    pk = getattr(payment_id, "pk", payment_id)

    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=pk)

        if not payment.provider or not payment.provider_reference:
            return SyncResult(outcome="no_provider", payment=payment)

        provider = get_provider(payment.provider)
        result = provider.verify(payment.provider_reference)

        raw_status = None
        if isinstance(result.raw, dict):
            raw_status = result.raw.get("status")
        if raw_status is None:
            raw_status = result.status

        payment.provider_status = str(raw_status) if raw_status is not None else None
        payment.save(update_fields=["provider_status", "updated_at"])

        target_status = result.status
        current_status = payment.status

        if target_status == current_status:
            return SyncResult(outcome="unchanged", payment=payment)

        initial_terminal = current_status in TERMINAL_STATUSES
        outcome = "unchanged"

        if current_status == "pending":
            if target_status == "succeeded":
                payment.transition_to("processing")
                payment.transition_to("succeeded")
                outcome = "updated"
            elif target_status == "failed":
                payment.transition_to("processing")
                payment.transition_to("failed")
                outcome = "updated"
            elif target_status in ("cancelled", "expired"):
                payment.transition_to(target_status)
                outcome = "updated"
            elif target_status == "processing":
                payment.transition_to("processing")
                outcome = "updated"
            else:
                logger.warning(
                    "Provider status mismatch for payment %s: current status '%s', provider returned '%s' (mapped '%s')",
                    payment.id,
                    current_status,
                    payment.provider_status,
                    target_status,
                )
                outcome = "mismatch"
        elif current_status == "processing":
            if target_status in ("succeeded", "failed"):
                payment.transition_to(target_status)
                outcome = "updated"
            else:
                logger.warning(
                    "Provider status mismatch for payment %s: current status '%s', provider returned '%s' (mapped '%s')",
                    payment.id,
                    current_status,
                    payment.provider_status,
                    target_status,
                )
                outcome = "mismatch"
        else:
            if current_status == "expired" and target_status == "pending":
                # The payment expired in SafePay and remains unpaid at the provider.
                outcome = "unchanged"
            else:
                logger.warning(
                    "Provider status mismatch for payment %s: current status '%s', provider returned '%s' (mapped '%s')",
                    payment.id,
                    current_status,
                    payment.provider_status,
                    target_status,
                )
                outcome = "mismatch"

        if outcome == "mismatch":
            payment.flag_for_review("mismatch")
            payment.save(
                update_fields=[
                    "needs_review",
                    "review_reason",
                    "needs_review_at",
                    "review_alerted_at",
                    "next_sync_at",
                    "updated_at",
                ]
            )
            transaction.on_commit(_schedule_review_alert)
            return SyncResult(outcome="mismatch", payment=payment)

        if outcome == "updated" and (not initial_terminal) and (payment.status in TERMINAL_STATUSES):
            p = payment
            transaction.on_commit(lambda: notify_status_change(p))

        return SyncResult(outcome=outcome, payment=payment)


@dataclass
class ReconcileResult:
    outcome: str
    payment: Any

    def __getitem__(self, item):
        if item == "outcome":
            return self.outcome
        if item == "payment":
            return self.payment
        raise KeyError(item)


def backoff_delay(attempt: int, jitter: float | None = None, rng=None) -> int:
    delays = getattr(
        settings,
        "RECONCILE_BACKOFF_SECONDS",
        [60, 120, 300, 900, 3600, 3600, 3600, 3600],
    )
    idx = min(max(0, attempt - 1), len(delays) - 1)
    base = delays[idx]
    if jitter is None:
        jitter = getattr(settings, "RECONCILE_JITTER", 0.1)
    if jitter == 0:
        return int(base)
    r = rng if rng is not None else random
    delta = r.uniform(-jitter, jitter)
    return max(1, int(round(base * (1 + delta))))


def schedule_first_check(payment):
    now = timezone.now()
    delay = getattr(settings, "RECONCILE_FIRST_CHECK_DELAY_SECONDS", 300)
    payment.next_sync_at = now + timedelta(seconds=delay)
    payment.save(update_fields=["next_sync_at", "updated_at"])
    return payment


def reconcile_one(payment_id, now=None) -> ReconcileResult:
    if now is None:
        now = timezone.now()

    pk = getattr(payment_id, "pk", payment_id)

    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(pk=pk)
        except Payment.DoesNotExist:
            return ReconcileResult(outcome="skipped", payment=None)

        # 1. Skip if no provider, no provider_reference, or already flagged for review
        if not payment.provider or not payment.provider_reference or payment.needs_review:
            if payment.status != "pending" and payment.next_sync_at is not None:
                payment.next_sync_at = None
                payment.save(update_fields=["next_sync_at", "updated_at"])
            return ReconcileResult(outcome="skipped", payment=payment)

        max_attempts = getattr(settings, "RECONCILE_MAX_ATTEMPTS", 8)

        # 2. Call sync_payment_from_provider logic (reuse it; safe inside outer transaction)
        try:
            sync_result = sync_payment_from_provider(payment.pk)
            payment = sync_result.payment
            payment.last_synced_at = now

            if sync_result.outcome == "mismatch":
                payment.flag_for_review("mismatch", now=now)
                payment.save(
                    update_fields=[
                        "needs_review",
                        "review_reason",
                        "needs_review_at",
                        "review_alerted_at",
                        "next_sync_at",
                        "last_synced_at",
                        "updated_at",
                    ]
                )
                transaction.on_commit(_schedule_review_alert)
                return ReconcileResult(outcome="mismatch", payment=payment)

            # If payment reached terminal state (succeeded, failed, cancelled, expired)
            if payment.status in TERMINAL_STATUSES:
                payment.next_sync_at = None
                payment.save(
                    update_fields=["next_sync_at", "last_synced_at", "updated_at"]
                )
                if sync_result.outcome == "updated":
                    return ReconcileResult(outcome=payment.status, payment=payment)
                else:
                    return ReconcileResult(outcome="skipped", payment=payment)

            # Reachable but still pending
            payment.sync_attempts += 1
            if payment.sync_attempts >= max_attempts:
                # Payments that remain pending after max reconciliation attempts stop being
                # re-checked here and are expired by the expiry sweep when their time comes.
                payment.next_sync_at = None
            else:
                delay = backoff_delay(payment.sync_attempts)
                payment.next_sync_at = now + timedelta(seconds=delay)

            payment.save(
                update_fields=[
                    "sync_attempts",
                    "next_sync_at",
                    "last_synced_at",
                    "updated_at",
                ]
            )
            return ReconcileResult(outcome="pending", payment=payment)

        except UnsupportedProviderStatus:
            payment.last_synced_at = now
            payment.flag_for_review("unsupported_status", now=now)
            payment.save(
                update_fields=[
                    "needs_review",
                    "review_reason",
                    "needs_review_at",
                    "review_alerted_at",
                    "next_sync_at",
                    "last_synced_at",
                    "updated_at",
                ]
            )
            transaction.on_commit(_schedule_review_alert)
            return ReconcileResult(outcome="unsupported_status", payment=payment)

        except ProviderError:
            payment.last_synced_at = now
            payment.sync_attempts += 1
            if payment.sync_attempts >= max_attempts:
                payment.flag_for_review("unknown_outcome", now=now)
            else:
                delay = backoff_delay(payment.sync_attempts)
                payment.next_sync_at = now + timedelta(seconds=delay)
            payment.save(
                update_fields=[
                    "sync_attempts",
                    "needs_review",
                    "review_reason",
                    "needs_review_at",
                    "review_alerted_at",
                    "next_sync_at",
                    "last_synced_at",
                    "updated_at",
                ]
            )
            if payment.needs_review:
                transaction.on_commit(_schedule_review_alert)
            return ReconcileResult(outcome="unknown", payment=payment)

        except Exception:
            logger.exception("Unexpected error during reconciliation of payment %s", payment.id)
            payment.last_synced_at = now
            payment.sync_attempts += 1
            if payment.sync_attempts >= max_attempts:
                payment.flag_for_review("unknown_outcome", now=now)
            else:
                delay = backoff_delay(payment.sync_attempts)
                payment.next_sync_at = now + timedelta(seconds=delay)
            payment.save(
                update_fields=[
                    "sync_attempts",
                    "needs_review",
                    "review_reason",
                    "needs_review_at",
                    "review_alerted_at",
                    "next_sync_at",
                    "last_synced_at",
                    "updated_at",
                ]
            )
            if payment.needs_review:
                transaction.on_commit(_schedule_review_alert)
            return ReconcileResult(outcome="unknown", payment=payment)


def expire_one(payment_id, now=None) -> str:
    if now is None:
        now = timezone.now()

    pk = getattr(payment_id, "pk", payment_id)
    grace_seconds = getattr(settings, "EXPIRY_GRACE_SECONDS", 900)
    grace_delta = timedelta(seconds=grace_seconds)

    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(pk=pk)
        except Payment.DoesNotExist:
            return "skipped"

        # 1. Skip if the payment is not pending, or expires_at is NULL,
        #    or now < expires_at + EXPIRY_GRACE_SECONDS, or needs_review is True.
        if (
            payment.status != "pending"
            or payment.expires_at is None
            or now < payment.expires_at + grace_delta
            or payment.needs_review
        ):
            return "skipped"

        # 2. If the payment has a provider and provider_reference:
        #    call sync_payment_from_provider logic (reuses state-change logic safely inside savepoint).
        if payment.provider and payment.provider_reference:
            try:
                sync_result = sync_payment_from_provider(payment.pk)
                payment = sync_result.payment
            except ProviderError:
                logger.warning(
                    "Provider unavailable during expiry check for payment %s",
                    payment.id,
                )
                return "unknown"
            except Exception:
                logger.exception(
                    "Unexpected error during expiry check for payment %s",
                    payment.id,
                )
                return "unknown"

            # Outcomes:
            # - provider says paid/failed/cancelled/processing (or mismatch/flagged):
            #   existing sync logic updated the payment; return "settled" (do NOT expire).
            if payment.status in ("succeeded", "failed", "cancelled", "processing"):
                return "settled"

            if sync_result.outcome == "mismatch" or payment.needs_review:
                return "settled"

            # Provider says still pending (or sync returns "unchanged" while pending) -> continue
            if payment.status != "pending":
                return "settled"

        # 3. Only then: payment.transition_to("expired"), clear next_sync_at,
        #    and notify_status_change after commit.
        payment.transition_to("expired")
        payment.next_sync_at = None
        payment.save(update_fields=["next_sync_at", "updated_at"])

        p = payment
        transaction.on_commit(lambda: notify_status_change(p))

        return "expired"

