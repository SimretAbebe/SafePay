from decimal import Decimal
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.test import RequestFactory, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from payments.admin import PaymentAdmin
from payments.models import Merchant, Payment, PaymentStatusHistory, WebhookDelivery
from payments.providers import FakeProvider, ProviderError, UnsupportedProviderStatus
from payments.services import backoff_delay, reconcile_one, schedule_first_check
from payments.tasks import reconcile_payment, reconcile_pending_payments
from payments.test_utils import create_test_merchant


@override_settings(
    ENABLE_FAKE_PROVIDER=True,
    DEFAULT_PROVIDER="fake",
    RECONCILE_SWEEP_SECONDS=60,
    RECONCILE_FIRST_CHECK_DELAY_SECONDS=300,
    RECONCILE_BACKOFF_SECONDS=[60, 120, 300, 900, 3600, 3600, 3600, 3600],
    RECONCILE_MAX_ATTEMPTS=8,
    RECONCILE_BATCH_SIZE=50,
    RECONCILE_LEASE_SECONDS=120,
    RECONCILE_JITTER=0.0,
)
class PaymentReconciliationTests(TransactionTestCase):

    def setUp(self):
        cache.clear()
        FakeProvider.reset()
        self.merchant, self.api_key = create_test_merchant(
            "Reconciliation Merchant",
            webhook_url="https://merchant.example.com/webhook",
        )
        self.client = APIClient()
        self.now = timezone.now()

    def tearDown(self):
        cache.clear()
        FakeProvider.reset()

    # 1. A due pending provider payment whose provider says paid ends succeeded
    #    with exactly two history rows and exactly one WebhookDelivery; next_sync_at is None.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_due_pending_provider_paid_transitions_to_succeeded(self, mock_webhook_delay):
        ref = "fake_reconcile_paid_1"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="rec-paid-1",
            amount=Decimal("150.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="pending",
            next_sync_at=self.now,
        )
        FakeProvider.simulate(ref, "paid")

        result = reconcile_one(payment.id, now=self.now)
        payment.refresh_from_db()

        self.assertEqual(payment.status, "succeeded")
        self.assertIsNone(payment.next_sync_at)
        self.assertEqual(payment.last_synced_at, self.now)
        self.assertEqual(payment.history.count(), 2)
        history_steps = list(payment.history.values_list("from_status", "to_status"))
        self.assertEqual(
            history_steps,
            [("pending", "processing"), ("processing", "succeeded")],
        )
        deliveries = WebhookDelivery.objects.filter(payment=payment)
        self.assertEqual(deliveries.count(), 1)
        self.assertEqual(deliveries.first().event_type, "payment.succeeded")
        self.assertEqual(result.outcome, "succeeded")

    # 2. Provider error (test double raising ProviderError): the payment STAYS pending,
    #    no history rows, no webhook, sync_attempts == 1, next_sync_at == now + first backoff delay.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_provider_error_stays_pending_with_backoff(self, mock_webhook_delay):
        ref = "fake_reconcile_err_1"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="rec-err-1",
            amount=Decimal("150.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="pending",
            sync_attempts=0,
            next_sync_at=self.now,
        )

        with patch.object(FakeProvider, "verify", side_effect=ProviderError("Gateway timeout")):
            result = reconcile_one(payment.id, now=self.now)

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertEqual(payment.history.count(), 0)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 0)
        self.assertEqual(payment.sync_attempts, 1)
        self.assertEqual(payment.next_sync_at, self.now + timedelta(seconds=60))
        self.assertFalse(payment.needs_review)
        self.assertEqual(result.outcome, "unknown")

    # 3. Backoff grows along the list and is capped at its last value (attempts 1 to 10).
    def test_backoff_delay_sequence_and_cap(self):
        expected_delays = [60, 120, 300, 900, 3600, 3600, 3600, 3600, 3600, 3600]
        for attempt in range(1, 11):
            delay = backoff_delay(attempt, jitter=0.0)
            self.assertEqual(
                delay,
                expected_delays[attempt - 1],
                f"Attempt {attempt} expected delay {expected_delays[attempt - 1]}, got {delay}",
            )

    # 4. After RECONCILE_MAX_ATTEMPTS consecutive provider errors: needs_review True,
    #    review_reason "unknown_outcome", next_sync_at None, status still "pending" (never "failed").
    def test_max_attempts_provider_error_flags_review(self):
        ref = "fake_reconcile_max_err"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="rec-max-err",
            amount=Decimal("200.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="pending",
            sync_attempts=7,
            next_sync_at=self.now,
        )

        with patch.object(FakeProvider, "verify", side_effect=ProviderError("Service unavailable")):
            result = reconcile_one(payment.id, now=self.now)

        payment.refresh_from_db()
        self.assertEqual(payment.sync_attempts, 8)
        self.assertTrue(payment.needs_review)
        self.assertEqual(payment.review_reason, "unknown_outcome")
        self.assertIsNone(payment.next_sync_at)
        self.assertEqual(payment.status, "pending")
        self.assertEqual(result.outcome, "unknown")

    # 5. Provider reachable but still pending: attempts increase and next_sync_at moves later;
    #    at the max it stops with needs_review False.
    def test_provider_reachable_still_pending_backoff_and_stop(self):
        ref = "fake_reconcile_pending_seq"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="rec-pending-seq",
            amount=Decimal("100.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="pending",
            sync_attempts=0,
            next_sync_at=self.now,
        )
        FakeProvider.simulate(ref, "pending")

        # First attempt while still pending
        result1 = reconcile_one(payment.id, now=self.now)
        payment.refresh_from_db()
        self.assertEqual(payment.sync_attempts, 1)
        self.assertEqual(payment.next_sync_at, self.now + timedelta(seconds=60))
        self.assertFalse(payment.needs_review)
        self.assertEqual(payment.status, "pending")
        self.assertEqual(result1.outcome, "pending")

        # Simulate reaching max attempts (attempt 8)
        payment.sync_attempts = 7
        payment.save(update_fields=["sync_attempts"])

        result2 = reconcile_one(payment.id, now=self.now)
        payment.refresh_from_db()
        self.assertEqual(payment.sync_attempts, 8)
        self.assertIsNone(payment.next_sync_at)
        self.assertFalse(payment.needs_review)
        self.assertEqual(payment.status, "pending")
        self.assertEqual(result2.outcome, "pending")

    # 6. Mismatch (for example a payment that is "cancelled" in SafePay while the provider says paid):
    #    needs_review True, reason "mismatch", state unchanged, no history, no webhook.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_status_mismatch_flags_review(self, mock_webhook_delay):
        ref = "fake_reconcile_mismatch"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="rec-mismatch",
            amount=Decimal("120.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="cancelled",
            next_sync_at=self.now,
        )
        FakeProvider.simulate(ref, "paid")

        result = reconcile_one(payment.id, now=self.now)
        payment.refresh_from_db()

        self.assertTrue(payment.needs_review)
        self.assertEqual(payment.review_reason, "mismatch")
        self.assertEqual(payment.status, "cancelled")
        self.assertEqual(payment.history.count(), 0)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 0)
        self.assertIsNone(payment.next_sync_at)
        self.assertEqual(result.outcome, "mismatch")

    # 7. UnsupportedProviderStatus: needs_review True immediately, reason "unsupported_status", no backoff loop.
    def test_unsupported_provider_status_flags_review_immediately(self):
        ref = "fake_reconcile_unsupported"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="rec-unsupported",
            amount=Decimal("300.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="pending",
            sync_attempts=0,
            next_sync_at=self.now,
        )
        FakeProvider.simulate(ref, "refunded")

        result = reconcile_one(payment.id, now=self.now)
        payment.refresh_from_db()

        self.assertTrue(payment.needs_review)
        self.assertEqual(payment.review_reason, "unsupported_status")
        self.assertIsNone(payment.next_sync_at)
        self.assertEqual(payment.sync_attempts, 0)
        self.assertEqual(payment.status, "pending")
        self.assertEqual(result.outcome, "unsupported_status")

    # 8. The sweep selects ONLY due payments: excludes future next_sync_at,
    #    brand-new payments with null next_sync_at younger than the first delay,
    #    needs_review True, no provider, and non-pending; includes due ones and old ones with null next_sync_at.
    @patch("payments.tasks.reconcile_payment.delay")
    def test_sweep_selects_only_due_payments(self, mock_reconcile_delay):
        now = timezone.now()

        # 1. Due payment with past next_sync_at -> INCLUDE
        p1 = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="due-1",
            amount=Decimal("10.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference="ref-due-1",
            status="pending",
            next_sync_at=now - timedelta(seconds=10),
        )

        # 2. Old payment with null next_sync_at (> 300s old) -> INCLUDE
        p2 = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="due-old-null",
            amount=Decimal("20.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference="ref-due-2",
            status="pending",
            next_sync_at=None,
        )
        Payment.objects.filter(id=p2.id).update(created_at=now - timedelta(seconds=350))

        # 3. Future next_sync_at -> EXCLUDE
        Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="ex-future",
            amount=Decimal("30.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference="ref-ex-1",
            status="pending",
            next_sync_at=now + timedelta(seconds=60),
        )

        # 4. Brand-new payment with null next_sync_at (< 300s old) -> EXCLUDE
        Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="ex-new-null",
            amount=Decimal("40.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference="ref-ex-2",
            status="pending",
            next_sync_at=None,
        )

        # 5. needs_review True -> EXCLUDE
        Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="ex-review",
            amount=Decimal("50.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference="ref-ex-3",
            status="pending",
            needs_review=True,
            next_sync_at=now - timedelta(seconds=10),
        )

        # 6. No provider -> EXCLUDE
        Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="ex-no-prov",
            amount=Decimal("60.00"),
            sender="a",
            receiver="b",
            provider=None,
            provider_reference=None,
            status="pending",
            next_sync_at=now - timedelta(seconds=10),
        )

        # 7. Non-pending (succeeded) -> EXCLUDE
        Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="ex-succeeded",
            amount=Decimal("70.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference="ref-ex-4",
            status="succeeded",
            next_sync_at=now - timedelta(seconds=10),
        )

        result = reconcile_pending_payments()
        self.assertEqual(result["due_count"], 2)
        self.assertEqual(result["dispatched_count"], 2)

        dispatched_ids = [call.args[0] for call in mock_reconcile_delay.call_args_list]
        self.assertCountEqual(dispatched_ids, [p1.id, p2.id])

    # 9. The sweep leases what it selects: a second immediate sweep dispatches nothing.
    @patch("payments.tasks.reconcile_payment.delay")
    def test_sweep_leases_selected_payments(self, mock_reconcile_delay):
        now = timezone.now()
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="sweep-lease-1",
            amount=Decimal("100.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference="ref-lease-1",
            status="pending",
            next_sync_at=now - timedelta(seconds=10),
        )

        res1 = reconcile_pending_payments()
        self.assertEqual(res1["dispatched_count"], 1)
        self.assertEqual(mock_reconcile_delay.call_count, 1)

        payment.refresh_from_db()
        self.assertGreater(payment.next_sync_at, now)

        # Second immediate sweep
        res2 = reconcile_pending_payments()
        self.assertEqual(res2["dispatched_count"], 0)
        self.assertEqual(mock_reconcile_delay.call_count, 1)

    # 10. The sweep respects RECONCILE_BATCH_SIZE.
    @patch("payments.tasks.reconcile_payment.delay")
    def test_sweep_respects_batch_size(self, mock_reconcile_delay):
        now = timezone.now()
        for i in range(5):
            Payment.objects.create(
                merchant=self.merchant,
                idempotency_key=f"batch-test-{i}",
                amount=Decimal("10.00"),
                sender="a",
                receiver="b",
                provider="fake",
                provider_reference=f"ref-batch-{i}",
                status="pending",
                next_sync_at=now - timedelta(seconds=10 + i),
            )

        with self.settings(RECONCILE_BATCH_SIZE=2):
            result = reconcile_pending_payments()

        self.assertEqual(result["due_count"], 2)
        self.assertEqual(result["dispatched_count"], 2)
        self.assertEqual(mock_reconcile_delay.call_count, 2)

    # 11. create_checkout_session sets next_sync_at about now + the first delay.
    def test_create_checkout_session_schedules_first_check(self):
        before = timezone.now()
        url = reverse("create_checkout_session")
        payload = {
            "amount": "250.00",
            "provider": "fake",
            "reference": "order-chk-1",
            "customer_email": "customer@example.com",
        }
        headers = {
            "HTTP_X_API_KEY": self.api_key,
            "HTTP_IDEMPOTENCY_KEY": "chk-sched-1",
        }

        response = self.client.post(url, data=payload, format="json", **headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        payment = Payment.objects.get(idempotency_key="chk-sched-1")
        self.assertIsNotNone(payment.next_sync_at)
        expected_min = before + timedelta(seconds=295)
        expected_max = timezone.now() + timedelta(seconds=305)
        self.assertTrue(
            expected_min <= payment.next_sync_at <= expected_max,
            f"next_sync_at {payment.next_sync_at} not in expected window [{expected_min}, {expected_max}]",
        )

    # 12. reconcile_one is idempotent: running it twice after success creates
    #     no extra history row and no second WebhookDelivery.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_reconcile_one_is_idempotent_after_success(self, mock_webhook_delay):
        ref = "fake_reconcile_idem_1"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="rec-idem-1",
            amount=Decimal("100.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference=ref,
            status="pending",
            next_sync_at=self.now,
        )
        FakeProvider.simulate(ref, "paid")

        res1 = reconcile_one(payment.id, now=self.now)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")
        self.assertEqual(payment.history.count(), 2)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 1)

        # Run reconcile_one again
        res2 = reconcile_one(payment.id, now=self.now)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")
        self.assertEqual(payment.history.count(), 2)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 1)
        self.assertIsNone(payment.next_sync_at)
        self.assertEqual(res2.outcome, "skipped")

    # 13. An unexpected Exception inside the provider call is logged and treated
    #     as unknown with backoff (assert no crash and no secret in the log output).
    def test_unexpected_exception_logged_and_handled_with_backoff(self):
        ref = "fake_reconcile_unexpected"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="rec-unexp-1",
            amount=Decimal("100.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference=ref,
            status="pending",
            sync_attempts=0,
            next_sync_at=self.now,
        )

        with patch.object(
            FakeProvider,
            "verify",
            side_effect=RuntimeError("internal crash with secret token_abc_123"),
        ):
            with self.assertLogs("payments.services", level="ERROR") as cm:
                result = reconcile_one(payment.id, now=self.now)

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertEqual(payment.sync_attempts, 1)
        self.assertEqual(payment.next_sync_at, self.now + timedelta(seconds=60))
        self.assertEqual(result.outcome, "unknown")

        log_output = "\n".join(cm.output)
        self.assertIn("Unexpected error during reconciliation", log_output)
        self.assertNotIn("Authorization", log_output)

    # 14. Both new admin actions work (reset values, queued task).
    @patch("payments.tasks.reconcile_payment.delay")
    def test_admin_actions(self, mock_reconcile_delay):
        payment1 = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="admin-act-1",
            amount=Decimal("100.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference="ref-adm-1",
            status="pending",
            needs_review=True,
            review_reason="unknown_outcome",
            sync_attempts=8,
            next_sync_at=None,
        )
        payment2 = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="admin-act-2",
            amount=Decimal("200.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference="ref-adm-2",
            status="pending",
            needs_review=False,
            next_sync_at=self.now,
        )

        factory = RequestFactory()
        request = factory.get("/admin/payments/payment/")
        from django.contrib.messages.storage.fallback import FallbackStorage
        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))
        model_admin = PaymentAdmin(Payment, None)

        # Action 1: "Check with provider now"
        qs = Payment.objects.filter(id__in=[payment1.id, payment2.id])
        model_admin.check_with_provider_now(request, qs)
        self.assertEqual(mock_reconcile_delay.call_count, 2)
        called_ids = [call.args[0] for call in mock_reconcile_delay.call_args_list]
        self.assertCountEqual(called_ids, [payment1.id, payment2.id])

        # Action 2: "Clear review flag and re-check"
        model_admin.clear_review_and_recheck(request, Payment.objects.filter(id=payment1.id))
        payment1.refresh_from_db()
        self.assertFalse(payment1.needs_review)
        self.assertEqual(payment1.review_reason, "")
        self.assertEqual(payment1.sync_attempts, 0)
        self.assertIsNotNone(payment1.next_sync_at)

    # 15. The Day 13 verify endpoint and the inbound Chapa webhook behave exactly as before.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_verify_endpoint_and_inbound_chapa_webhook_unchanged(self, mock_webhook_delay):
        # A. Day 13 verify endpoint works as before
        ref = "fake_day13_verify_ref"
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="day13-verify",
            amount=Decimal("150.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=ref,
            status="pending",
        )
        FakeProvider.simulate(ref, "paid")

        verify_url = reverse("verify_payment", kwargs={"pk": payment.pk})
        response = self.client.get(verify_url, HTTP_X_API_KEY=self.api_key)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "succeeded")
        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")

        # B. Inbound Chapa webhook behaves as before
        webhook_secret = "test-webhook-secret-day13"
        chapa_key = "test-chapa-key-day13"
        with override_settings(
            CHAPA_WEBHOOK_SECRET=webhook_secret,
            CHAPA_SECRET_KEY=chapa_key,
        ):
            payment_chapa = Payment.objects.create(
                merchant=self.merchant,
                idempotency_key="day13-chapa-wh",
                amount=Decimal("50.00"),
                sender="alice",
                receiver=self.merchant.name,
                provider="chapa",
                provider_reference="tx-chapa-123",
                status="pending",
            )
            webhook_payload = {
                "event": "charge.success",
                "tx_ref": "tx-chapa-123",
                "status": "success",
            }
            body_bytes = json.dumps(webhook_payload).encode("utf-8")
            signature = hmac.new(webhook_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

            with patch("payments.providers.chapa.requests.get") as mock_chapa_get:
                mock_chapa_get.return_value = MagicMock(
                    status_code=200,
                    json=lambda: {
                        "status": "success",
                        "data": {
                            "tx_ref": "tx-chapa-123",
                            "status": "success",
                        },
                    },
                )
                webhook_url = reverse("chapa_webhook")
                wh_response = self.client.post(
                    webhook_url,
                    data=body_bytes,
                    content_type="application/json",
                    HTTP_X_CHAPA_SIGNATURE=signature,
                )

            self.assertEqual(wh_response.status_code, status.HTTP_200_OK)
            self.assertEqual(wh_response.json()["status"], "ok")
            payment_chapa.refresh_from_db()
            self.assertEqual(payment_chapa.status, "succeeded")
