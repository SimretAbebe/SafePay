from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payments.models import Payment, PaymentStatusHistory, WebhookDelivery
from payments.providers import FakeProvider, ProviderError
from payments.services import sync_payment_from_provider
from payments.test_utils import create_test_merchant


@override_settings(ENABLE_FAKE_PROVIDER=True, DEFAULT_PROVIDER="fake")
class ProviderSyncTests(TransactionTestCase):

    def setUp(self):
        cache.clear()
        FakeProvider.reset()
        self.client = APIClient()
        self.merchant_a, self.key_a = create_test_merchant(
            "Sync Merchant A",
            webhook_url="https://merchant.example.com/webhook",
        )
        self.merchant_b, self.key_b = create_test_merchant("Sync Merchant B")

    def tearDown(self):
        cache.clear()
        FakeProvider.reset()

    # 1. Provider paid: verify returns "succeeded"; history has exactly two rows
    #    (pending->processing, processing->succeeded); exactly one WebhookDelivery
    #    with succeeded event type is created; provider_status is saved.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_provider_paid_transitions_to_succeeded(self, mock_delay):
        ref = "fake_ref_paid_1"
        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="sync-paid-1",
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            provider_reference=ref,
            status="pending",
        )
        FakeProvider.simulate(ref, "paid")

        url = reverse("verify_payment", kwargs={"pk": payment.pk})
        response = self.client.get(url, HTTP_X_API_KEY=self.key_a)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], payment.pk)
        self.assertEqual(response.data["status"], "succeeded")
        self.assertIn("updated_at", response.data)

        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")
        self.assertEqual(payment.provider_status, "paid")

        history = list(payment.status_history.order_by("changed_at"))
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].from_status, "pending")
        self.assertEqual(history[0].to_status, "processing")
        self.assertEqual(history[1].from_status, "processing")
        self.assertEqual(history[1].to_status, "succeeded")

        deliveries = list(payment.webhook_deliveries.all())
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].event_type, "payment.succeeded")

    # 2. Calling verify a second time returns "succeeded" and creates no new
    #    history row and no second WebhookDelivery.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_verify_second_time_is_idempotent(self, mock_delay):
        ref = "fake_ref_paid_2"
        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="sync-paid-2",
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            provider_reference=ref,
            status="pending",
        )
        FakeProvider.simulate(ref, "paid")

        url = reverse("verify_payment", kwargs={"pk": payment.pk})
        res1 = self.client.get(url, HTTP_X_API_KEY=self.key_a)
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(res1.data["status"], "succeeded")

        res2 = self.client.get(url, HTTP_X_API_KEY=self.key_a)
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.data["status"], "succeeded")

        payment.refresh_from_db()
        self.assertEqual(payment.status_history.count(), 2)
        self.assertEqual(payment.webhook_deliveries.count(), 1)

    # 3. Provider still pending: status stays pending, no history rows, no webhook.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_provider_still_pending(self, mock_delay):
        ref = "fake_ref_pending_3"
        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="sync-pending-3",
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            provider_reference=ref,
            status="pending",
        )
        FakeProvider.simulate(ref, "pending")

        url = reverse("verify_payment", kwargs={"pk": payment.pk})
        response = self.client.get(url, HTTP_X_API_KEY=self.key_a)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "pending")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertEqual(payment.provider_status, "pending")
        self.assertEqual(payment.status_history.count(), 0)
        self.assertEqual(payment.webhook_deliveries.count(), 0)

    # 4. Provider failed: pending -> processing -> failed (two history rows),
    #    one WebhookDelivery for failed.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_provider_failed(self, mock_delay):
        ref = "fake_ref_failed_4"
        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="sync-failed-4",
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            provider_reference=ref,
            status="pending",
        )
        FakeProvider.simulate(ref, "failed")

        url = reverse("verify_payment", kwargs={"pk": payment.pk})
        response = self.client.get(url, HTTP_X_API_KEY=self.key_a)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "failed")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "failed")
        self.assertEqual(payment.provider_status, "failed")

        history = list(payment.status_history.order_by("changed_at"))
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].from_status, "pending")
        self.assertEqual(history[0].to_status, "processing")
        self.assertEqual(history[1].from_status, "processing")
        self.assertEqual(history[1].to_status, "failed")

        deliveries = list(payment.webhook_deliveries.all())
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].event_type, "payment.failed")

    # 5. Provider cancelled: pending -> cancelled, one history row, one WebhookDelivery.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_provider_cancelled(self, mock_delay):
        ref = "fake_ref_cancelled_5"
        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="sync-cancelled-5",
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            provider_reference=ref,
            status="pending",
        )
        FakeProvider.simulate(ref, "cancelled")

        url = reverse("verify_payment", kwargs={"pk": payment.pk})
        response = self.client.get(url, HTTP_X_API_KEY=self.key_a)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "cancelled")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "cancelled")
        self.assertEqual(payment.provider_status, "cancelled")

        history = list(payment.status_history.order_by("changed_at"))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].from_status, "pending")
        self.assertEqual(history[0].to_status, "cancelled")

        deliveries = list(payment.webhook_deliveries.all())
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].event_type, "payment.cancelled")

    # 6. Mismatch: a payment already succeeded in SafePay while the provider says failed:
    #    status unchanged, no new history, no webhook, outcome "mismatch", and the endpoint
    #    still returns 200 with the current status.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_mismatch_status_unchanged(self, mock_delay):
        ref = "fake_ref_mismatch_6"
        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="sync-mismatch-6",
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            provider_reference=ref,
            status="pending",
        )
        payment.transition_to("processing")
        payment.transition_to("succeeded")
        initial_history_count = payment.status_history.count()

        FakeProvider.simulate(ref, "failed")

        sync_result = sync_payment_from_provider(payment.pk)
        self.assertEqual(sync_result.outcome, "mismatch")

        url = reverse("verify_payment", kwargs={"pk": payment.pk})
        response = self.client.get(url, HTTP_X_API_KEY=self.key_a)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "succeeded")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")
        self.assertEqual(payment.provider_status, "failed")
        self.assertEqual(payment.status_history.count(), initial_history_count)
        self.assertEqual(payment.webhook_deliveries.count(), 0)

    # 7. ProviderError: response 502, payment unchanged, no history, no webhook,
    #    and the response body has no provider error text.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_provider_error_returns_502_generic_message(self, mock_delay):
        ref = "fake_ref_error_7"
        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="sync-error-7",
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            provider_reference=ref,
            status="pending",
        )

        secret_err_text = "SECRET_CREDENTIALS_PROVIDER_CRASH_XYZ"
        with patch.object(FakeProvider, "verify", side_effect=ProviderError(secret_err_text)):
            url = reverse("verify_payment", kwargs={"pk": payment.pk})
            response = self.client.get(url, HTTP_X_API_KEY=self.key_a)

            self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
            self.assertEqual(
                response.data,
                {"detail": "Payment provider unavailable. Try again."},
            )
            self.assertNotIn(secret_err_text, str(response.data))

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertEqual(payment.status_history.count(), 0)
        self.assertEqual(payment.webhook_deliveries.count(), 0)

    # 8. Another merchant's request returns 404 before any provider call (assert provider not called).
    def test_other_merchant_returns_404_before_provider_call(self):
        ref = "fake_ref_other_8"
        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="sync-other-8",
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            provider_reference=ref,
            status="pending",
        )
        FakeProvider.simulate(ref, "paid")

        with patch.object(FakeProvider, "verify") as mock_verify:
            url = reverse("verify_payment", kwargs={"pk": payment.pk})
            response = self.client.get(url, HTTP_X_API_KEY=self.key_b)

            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
            mock_verify.assert_not_called()

    # 9. A payment without a provider returns the stored status exactly as before.
    def test_payment_without_provider_returns_stored_status(self):
        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="sync-no-provider-9",
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            status="pending",
            provider=None,
            provider_reference=None,
        )

        url = reverse("verify_payment", kwargs={"pk": payment.pk})
        response = self.client.get(url, HTTP_X_API_KEY=self.key_a)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], payment.pk)
        self.assertEqual(response.data["status"], "pending")
        self.assertIn("updated_at", response.data)

        sync_result = sync_payment_from_provider(payment.pk)
        self.assertEqual(sync_result.outcome, "no_provider")
        self.assertEqual(sync_result.payment.pk, payment.pk)

    # 10. Webhook is only created after commit: simulate a failure inside transaction
    #     and assert no WebhookDelivery exists.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_webhook_only_created_after_commit(self, mock_delay):
        ref = "fake_ref_rollback_10"
        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="sync-rollback-10",
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            provider_reference=ref,
            status="pending",
        )
        FakeProvider.simulate(ref, "paid")

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                sync_payment_from_provider(payment.pk)
                raise RuntimeError("Simulated crash right after sync inside transaction")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 0)

    # 11. Two sequential sync calls on the same payment create only one WebhookDelivery in total.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_two_sequential_sync_calls_create_one_webhook(self, mock_delay):
        ref = "fake_ref_seq_11"
        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="sync-seq-11",
            amount=Decimal("100.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            provider_reference=ref,
            status="pending",
        )
        FakeProvider.simulate(ref, "paid")

        res1 = sync_payment_from_provider(payment.pk)
        self.assertEqual(res1.outcome, "updated")
        self.assertEqual(res1.payment.status, "succeeded")

        res2 = sync_payment_from_provider(payment.pk)
        self.assertEqual(res2.outcome, "unchanged")
        self.assertEqual(res2.payment.status, "succeeded")

        payment.refresh_from_db()
        self.assertEqual(payment.webhook_deliveries.count(), 1)
        self.assertEqual(payment.status_history.count(), 2)
