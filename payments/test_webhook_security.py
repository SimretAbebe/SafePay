import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase

from .models import Payment, WebhookDelivery
from .services import notify_status_change
from .webhook_verification import verify_webhook


class WebhookSecurityAndStructureTests(TestCase):

    def setUp(self):
        self.payment = Payment.objects.create(
            idempotency_key="security-test-key",
            amount=150.00,
            sender="alice",
            receiver="bob",
        )

    # 1. The payload has id, type and data, and id equals the delivery's event_id.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_payload_structure_and_event_id_match(self, mock_delay):
        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")

        webhook = notify_status_change(self.payment)
        webhook.refresh_from_db()

        self.assertIn("id", webhook.payload)
        self.assertIn("type", webhook.payload)
        self.assertIn("data", webhook.payload)

        self.assertEqual(webhook.payload["id"], str(webhook.event_id))
        self.assertEqual(webhook.payload["type"], webhook.event_type)

        data = webhook.payload["data"]
        self.assertEqual(data["payment_id"], self.payment.id)
        self.assertEqual(data["idempotency_key"], self.payment.idempotency_key)
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["amount"], str(self.payment.amount))

    # 2. Event type is correct for succeeded, failed and cancelled.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_event_type_mapping(self, mock_delay):
        # Succeeded
        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")
        w_succeeded = notify_status_change(self.payment)
        self.assertEqual(w_succeeded.event_type, "payment.succeeded")
        self.assertEqual(w_succeeded.payload["type"], "payment.succeeded")

        # Failed
        payment_failed = Payment.objects.create(
            idempotency_key="failed-key",
            amount=50.00,
            sender="carol",
            receiver="dave",
        )
        payment_failed.transition_to("processing")
        payment_failed.transition_to("failed")
        w_failed = notify_status_change(payment_failed)
        self.assertEqual(w_failed.event_type, "payment.failed")
        self.assertEqual(w_failed.payload["type"], "payment.failed")

        # Cancelled
        payment_cancelled = Payment.objects.create(
            idempotency_key="cancelled-key",
            amount=75.00,
            sender="eve",
            receiver="frank",
        )
        payment_cancelled.transition_to("cancelled")
        w_cancelled = notify_status_change(payment_cancelled)
        self.assertEqual(w_cancelled.event_type, "payment.cancelled")
        self.assertEqual(w_cancelled.payload["type"], "payment.cancelled")

        # Unsupported status raises ValueError
        payment_pending = Payment.objects.create(
            idempotency_key="pending-key",
            amount=10.00,
            sender="grace",
            receiver="heidi",
        )
        with self.assertRaises(ValueError):
            notify_status_change(payment_pending)

    # 3. The signature is HMAC of timestamp + "." + body, and verify_webhook returns True for freshly signed request.
    @patch("payments.tasks.requests.post")
    def test_signature_construction_and_verification(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)

        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")
        webhook = notify_status_change(self.payment)

        call_args = mock_post.call_args
        headers = call_args.kwargs["headers"]
        data = call_args.kwargs["data"]

        timestamp = headers["X-SafePay-Timestamp"]
        signature = headers["X-SafePay-Signature"]

        to_sign = f"{timestamp}.".encode("utf-8") + data
        computed_sig = hmac.new(
            settings.WEBHOOK_SECRET.encode("utf-8"),
            to_sign,
            hashlib.sha256,
        ).hexdigest()

        self.assertEqual(signature, computed_sig)
        self.assertTrue(
            verify_webhook(
                secret=settings.WEBHOOK_SECRET,
                timestamp=timestamp,
                signature=signature,
                body=data,
            )
        )

    # 4. Changing the body OR the timestamp makes verify_webhook return False.
    def test_tampered_body_or_timestamp_fails_verification(self):
        secret = "test-secret-key"
        timestamp = "1700000000"
        now = 1700000050
        body = b'{"id": "evt-123", "type": "payment.succeeded", "data": {}}'

        to_sign = f"{timestamp}.".encode("utf-8") + body
        valid_signature = hmac.new(
            secret.encode("utf-8"),
            to_sign,
            hashlib.sha256,
        ).hexdigest()

        # Valid baseline
        self.assertTrue(
            verify_webhook(secret, timestamp, valid_signature, body, now=now)
        )

        # Tampered body
        tampered_body = b'{"id": "evt-123", "type": "payment.succeeded", "data": {"amount": "9999"}}'
        self.assertFalse(
            verify_webhook(secret, timestamp, valid_signature, tampered_body, now=now)
        )

        # Tampered timestamp
        tampered_timestamp = "1700000001"
        self.assertFalse(
            verify_webhook(secret, tampered_timestamp, valid_signature, body, now=now)
        )

        # Invalid non-integer timestamp
        self.assertFalse(
            verify_webhook(secret, "invalid-ts", valid_signature, body, now=now)
        )

    # 5. Timestamp older than 5 minutes is rejected, and inside tolerance is accepted.
    def test_timestamp_tolerance_and_replay_protection(self):
        secret = "test-secret-key"
        now = 1700000000
        body = b'{"status": "test"}'

        # Exactly 300 seconds old -> valid (tolerance=300)
        ts_exact = str(now - 300)
        sig_exact = hmac.new(
            secret.encode("utf-8"),
            f"{ts_exact}.".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()
        self.assertTrue(
            verify_webhook(secret, ts_exact, sig_exact, body, tolerance_seconds=300, now=now)
        )

        # 301 seconds old -> rejected
        ts_expired = str(now - 301)
        sig_expired = hmac.new(
            secret.encode("utf-8"),
            f"{ts_expired}.".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()
        self.assertFalse(
            verify_webhook(secret, ts_expired, sig_expired, body, tolerance_seconds=300, now=now)
        )

        # 100 seconds old -> inside tolerance
        ts_fresh = str(now - 100)
        sig_fresh = hmac.new(
            secret.encode("utf-8"),
            f"{ts_fresh}.".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()
        self.assertTrue(
            verify_webhook(secret, ts_fresh, sig_fresh, body, tolerance_seconds=300, now=now)
        )

        # Too far in the future (> 300s ahead) -> rejected
        ts_future = str(now + 305)
        sig_future = hmac.new(
            secret.encode("utf-8"),
            f"{ts_future}.".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()
        self.assertFalse(
            verify_webhook(secret, ts_future, sig_future, body, tolerance_seconds=300, now=now)
        )

    # 6. Across a retry-then-success sequence, event_id is the same on every attempt while timestamp is fresh.
    @patch("payments.tasks.time.time")
    @patch("payments.tasks.requests.post")
    def test_retry_sequence_keeps_event_id_and_refreshes_timestamp(self, mock_post, mock_time):
        mock_time.side_effect = [1700000000, 1700000010, 1700000020]
        mock_post.side_effect = [
            MagicMock(status_code=500),
            MagicMock(status_code=500),
            MagicMock(status_code=200),
        ]

        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")
        webhook = notify_status_change(self.payment)
        webhook.refresh_from_db()

        self.assertEqual(webhook.status, "succeeded")
        self.assertEqual(webhook.attempt_count, 3)
        self.assertEqual(mock_post.call_count, 3)

        attempts_headers = [call.kwargs["headers"] for call in mock_post.call_args_list]

        # event_id is constant across all attempts
        for headers in attempts_headers:
            self.assertEqual(headers["X-SafePay-Event-Id"], str(webhook.event_id))

        # timestamp is fresh on each attempt
        timestamps = [headers["X-SafePay-Timestamp"] for headers in attempts_headers]
        self.assertEqual(timestamps, ["1700000000", "1700000010", "1700000020"])

        # signatures are distinct because timestamp changed
        signatures = [headers["X-SafePay-Signature"] for headers in attempts_headers]
        self.assertEqual(len(set(signatures)), 3)

    # 7. Two different deliveries get two different event_ids.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_different_deliveries_get_unique_event_ids(self, mock_delay):
        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")

        p2 = Payment.objects.create(
            idempotency_key="unique-id-key-2",
            amount=300.00,
            sender="alice",
            receiver="bob",
        )
        p2.transition_to("processing")
        p2.transition_to("succeeded")

        delivery1 = notify_status_change(self.payment)
        delivery2 = notify_status_change(p2)

        self.assertNotEqual(delivery1.event_id, delivery2.event_id)
        self.assertNotEqual(delivery1.payload["id"], delivery2.payload["id"])
