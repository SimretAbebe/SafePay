"""
Refund service: request_refund orchestrates idempotency, validation,
provider interaction, and Refund row lifecycle.

No ledger posting, no partial-refund fees, no webhooks for refunds.
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum

from .models import Payment, Refund
from .providers import ProviderError, get_provider

logger = logging.getLogger(__name__)


def request_refund(payment: Payment, amount: Decimal, idempotency_key: str):
    """
    Request a refund for the given payment.

    Returns (refund, http_status_code) tuple.
    """
    merchant = payment.merchant
    livemode = payment.livemode

    with transaction.atomic():
        # Lock the payment row for the duration.
        payment = Payment.objects.select_for_update().get(pk=payment.pk)

        # Idempotency: return existing refund if same key.
        existing = Refund.objects.filter(
            merchant=merchant,
            livemode=livemode,
            idempotency_key=idempotency_key,
        ).first()
        if existing:
            return existing, 200

        # Reject if payment is not succeeded.
        if payment.status != "succeeded":
            return None, 409

        # Sum existing non-failed refunds for this payment.
        refunded = (
            Refund.objects.filter(
                payment=payment,
                status__in=["pending", "succeeded"],
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0")
        )
        if refunded + amount > payment.amount:
            return None, 409

        # Check provider support.
        provider = get_provider(payment.provider)
        if not provider.supports_refund:
            refund = Refund.objects.create(
                payment=payment,
                merchant=merchant,
                livemode=livemode,
                amount=amount,
                status=Refund.STATUS_UNSUPPORTED,
                idempotency_key=idempotency_key,
                error_code="refund_unsupported",
            )
            return refund, 409

        # Provider supports refunds - create as pending.
        refund = Refund.objects.create(
            payment=payment,
            merchant=merchant,
            livemode=livemode,
            amount=amount,
            status=Refund.STATUS_PENDING,
            idempotency_key=idempotency_key,
        )

    # Call the provider AFTER the atomic block commits so the row is visible.
    try:
        provider_ref = provider.refund(payment, amount)
        refund.status = Refund.STATUS_SUCCEEDED
        refund.provider_refund_reference = provider_ref
        refund.save(update_fields=["status", "provider_refund_reference", "updated_at"])
    except ProviderError as exc:
        if exc.not_created:
            refund.status = Refund.STATUS_FAILED
            refund.error_code = "provider_rejected"
        else:
            # Uncertain failure - leave as pending for manual review.
            refund.error_code = "provider_unknown"
        refund.save(update_fields=["status", "error_code", "updated_at"])

    return refund, 201

