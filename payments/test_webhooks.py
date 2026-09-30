from unittest.mock import patch, MagicMock

from django.test import TestCase

from .models import Payment, WebhookDelivery
from .services import notify_status_change


class WebhookDeliveryTests(TestCase):

    def setUp(self):
        self.payment = Payment.objects.create(
            idempotency_key="webhook-test-key",
            amount=250.00,
            sender="alice",
            receiver="bob",
        )
        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")

    @patch("payments.tasks.requests.post")
    def test_webhook_succeeds_on_first_attempt(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)

        webhook = notify_status_change(self.payment)
        webhook.refresh_from_db()

        self.assertEqual(webhook.status, "succeeded")
        self.assertEqual(webhook.attempt_count, 1)
        self.assertEqual(mock_post.call_count, 1)

    @patch("payments.tasks.requests.post")
    def test_webhook_retries_then_succeeds(self, mock_post):
        # Fail twice (500 errors), then succeed on the 3rd attempt.
        mock_post.side_effect = [
            MagicMock(status_code=500),
            MagicMock(status_code=500),
            MagicMock(status_code=200),
        ]

        webhook = notify_status_change(self.payment)
        webhook.refresh_from_db()

        self.assertEqual(webhook.status, "succeeded")
        self.assertEqual(webhook.attempt_count, 3)
        self.assertEqual(mock_post.call_count, 3)

    @patch("payments.tasks.requests.post")
    def test_webhook_gives_up_after_max_attempts(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500)

        webhook = notify_status_change(self.payment)
        webhook.refresh_from_db()

        self.assertEqual(webhook.status, "failed")
        self.assertEqual(webhook.attempt_count, webhook.max_attempts)
        self.assertEqual(mock_post.call_count, webhook.max_attempts)

    @patch("payments.tasks.requests.post")
    def test_webhook_payload_is_signed(self, mock_post):
        """
        Confirm a real HMAC signature header is sent -- not just that
        SOME header exists, but that it's actually verifiable using the
        same secret and payload.
        """
        import hashlib
        import hmac
        import json
        from django.conf import settings

        mock_post.return_value = MagicMock(status_code=200)

        webhook = notify_status_change(self.payment)
        webhook.refresh_from_db()

        sent_headers = mock_post.call_args.kwargs["headers"]
        sent_body = mock_post.call_args.kwargs["data"]

        expected_signature = hmac.new(
            settings.WEBHOOK_SECRET.encode(),
            sent_body,
            hashlib.sha256,
        ).hexdigest()

        self.assertEqual(
            sent_headers["X-SafePay-Signature"],
            f"sha256={expected_signature}",
        )