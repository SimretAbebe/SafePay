import logging
import uuid
from dataclasses import dataclass
from typing import Any
from django.conf import settings
from django.db import transaction

from .models import Payment, WebhookDelivery
from .providers import get_provider
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