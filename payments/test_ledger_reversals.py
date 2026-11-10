"""
Tests for double-entry ledger reversals on refund success.

Covers:
1. Succeeded refund creates one reversal journal with debit sales, credit receivable.
2. Net balances after sale + full/partial refund.
3. Pending, failed, and unsupported refunds post nothing.
4. Idempotency: repeated posting and repeated refund requests create nothing new.
5. Atomicity: exception during refund post rolls back status and journal.
6. Existing LedgerEntry, LedgerJournal, and LedgerAccount rows are unchanged.
7. Test and live refunds post to separate accounts; merchants never share accounts.
8. ledger_verify passes on clean data and fails (exit code 1) on tampered or missing journals.
9. Immutability guards block edits and deletes of reversal journals.
10. Refund API responses remain unchanged.
"""

from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payments.ledger.models import LedgerAccount, LedgerEntry, LedgerJournal
from payments.ledger.services import create_journal, post_refund_succeeded
from payments.models import Merchant, Payment, Refund
from payments.providers.fake import FakeProvider
from payments.refunds import request_refund
from payments.test_utils import create_test_merchant


class LedgerReversalsTests(TestCase):
    def setUp(self):
        super().setUp()
        FakeProvider.reset()
        self.merchant, self.raw_key = create_test_merchant(name="Ledger Reversal Merchant")
        self.other_merchant, self.other_raw_key = create_test_merchant(name="Other Merchant")
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY=self.raw_key)

    def tearDown(self):
        FakeProvider.reset()
        super().tearDown()

    def _create_succeeded_payment(self, merchant=None, amount="100.00", provider="fake", livemode=False):
        p = Payment.objects.create(
            merchant=merchant or self.merchant,
            amount=Decimal(amount),
            status="processing",
            provider=provider,
            livemode=livemode,
            sender="alice@example.com",
            receiver="bob@example.com",
            idempotency_key=f"pay-rev-{Payment.objects.count() + 1}",
        )
        p.transition_to("succeeded")
        return p

    # -------------------------------------------------------------------------
    # 1. Succeeded refund creates one reversal journal with debit sales, credit rec
    # -------------------------------------------------------------------------
    def test_succeeded_refund_creates_reversal_journal(self):
        payment = self._create_succeeded_payment(amount="100.00", provider="fake")
        initial_journals = list(LedgerJournal.objects.filter(payment=payment))
        self.assertEqual(len(initial_journals), 1)
        self.assertEqual(initial_journals[0].kind, "payment_succeeded")

        refund, code = request_refund(payment, Decimal("40.00"), "idem-rev-1")
        self.assertEqual(code, 201)
        self.assertEqual(refund.status, "succeeded")

        # Must have exactly 1 reversal journal
        reversal_journals = list(LedgerJournal.objects.filter(refund=refund))
        self.assertEqual(len(reversal_journals), 1)
        journal = reversal_journals[0]
        self.assertEqual(journal.kind, "refund_succeeded")
        self.assertEqual(journal.payment, payment)
        self.assertEqual(journal.refund, refund)

        entries = list(journal.entries.all())
        self.assertEqual(len(entries), 2)

        debit_entry = next(e for e in entries if e.side == "debit")
        credit_entry = next(e for e in entries if e.side == "credit")

        self.assertEqual(debit_entry.account.code, "sales")
        self.assertEqual(debit_entry.amount, Decimal("40.00"))
        self.assertEqual(credit_entry.account.code, "receivable:fake")
        self.assertEqual(credit_entry.amount, Decimal("40.00"))

        # Sale journal is unchanged
        sale_journal = LedgerJournal.objects.get(payment=payment, kind="payment_succeeded")
        sale_entries = list(sale_journal.entries.all())
        self.assertEqual(len(sale_entries), 2)
        sale_rec = next(e for e in sale_entries if e.side == "debit")
        sale_sales = next(e for e in sale_entries if e.side == "credit")
        self.assertEqual(sale_rec.amount, Decimal("100.00"))
        self.assertEqual(sale_sales.amount, Decimal("100.00"))

    # -------------------------------------------------------------------------
    # 2. Net balances after sale + full refund and partial refund
    # -------------------------------------------------------------------------
    def test_net_balances_partial_and_full_refund(self):
        payment = self._create_succeeded_payment(amount="100.00", provider="fake")
        sales_acc = LedgerAccount.objects.get(merchant=self.merchant, code="sales", livemode=False)
        rec_acc = LedgerAccount.objects.get(merchant=self.merchant, code="receivable:fake", livemode=False)

        def sales_net():
            credits = sum(e.amount for e in sales_acc.entries.filter(side="credit"))
            debits = sum(e.amount for e in sales_acc.entries.filter(side="debit"))
            return credits - debits

        def rec_net():
            debits = sum(e.amount for e in rec_acc.entries.filter(side="debit"))
            credits = sum(e.amount for e in rec_acc.entries.filter(side="credit"))
            return debits - credits

        # Initial sale: 100 net on both
        self.assertEqual(sales_net(), Decimal("100.00"))
        self.assertEqual(rec_net(), Decimal("100.00"))

        # Partial refund 40.00
        refund1, _ = request_refund(payment, Decimal("40.00"), "idem-bal-1")
        self.assertEqual(refund1.status, "succeeded")
        self.assertEqual(sales_net(), Decimal("60.00"))
        self.assertEqual(rec_net(), Decimal("60.00"))

        # Full remaining refund 60.00
        refund2, _ = request_refund(payment, Decimal("60.00"), "idem-bal-2")
        self.assertEqual(refund2.status, "succeeded")
        self.assertEqual(sales_net(), Decimal("0.00"))
        self.assertEqual(rec_net(), Decimal("0.00"))

    # -------------------------------------------------------------------------
    # 3. Pending, failed, and unsupported refunds post nothing
    # -------------------------------------------------------------------------
    def test_non_succeeded_refunds_post_nothing(self):
        payment_chapa = self._create_succeeded_payment(amount="50.00", provider="chapa")
        journals_before = LedgerJournal.objects.count()

        # 1. Unsupported provider
        ref_unsupported, code = request_refund(payment_chapa, Decimal("50.00"), "idem-unsupp")
        self.assertEqual(code, 409)
        self.assertEqual(ref_unsupported.status, "unsupported")
        self.assertEqual(LedgerJournal.objects.count(), journals_before)

        # 2. Pending/processing payment
        payment_pending = Payment.objects.create(
            merchant=self.merchant,
            amount=Decimal("50.00"),
            status="pending",
            provider="fake",
            sender="a@b.com",
            receiver="b@b.com",
            idempotency_key="pay-pend",
        )
        ref_none, code = request_refund(payment_pending, Decimal("50.00"), "idem-pend")
        self.assertEqual(code, 409)
        self.assertEqual(LedgerJournal.objects.count(), journals_before)

        # 3. Provider error (not_created=True) -> status failed
        payment_fake = self._create_succeeded_payment(amount="50.00", provider="fake")
        journals_before = LedgerJournal.objects.count()
        FakeProvider.set_error(not_created=True)
        ref_failed, _ = request_refund(payment_fake, Decimal("20.00"), "idem-fail")
        self.assertEqual(ref_failed.status, "failed")
        self.assertEqual(LedgerJournal.objects.count(), journals_before)

        # 4. Uncertain error -> status pending
        FakeProvider.set_error(not_created=False, infrastructure=True)
        ref_pending, _ = request_refund(payment_fake, Decimal("20.00"), "idem-uncert")
        self.assertEqual(ref_pending.status, "pending")
        self.assertEqual(LedgerJournal.objects.count(), journals_before)

    # -------------------------------------------------------------------------
    # 4. Idempotency: calling posting twice or repeat refund requests
    # -------------------------------------------------------------------------
    def test_idempotency_of_reversal_posting(self):
        payment = self._create_succeeded_payment(amount="100.00", provider="fake")
        refund, _ = request_refund(payment, Decimal("30.00"), "idem-repeat")
        self.assertEqual(refund.status, "succeeded")

        journals_count = LedgerJournal.objects.filter(refund=refund).count()
        self.assertEqual(journals_count, 1)

        # Calling post_refund_succeeded again directly returns None and creates nothing
        res = post_refund_succeeded(refund)
        self.assertIsNone(res)
        self.assertEqual(LedgerJournal.objects.filter(refund=refund).count(), 1)

        # Repeating the refund request returns the same refund with 200 and no new journal
        refund_replayed, code = request_refund(payment, Decimal("30.00"), "idem-repeat")
        self.assertEqual(code, 200)
        self.assertEqual(refund_replayed.id, refund.id)
        self.assertEqual(LedgerJournal.objects.filter(refund=refund).count(), 1)

    # -------------------------------------------------------------------------
    # 5. Atomicity: exception during refund post rolls back status and journal
    # -------------------------------------------------------------------------
    def test_atomicity_rolls_back_status_and_journal_on_error(self):
        payment = self._create_succeeded_payment(amount="100.00", provider="fake")

        with patch("payments.ledger.services.post_refund_succeeded", side_effect=RuntimeError("Simulated ledger failure")):
            with self.assertRaises(RuntimeError):
                request_refund(payment, Decimal("40.00"), "idem-atomicity")

        # The refund row should not be marked succeeded in the DB
        refund = Refund.objects.filter(idempotency_key="idem-atomicity").first()
        if refund:
            self.assertNotEqual(refund.status, "succeeded")
        # No refund_succeeded journal created
        self.assertEqual(LedgerJournal.objects.filter(kind="refund_succeeded").count(), 0)

    # -------------------------------------------------------------------------
    # 6. Existing rows unchanged
    # -------------------------------------------------------------------------
    def test_existing_rows_unchanged(self):
        payment = self._create_succeeded_payment(amount="100.00", provider="fake")
        account_snapshot = list(LedgerAccount.objects.values())
        journal_snapshot = list(LedgerJournal.objects.values())
        entry_snapshot = list(LedgerEntry.objects.values())

        refund, _ = request_refund(payment, Decimal("35.00"), "idem-snapshot")
        self.assertEqual(refund.status, "succeeded")

        for acc in account_snapshot:
            current = LedgerAccount.objects.filter(id=acc["id"]).values().first()
            self.assertEqual(acc, current)

        for j in journal_snapshot:
            current = LedgerJournal.objects.filter(id=j["id"]).values().first()
            self.assertEqual(j, current)

        for e in entry_snapshot:
            current = LedgerEntry.objects.filter(id=e["id"]).values().first()
            self.assertEqual(e, current)

    # -------------------------------------------------------------------------
    # 7. Test and live refunds post to separate accounts; merchants never share
    # -------------------------------------------------------------------------
    def test_account_isolation_merchants_and_livemode(self):
        p_test_a = self._create_succeeded_payment(merchant=self.merchant, livemode=False, amount="50.00")
        p_live_a = self._create_succeeded_payment(merchant=self.merchant, livemode=True, amount="50.00")
        p_test_b = self._create_succeeded_payment(merchant=self.other_merchant, livemode=False, amount="50.00")

        ref_test_a, _ = request_refund(p_test_a, Decimal("10.00"), "idem-test-a")
        ref_live_a, _ = request_refund(p_live_a, Decimal("10.00"), "idem-live-a")
        ref_test_b, _ = request_refund(p_test_b, Decimal("10.00"), "idem-test-b")

        j_test_a = LedgerJournal.objects.get(refund=ref_test_a)
        j_live_a = LedgerJournal.objects.get(refund=ref_live_a)
        j_test_b = LedgerJournal.objects.get(refund=ref_test_b)

        accs_test_a = set(j_test_a.entries.values_list("account_id", flat=True))
        accs_live_a = set(j_live_a.entries.values_list("account_id", flat=True))
        accs_test_b = set(j_test_b.entries.values_list("account_id", flat=True))

        # None of the accounts intersect across modes or merchants
        self.assertTrue(accs_test_a.isdisjoint(accs_live_a))
        self.assertTrue(accs_test_a.isdisjoint(accs_test_b))
        self.assertTrue(accs_live_a.isdisjoint(accs_test_b))

    # -------------------------------------------------------------------------
    # 8. ledger_verify passes on clean data and fails when tampered
    # -------------------------------------------------------------------------
    def test_ledger_verify_detects_tampering_and_missing_refund_journal(self):
        payment = self._create_succeeded_payment(amount="100.00", provider="fake")
        refund, _ = request_refund(payment, Decimal("40.00"), "idem-vfy")
        self.assertEqual(refund.status, "succeeded")

        # Clean check passes
        call_command("ledger_verify")

        # Tampering with refund journal entry amount via raw SQL
        journal = LedgerJournal.objects.get(refund=refund)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE payments_ledgerentry SET amount = 999.99 WHERE journal_id = %s AND side = 'debit'",
                [journal.id],
            )

        with self.assertRaises(SystemExit) as ctx:
            call_command("ledger_verify")
        self.assertEqual(ctx.exception.code, 1)

        # Restore amount
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE payments_ledgerentry SET amount = 40.00 WHERE journal_id = %s AND side = 'debit'",
                [journal.id],
            )
        call_command("ledger_verify")

        # Missing refund journal (delete via raw SQL)
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM payments_ledgerentry WHERE journal_id = %s", [journal.id])
            cursor.execute("DELETE FROM payments_ledgerjournal WHERE id = %s", [journal.id])

        with self.assertRaises(SystemExit) as ctx:
            call_command("ledger_verify")
        self.assertEqual(ctx.exception.code, 1)

    # -------------------------------------------------------------------------
    # 9. Immutability guards block edits and deletes of new journals
    # -------------------------------------------------------------------------
    def test_immutability_guards_on_refund_journals(self):
        payment = self._create_succeeded_payment(amount="100.00", provider="fake")
        refund, _ = request_refund(payment, Decimal("25.00"), "idem-imm")
        journal = LedgerJournal.objects.get(refund=refund)

        with self.assertRaises(RuntimeError):
            journal.save()

        with self.assertRaises(RuntimeError):
            journal.delete()

        with self.assertRaises(RuntimeError):
            LedgerJournal.objects.filter(pk=journal.pk).update(kind="tampered")

        with self.assertRaises(RuntimeError):
            LedgerJournal.objects.filter(pk=journal.pk).delete()

    # -------------------------------------------------------------------------
    # 10. The refund API responses are unchanged
    # -------------------------------------------------------------------------
    def test_refund_api_response_structure_unchanged(self):
        payment = self._create_succeeded_payment(amount="100.00", provider="fake")
        url = reverse("create_refund", args=[payment.id])

        response = self.client.post(
            url,
            {"amount": "30.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-api-resp",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()

        expected_keys = {"id", "payment_id", "amount", "status", "error_code", "created_at"}
        self.assertEqual(set(data.keys()), expected_keys)
        self.assertEqual(data["payment_id"], payment.id)
        self.assertEqual(data["amount"], "30.00")
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["error_code"], "")

        # Replay response
        replayed = self.client.post(
            url,
            {"amount": "30.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-api-resp",
        )
        self.assertEqual(replayed.status_code, status.HTTP_200_OK)
        self.assertEqual(replayed.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(set(replayed.json().keys()), expected_keys)

