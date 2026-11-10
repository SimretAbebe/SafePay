"""
Ledger posting logic.

All writes go through create_journal() which enforces double-entry invariants
inside a single transaction.
"""

import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import IntegrityError, transaction

from .models import LedgerAccount, LedgerEntry, LedgerJournal

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal("0.01")


class LedgerError(Exception):
    """Raised when a journal cannot be posted due to a validation failure."""
    pass


def _quantize(amount) -> Decimal:
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    return amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def create_journal(payment, kind, lines, refund=None):
    """
    Create a balanced journal with its entries atomically.

    Args:
        payment: Payment instance.
        kind: Journal kind string (e.g. "payment_succeeded", "refund_succeeded").
        lines: List of (account, side, amount) tuples.
               - account: LedgerAccount instance
               - side: "debit" or "credit"
               - amount: Decimal > 0, quantized to 2 decimal places
        refund: Optional Refund instance (linked for refund journals).

    Returns:
        The created LedgerJournal instance.

    Raises:
        LedgerError: If any validation fails (nothing is written).
    """
    if len(lines) < 2:
        raise LedgerError(
            f"A journal requires at least 2 lines, got {len(lines)}."
        )

    # Validate each line
    currencies = set()
    total_debit = Decimal("0.00")
    total_credit = Decimal("0.00")

    for i, (account, side, amount) in enumerate(lines):
        # Amount must be a Decimal
        if not isinstance(amount, Decimal):
            raise LedgerError(
                f"Line {i}: amount must be a Decimal, got {type(amount).__name__}."
            )

        # Check quantization
        quantized = _quantize(amount)
        if amount != quantized:
            raise LedgerError(
                f"Line {i}: amount {amount} is not quantized to 2 decimal places."
            )

        # Must be positive
        if amount <= Decimal("0"):
            raise LedgerError(
                f"Line {i}: amount must be > 0, got {amount}."
            )

        # Side validation
        if side not in ("debit", "credit"):
            raise LedgerError(
                f"Line {i}: side must be 'debit' or 'credit', got '{side}'."
            )

        currencies.add(account.currency)

        if side == "debit":
            total_debit += amount
        else:
            total_credit += amount

    # Single currency
    if len(currencies) > 1:
        raise LedgerError(
            f"All accounts must share a single currency, got {sorted(currencies)}."
        )

    # Balanced
    if total_debit != total_credit:
        raise LedgerError(
            f"Journal is not balanced: total debits={total_debit}, "
            f"total credits={total_credit}."
        )

    with transaction.atomic():
        journal = LedgerJournal(payment=payment, refund=refund, kind=kind)
        journal.save()

        entries = []
        for account, side, amount in lines:
            entry = LedgerEntry(
                journal=journal,
                account=account,
                side=side,
                amount=amount,
            )
            entry.save()
            entries.append(entry)

    return journal


def post_payment_succeeded(payment):
    """
    Post the double-entry journal for a succeeded payment.

    Debits "receivable:<provider>" and credits "sales" for the payment amount.
    Idempotent: does nothing if the journal already exists.
    Skips payments without a merchant (logs a warning).

    This function is designed to be called inside an existing transaction.atomic()
    block (the one wrapping transition_to), so failures roll back the status
    change as well.
    """
    if payment.merchant is None:
        logger.info(
            "Skipping ledger posting for payment %s: no merchant.", payment.pk
        )
        return None

    # Check if already posted (fast path, no lock needed)
    if LedgerJournal.objects.filter(
        payment=payment, kind="payment_succeeded"
    ).exists():
        return None

    currency = "ETB"
    provider_code = payment.provider or "direct"
    receivable_code = f"receivable:{provider_code}"

    amount = _quantize(payment.amount)

    # get_or_create the two accounts
    receivable_account, _ = LedgerAccount.objects.get_or_create(
        merchant=payment.merchant,
        livemode=payment.livemode,
        code=receivable_code,
        currency=currency,
    )
    sales_account, _ = LedgerAccount.objects.get_or_create(
        merchant=payment.merchant,
        livemode=payment.livemode,
        code="sales",
        currency=currency,
    )

    lines = [
        (receivable_account, "debit", amount),
        (sales_account, "credit", amount),
    ]

    try:
        return create_journal(payment, "payment_succeeded", lines)
    except IntegrityError:
        # Concurrent insert — journal already exists. Treat as success.
        logger.info(
            "Ledger journal for payment %s (payment_succeeded) already exists "
            "(concurrent create).",
            payment.pk,
        )
        return None


def post_refund_succeeded(refund):
    """
    Post the double-entry reversing journal for a succeeded refund.

    Debits "sales" and credits "receivable:<provider>" for the refund amount.
    Idempotent: does nothing if the refund_succeeded journal already exists for this refund.
    Skips refunds whose payment has no merchant (logs a warning).
    Refunds that are pending, failed or unsupported post nothing.

    Designed to be called inside the caller's transaction.atomic() block
    when the refund's status is set to succeeded.
    """
    if refund.status != "succeeded":
        return None

    payment = refund.payment
    if payment.merchant is None:
        logger.info(
            "Skipping ledger posting for refund %s: payment %s has no merchant.",
            refund.pk,
            payment.pk,
        )
        return None

    # Check if already posted for this refund (fast path)
    if LedgerJournal.objects.filter(
        refund=refund, kind="refund_succeeded"
    ).exists():
        return None

    currency = "ETB"
    provider_code = payment.provider or "direct"
    receivable_code = f"receivable:{provider_code}"

    amount = _quantize(refund.amount)

    sales_account, _ = LedgerAccount.objects.get_or_create(
        merchant=payment.merchant,
        livemode=payment.livemode,
        code="sales",
        currency=currency,
    )
    receivable_account, _ = LedgerAccount.objects.get_or_create(
        merchant=payment.merchant,
        livemode=payment.livemode,
        code=receivable_code,
        currency=currency,
    )

    lines = [
        (sales_account, "debit", amount),
        (receivable_account, "credit", amount),
    ]

    try:
        return create_journal(payment, "refund_succeeded", lines, refund=refund)
    except IntegrityError:
        # Concurrent insert - journal already exists. Treat as success.
        logger.info(
            "Ledger journal for refund %s (refund_succeeded) already exists "
            "(concurrent create).",
            refund.pk,
        )
        return None


