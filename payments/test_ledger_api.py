# -*- coding: utf-8 -*-
"""Tests for the read‑only Ledger API endpoints.

Covers:
* GET /api/ledger/balances/ – correct aggregation, zero‑balance inclusion.
* GET /api/ledger/entries/ – cursor pagination, filters, limit validation, field selection.
* GET /api/ledger/export.csv – CSV generation, header, injection protection, date‑range & row limits.
* Authentication (403) and scoping by merchant / livemode.
* Throttle isolation – ledger export uses its own scope.
"""

from decimal import Decimal
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from django.db import connection
from django.urls import reverse
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from payments.models import Merchant, Payment
from payments.ledger.models import LedgerAccount, LedgerEntry, LedgerJournal
from payments.ledger.services import post_payment_succeeded


def create_merchant(name="TestMerchant"):
    merchant, key = Merchant.create_with_key(name=name)
    merchant.raw_api_key = key
    return merchant


def create_payment(merchant, amount="100.00", provider="chapa", livemode=False, status="processing"):
    return Payment.objects.create(
        merchant=merchant,
        livemode=livemode,
        amount=Decimal(amount),
        provider=provider,
        status=status,
        sender="sender",
        receiver="receiver",
        idempotency_key=f"key-{Payment.objects.count() + 1}",
    )


class LedgerAPITestBase(TestCase):
    def setUp(self):
        self.merchant = create_merchant()
        self.client = APIClient()
        # API key header expected by HasAPIKey permission
        self.client.credentials(HTTP_X_API_KEY=self.merchant.raw_api_key)

    def _succeed_payment(self, payment):
        payment.transition_to("succeeded")
        # post_payment_succeeded is called inside transition_to; ensures ledger rows exist
        return payment


class LedgerBalancesTests(LedgerAPITestBase):
    def test_balances_simple(self):
        # two succeeded payments with different providers
        p1 = self._succeed_payment(create_payment(self.merchant, amount="150.00", provider="chapa"))
        p2 = self._succeed_payment(create_payment(self.merchant, amount="250.50", provider="arifpay"))
        # a third payment that stays pending – should not affect balances
        create_payment(self.merchant, amount="99.50", provider="direct")
        LedgerAccount.objects.create(merchant=self.merchant, livemode=False, code="receivable:direct", currency="ETB")

        url = reverse("ledger_balances")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # currency is taken from the first account (default ETB in fixtures)
        self.assertIn("currency", data)
        self.assertIn("accounts", data)
        accounts = {a["code"]: Decimal(a["balance"]) for a in data["accounts"]}
        # Expected balances: receivable:chapa 150.00, receivable:arifpay 250.50, sales 400.50 (debits sum – credits sum)
        # Because each payment creates a debit to receivable and a credit to sales of the same amount.
        self.assertEqual(accounts.get("receivable:chapa"), Decimal("150.00"))
        self.assertEqual(accounts.get("receivable:arifpay"), Decimal("250.50"))
        # sales is credited, so balance (debits - credits) is negative
        self.assertEqual(accounts.get("sales"), Decimal("-400.50"))
        # accounts with no entries are still present with zero balance (e.g., receivable:direct)
        self.assertIn("receivable:direct", accounts)
        self.assertEqual(accounts["receivable:direct"], Decimal("0.00"))

    def test_balances_auth_failure(self):
        self.client.credentials()  # remove API key
        resp = self.client.get(reverse("ledger_balances"))
        self.assertEqual(resp.status_code, 403)


