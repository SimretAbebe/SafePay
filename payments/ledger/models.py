"""
Double-entry ledger models.

These models are **append-only**: rows may be created but never updated or
deleted through the ORM.  The save()/delete() overrides and the custom
ImmutableQuerySet enforce this at the application layer.

NOTE: For production-grade immutability a database trigger that raises on
UPDATE or DELETE would be the definitive guard.  The Python-level checks
here are a defence-in-depth measure.
"""

from django.db import models


# ---------------------------------------------------------------------------
# Immutable QuerySet / Manager
# ---------------------------------------------------------------------------

class ImmutableQuerySet(models.QuerySet):
    """QuerySet that forbids update(), delete() and bulk_update()."""

    def update(self, **kwargs):
        raise RuntimeError(
            f"{self.model.__name__} rows are immutable and cannot be updated."
        )

    def delete(self):
        raise RuntimeError(
            f"{self.model.__name__} rows are immutable and cannot be deleted."
        )

    def bulk_update(self, objs, fields, batch_size=None):
        raise RuntimeError(
            f"{self.model.__name__} rows are immutable and cannot be bulk-updated."
        )


class ImmutableManager(models.Manager):
    def get_queryset(self):
        return ImmutableQuerySet(self.model, using=self._db)

    def update(self, **kwargs):
        raise RuntimeError(
            f"{self.model.__name__} rows are immutable and cannot be updated."
        )

    def delete(self):
        raise RuntimeError(
            f"{self.model.__name__} rows are immutable and cannot be deleted."
        )

    def bulk_update(self, objs, fields, batch_size=None):
        raise RuntimeError(
            f"{self.model.__name__} rows are immutable and cannot be bulk-updated."
        )


# ---------------------------------------------------------------------------
# Base immutable model
# ---------------------------------------------------------------------------

class ImmutableModel(models.Model):
    """Abstract base that prevents save-on-existing and delete."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None and self.__class__.objects.filter(pk=self.pk).exists():
            raise RuntimeError(
                f"{self.__class__.__name__} rows are immutable and cannot be updated."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError(
            f"{self.__class__.__name__} rows are immutable and cannot be deleted."
        )


# ---------------------------------------------------------------------------
# LedgerAccount
# ---------------------------------------------------------------------------

class LedgerAccount(ImmutableModel):
    """
    A named ledger account scoped to a merchant, livemode, code and currency.

    Examples of codes: "receivable:chapa", "receivable:direct", "sales".
    """

    merchant = models.ForeignKey(
        "payments.Merchant",
        on_delete=models.PROTECT,
        related_name="ledger_accounts",
    )
    livemode = models.BooleanField()
    code = models.CharField(max_length=100)
    currency = models.CharField(max_length=10, default="ETB")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableManager()

    class Meta:
        app_label = "payments"
        constraints = [
            models.UniqueConstraint(
                fields=["merchant", "livemode", "code", "currency"],
                name="unique_ledger_account",
            ),
        ]

    def __str__(self):
        return f"{self.code} ({self.currency}) merchant={self.merchant_id} live={self.livemode}"


# ---------------------------------------------------------------------------
# LedgerJournal
# ---------------------------------------------------------------------------

class LedgerJournal(ImmutableModel):
    """
    Groups a balanced set of LedgerEntry rows for one event on a payment.
    """

    KIND_PAYMENT_SUCCEEDED = "payment_succeeded"
    KIND_REFUND_SUCCEEDED = "refund_succeeded"

    payment = models.ForeignKey(
        "payments.Payment",
        on_delete=models.PROTECT,
        related_name="ledger_journals",
    )
    refund = models.ForeignKey(
        "payments.Refund",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ledger_journals",
    )
    kind = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableManager()

    class Meta:
        app_label = "payments"
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "kind"],
                condition=models.Q(kind="payment_succeeded"),
                name="unique_journal_per_payment_kind",
            ),
            models.UniqueConstraint(
                fields=["refund", "kind"],
                condition=models.Q(kind="refund_succeeded"),
                name="unique_journal_per_refund_kind",
            ),
        ]

    def __str__(self):
        if self.refund_id:
            return f"Journal {self.pk}: {self.kind} for refund {self.refund_id} (payment {self.payment_id})"
        return f"Journal {self.pk}: {self.kind} for payment {self.payment_id}"


# ---------------------------------------------------------------------------
# LedgerEntry
# ---------------------------------------------------------------------------

SIDE_CHOICES = [
    ("debit", "Debit"),
    ("credit", "Credit"),
]


class LedgerEntry(ImmutableModel):
    """
    One leg of a journal entry (debit or credit to an account).
    """

    journal = models.ForeignKey(
        LedgerJournal,
        on_delete=models.PROTECT,
        related_name="entries",
    )
    account = models.ForeignKey(
        LedgerAccount,
        on_delete=models.PROTECT,
        related_name="entries",
    )
    side = models.CharField(max_length=6, choices=SIDE_CHOICES)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableManager()

    class Meta:
        app_label = "payments"
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount__gt=0),
                name="ledger_entry_amount_positive",
            ),
        ]

    def __str__(self):
        return f"{self.side} {self.amount} -> {self.account.code}"

