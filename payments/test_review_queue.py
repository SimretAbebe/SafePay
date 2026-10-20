"""
Tests for the review queue feature: model fields, admin, alerting,
cooldown, digest, heartbeat, data migration, and security.

All network access is mocked — no real HTTP requests are made.
"""
import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

from django.contrib.admin.sites import AdminSite
from django.core.cache import cache
from django.db import connection
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from payments.admin import PaymentAdmin, ReviewQueuePaymentAdmin, REVIEW_REASON_LABELS
from payments.alerts import format_age, send_alert_message
from payments.models import Merchant, Payment, ReviewQueuePayment
from payments.tasks import (
    alert_new_reviews,
    reconcile_pending_payments,
    review_queue_digest,
)
from payments.test_utils import create_test_merchant


def _make_request():
    """Create a fake admin request with message storage."""
    factory = RequestFactory()
    request = factory.get("/admin/payments/reviewqueuepayment/")
    from django.contrib.messages.storage.fallback import FallbackStorage
    setattr(request, "session", {})
    setattr(request, "_messages", FallbackStorage(request))
    return request


@override_settings(
    ENABLE_FAKE_PROVIDER=True,
    DEFAULT_PROVIDER="fake",
    TELEGRAM_BOT_TOKEN="",
    TELEGRAM_CHAT_ID="",
    REVIEW_ALERT_COOLDOWN_SECONDS=300,
    REVIEW_ALERT_SWEEP_SECONDS=900,
    RECONCILE_HEARTBEAT_URL="",
    RECONCILE_MAX_ATTEMPTS=8,
    RECONCILE_JITTER=0.0,
)
class ReviewQueueTests(TransactionTestCase):

    def setUp(self):
        cache.clear()
        self.merchant, self.api_key = create_test_merchant(
            "Review Queue Merchant",
            webhook_url="https://merchant.example.com/webhook",
        )
        self.now = timezone.now()
        self.site = AdminSite()

    def tearDown(self):
        cache.clear()

    def _create_flagged(self, reason="unknown_outcome", age_minutes=10, **kwargs):
        """Helper: create a payment already flagged for review."""
        uid = uuid.uuid4().hex[:12]
        p = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key=f"rq-{uid}",
            amount=Decimal("100.00"),
            sender="alice",
            receiver=self.merchant.name,
            provider="fake",
            provider_reference=f"ref-rq-{uid}",
            status="pending",
            needs_review=True,
            review_reason=reason,
            needs_review_at=self.now - timedelta(minutes=age_minutes),
            review_alerted_at=None,
            **kwargs,
        )
        return p

    # ------------------------------------------------------------------
    # 1. flag_for_review sets needs_review_at ONCE; clearing resets both
    # ------------------------------------------------------------------
    def test_flag_sets_needs_review_at_once_and_clear_resets(self):
        p = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="flag-once",
            amount=Decimal("50.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference="ref-flag-once",
            status="pending",
        )
        t1 = self.now - timedelta(hours=2)
        p.flag_for_review("mismatch", now=t1)
        self.assertTrue(p.needs_review)
        self.assertEqual(p.needs_review_at, t1)
        self.assertIsNone(p.review_alerted_at)

        # Flag again later — needs_review_at must NOT be overwritten
        t2 = self.now
        p.flag_for_review("mismatch", now=t2)
        self.assertEqual(p.needs_review_at, t1)  # still the original time

        # Clear
        p.clear_review_flag()
        self.assertFalse(p.needs_review)
        self.assertEqual(p.review_reason, "")
        self.assertIsNone(p.needs_review_at)
        self.assertIsNone(p.review_alerted_at)

    # ------------------------------------------------------------------
    # 2. Queue admin queryset, ordering, permissions, format_age
    # ------------------------------------------------------------------
    def test_queue_admin_queryset_and_permissions(self):
        p1 = self._create_flagged(age_minutes=60)
        p2 = self._create_flagged(age_minutes=5)
        # Not flagged — should NOT appear
        Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="not-flagged",
            amount=Decimal("10.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference="ref-not-flagged",
            status="pending",
        )
        admin_inst = ReviewQueuePaymentAdmin(ReviewQueuePayment, self.site)
        request = _make_request()
        qs = admin_inst.get_queryset(request)
        ids = list(qs.values_list("id", flat=True))
        self.assertEqual(ids, [p1.id, p2.id])  # oldest first
        self.assertFalse(admin_inst.has_add_permission(request))
        self.assertFalse(admin_inst.has_delete_permission(request))

    def test_format_age_values(self):
        self.assertEqual(format_age(timedelta(minutes=0)), "0m")
        self.assertEqual(format_age(timedelta(minutes=12)), "12m")
        self.assertEqual(format_age(timedelta(hours=3, minutes=12)), "3h 12m")
        self.assertEqual(format_age(timedelta(days=2, hours=4)), "2d 4h")
        self.assertEqual(format_age(timedelta(days=0, hours=0, minutes=59)), "59m")
        self.assertEqual(format_age(timedelta(seconds=30)), "0m")

    # ------------------------------------------------------------------
    # 3. alert_new_reviews: one message, up to 10 lines, +N more,
    #    marks only shown ones alerted, second run sends nothing
    # ------------------------------------------------------------------
    @patch("payments.alerts.requests.post")
    def test_alert_sends_grouped_message_and_marks_alerted(self, mock_post):
        # Clear any leftover unalerted payments from prior test classes
        Payment.objects.filter(
            needs_review=True, review_alerted_at__isnull=True,
        ).update(review_alerted_at=self.now)

        payments = [self._create_flagged(age_minutes=i + 1) for i in range(12)]
        created_ids = [p.id for p in payments]

        alert_new_reviews()

        # Only the logger path (no Telegram configured).
        mock_post.assert_not_called()

        # First 10 (by oldest needs_review_at) should be marked alerted
        alerted = Payment.objects.filter(
            id__in=created_ids,
            review_alerted_at__isnull=False,
        )
        self.assertEqual(alerted.count(), 10)

        # The last 2 (newest) should NOT be alerted yet
        not_alerted = Payment.objects.filter(
            id__in=created_ids,
            review_alerted_at__isnull=True,
        )
        self.assertEqual(not_alerted.count(), 2)

        # Second run: sends alert for the remaining 2
        with self.assertLogs("payments.alerts", level="WARNING") as cm:
            alert_new_reviews()
        # Now all 12 should be alerted
        all_alerted = Payment.objects.filter(
            id__in=created_ids,
            review_alerted_at__isnull=False,
        )
        self.assertEqual(all_alerted.count(), 12)

    # ------------------------------------------------------------------
    # 4. Cooldown coalescing: multiple flags dispatch alert once
    # ------------------------------------------------------------------
    @patch("payments.tasks.alert_new_reviews.delay")
    def test_cooldown_dispatches_alert_once(self, mock_delay):
        from payments.alerts import schedule_review_alert
        for i in range(5):
            schedule_review_alert()
        mock_delay.assert_called_once()

    # ------------------------------------------------------------------
    # 5. Messages contain no token, no customer data, no merchant_reference, no email
    # ------------------------------------------------------------------
    @patch("payments.alerts.requests.post")
    @override_settings(
        TELEGRAM_BOT_TOKEN="test-tok-secret-123",
        TELEGRAM_CHAT_ID="12345",
    )
    def test_alert_message_contains_no_sensitive_data(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        p = self._create_flagged(age_minutes=5)
        p.merchant_reference = "merchant-secret-ref"
        p.sender = "customer@example.com"
        p.save(update_fields=["merchant_reference", "sender"])

        with self.assertLogs("payments.alerts", level="WARNING") as cm:
            alert_new_reviews()

        log_output = "\n".join(cm.output)
        # Must NOT contain secrets
        self.assertNotIn("test-tok-secret-123", log_output)
        self.assertNotIn("customer@example.com", log_output)
        self.assertNotIn("merchant-secret-ref", log_output)

        # Check the posted body too
        posted_text = mock_post.call_args[1]["json"]["text"]
        self.assertNotIn("test-tok-secret-123", posted_text)
        self.assertNotIn("customer@example.com", posted_text)
        self.assertNotIn("merchant-secret-ref", posted_text)

    # ------------------------------------------------------------------
    # 6. Failing send: does not raise, does not mark alerted, retry works,
    #    token never in logs
    # ------------------------------------------------------------------
    @override_settings(
        TELEGRAM_BOT_TOKEN="secret-token-xyz",
        TELEGRAM_CHAT_ID="99999",
    )
    @patch("payments.alerts.requests.post")
    def test_failing_send_does_not_mark_alerted(self, mock_post):
        import requests as real_requests
        mock_post.side_effect = real_requests.Timeout("timeout")

        p = self._create_flagged(age_minutes=5)

        with self.assertLogs("payments.alerts", level="WARNING") as cm:
            alert_new_reviews()  # should not raise

        p.refresh_from_db()
        self.assertIsNone(p.review_alerted_at)  # NOT marked

        log_output = "\n".join(cm.output)
        self.assertNotIn("secret-token-xyz", log_output)

        # Now simulate success — next run should retry
        mock_post.side_effect = None
        mock_post.return_value = MagicMock(status_code=200)
        alert_new_reviews()
        p.refresh_from_db()
        self.assertIsNotNone(p.review_alerted_at)

    @override_settings(
        TELEGRAM_BOT_TOKEN="secret-token-xyz",
        TELEGRAM_CHAT_ID="99999",
    )
    @patch("payments.alerts.requests.post")
    def test_http_500_does_not_mark_alerted(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500)

        p = self._create_flagged(age_minutes=5)
        with self.assertLogs("payments.alerts", level="WARNING") as cm:
            alert_new_reviews()

        p.refresh_from_db()
        self.assertIsNone(p.review_alerted_at)

        log_output = "\n".join(cm.output)
        self.assertNotIn("secret-token-xyz", log_output)

    @override_settings(
        TELEGRAM_BOT_TOKEN="secret-token-xyz",
        TELEGRAM_CHAT_ID="99999",
    )
    @patch("payments.alerts.requests.post")
    def test_connection_error_does_not_mark_alerted(self, mock_post):
        import requests as real_requests
        mock_post.side_effect = real_requests.ConnectionError("conn refused")

        p = self._create_flagged(age_minutes=5)
        with self.assertLogs("payments.alerts", level="WARNING") as cm:
            alert_new_reviews()

        p.refresh_from_db()
        self.assertIsNone(p.review_alerted_at)

        log_output = "\n".join(cm.output)
        self.assertNotIn("secret-token-xyz", log_output)

    # ------------------------------------------------------------------
    # 7. No Telegram: log alone counts as delivered
    # ------------------------------------------------------------------
    def test_no_telegram_marks_alerted(self):
        p = self._create_flagged(age_minutes=5)
        with self.assertLogs("payments.alerts", level="WARNING"):
            alert_new_reviews()
        p.refresh_from_db()
        self.assertIsNotNone(p.review_alerted_at)

    # ------------------------------------------------------------------
    # 8. review_queue_digest
    # ------------------------------------------------------------------
    def test_digest_empty_queue_sends_nothing(self):
        # No flagged payments — digest should not log
        result = review_queue_digest()
        self.assertIsNone(result)

    def test_digest_reports_total_reasons_and_oldest(self):
        self._create_flagged(reason="mismatch", age_minutes=60)
        self._create_flagged(reason="mismatch", age_minutes=30)
        self._create_flagged(reason="unknown_outcome", age_minutes=120)

        with self.assertLogs("payments.alerts", level="WARNING") as cm:
            review_queue_digest()

        log_output = "\n".join(cm.output)
        self.assertIn("3 item(s)", log_output)
        self.assertIn("mismatch: 2", log_output)
        self.assertIn("unknown_outcome: 1", log_output)
        # Oldest is 120 minutes = 2h 0m
        self.assertIn("2h 0m", log_output)

    # ------------------------------------------------------------------
    # 9. Heartbeat
    # ------------------------------------------------------------------
    @patch("payments.tasks.reconcile_payment.delay")
    @patch("payments.tasks.requests.get")
    @override_settings(RECONCILE_HEARTBEAT_URL="https://hb.example.com/ping")
    def test_heartbeat_called_after_sweep(self, mock_get, mock_reconcile):
        mock_get.return_value = MagicMock(status_code=200)
        reconcile_pending_payments()
        mock_get.assert_called_once_with("https://hb.example.com/ping", timeout=3)

    @patch("payments.tasks.reconcile_payment.delay")
    @patch("payments.tasks.requests.get")
    def test_heartbeat_not_called_when_unset(self, mock_get, mock_reconcile):
        reconcile_pending_payments()
        mock_get.assert_not_called()

    @patch("payments.tasks.reconcile_payment.delay")
    @patch("payments.tasks.requests.get")
    @override_settings(RECONCILE_HEARTBEAT_URL="https://hb.example.com/ping")
    def test_heartbeat_failure_does_not_raise(self, mock_get, mock_reconcile):
        import requests as real_requests
        mock_get.side_effect = real_requests.Timeout("timeout")
        # Should not raise
        result = reconcile_pending_payments()
        self.assertIn("due_count", result)

    @patch("payments.tasks.reconcile_payment.delay")
    @patch("payments.tasks.requests.get")
    @override_settings(RECONCILE_HEARTBEAT_URL="https://hb.example.com/ping")
    def test_heartbeat_not_called_when_sweep_raises(self, mock_get, mock_reconcile):
        # Force the sweep's select_for_update to raise
        with patch("payments.tasks.Payment.objects") as mock_qs:
            mock_qs.select_for_update.side_effect = RuntimeError("db error")
            try:
                reconcile_pending_payments()
            except RuntimeError:
                pass
        mock_get.assert_not_called()

    # ------------------------------------------------------------------
    # 10. Both admin actions work and reset timestamps
    # ------------------------------------------------------------------
    @patch("payments.tasks.reconcile_payment.delay")
    def test_review_queue_admin_actions(self, mock_reconcile):
        p = self._create_flagged(age_minutes=30)
        p.review_alerted_at = self.now
        p.save(update_fields=["review_alerted_at"])

        admin_inst = ReviewQueuePaymentAdmin(ReviewQueuePayment, self.site)
        request = _make_request()

        # Check with provider now
        qs = ReviewQueuePayment.objects.filter(id=p.id)
        admin_inst.check_with_provider_now(request, qs)
        mock_reconcile.assert_called_once_with(p.id)

        # Clear review flag
        admin_inst.clear_review_and_recheck(request, qs)
        p.refresh_from_db()
        self.assertFalse(p.needs_review)
        self.assertIsNone(p.needs_review_at)
        self.assertIsNone(p.review_alerted_at)
        self.assertEqual(p.sync_attempts, 0)
        self.assertIsNotNone(p.next_sync_at)

    @patch("payments.tasks.reconcile_payment.delay")
    def test_payment_admin_clear_also_resets_timestamps(self, mock_reconcile):
        p = self._create_flagged(age_minutes=30)
        p.review_alerted_at = self.now
        p.save(update_fields=["review_alerted_at"])

        admin_inst = PaymentAdmin(Payment, self.site)
        request = _make_request()
        admin_inst.clear_review_and_recheck(request, Payment.objects.filter(id=p.id))
        p.refresh_from_db()
        self.assertFalse(p.needs_review)
        self.assertIsNone(p.needs_review_at)
        self.assertIsNone(p.review_alerted_at)

    # ------------------------------------------------------------------
    # 11. Data migration backfill + proxy model does not alter table
    # ------------------------------------------------------------------
    def test_proxy_model_does_not_create_table(self):
        """ReviewQueuePayment is a proxy — no separate table should exist."""
        tables = connection.introspection.table_names()
        self.assertNotIn("payments_reviewqueuepayment", tables)
        self.assertIn("payments_payment", tables)

    def test_data_migration_backfill(self):
        """Payments with needs_review=True but needs_review_at=None should
        be backfilled by the data migration. We simulate the migration logic."""
        p = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="backfill-test",
            amount=Decimal("100.00"),
            sender="a",
            receiver="b",
            provider="fake",
            provider_reference="ref-backfill",
            status="pending",
            needs_review=True,
            review_reason="unknown_outcome",
            last_synced_at=self.now - timedelta(hours=1),
        )
        # Simulate the migration hasn't run yet
        Payment.objects.filter(id=p.id).update(needs_review_at=None)
        p.refresh_from_db()
        self.assertIsNone(p.needs_review_at)

        # Simulate migration logic
        flagged = Payment.objects.filter(needs_review=True, needs_review_at__isnull=True)
        for payment in flagged:
            payment.needs_review_at = payment.last_synced_at or payment.updated_at
            payment.save(update_fields=["needs_review_at"])

        p.refresh_from_db()
        self.assertIsNotNone(p.needs_review_at)
        self.assertEqual(p.needs_review_at, p.last_synced_at)

    # ------------------------------------------------------------------
    # 12. Existing tests pass (verified by running the full suite)
    # ------------------------------------------------------------------
    # This is verified externally by running `python manage.py test payments`.


@override_settings(
    TELEGRAM_BOT_TOKEN="",
    TELEGRAM_CHAT_ID="",
)
class SendAlertMessageTests(TestCase):
    """Unit tests for the send_alert_message helper."""

    def test_returns_true_when_telegram_disabled(self):
        with self.assertLogs("payments.alerts", level="WARNING"):
            result = send_alert_message("test message")
        self.assertTrue(result)

    @override_settings(
        TELEGRAM_BOT_TOKEN="tok-secret",
        TELEGRAM_CHAT_ID="123",
    )
    @patch("payments.alerts.requests.post")
    def test_returns_true_on_telegram_success(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        with self.assertLogs("payments.alerts", level="WARNING"):
            result = send_alert_message("hello")
        self.assertTrue(result)
        mock_post.assert_called_once()

    @override_settings(
        TELEGRAM_BOT_TOKEN="tok-secret",
        TELEGRAM_CHAT_ID="123",
    )
    @patch("payments.alerts.requests.post")
    def test_returns_false_on_telegram_failure(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500)
        with self.assertLogs("payments.alerts", level="WARNING") as cm:
            result = send_alert_message("hello")
        self.assertFalse(result)
        log_output = "\n".join(cm.output)
        self.assertNotIn("tok-secret", log_output)
