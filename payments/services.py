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
                # Target status cannot be legally reached from pending
                # TODO: A later reconciliation job should flag these status mismatches for manual review.
                logger.warning(
                    "Provider status mismatch for payment %s: current status '%s', provider returned '%s' (mapped '%s')",
                    payment.id,
                    current_status,
                    payment.provider_status,
                    target_status,
                )
                return SyncResult(outcome="mismatch", payment=payment)
        elif current_status == "processing":
            if target_status in ("succeeded", "failed"):
                payment.transition_to(target_status)
                outcome = "updated"
            else:
                # Target status cannot be legally reached from processing
                # TODO: A later reconciliation job should flag these status mismatches for manual review.
                logger.warning(
                    "Provider status mismatch for payment %s: current status '%s', provider returned '%s' (mapped '%s')",
                    payment.id,
                    current_status,
                    payment.provider_status,
                    target_status,
                )
                return SyncResult(outcome="mismatch", payment=payment)
        else:
            # Payment is already in a terminal state (succeeded, failed, cancelled, expired)
            # Cannot be reached legally from terminal state
            # TODO: A later reconciliation job should flag these status mismatches for manual review.
            logger.warning(
                "Provider status mismatch for payment %s: current status '%s', provider returned '%s' (mapped '%s')",
                payment.id,
                current_status,
                payment.provider_status,
                target_status,
            )
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
                payment.needs_review = True
                payment.review_reason = "mismatch"
                payment.next_sync_at = None
                payment.save(
                    update_fields=[
                        "needs_review",
                        "review_reason",
                        "next_sync_at",
                        "last_synced_at",
                        "updated_at",
                    ]
                )
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
                # TODO: A later expiry feature will handle abandoned payments
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
            payment.needs_review = True
            payment.review_reason = "unsupported_status"
            payment.next_sync_at = None
            payment.save(
                update_fields=[
                    "needs_review",
                    "review_reason",
                    "next_sync_at",
                    "last_synced_at",
                    "updated_at",
                ]
            )
            return ReconcileResult(outcome="unsupported_status", payment=payment)

        except ProviderError:
            payment.last_synced_at = now
            payment.sync_attempts += 1
            if payment.sync_attempts >= max_attempts:
                payment.needs_review = True
                payment.review_reason = "unknown_outcome"
                payment.next_sync_at = None
            else:
                delay = backoff_delay(payment.sync_attempts)
                payment.next_sync_at = now + timedelta(seconds=delay)
            payment.save(
                update_fields=[
                    "sync_attempts",
                    "needs_review",
                    "review_reason",
                    "next_sync_at",
                    "last_synced_at",
                    "updated_at",
                ]
            )
            return ReconcileResult(outcome="unknown", payment=payment)

        except Exception:
            logger.exception("Unexpected error during reconciliation of payment %s", payment.id)
            payment.last_synced_at = now
            payment.sync_attempts += 1
            if payment.sync_attempts >= max_attempts:
                payment.needs_review = True
                payment.review_reason = "unknown_outcome"
                payment.next_sync_at = None
            else:
                delay = backoff_delay(payment.sync_attempts)
                payment.next_sync_at = now + timedelta(seconds=delay)
            payment.save(
                update_fields=[
                    "sync_attempts",
                    "needs_review",
                    "review_reason",
                    "next_sync_at",
                    "last_synced_at",
                    "updated_at",
                ]
            )
            return ReconcileResult(outcome="unknown", payment=payment)
