import json
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payments.ledger.models import LedgerEntry
from payments.models import Merchant, Payment, Refund
from payments.providers.arifpay import ArifPayProvider
from payments.providers.chapa import ChapaProvider
from payments.providers.fake import FakeProvider
from payments.test_utils import create_test_merchant


class RefundTests(TestCase):
    def setUp(self):
        super().setUp()
        FakeProvider.reset()
        self.merchant, self.raw_key = create_test_merchant(name="Refund Test Merchant")
        self.other_merchant, self.other_raw_key = create_test_merchant(name="Other Merchant")
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY=self.raw_key)

    def tearDown(self):
        FakeProvider.reset()
        super().tearDown()

    def _create_payment(
        self,
        merchant=None,
        amount="100.00",
        status="succeeded",
        provider="fake",
        livemode=False,
    ):
        return Payment.objects.create(
            merchant=merchant or self.merchant,
            amount=Decimal(amount),
            status=status,
            provider=provider,
            livemode=livemode,
            sender="customer@example.com",
            receiver="merchant@example.com",
            idempotency_key=f"pay-{uuid.uuid4().hex[:12]}",
        )

    # -------------------------------------------------------------------------
    # 1. Succeeded payment + fake provider: full refund -> 201, status succeeded
    # -------------------------------------------------------------------------
    def test_full_refund_succeeded_payment_fake_provider(self):
        payment = self._create_payment(amount="100.00", status="succeeded", provider="fake")
        url = reverse("create_refund", args=[payment.id])
        idem_key = f"ref-{uuid.uuid4().hex}"

        response = self.client.post(
            url,
            {"amount": "100.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=idem_key,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertEqual(data["payment_id"], payment.id)
        self.assertEqual(data["amount"], "100.00")
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["error_code"], "")
        self.assertIn("id", data)
        self.assertIn("created_at", data)

        # Payment status must remain succeeded (refunds do not change payment status)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")

        # Database row verification
        refund = Refund.objects.get(id=data["id"])
        self.assertEqual(refund.status, Refund.STATUS_SUCCEEDED)
        self.assertTrue(refund.provider_refund_reference.startswith("fake_refund_"))

    # -------------------------------------------------------------------------
    # 2. Partial refunds: summing to total accepted; exceeding total -> 409
    # -------------------------------------------------------------------------
    def test_partial_refunds_and_over_refund_rejection(self):
        payment = self._create_payment(amount="100.00", status="succeeded", provider="fake")
        url = reverse("create_refund", args=[payment.id])

        # First partial: 40.00
        r1 = self.client.post(
            url,
            {"amount": "40.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-part-1",
        )
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r1.json()["status"], "succeeded")

        # Second partial: 60.00
        r2 = self.client.post(
            url,
            {"amount": "60.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-part-2",
        )
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r2.json()["status"], "succeeded")

        initial_count = Refund.objects.filter(payment=payment).count()
        self.assertEqual(initial_count, 2)

        # Third attempt exceeding total (0.01 more) -> 409, no row created
        r3 = self.client.post(
            url,
            {"amount": "0.01"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-part-3",
        )
        self.assertEqual(r3.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(Refund.objects.filter(payment=payment).count(), initial_count)

    # -------------------------------------------------------------------------
    # 3. Pending/failed payment -> 409, no row
    # -------------------------------------------------------------------------
    def test_pending_and_failed_payment_rejected(self):
        url_tmpl = "create_refund"

        for st in ["pending", "processing", "failed", "cancelled", "expired"]:
            payment = self._create_payment(amount="50.00", status=st, provider="fake")
            url = reverse(url_tmpl, args=[payment.id])
            response = self.client.post(
                url,
                {"amount": "50.00"},
                format="json",
                HTTP_IDEMPOTENCY_KEY=f"idem-status-{st}",
            )
            self.assertEqual(
                response.status_code,
                status.HTTP_409_CONFLICT,
                f"Expected 409 for status {st}, got {response.status_code}",
            )
            self.assertFalse(Refund.objects.filter(payment=payment).exists())

    # -------------------------------------------------------------------------
    # 4. Chapa/ArifPay payment -> 409, error_code "refund_unsupported", row saved
    # -------------------------------------------------------------------------
    def test_chapa_refund_unsupported(self):
        payment = self._create_payment(amount="50.00", status="succeeded", provider="chapa")
        url = reverse("create_refund", args=[payment.id])

        with patch.object(ChapaProvider, "refund") as mock_refund:
            response = self.client.post(
                url,
                {"amount": "50.00"},
                format="json",
                HTTP_IDEMPOTENCY_KEY="idem-chapa-unsupported",
            )
            mock_refund.assert_not_called()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        data = response.json()
        self.assertEqual(data["status"], "unsupported")
        self.assertEqual(data["error_code"], "refund_unsupported")

        refund = Refund.objects.get(id=data["id"])
        self.assertEqual(refund.status, Refund.STATUS_UNSUPPORTED)
        self.assertEqual(refund.error_code, "refund_unsupported")

    def test_arifpay_refund_unsupported(self):
        payment = self._create_payment(amount="50.00", status="succeeded", provider="arifpay")
        url = reverse("create_refund", args=[payment.id])

        with patch.object(ArifPayProvider, "refund") as mock_refund:
            response = self.client.post(
                url,
                {"amount": "50.00"},
                format="json",
                HTTP_IDEMPOTENCY_KEY="idem-arifpay-unsupported",
            )
            mock_refund.assert_not_called()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        data = response.json()
        self.assertEqual(data["status"], "unsupported")
        self.assertEqual(data["error_code"], "refund_unsupported")

        refund = Refund.objects.get(id=data["id"])
        self.assertEqual(refund.status, Refund.STATUS_UNSUPPORTED)
        self.assertEqual(refund.error_code, "refund_unsupported")

    # -------------------------------------------------------------------------
    # 5. Idempotency-Key handling: replay -> 200, missing/invalid -> 400
    # -------------------------------------------------------------------------
    def test_idempotent_replay_returns_200_and_does_not_call_provider_again(self):
        payment = self._create_payment(amount="100.00", status="succeeded", provider="fake")
        url = reverse("create_refund", args=[payment.id])
        idem_key = "idem-replay-key-001"

        # First request -> 201
        r1 = self.client.post(
            url,
            {"amount": "30.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY=idem_key,
        )
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        r1_id = r1.json()["id"]

        with patch.object(FakeProvider, "refund") as mock_refund:
            # Second request with same key -> 200, Idempotent-Replayed: true
            r2 = self.client.post(
                url,
                {"amount": "30.00"},
                format="json",
                HTTP_IDEMPOTENCY_KEY=idem_key,
            )
            mock_refund.assert_not_called()

        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(r2.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(r2.json()["id"], r1_id)
        self.assertEqual(Refund.objects.filter(payment=payment).count(), 1)

    def test_missing_and_invalid_idempotency_keys(self):
        payment = self._create_payment(amount="100.00", status="succeeded", provider="fake")
        url = reverse("create_refund", args=[payment.id])

        # Missing key
        r_missing = self.client.post(url, {"amount": "10.00"}, format="json")
        self.assertEqual(r_missing.status_code, status.HTTP_400_BAD_REQUEST)

        # Empty whitespace key
        r_empty = self.client.post(
            url,
            {"amount": "10.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="   ",
        )
        self.assertEqual(r_empty.status_code, status.HTTP_400_BAD_REQUEST)

        # Over 100 characters key
        r_long = self.client.post(
            url,
            {"amount": "10.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="a" * 101,
        )
        self.assertEqual(r_long.status_code, status.HTTP_400_BAD_REQUEST)

    # -------------------------------------------------------------------------
    # 6. Provider errors: not_created=True -> failed; uncertain -> pending
    # -------------------------------------------------------------------------
    def test_provider_error_not_created_sets_failed(self):
        payment = self._create_payment(amount="100.00", status="succeeded", provider="fake")
        url = reverse("create_refund", args=[payment.id])

        FakeProvider.set_error(not_created=True, infrastructure=False)
        response = self.client.post(
            url,
            {"amount": "25.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-err-not-created",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error_code"], "provider_rejected")

        refund = Refund.objects.get(id=data["id"])
        self.assertEqual(refund.status, Refund.STATUS_FAILED)
        self.assertEqual(refund.error_code, "provider_rejected")

    def test_provider_uncertain_error_sets_pending_provider_unknown(self):
        payment = self._create_payment(amount="100.00", status="succeeded", provider="fake")
        url = reverse("create_refund", args=[payment.id])

        FakeProvider.set_error(not_created=False, infrastructure=True)
        response = self.client.post(
            url,
            {"amount": "25.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-err-uncertain",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["error_code"], "provider_unknown")

        refund = Refund.objects.get(id=data["id"])
        self.assertEqual(refund.status, Refund.STATUS_PENDING)
        self.assertEqual(refund.error_code, "provider_unknown")

    # -------------------------------------------------------------------------
    # 7. Scoped lookup: other merchant -> 404; other livemode -> 404; list own
    # -------------------------------------------------------------------------
    def test_merchant_isolation_and_livemode_scoping(self):
        # Payment belongs to other_merchant
        payment_other = self._create_payment(
            merchant=self.other_merchant,
            amount="100.00",
            status="succeeded",
            provider="fake",
        )
        url = reverse("create_refund", args=[payment_other.id])

        # Calling with self.merchant credentials -> 404
        r = self.client.post(
            url,
            {"amount": "50.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-other-merchant",
        )
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)

        # Listing refunds for other merchant's payment -> 404
        r_list = self.client.get(reverse("list_refunds", args=[payment_other.id]))
        self.assertEqual(r_list.status_code, status.HTTP_404_NOT_FOUND)

        # Mode scoping: livemode payment accessed with test key -> 404
        payment_live = self._create_payment(
            merchant=self.merchant,
            amount="100.00",
            status="succeeded",
            provider="fake",
            livemode=True,
        )
        r_live = self.client.post(
            reverse("create_refund", args=[payment_live.id]),
            {"amount": "50.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-livemode-mismatch",
        )
        self.assertEqual(r_live.status_code, status.HTTP_404_NOT_FOUND)

    def test_list_refunds_returns_only_payment_refunds_newest_first(self):
        payment1 = self._create_payment(amount="100.00", status="succeeded", provider="fake")
        payment2 = self._create_payment(amount="100.00", status="succeeded", provider="fake")

        # Create two refunds for payment1
        r1 = self.client.post(
            reverse("create_refund", args=[payment1.id]),
            {"amount": "10.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-list-1",
        )
        r2 = self.client.post(
            reverse("create_refund", args=[payment1.id]),
            {"amount": "20.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-list-2",
        )
        # Create one refund for payment2
        r3 = self.client.post(
            reverse("create_refund", args=[payment2.id]),
            {"amount": "15.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-list-3",
        )

        res = self.client.get(reverse("list_refunds", args=[payment1.id]))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        items = res.json()
        self.assertEqual(len(items), 2)
        # Newest first
        self.assertEqual(items[0]["id"], r2.json()["id"])
        self.assertEqual(items[1]["id"], r1.json()["id"])
        self.assertEqual(items[0]["payment_id"], payment1.id)
        self.assertEqual(items[1]["payment_id"], payment1.id)

    # -------------------------------------------------------------------------
    # 8. Invalid amounts: 0, negative, >2 decimals, text, missing -> 400
    # -------------------------------------------------------------------------
    def test_invalid_amounts_rejected_with_400(self):
        payment = self._create_payment(amount="100.00", status="succeeded", provider="fake")
        url = reverse("create_refund", args=[payment.id])

        invalid_cases = [
            ("0.00", "zero"),
            ("-10.00", "negative"),
            ("10.555", "3 decimal places"),
            ("abc", "alphabetic"),
            ("", "empty string"),
        ]

        for val, label in invalid_cases:
            res = self.client.post(
                url,
                {"amount": val},
                format="json",
                HTTP_IDEMPOTENCY_KEY=f"idem-invalid-{label}",
            )
            self.assertEqual(
                res.status_code,
                status.HTTP_400_BAD_REQUEST,
                f"Expected 400 for {label} amount, got {res.status_code}",
            )

        # Missing amount key altogether
        res_missing = self.client.post(
            url,
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-missing-amount",
        )
        self.assertEqual(res_missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Refund.objects.filter(payment=payment).exists())

    # -------------------------------------------------------------------------
    # 9. Reversal ledger rows created on succeeded refund
    # -------------------------------------------------------------------------
    def test_no_ledger_entries_created_by_refunds(self):
        payment = self._create_payment(amount="100.00", status="succeeded", provider="fake")
        url = reverse("create_refund", args=[payment.id])

        entries_before = LedgerEntry.objects.count()

        res = self.client.post(
            url,
            {"amount": "50.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-no-ledger",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        entries_after = LedgerEntry.objects.count()
        self.assertEqual(entries_after, entries_before + 2)

    # -------------------------------------------------------------------------
    # 10. Secret scan: response never contains provider references, secrets
    # -------------------------------------------------------------------------
    def test_secret_scan_responses(self):
        payment = self._create_payment(amount="100.00", status="succeeded", provider="fake")
        url = reverse("create_refund", args=[payment.id])

        res = self.client.post(
            url,
            {"amount": "40.00"},
            format="json",
            HTTP_IDEMPOTENCY_KEY="idem-secret-scan",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        data = res.json()

        # Disallowed response fields
        self.assertNotIn("provider_refund_reference", data)
        self.assertNotIn("provider", data)
        self.assertNotIn("merchant", data)

        # String search across raw response content
        content = res.content.decode("utf-8")
        self.assertNotIn("fake_refund_", content)
        self.assertNotIn("secret", content.lower())

        # Same for list response
        list_res = self.client.get(reverse("list_refunds", args=[payment.id]))
        list_data = list_res.json()
        self.assertEqual(len(list_data), 1)
        self.assertNotIn("provider_refund_reference", list_data[0])
        list_content = list_res.content.decode("utf-8")
        self.assertNotIn("fake_refund_", list_content)

