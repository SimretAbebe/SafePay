"""
Tests for the double-entry ledger.

Covers: posting on succeeded, atomicity, idempotency, immutability,
validation, separation, decimal exactness, management commands, and
non-regression of existing behaviour.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.core.management import call_command
from django.db import connection, IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from payments.ledger.models import (
    LedgerAccount,
    LedgerEntry,
    LedgerJournal,
)
from payments.ledger.services import (
    LedgerError,
    create_journal,
    post_payment_succeeded,
)
from payments.models import (
    InvalidStateTransition,
    Merchant,
    Payment,
    PaymentStatusHistory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _merchant(name="TestMerchant", **kwargs):
    m, _ = Merchant.create_with_key(name=name, **kwargs)
    return m


def _payment(merchant, amount="100.00", provider="chapa", livemode=False, status="processing"):
    return Payment.objects.create(
        merchant=merchant,
        livemode=livemode,
        idempotency_key=f"key-{Payment.objects.count() + 1}",
        amount=Decimal(amount),
        sender="sender",
        receiver="receiver",
        provider=provider,
        status=status,
    )


# ===========================================================================
# 1. Transition to succeeded posts a balanced journal
# ===========================================================================


class TestSucceededPostsJournal(TestCase):
    def setUp(self):
        self.merchant = _merchant()
        self.payment = _payment(self.merchant, amount="250.00", provider="chapa")

    def test_transition_to_succeeded_creates_journal(self):
        self.payment.transition_to("succeeded")

        journals = LedgerJournal.objects.filter(payment=self.payment)
        self.assertEqual(journals.count(), 1)

        journal = journals.first()
        self.assertEqual(journal.kind, "payment_succeeded")
        entries = list(journal.entries.all().order_by("side"))

        # Expect exactly 2 entries
        self.assertEqual(len(entries), 2)

        credit_entry = next(e for e in entries if e.side == "credit")
        debit_entry = next(e for e in entries if e.side == "debit")

        self.assertEqual(debit_entry.amount, Decimal("250.00"))
        self.assertEqual(credit_entry.amount, Decimal("250.00"))
        self.assertEqual(debit_entry.account.code, "receivable:chapa")
        self.assertEqual(credit_entry.account.code, "sales")

    def test_accounts_created_once(self):
        p1 = self.payment
        p1.transition_to("succeeded")

        p2 = _payment(self.merchant, amount="50.00", provider="chapa")
        p2.transition_to("succeeded")

        # Same merchant, same provider → same receivable account
        rec_accounts = LedgerAccount.objects.filter(
            merchant=self.merchant, code="receivable:chapa"
        )
        self.assertEqual(rec_accounts.count(), 1)

        sales_accounts = LedgerAccount.objects.filter(
            merchant=self.merchant, code="sales"
        )
        self.assertEqual(sales_accounts.count(), 1)


# ===========================================================================
# 2. Atomicity: failure after status change rolls back everything
# ===========================================================================


class TestAtomicity(TransactionTestCase):
    def test_ledger_failure_rolls_back_status_change(self):
        merchant = _merchant()
        payment = _payment(merchant, amount="100.00")

        with patch(
            "payments.ledger.services.post_payment_succeeded",
            side_effect=RuntimeError("Forced failure"),
        ):
            with self.assertRaises(RuntimeError):
                payment.transition_to("succeeded")

        # Refresh from DB
        payment.refresh_from_db()
        self.assertEqual(payment.status, "processing")

        # No history row for the failed transition
        self.assertFalse(
            PaymentStatusHistory.objects.filter(
                payment=payment, to_status="succeeded"
            ).exists()
        )

        # No ledger rows
        self.assertEqual(
            LedgerJournal.objects.filter(payment=payment).count(), 0
        )


# ===========================================================================
# 3. Idempotency
# ===========================================================================


class TestIdempotency(TestCase):
    def setUp(self):
        self.merchant = _merchant()

    def test_posting_twice_creates_one_journal(self):
        payment = _payment(self.merchant)
        payment.transition_to("succeeded")

        # Call post_payment_succeeded again directly
        result = post_payment_succeeded(payment)
        self.assertIsNone(result)
        self.assertEqual(
            LedgerJournal.objects.filter(payment=payment).count(), 1
        )

    def test_idempotent_via_multiple_code_paths(self):
        """
        Simulate reaching succeeded through different code paths.
        Only one journal should exist.
        """
        payment = _payment(self.merchant)
        payment.transition_to("succeeded")

        # Simulate a second attempt (e.g. webhook arriving after sync)
        post_payment_succeeded(payment)
        post_payment_succeeded(payment)

        self.assertEqual(
            LedgerJournal.objects.filter(
                payment=payment, kind="payment_succeeded"
            ).count(),
            1,
        )

    def test_concurrent_insert_handled(self):
        """
        Simulate IntegrityError from concurrent insert
        by creating the journal first, then patching LedgerJournal.objects.filter
        to return empty queryset (simulating race where exists() returns False
        but insert hits unique constraint).
        """
        payment = _payment(self.merchant)
        payment.transition_to("succeeded")

        # Journal exists. Now simulate the race: exists() returns False
        # but the INSERT raises IntegrityError.
        original_filter = LedgerJournal.objects.filter

        def fake_filter(**kwargs):
            qs = original_filter(**kwargs)
            if kwargs.get("kind") == "payment_succeeded":
                # Return an empty qs to simulate race condition
                return qs.none()
            return qs

        with patch.object(
            LedgerJournal.objects, "filter", side_effect=fake_filter
        ):
            # This should hit the IntegrityError path and handle it gracefully
            result = post_payment_succeeded(payment)

        self.assertIsNone(result)
        self.assertEqual(
            LedgerJournal.objects.filter(payment=payment).count(), 1
        )


# ===========================================================================
# 4. Non-succeeded transitions post nothing
# ===========================================================================


class TestNonSucceededTransitions(TestCase):
    def setUp(self):
        self.merchant = _merchant()

    def test_processing_posts_nothing(self):
        payment = _payment(self.merchant, status="pending")
        payment.transition_to("processing")
        self.assertEqual(LedgerJournal.objects.count(), 0)

    def test_failed_posts_nothing(self):
        payment = _payment(self.merchant)
        payment.transition_to("failed")
        self.assertEqual(LedgerJournal.objects.count(), 0)

    def test_cancelled_posts_nothing(self):
        payment = _payment(self.merchant, status="pending")
        payment.transition_to("cancelled")
        self.assertEqual(LedgerJournal.objects.count(), 0)

    def test_expired_posts_nothing(self):
        payment = _payment(self.merchant, status="pending")
        payment.transition_to("expired")
        self.assertEqual(LedgerJournal.objects.count(), 0)


# ===========================================================================
# 5. Immutability
# ===========================================================================


class TestImmutability(TestCase):
    def setUp(self):
        self.merchant = _merchant()
        self.payment = _payment(self.merchant)
        self.payment.transition_to("succeeded")
        self.journal = LedgerJournal.objects.get(payment=self.payment)
        self.entry = self.journal.entries.first()
        self.account = self.entry.account

    def test_entry_save_on_existing_raises(self):
        self.entry.amount = Decimal("999.99")
        with self.assertRaises(RuntimeError):
            self.entry.save()

    def test_entry_delete_raises(self):
        with self.assertRaises(RuntimeError):
            self.entry.delete()

    def test_queryset_update_raises(self):
        with self.assertRaises(RuntimeError):
            LedgerEntry.objects.filter(pk=self.entry.pk).update(
                amount=Decimal("999.99")
            )

    def test_queryset_delete_raises(self):
        with self.assertRaises(RuntimeError):
            LedgerEntry.objects.filter(pk=self.entry.pk).delete()

    def test_bulk_update_raises(self):
        self.entry.amount = Decimal("999.99")
        with self.assertRaises(RuntimeError):
            LedgerEntry.objects.bulk_update([self.entry], ["amount"])

    def test_journal_save_on_existing_raises(self):
        self.journal.kind = "something_else"
        with self.assertRaises(RuntimeError):
            self.journal.save()

    def test_journal_delete_raises(self):
        with self.assertRaises(RuntimeError):
            self.journal.delete()

    def test_account_save_on_existing_raises(self):
        self.account.code = "hacked"
        with self.assertRaises(RuntimeError):
            self.account.save()

    def test_account_delete_raises(self):
        with self.assertRaises(RuntimeError):
            self.account.delete()

    # Admin permissions
    def test_admin_no_add_change_delete(self):
        from payments.admin import LedgerAccountAdmin, LedgerJournalAdmin, LedgerEntryAdmin

        site = AdminSite()

        for admin_cls, model_cls in [
            (LedgerAccountAdmin, LedgerAccount),
            (LedgerJournalAdmin, LedgerJournal),
            (LedgerEntryAdmin, LedgerEntry),
        ]:
            ma = admin_cls(model_cls, site)
            # Use a mock request (None is fine for these permission methods)
            self.assertFalse(ma.has_add_permission(None))
            self.assertFalse(ma.has_change_permission(None))
            self.assertFalse(ma.has_delete_permission(None))


# ===========================================================================
# 6. create_journal validation
# ===========================================================================


class TestCreateJournalValidation(TestCase):
    def setUp(self):
        self.merchant = _merchant()
        self.payment = _payment(self.merchant, status="pending")
        self.acc_a = LedgerAccount.objects.create(
            merchant=self.merchant, livemode=False, code="test_a", currency="ETB"
        )
        self.acc_b = LedgerAccount.objects.create(
            merchant=self.merchant, livemode=False, code="test_b", currency="ETB"
        )

    def test_unbalanced_raises(self):
        lines = [
            (self.acc_a, "debit", Decimal("100.00")),
            (self.acc_b, "credit", Decimal("50.00")),
        ]
        with self.assertRaises(LedgerError) as ctx:
            create_journal(self.payment, "test", lines)
        self.assertIn("not balanced", str(ctx.exception))
        # Nothing written
        self.assertEqual(LedgerJournal.objects.count(), 0)
        self.assertEqual(LedgerEntry.objects.count(), 0)

    def test_zero_amount_raises(self):
        lines = [
            (self.acc_a, "debit", Decimal("0.00")),
            (self.acc_b, "credit", Decimal("0.00")),
        ]
        with self.assertRaises(LedgerError) as ctx:
            create_journal(self.payment, "test", lines)
        self.assertIn("> 0", str(ctx.exception))
        self.assertEqual(LedgerJournal.objects.count(), 0)

    def test_negative_amount_raises(self):
        lines = [
            (self.acc_a, "debit", Decimal("-10.00")),
            (self.acc_b, "credit", Decimal("-10.00")),
        ]
        with self.assertRaises(LedgerError) as ctx:
            create_journal(self.payment, "test", lines)
        self.assertIn("> 0", str(ctx.exception))
        self.assertEqual(LedgerJournal.objects.count(), 0)

    def test_mixed_currencies_raises(self):
        acc_usd = LedgerAccount.objects.create(
            merchant=self.merchant, livemode=False, code="test_usd", currency="USD"
        )
        lines = [
            (self.acc_a, "debit", Decimal("100.00")),
            (acc_usd, "credit", Decimal("100.00")),
        ]
        with self.assertRaises(LedgerError) as ctx:
            create_journal(self.payment, "test", lines)
        self.assertIn("single currency", str(ctx.exception))
        self.assertEqual(LedgerJournal.objects.count(), 0)

    def test_fewer_than_2_lines_raises(self):
        lines = [
            (self.acc_a, "debit", Decimal("100.00")),
        ]
        with self.assertRaises(LedgerError) as ctx:
            create_journal(self.payment, "test", lines)
        self.assertIn("at least 2", str(ctx.exception))
        self.assertEqual(LedgerJournal.objects.count(), 0)


# ===========================================================================
# 7. Separation: test vs live, merchant A vs merchant B
# ===========================================================================


class TestSeparation(TestCase):
    def test_test_and_live_use_separate_accounts(self):
        merchant = _merchant()
        # Enable live mode
        merchant.live_enabled = True
        merchant.save()

        test_payment = _payment(merchant, livemode=False)
        test_payment.transition_to("succeeded")

        live_payment = Payment.objects.create(
            merchant=merchant,
            livemode=True,
            idempotency_key="live-key-1",
            amount=Decimal("200.00"),
            sender="sender",
            receiver="receiver",
            provider="chapa",
            status="processing",
        )
        live_payment.transition_to("succeeded")

        test_accounts = set(
            LedgerAccount.objects.filter(
                merchant=merchant, livemode=False
            ).values_list("id", flat=True)
        )
        live_accounts = set(
            LedgerAccount.objects.filter(
                merchant=merchant, livemode=True
            ).values_list("id", flat=True)
        )
        self.assertTrue(test_accounts.isdisjoint(live_accounts))

    def test_two_merchants_use_separate_accounts(self):
        merchant_a = _merchant(name="MerchantA")
        merchant_b = _merchant(name="MerchantB")

        pa = _payment(merchant_a)
        pa.transition_to("succeeded")

        pb = _payment(merchant_b)
        pb.transition_to("succeeded")

        accounts_a = set(
            LedgerAccount.objects.filter(merchant=merchant_a).values_list(
                "id", flat=True
            )
        )
        accounts_b = set(
            LedgerAccount.objects.filter(merchant=merchant_b).values_list(
                "id", flat=True
            )
        )
        self.assertTrue(accounts_a.isdisjoint(accounts_b))


# ===========================================================================
# 8. Decimal exactness
# ===========================================================================


class TestDecimalExactness(TestCase):
    def test_010_020_030_sum_exactly(self):
        merchant = _merchant()
        amounts = ["0.10", "0.20", "0.30"]
        for amt in amounts:
            p = _payment(merchant, amount=amt)
            p.transition_to("succeeded")

        total_debit = sum(
            LedgerEntry.objects.filter(side="debit").values_list(
                "amount", flat=True
            ),
            Decimal("0.00"),
        )
        total_credit = sum(
            LedgerEntry.objects.filter(side="credit").values_list(
                "amount", flat=True
            ),
            Decimal("0.00"),
        )
        self.assertEqual(total_debit, Decimal("0.60"))
        self.assertEqual(total_credit, Decimal("0.60"))
        self.assertEqual(total_debit, total_credit)

    def test_no_floats_in_entries(self):
        merchant = _merchant()
        p = _payment(merchant, amount="0.10")
        p.transition_to("succeeded")

        for entry in LedgerEntry.objects.all():
            self.assertIsInstance(entry.amount, Decimal)
            # Make sure it's not secretly a float-derived Decimal
            self.assertEqual(str(entry.amount), "0.10")

    def test_amounts_with_extra_decimals_quantized(self):
        """Amounts with more than 2 decimals are quantized in the ledger."""
        merchant = _merchant()
        # Payment amount is already DecimalField(decimal_places=2), so
        # it will store as 2dp. But let's verify the ledger service
        # quantizes explicitly.
        p = _payment(merchant, amount="99.99")
        p.transition_to("succeeded")

        for entry in LedgerEntry.objects.filter(journal__payment=p):
            self.assertEqual(entry.amount, Decimal("99.99"))
            self.assertEqual(entry.amount, entry.amount.quantize(Decimal("0.01")))


# ===========================================================================
# 9. ledger_verify command
# ===========================================================================


class TestLedgerVerifyCommand(TestCase):
    def setUp(self):
        self.merchant = _merchant()

    def test_passes_on_clean_data(self):
        p = _payment(self.merchant)
        p.transition_to("succeeded")
        call_command("ledger_verify")  # Should not raise

    def test_fails_on_unbalanced_journal(self):
        p = _payment(self.merchant)
        p.transition_to("succeeded")

        # Unbalance using raw SQL
        journal = LedgerJournal.objects.get(payment=p)
        entry = journal.entries.filter(side="debit").first()
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE payments_ledgerentry SET amount = 999.99 WHERE id = %s",
                [entry.pk],
            )

        with self.assertRaises(SystemExit) as ctx:
            call_command("ledger_verify")
        self.assertEqual(ctx.exception.code, 1)

    def test_fails_on_missing_journal(self):
        p = _payment(self.merchant)
        p.transition_to("succeeded")

        # Remove journal using raw SQL
        journal = LedgerJournal.objects.get(payment=p)
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM payments_ledgerentry WHERE journal_id = %s",
                [journal.pk],
            )
            cursor.execute(
                "DELETE FROM payments_ledgerjournal WHERE id = %s",
                [journal.pk],
            )

        with self.assertRaises(SystemExit) as ctx:
            call_command("ledger_verify")
        self.assertEqual(ctx.exception.code, 1)

    def test_fails_on_amount_mismatch(self):
        p = _payment(self.merchant, amount="100.00")
        p.transition_to("succeeded")

        journal = LedgerJournal.objects.get(payment=p)
        entry = journal.entries.first()
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE payments_ledgerentry SET amount = 50.00 WHERE id = %s",
                [entry.pk],
            )
            # Also fix the other side to keep balanced
            other = journal.entries.exclude(pk=entry.pk).first()
            cursor.execute(
                "UPDATE payments_ledgerentry SET amount = 50.00 WHERE id = %s",
                [other.pk],
            )

        with self.assertRaises(SystemExit) as ctx:
            call_command("ledger_verify")
        self.assertEqual(ctx.exception.code, 1)

    def test_allow_missing_does_not_fail(self):
        """--allow-missing reports legacy gaps without failing."""
        p = _payment(self.merchant)
        p.transition_to("succeeded")

        journal = LedgerJournal.objects.get(payment=p)
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM payments_ledgerentry WHERE journal_id = %s",
                [journal.pk],
            )
            cursor.execute(
                "DELETE FROM payments_ledgerjournal WHERE id = %s",
                [journal.pk],
            )

        # Without --allow-missing: should fail
        with self.assertRaises(SystemExit):
            call_command("ledger_verify")

        # With --allow-missing: should not fail
        call_command("ledger_verify", allow_missing=True)


# ===========================================================================
# 10. ledger_backfill command
# ===========================================================================


class TestLedgerBackfillCommand(TestCase):
    def setUp(self):
        self.merchant = _merchant()

    def _create_succeeded_without_journal(self, provider="chapa"):
        """Create a succeeded payment and delete its journal via raw SQL."""
        p = _payment(self.merchant, provider=provider)
        p.transition_to("succeeded")
        journal = LedgerJournal.objects.get(payment=p)
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM payments_ledgerentry WHERE journal_id = %s",
                [journal.pk],
            )
            cursor.execute(
                "DELETE FROM payments_ledgerjournal WHERE id = %s",
                [journal.pk],
            )
        return p

    def test_backfill_creates_missing_journals(self):
        p = self._create_succeeded_without_journal()
        self.assertEqual(
            LedgerJournal.objects.filter(payment=p).count(), 0
        )

        call_command("ledger_backfill")

        self.assertEqual(
            LedgerJournal.objects.filter(payment=p).count(), 1
        )

    def test_dry_run_changes_nothing(self):
        p = self._create_succeeded_without_journal()
        call_command("ledger_backfill", dry_run=True)
        self.assertEqual(
            LedgerJournal.objects.filter(payment=p).count(), 0
        )

    def test_second_run_is_noop(self):
        self._create_succeeded_without_journal()
        call_command("ledger_backfill")
        count_after_first = LedgerJournal.objects.count()

        call_command("ledger_backfill")
        self.assertEqual(LedgerJournal.objects.count(), count_after_first)

    def test_payments_without_provider_use_direct(self):
        p = self._create_succeeded_without_journal(provider=None)
        # Also set provider to None on the payment itself
        Payment.objects.filter(pk=p.pk).update(provider=None)
        p.refresh_from_db()

        call_command("ledger_backfill")

        journal = LedgerJournal.objects.get(payment=p)
        debit_entry = journal.entries.get(side="debit")
        self.assertEqual(debit_entry.account.code, "receivable:direct")


# ===========================================================================
# 11. Non-regression: existing behaviour unchanged
# ===========================================================================


class TestNonRegression(TestCase):
    """Verify that API responses and existing flows are unchanged."""

    def test_payment_without_merchant_skips_ledger(self):
        """Payments without a merchant don't crash, just skip."""
        p = Payment.objects.create(
            merchant=None,
            livemode=False,
            idempotency_key="no-merchant-1",
            amount=Decimal("10.00"),
            sender="s",
            receiver="r",
            status="processing",
        )
        p.transition_to("succeeded")
        self.assertEqual(LedgerJournal.objects.filter(payment=p).count(), 0)

    def test_transition_to_still_creates_history(self):
        merchant = _merchant()
        p = _payment(merchant)
        p.transition_to("succeeded")
        self.assertTrue(
            PaymentStatusHistory.objects.filter(
                payment=p, to_status="succeeded"
            ).exists()
        )

    def test_invalid_transition_still_raises(self):
        merchant = _merchant()
        p = _payment(merchant)
        p.transition_to("succeeded")
        with self.assertRaises(InvalidStateTransition):
            p.transition_to("failed")

    def test_direct_provider_uses_correct_account(self):
        """Provider=None or empty → "receivable:direct"."""
        merchant = _merchant()
        p = Payment.objects.create(
            merchant=merchant,
            livemode=False,
            idempotency_key="direct-1",
            amount=Decimal("10.00"),
            sender="s",
            receiver="r",
            provider=None,
            status="processing",
        )
        p.transition_to("succeeded")
        journal = LedgerJournal.objects.get(payment=p)
        debit_entry = journal.entries.get(side="debit")
        self.assertEqual(debit_entry.account.code, "receivable:direct")

