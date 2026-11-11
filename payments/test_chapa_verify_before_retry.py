"""Unit and integration tests for Chapa verify-before-retry behavior."""
import logging
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payments import breaker
from payments.models import Payment, ProviderAttempt
from payments.providers import ProviderError
from payments.providers.fake import FakeProvider
from payments.routing import build_provider_chain, start_checkout
from payments.test_utils import create_test_merchant

URL = reverse("create_checkout_session")


@override_settings(
    DEFAULT_PROVIDER="chapa",
    CHAPA_SECRET_KEY="test-chapa-secret",
    CHAPA_WEBHOOK_SECRET="test-wh-secret",
    CHAPA_BASE_URL="https://api.chapa.co/v1",
    CHAPA_REQUEST_TIMEOUT=10,
    ENABLE_FAKE_PROVIDER=True,
)
class ChapaVerifyBeforeRetryTests(TestCase):
    def setUp(self):
        cache.clear()
        FakeProvider.reset()
        self.client = APIClient()
        self.merchant, self.api_key = create_test_merchant("Chapa Retry Merchant")
        self.merchant.default_provider = "chapa"
        self.merchant.fallback_providers = ["fake"]
        self.merchant.save()

    def tearDown(self):
        cache.clear()
        FakeProvider.reset()

    def _post(self, idemp_key=None, allow_fallback=True, **kwargs):
        payload = {
            "amount": "150.00",
            "provider": "chapa",
            "allow_fallback": allow_fallback,
            "customer_email": "user@example.com",
            "customer_first_name": "Abebe",
            "customer_last_name": "Bikila",
        }
        payload.update(kwargs)
        headers = {
            "HTTP_X_API_KEY": self.api_key,
            "HTTP_IDEMPOTENCY_KEY": idemp_key or f"ikey-{uuid.uuid4().hex}",
        }
        return self.client.post(URL, payload, format="json", **headers)

    def _make_timeout_initial_call(self, allow_fallback=False):
        """Make initial call that fails with Timeout, return (idemp_key, payment)."""
        idemp_key = f"ikey-{uuid.uuid4().hex}"
        with patch("payments.providers.chapa.requests.post") as mock_post:
            mock_post.side_effect = requests.Timeout("Network drop")
            self._post(idemp_key=idemp_key, allow_fallback=allow_fallback)
        payment = Payment.objects.get(merchant=self.merchant, idempotency_key=idemp_key)
        return idemp_key, payment

    # -------------------------------------------------------------------------
    # 1. Fresh checkout saves tx_ref before initialize is called.
    # -------------------------------------------------------------------------
    @patch("payments.providers.chapa.requests.post")
    def test_1_fresh_checkout_saves_tx_ref_before_initialize(self, mock_post):
        captured_payment_ref_at_post = []

        def side_effect(*args, **kwargs):
            p = Payment.objects.filter(merchant=self.merchant).first()
            self.assertIsNotNone(p)
            self.assertTrue(p.provider_reference.startswith("sp-"))
            self.assertEqual(len(p.provider_reference), 35)
            self.assertEqual(p.provider, "chapa")
            self.assertFalse(bool(p.checkout_url))
            captured_payment_ref_at_post.append(p.provider_reference)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "status": "success",
                "message": "Hosted Link",
                "data": {"checkout_url": "https://checkout.chapa.co/pay/12345"},
            }
            return resp

        mock_post.side_effect = side_effect
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(captured_payment_ref_at_post), 1)

        payment = Payment.objects.get(id=resp.data["id"])
        self.assertEqual(payment.provider_reference, captured_payment_ref_at_post[0])
        self.assertEqual(payment.checkout_url, "https://checkout.chapa.co/pay/12345")
        self.assertEqual(payment.status, "pending")

        attempt = payment.provider_attempts.first()
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.outcome, ProviderAttempt.OUTCOME_CREATED)
        self.assertEqual(attempt.provider, "chapa")
        self.assertEqual(attempt.provider_reference, payment.provider_reference)

    # -------------------------------------------------------------------------
    # 2. Fresh checkout that fails does not call verify on first attempt.
    # -------------------------------------------------------------------------
    @patch("payments.providers.chapa.requests.get")
    @patch("payments.providers.chapa.requests.post")
    def test_2_fresh_checkout_pre_send_error_keeps_stored_tx_ref_no_verify(self, mock_post, mock_get):
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 400
        mock_post_resp.json.return_value = {
            "status": "failed",
            "message": "Invalid amount parameter",
        }
        mock_post.return_value = mock_post_resp

        idemp_key = f"ikey-{uuid.uuid4().hex}"
        resp = self._post(idemp_key=idemp_key, allow_fallback=False)
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)

        mock_get.assert_not_called()

        payment = Payment.objects.get(merchant=self.merchant, idempotency_key=idemp_key)
        self.assertTrue(payment.provider_reference.startswith("sp-"))
        self.assertFalse(bool(payment.checkout_url))
        self.assertEqual(payment.status, "pending")

        attempt = payment.provider_attempts.first()
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.outcome, ProviderAttempt.OUTCOME_FAILED_NOT_CREATED)
        self.assertEqual(attempt.provider_reference, payment.provider_reference)

    # -------------------------------------------------------------------------
    # 3. Verify 404 "not found" → new ref generated exactly once, initialize called.
    # -------------------------------------------------------------------------
    @patch("payments.providers.chapa.requests.get")
    @patch("payments.providers.chapa.requests.post")
    def test_3_retry_verify_404_not_found_generates_new_ref_exactly_once(self, mock_post, mock_get):
        idemp_key, payment = self._make_timeout_initial_call()
        old_ref = payment.provider_reference
        self.assertTrue(old_ref.startswith("sp-"))

        # Verify returns 404 "not found"
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 404
        mock_get_resp.json.return_value = {
            "status": "failed",
            "message": "Invalid transaction or Transaction not found",
        }
        mock_get.return_value = mock_get_resp

        # Initialize succeeds
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {
            "status": "success",
            "message": "Hosted Link",
            "data": {"checkout_url": "https://checkout.chapa.co/pay/67890"},
        }
        mock_post.return_value = mock_post_resp

        resp2 = self._post(idemp_key=idemp_key, allow_fallback=False)
        self.assertEqual(resp2.status_code, status.HTTP_201_CREATED)

        payment.refresh_from_db()
        new_ref = payment.provider_reference
        self.assertNotEqual(old_ref, new_ref)
        self.assertTrue(new_ref.startswith("sp-"))
        self.assertEqual(payment.checkout_url, "https://checkout.chapa.co/pay/67890")

        # Verify called once with old_ref
        mock_get.assert_called_once()
        self.assertIn(old_ref, mock_get.call_args[0][0])

        # Initialize called once with new_ref
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args[1]["json"]["tx_ref"], new_ref)

        attempts = list(payment.provider_attempts.order_by("created_at", "id"))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].provider_reference, old_ref)
        self.assertEqual(attempts[0].outcome, ProviderAttempt.OUTCOME_FAILED_UNKNOWN)
        self.assertEqual(attempts[1].provider_reference, new_ref)
        self.assertEqual(attempts[1].outcome, ProviderAttempt.OUTCOME_CREATED)

    # -------------------------------------------------------------------------
    # 4. Verify 404 "not paid yet" → no new ref, flags review, no fallback.
    # -------------------------------------------------------------------------
    @patch("payments.providers.chapa.requests.get")
    @patch("payments.providers.chapa.requests.post")
    def test_4_retry_verify_404_not_paid_yet_flags_review_no_new_ref_no_fallback(self, mock_post, mock_get):
        idemp_key, payment = self._make_timeout_initial_call(allow_fallback=True)
        stored_ref = payment.provider_reference

        # Verify returns 404 "Payment not paid yet"
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 404
        mock_get_resp.json.return_value = {
            "status": "failed",
            "message": "Payment not paid yet",
        }
        mock_get.return_value = mock_get_resp
        mock_post.reset_mock()  # clear calls from initial attempt

        with self.assertLogs("payments.routing", level="WARNING") as cm:
            resp = self._post(idemp_key=idemp_key, allow_fallback=True)
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)

        # Initialize must NOT be called on the retry
        mock_post.assert_not_called()

        payment.refresh_from_db()
        # Reference unchanged
        self.assertEqual(payment.provider_reference, stored_ref)
        self.assertFalse(bool(payment.checkout_url))
        self.assertEqual(payment.status, "pending")

        # Review flag set
        self.assertTrue(payment.needs_review)
        self.assertEqual(payment.review_reason, "unknown_outcome")

        # Attempt recorded with failed_unknown
        last_attempt = payment.provider_attempts.order_by("-created_at", "-id").first()
        self.assertEqual(last_attempt.outcome, ProviderAttempt.OUTCOME_FAILED_UNKNOWN)
        self.assertEqual(last_attempt.provider_reference, stored_ref)

        # No fallback to fake
        self.assertFalse(payment.provider_attempts.filter(provider="fake").exists())

        # Warning log contains no tx_ref
        all_logs = "\n".join(cm.output)
        self.assertNotIn(stored_ref, all_logs)

    # -------------------------------------------------------------------------
    # 5. Verify 404 with any other message → unknown, flags review, no new ref, no fallback.
    # -------------------------------------------------------------------------
    @patch("payments.providers.chapa.requests.get")
    @patch("payments.providers.chapa.requests.post")
    def test_5_retry_verify_404_unknown_message_flags_review_no_new_ref(self, mock_post, mock_get):
        idemp_key, payment = self._make_timeout_initial_call(allow_fallback=True)
        stored_ref = payment.provider_reference

        # Verify returns 404 with an unexpected message
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 404
        mock_get_resp.json.return_value = {
            "status": "failed",
            "message": "Some completely unexpected Chapa error message",
        }
        mock_get.return_value = mock_get_resp

        with self.assertLogs("payments.routing", level="WARNING") as cm:
            resp = self._post(idemp_key=idemp_key, allow_fallback=True)
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)

        # Initialize must NOT be called
        mock_post.assert_not_called()

        payment.refresh_from_db()
        self.assertEqual(payment.provider_reference, stored_ref)
        self.assertFalse(bool(payment.checkout_url))

        # Review flag set
        self.assertTrue(payment.needs_review)
        self.assertEqual(payment.review_reason, "unknown_outcome")

        # Attempt is failed_unknown
        last_attempt = payment.provider_attempts.order_by("-created_at", "-id").first()
        self.assertEqual(last_attempt.outcome, ProviderAttempt.OUTCOME_FAILED_UNKNOWN)
        self.assertEqual(last_attempt.provider_reference, stored_ref)

        # No fallback to fake
        self.assertFalse(payment.provider_attempts.filter(provider="fake").exists())

        # Logs contain no tx_ref
        all_logs = "\n".join(cm.output)
        self.assertNotIn(stored_ref, all_logs)

    # -------------------------------------------------------------------------
    # 6. Verify 200: found_existing, no ref change, no initialize, 502 returned.
    # -------------------------------------------------------------------------
    @patch("payments.providers.chapa.requests.get")
    @patch("payments.providers.chapa.requests.post")
    def test_6_retry_verify_200_records_found_existing_returns_502_leaves_pending(self, mock_post, mock_get):
        idemp_key, payment = self._make_timeout_initial_call(allow_fallback=False)
        stored_ref = payment.provider_reference

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {
            "status": "success",
            "message": "Payment details",
            "data": {
                "tx_ref": stored_ref,
                "status": "pending",
                "amount": "150.00",
                "currency": "ETB",
            },
        }
        mock_get.return_value = mock_get_resp

        resp2 = self._post(idemp_key=idemp_key, allow_fallback=False)
        self.assertEqual(resp2.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(
            resp2.data,
            {"detail": "Payment provider unavailable. Retry with the same Idempotency-Key."},
        )

        # Initialize was NOT called on retry
        mock_post.assert_not_called()

        payment.refresh_from_db()
        self.assertEqual(payment.provider_reference, stored_ref)
        self.assertFalse(bool(payment.checkout_url))
        self.assertEqual(payment.status, "pending")
        self.assertEqual(payment.provider_status, "pending")

        last_attempt = payment.provider_attempts.order_by("-created_at", "-id").first()
        self.assertEqual(last_attempt.outcome, ProviderAttempt.OUTCOME_FOUND_EXISTING)
        self.assertEqual(last_attempt.provider_reference, stored_ref)

    # -------------------------------------------------------------------------
    # 7. Verify timeout → failed_unknown, circuit failure recorded, no fallback.
    # -------------------------------------------------------------------------
    @patch("payments.breaker.record_failure")
    @patch("payments.providers.chapa.requests.get")
    @patch("payments.providers.chapa.requests.post")
    def test_7_retry_verify_timeout_records_failed_unknown_registers_circuit_failure(
        self, mock_post, mock_get, mock_breaker_failure
    ):
        idemp_key, payment = self._make_timeout_initial_call(allow_fallback=True)
        stored_ref = payment.provider_reference

        mock_get.side_effect = requests.Timeout("Verify timeout")
        mock_breaker_failure.reset_mock()

        resp2 = self._post(idemp_key=idemp_key, allow_fallback=True)
        self.assertEqual(resp2.status_code, status.HTTP_502_BAD_GATEWAY)

        # Initialize not called; no fake fallback
        mock_post.assert_not_called()
        self.assertFalse(payment.provider_attempts.filter(provider="fake").exists())

        payment.refresh_from_db()
        self.assertEqual(payment.provider_reference, stored_ref)
        self.assertEqual(payment.status, "pending")

        last_attempt = payment.provider_attempts.order_by("-created_at", "-id").first()
        self.assertEqual(last_attempt.outcome, ProviderAttempt.OUTCOME_FAILED_UNKNOWN)
        self.assertEqual(last_attempt.provider_reference, stored_ref)

        mock_breaker_failure.assert_called_with("chapa", livemode=False)

    # -------------------------------------------------------------------------
    # 8. Initialize 400 "used before" + verify 200 → found_existing.
    # -------------------------------------------------------------------------
    @patch("payments.providers.chapa.requests.get")
    @patch("payments.providers.chapa.requests.post")
    def test_8_initialize_400_used_before_verify_200_found_existing(self, mock_post, mock_get):
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 400
        mock_post_resp.json.return_value = {
            "status": "failed",
            "message": "Transaction reference has been used before",
            "data": None,
        }
        mock_post.return_value = mock_post_resp

        def get_side_effect(url, *args, **kwargs):
            ref = url.split("/")[-1]
            mock_g = MagicMock()
            mock_g.status_code = 200
            mock_g.json.return_value = {
                "status": "success",
                "message": "Payment details",
                "data": {
                    "tx_ref": ref,
                    "status": "success",
                    "amount": "150.00",
                    "currency": "ETB",
                },
            }
            return mock_g

        mock_get.side_effect = get_side_effect

        idemp_key = f"ikey-{uuid.uuid4().hex}"
        resp = self._post(idemp_key=idemp_key, allow_fallback=True)
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)

        payment = Payment.objects.get(merchant=self.merchant, idempotency_key=idemp_key)
        self.assertEqual(payment.status, "pending")
        self.assertFalse(bool(payment.checkout_url))
        self.assertEqual(payment.provider_status, "success")

        attempt = payment.provider_attempts.first()
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt.outcome, ProviderAttempt.OUTCOME_FOUND_EXISTING)
        self.assertEqual(attempt.provider_reference, payment.provider_reference)

    # -------------------------------------------------------------------------
    # 9. Initialize 400 "used before" + verify 404 "not found" → failed_unknown,
    #    flags review, no second new reference generated, no fallback, warning log
    #    contains no tx_ref.
    # -------------------------------------------------------------------------
    @patch("payments.providers.chapa.requests.get")
    @patch("payments.providers.chapa.requests.post")
    def test_9_initialize_400_used_before_verify_404_not_found_flags_review_no_new_ref(self, mock_post, mock_get):
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 400
        mock_post_resp.json.return_value = {
            "status": "failed",
            "message": "Transaction reference has been used before",
            "data": None,
        }
        mock_post.return_value = mock_post_resp

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 404
        mock_get_resp.json.return_value = {
            "status": "failed",
            "message": "Invalid transaction or Transaction not found",
        }
        mock_get.return_value = mock_get_resp

        idemp_key = f"ikey-{uuid.uuid4().hex}"
        with self.assertLogs("payments.routing", level="WARNING") as cm:
            resp = self._post(idemp_key=idemp_key, allow_fallback=True)
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)

        payment = Payment.objects.get(merchant=self.merchant, idempotency_key=idemp_key)
        stored_ref = payment.provider_reference
        self.assertEqual(payment.status, "pending")
        self.assertFalse(bool(payment.checkout_url))

        # Review flagged
        self.assertTrue(payment.needs_review)
        self.assertEqual(payment.review_reason, "unknown_outcome")

        attempt = payment.provider_attempts.first()
        self.assertEqual(attempt.outcome, ProviderAttempt.OUTCOME_FAILED_UNKNOWN)
        self.assertEqual(attempt.provider_reference, stored_ref)

        # No fallback attempt for fake
        self.assertFalse(payment.provider_attempts.filter(provider="fake").exists())

        # Warning log must not contain the tx_ref
        all_logs = "\n".join(cm.output)
        self.assertNotIn(stored_ref, all_logs)

    # -------------------------------------------------------------------------
    # 10. Initialize 400 "used before" + verify 404 "not paid yet" → failed_unknown,
    #     flags review, no new ref, no fallback.
    # -------------------------------------------------------------------------
    @patch("payments.providers.chapa.requests.get")
    @patch("payments.providers.chapa.requests.post")
    def test_10_initialize_400_used_before_verify_404_not_paid_yet_flags_review(self, mock_post, mock_get):
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 400
        mock_post_resp.json.return_value = {
            "status": "failed",
            "message": "Transaction reference has been used before",
            "data": None,
        }
        mock_post.return_value = mock_post_resp

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 404
        mock_get_resp.json.return_value = {
            "status": "failed",
            "message": "Payment not paid yet",
        }
        mock_get.return_value = mock_get_resp

        idemp_key = f"ikey-{uuid.uuid4().hex}"
        with self.assertLogs("payments.routing", level="WARNING") as cm:
            resp = self._post(idemp_key=idemp_key, allow_fallback=True)
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)

        payment = Payment.objects.get(merchant=self.merchant, idempotency_key=idemp_key)
        stored_ref = payment.provider_reference

        self.assertTrue(payment.needs_review)
        self.assertEqual(payment.review_reason, "unknown_outcome")
        self.assertFalse(bool(payment.checkout_url))

        attempt = payment.provider_attempts.first()
        self.assertEqual(attempt.outcome, ProviderAttempt.OUTCOME_FAILED_UNKNOWN)
        self.assertEqual(attempt.provider_reference, stored_ref)

        # No fallback
        self.assertFalse(payment.provider_attempts.filter(provider="fake").exists())

        # Logs contain no tx_ref
        all_logs = "\n".join(cm.output)
        self.assertNotIn(stored_ref, all_logs)

    # -------------------------------------------------------------------------
    # 11. Chain widening prevention: a payment with a Chapa attempt refuses fallback.
    # -------------------------------------------------------------------------
    def test_11_chain_widening_prevention_refuses_fallback(self):
        payment = Payment.objects.create(
            merchant=self.merchant,
            amount=Decimal("100.00"),
            provider="chapa",
            provider_reference="sp-testref12345678901234567890123",
            status="pending",
            sender="a@example.com",
            receiver=self.merchant.name,
            idempotency_key="chain-widen-test-key",
        )
        ProviderAttempt.objects.create(
            payment=payment,
            provider="chapa",
            provider_reference=payment.provider_reference,
            outcome=ProviderAttempt.OUTCOME_FAILED_UNKNOWN,
            error_type="Timeout",
        )

        chain = build_provider_chain(
            self.merchant,
            requested_provider="chapa",
            allow_fallback=True,
            payment=payment,
        )
        self.assertEqual(chain, ["chapa"])

        # Also when outcome was found_existing
        payment.provider_attempts.all().delete()
        ProviderAttempt.objects.create(
            payment=payment,
            provider="chapa",
            provider_reference=payment.provider_reference,
            outcome=ProviderAttempt.OUTCOME_FOUND_EXISTING,
        )
        self.assertEqual(
            build_provider_chain(self.merchant, "chapa", True, payment),
            ["chapa"],
        )

        # Also when outcome was failed_not_created
        payment.provider_attempts.all().delete()
        ProviderAttempt.objects.create(
            payment=payment,
            provider="chapa",
            provider_reference=payment.provider_reference,
            outcome=ProviderAttempt.OUTCOME_FAILED_NOT_CREATED,
        )
        self.assertEqual(
            build_provider_chain(self.merchant, "chapa", True, payment),
            ["chapa"],
        )