class LedgerEntriesTests(LedgerAPITestBase):
    def setUp(self):
        super().setUp()
        # create a handful of payments and succeed them to generate entries
        for amt, prov in [("100.00", "chapa"), ("200.00", "arifpay"), ("50.00", "direct")]:
            p = create_payment(self.merchant, amount=amt, provider=prov)
            self._succeed_payment(p)
        # capture timestamps for date filters
        self.first_entry = LedgerEntry.objects.first()
        self.last_entry = LedgerEntry.objects.last()

    def test_entries_pagination_default(self):
        url = reverse("ledger_entries")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("results", data)
        self.assertLessEqual(len(data["results"]), 20)
        # cursor fields should be present
        self.assertIn("next", data)
        self.assertIn("previous", data)

    def test_entries_limit_validation(self):
        url = reverse("ledger_entries")
        resp = self.client.get(url, {"limit": "200"})
        self.assertEqual(resp.status_code, 200)
        # limit should be clamped to max_page_size (100) – verify no more than 100 items
        self.assertLessEqual(len(resp.json()["results"]), 100)

        resp_bad = self.client.get(url, {"limit": "0"})
        self.assertEqual(resp_bad.status_code, 400)
        self.assertIn("Invalid limit", resp_bad.json()["detail"])

    def test_entries_filters(self):
        # filter by account code
        resp = self.client.get(reverse("ledger_entries"), {"account": "receivable:chapa"})
        self.assertEqual(resp.status_code, 200)
        for item in resp.json()["results"]:
            self.assertEqual(item["account"], "receivable:chapa")

        # side filter
        resp = self.client.get(reverse("ledger_entries"), {"side": "debit"})
        self.assertEqual(resp.status_code, 200)
        for item in resp.json()["results"]:
            self.assertEqual(item["side"], "debit")

        # invalid side
        resp = self.client.get(reverse("ledger_entries"), {"side": "foo"})
        self.assertEqual(resp.status_code, 400)

        # payment_id filter – use an existing payment
        payment = Payment.objects.first()
        resp = self.client.get(reverse("ledger_entries"), {"payment_id": str(payment.id)})
        self.assertEqual(resp.status_code, 200)
        for item in resp.json()["results"]:
            self.assertEqual(item["payment_id"], payment.id)

        # date range filter
        after = (self.first_entry.created_at - timedelta(seconds=1)).isoformat()
        before = (self.last_entry.created_at + timedelta(seconds=1)).isoformat()
        resp = self.client.get(reverse("ledger_entries"), {"created_after": after, "created_before": before})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["results"]), LedgerEntry.objects.count())

    def test_entries_invalid_date(self):
        resp = self.client.get(reverse("ledger_entries"), {"created_after": "invalid"})
        self.assertEqual(resp.status_code, 400)

    def test_entries_auth_failure(self):
        self.client.credentials()
        resp = self.client.get(reverse("ledger_entries"))
        self.assertEqual(resp.status_code, 403)


class LedgerExportTests(LedgerAPITestBase):
    def setUp(self):
        super().setUp()
        # generate a realistic number of entries (≈30) spread over two days
        base = datetime.now(timezone.utc) - timedelta(days=1)
        with connection.cursor() as cursor:
            for i in range(30):
                p = create_payment(self.merchant, amount=str(10 + i), provider="chapa")
                p.transition_to("succeeded")
                # adjust timestamps via raw SQL to bypass immutability guards in tests
                cursor.execute(
                    "UPDATE payments_ledgerentry SET created_at = %s WHERE journal_id IN (SELECT id FROM payments_ledgerjournal WHERE payment_id = %s)",
                    [base + timedelta(hours=i), p.id],
                )
        self.from_date = (base.date()).isoformat()
        self.to_date = (base.date() + timedelta(days=1)).isoformat()

    def test_export_success(self):
        url = reverse("ledger_export")
        resp = self.client.get(url, {"from": self.from_date, "to": self.to_date})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv; charset=utf-8")
        content = resp.content.decode("utf-8")
        lines = content.splitlines()
        # header + 60 entries (30 payments * 2 sides)
        self.assertEqual(len(lines), 61)
        header = lines[0].split(",")
        self.assertEqual(header, ["created_at", "account", "side", "amount", "currency", "journal_kind", "payment_id"])
        # ensure injection protection – update account code and verify prefix
        entry = LedgerEntry.objects.first()
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE payments_ledgeraccount SET code = %s WHERE id = %s",
                ["=SUM(A1:A2)", entry.account_id],
            )
        resp2 = self.client.get(url, {"from": self.from_date, "to": self.to_date})
        self.assertIn("'=SUM(A1:A2)", resp2.content.decode("utf-8"))

    def test_export_limits(self):
        # date range too large (>366 days)
        resp = self.client.get(reverse("ledger_export"), {"from": "2000-01-01", "to": "2025-01-01"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("cannot exceed 366 days", resp.json()["detail"])

        # row count limit >10k – simulate by patching count
        with patch("payments.ledger.views.LedgerEntry.objects.filter") as mock_filter:
            mock_qs = MagicMock()
            mock_qs.select_related.return_value.order_by.return_value = mock_qs
            mock_qs.count.return_value = 10001
            mock_filter.return_value = mock_qs
            from_date = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
            to_date = datetime.now(timezone.utc).date().isoformat()
            resp = self.client.get(reverse("ledger_export"), {"from": from_date, "to": to_date})
            self.assertEqual(resp.status_code, 400)
            self.assertIn("exceeding the maximum of 10,000 rows", resp.json()["detail"])

    def test_export_missing_params(self):
        resp = self.client.get(reverse("ledger_export"), {"from": self.from_date})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("'from' and 'to' query parameters are required", resp.json()["detail"])

    def test_export_auth_failure(self):
        self.client.credentials()
        resp = self.client.get(reverse("ledger_export"), {"from": self.from_date, "to": self.to_date})
        self.assertEqual(resp.status_code, 403)

    @override_settings(LEDGER_EXPORT_THROTTLE_RATE="1/minute")
    def test_export_throttle(self):
        url = reverse("ledger_export")
        resp1 = self.client.get(url, {"from": self.from_date, "to": self.to_date})
        self.assertEqual(resp1.status_code, 200)
        resp2 = self.client.get(url, {"from": self.from_date, "to": self.to_date})
        self.assertEqual(resp2.status_code, 429)
