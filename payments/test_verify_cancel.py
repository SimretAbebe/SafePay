"""
Tests for the verify and cancel endpoints.
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from .models import Payment, PaymentStatusHistory, WebhookDelivery
from .test_utils import create_test_merchant


def _create_payment(merchant, idempotency_key="test-key", amount="100.00"):
    """Helper: directly create a Payment in the DB for a given merchant."""
    return Payment.objects.create(
        merchant=merchant,
        idempotency_key=idempotency_key,
        amount=amount,
        sender="alice",
        receiver="bob",
    )


class VerifyEndpointTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.merchant_a, self.key_a = create_test_merchant("Verify Merchant A")
        self.merchant_b, self.key_b = create_test_merchant("Verify Merchant B")

    
    # Test 1: Verify returns minimal status body for owning merchant
  
    def test_verify_returns_minimal_body_for_owning_merchant(self):
        payment = _create_payment(self.merchant_a, "verify-key-1")
        url = reverse("verify_payment", kwargs={"pk": payment.pk})

        response = self.client.get(url, HTTP_X_API_KEY=self.key_a)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Must include exactly these three fields
        self.assertEqual(response.data["id"], payment.pk)
        self.assertEqual(response.data["status"], "pending")
        self.assertIn("updated_at", response.data)
        # Must NOT include the full payment details (amount, sender, receiver...)
        self.assertNotIn("amount", response.data)
        self.assertNotIn("sender", response.data)

    
    # Test 2: Verify on another merchant's payment returns 404
  
    def test_verify_another_merchants_payment_returns_404(self):
        payment = _create_payment(self.merchant_a, "verify-key-2")
        url = reverse("verify_payment", kwargs={"pk": payment.pk})

        response = self.client.get(url, HTTP_X_API_KEY=self.key_b)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Test 3: Verify without a key returns 403

    def test_verify_without_api_key_returns_403(self):
        payment = _create_payment(self.merchant_a, "verify-key-3")
        url = reverse("verify_payment", kwargs={"pk": payment.pk})

        response = self.client.get(url)  # no key

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class CancelEndpointTests(TestCase):

    def setUp(self):
        self.client = APIClient()
        self.merchant_a, self.key_a = create_test_merchant("Cancel Merchant A")
        self.merchant_b, self.key_b = create_test_merchant("Cancel Merchant B")

       
    # Test 4: Cancel a pending payment -> 200, status=cancelled, 1 history row

    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_cancel_pending_payment_succeeds(self, mock_delay):
        payment = _create_payment(self.merchant_a, "cancel-pending-key")
        url = reverse("cancel_payment", kwargs={"pk": payment.pk})

        response = self.client.post(url, HTTP_X_API_KEY=self.key_a)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], payment.pk)
        self.assertEqual(response.data["status"], "cancelled")

        # DB must reflect the new status
        payment.refresh_from_db()
        self.assertEqual(payment.status, "cancelled")

        # Exactly one history row: pending -> cancelled
        history = list(
            PaymentStatusHistory.objects.filter(payment=payment).order_by("changed_at")
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].from_status, "pending")
        self.assertEqual(history[0].to_status, "cancelled")


    # Test 5: Cancel an already-cancelled payment -> 200, no extra history row

    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_cancel_already_cancelled_payment_is_idempotent(self, mock_delay):
        payment = _create_payment(self.merchant_a, "cancel-idem-key")
        # First cancellation (also queues one webhook)
        url = reverse("cancel_payment", kwargs={"pk": payment.pk})
        first_resp = self.client.post(url, HTTP_X_API_KEY=self.key_a)
        self.assertEqual(first_resp.status_code, status.HTTP_200_OK)

        history_count_after_first = PaymentStatusHistory.objects.filter(
            payment=payment
        ).count()

        # Second cancellation -- must not create another history row
        second_resp = self.client.post(url, HTTP_X_API_KEY=self.key_a)
        self.assertEqual(second_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(second_resp.data["status"], "cancelled")

        history_count_after_second = PaymentStatusHistory.objects.filter(
            payment=payment
        ).count()
        self.assertEqual(history_count_after_second, history_count_after_first)

    
    # Test 6: Cancel a processing payment -> 409, status unchanged

    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_cancel_processing_payment_returns_409(self, mock_delay):
        payment = _create_payment(self.merchant_a, "cancel-processing-key")
        # Advance to processing via the state machine
        payment.transition_to("processing")

        url = reverse("cancel_payment", kwargs={"pk": payment.pk})
        response = self.client.post(url, HTTP_X_API_KEY=self.key_a)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("detail", response.data)

        # Status must be unchanged
        payment.refresh_from_db()
        self.assertEqual(payment.status, "processing")

    
    # Test 7: Cancel another merchant's payment -> 404, not 409, even when that payment is in a state that would give 409 for its owner

    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_cancel_another_merchants_processing_payment_returns_404_not_409(
        self, mock_delay
    ):
        # Merchant A has a processing payment (owner would get 409)
        payment = _create_payment(self.merchant_a, "cancel-cross-merchant-key")
        payment.transition_to("processing")

        url = reverse("cancel_payment", kwargs={"pk": payment.pk})

        # Merchant B sends the cancel request -> must be 404, not 409
        response = self.client.post(url, HTTP_X_API_KEY=self.key_b)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Status must still be processing (untouched)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "processing")

    
    # Test 8: Cancelling a pending payment sends exactly one webhook delivery

    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_cancel_queues_exactly_one_webhook_delivery(self, mock_delay):
        payment = _create_payment(self.merchant_a, "cancel-webhook-key")
        url = reverse("cancel_payment", kwargs={"pk": payment.pk})

        response = self.client.post(url, HTTP_X_API_KEY=self.key_a)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Exactly one WebhookDelivery row created for this payment
        deliveries = WebhookDelivery.objects.filter(payment=payment)
        self.assertEqual(deliveries.count(), 1)

        delivery = deliveries.first()
        self.assertEqual(delivery.payload["data"]["status"], "cancelled")
        self.assertEqual(delivery.payload["type"], "payment.cancelled")

        # Celery task was enqueued exactly once
        self.assertEqual(mock_delay.call_count, 1)
        mock_delay.assert_called_once_with(delivery.id)
