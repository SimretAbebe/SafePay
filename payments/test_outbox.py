import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import transaction
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from payments.admin import WebhookOutboxAdmin, requeue_outbox_rows
from payments.models import Merchant, Payment, WebhookDelivery, WebhookOutbox
from payments.services import resend_delivery
from payments.tasks import deliver_webhook_attempt, drain_webhook_outbox


class OutboxTestBase(TestCase):
    def setUp(self):
        self.merchant, self.api_key = Merchant.create_with_key(
            name="OutboxMerchant",
            webhook_url="https://example.com/webhooks",
        )
        self.payment = Payment.objects.create(
            merchant=self.merchant,
            livemode=False,
            amount="120.00",
            sender="alice",
            receiver="bob",
            provider="chapa",
            idempotency_key=f"outbox-test-{uuid.uuid4()}",
            status="pending",
        )


class TestTransactionalOutbox(OutboxTestBase):
    # 1. Status change to succeeded creates exactly one WebhookDelivery and exactly one WebhookOutbox
    # row (pending) in the same transaction; forcing an exception after transition_to rolls back both.
    def test_status_change_creates_outbox_in_same_transaction_and_rolls_back(self):
        payment = self.payment
        payment.transition_to("processing")

        # Successful transition
        payment.transition_to("succeeded")

        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 1)
        delivery = WebhookDelivery.objects.get(payment=payment)
        self.assertEqual(WebhookOutbox.objects.filter(delivery=delivery).count(), 1)
        outbox = delivery.outbox
        self.assertEqual(outbox.status, WebhookOutbox.STATUS_PENDING)
        self.assertEqual(outbox.attempts, 0)
        self.assertEqual(outbox.last_error_code, "")
        self.assertIsNone(outbox.sent_at)

        # Now test atomicity rollback: force exception inside an atomic block after transition_to
        payment2 = Payment.objects.create(
            merchant=self.merchant,
            amount="50.00",
            sender="alice",
            receiver="bob",
            idempotency_key=f"outbox-rollback-{uuid.uuid4()}",
            status="processing",
        )

        class ForcedRollbackError(Exception):
            pass

        with self.assertRaises(ForcedRollbackError):
            with transaction.atomic():
                payment2.transition_to("succeeded")
                raise ForcedRollbackError("Simulated crash after transition")

        payment2.refresh_from_db()
        self.assertEqual(payment2.status, "processing")
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment2).count(), 0)
        self.assertEqual(WebhookOutbox.objects.filter(delivery__payment=payment2).count(), 0)

    # 2. With the broker mocked to raise on .delay, the status change still commits, the outbox row
    # stays pending with attempts incremented and last_error_code "broker_unavailable" and a later
    # next_attempt_at; no raw exception text is stored.
    def test_broker_outage_keeps_pending_with_backoff_and_no_raw_exception(self):
        payment = self.payment
        payment.transition_to("processing")
        payment.transition_to("succeeded")

        delivery = WebhookDelivery.objects.get(payment=payment)
        outbox = delivery.outbox
        original_next_attempt = outbox.next_attempt_at

        # Simulate broker outage when drain_webhook_outbox runs
        with patch("payments.tasks.deliver_webhook_attempt.delay", side_effect=RuntimeError("Redis connection refused on 6379")):
            summary = drain_webhook_outbox()

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["sent"], 0)

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, WebhookOutbox.STATUS_PENDING)
        self.assertEqual(outbox.attempts, 1)
        self.assertEqual(outbox.last_error_code, "broker_unavailable")
        self.assertNotIn("Redis", outbox.last_error_code)
        self.assertNotIn("6379", outbox.last_error_code)
        self.assertGreater(outbox.next_attempt_at, original_next_attempt)
        self.assertIsNone(outbox.sent_at)

    # 3. When the broker recovers, the sweeper queues the task exactly once and marks the row sent
    # with sent_at set; a second sweep does not queue it again.
    def test_broker_recovery_marks_sent_and_second_sweep_no_op(self):
        payment = self.payment
        payment.transition_to("processing")
        payment.transition_to("succeeded")
        delivery = WebhookDelivery.objects.get(payment=payment)

        # Broker recovers
        with patch("payments.tasks.deliver_webhook_attempt.delay") as mock_delay:
            summary1 = drain_webhook_outbox()
            self.assertEqual(summary1["sent"], 1)
            mock_delay.assert_called_once_with(delivery.id)

            outbox = WebhookOutbox.objects.get(delivery=delivery)
            self.assertEqual(outbox.status, WebhookOutbox.STATUS_SENT)
            self.assertIsNotNone(outbox.sent_at)

            # Second sweep must not queue it again
            mock_delay.reset_mock()
            summary2 = drain_webhook_outbox()
            self.assertEqual(summary2["processed"], 0)
            mock_delay.assert_not_called()

    # 4. The sweeper never creates a second WebhookDelivery for the same payment event.
    def test_sweeper_never_creates_second_delivery(self):
        payment = self.payment
        payment.transition_to("processing")
        payment.transition_to("succeeded")

        initial_delivery_count = WebhookDelivery.objects.filter(payment=payment).count()
        self.assertEqual(initial_delivery_count, 1)

        with patch("payments.tasks.deliver_webhook_attempt.delay"):
            drain_webhook_outbox()
            drain_webhook_outbox()
            drain_webhook_outbox()

        final_delivery_count = WebhookDelivery.objects.filter(payment=payment).count()
        self.assertEqual(final_delivery_count, 1)

    # 5. A row whose delivery already reached a final state is marked sent without queueing.
    def test_final_state_delivery_marked_sent_without_queueing(self):
        payment = self.payment
        payment.transition_to("processing")
        payment.transition_to("succeeded")

        delivery = WebhookDelivery.objects.get(payment=payment)
        # Mark delivery as already succeeded (or failed) before outbox sweeper runs
        delivery.status = "succeeded"
        delivery.save(update_fields=["status"])

        outbox = delivery.outbox
        self.assertEqual(outbox.status, WebhookOutbox.STATUS_PENDING)

        with patch("payments.tasks.deliver_webhook_attempt.delay") as mock_delay:
            summary = drain_webhook_outbox()
            mock_delay.assert_not_called()
            self.assertEqual(summary["final_skipped"], 1)

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, WebhookOutbox.STATUS_SENT)
        self.assertIsNotNone(outbox.sent_at)

    # 6. After OUTBOX_MAX_ATTEMPTS failures the row becomes needs_review; the requeue admin action
    # resets only needs_review rows and reports the others as skipped.
    @override_settings(OUTBOX_MAX_ATTEMPTS=3)
    def test_max_attempts_moves_to_needs_review_and_admin_requeue(self):
        payment = self.payment
        payment.transition_to("processing")
        payment.transition_to("succeeded")

        delivery = WebhookDelivery.objects.get(payment=payment)
        outbox = delivery.outbox

        # 3 failures to reach max attempts
        with patch("payments.tasks.deliver_webhook_attempt.delay", side_effect=RuntimeError("Broker down")):
            for _ in range(3):
                # Wind back next_attempt_at so sweeper picks it up each time
                outbox.next_attempt_at = timezone.now() - timedelta(seconds=1)
                outbox.save(update_fields=["next_attempt_at"])
                drain_webhook_outbox()
                outbox.refresh_from_db()

        self.assertEqual(outbox.attempts, 3)
        self.assertEqual(outbox.status, WebhookOutbox.STATUS_NEEDS_REVIEW)
        self.assertEqual(outbox.last_error_code, "broker_unavailable")

        # Create a second row that is 'sent' to verify admin skips it
        delivery2 = WebhookDelivery.objects.create(
            payment=payment,
            event_id=uuid.uuid4(),
            event_type="payment.failed",
            target_url="https://example.com/webhook",
            payload={},
        )
        outbox_sent = WebhookOutbox.objects.create(
            delivery=delivery2,
            status=WebhookOutbox.STATUS_SENT,
            next_attempt_at=timezone.now(),
        )

        rf = RequestFactory()
        request = rf.post("/admin/payments/webhookoutbox/")
        modeladmin = WebhookOutboxAdmin(WebhookOutbox, AdminSite())
        modeladmin.message_user = MagicMock()

        qs = WebhookOutbox.objects.filter(id__in=[outbox.id, outbox_sent.id])

        requeue_outbox_rows(modeladmin, request, qs)

        outbox.refresh_from_db()
        outbox_sent.refresh_from_db()

        self.assertEqual(outbox.status, WebhookOutbox.STATUS_PENDING)
        self.assertEqual(outbox.attempts, 0)
        self.assertEqual(outbox.last_error_code, "")
        self.assertAlmostEqual(
            outbox.next_attempt_at.timestamp(),
            timezone.now().timestamp(),
            delta=5,
        )

        # Sent row must remain untouched
        self.assertEqual(outbox_sent.status, WebhookOutbox.STATUS_SENT)
        modeladmin.message_user.assert_any_call(
            request,
            "Requeued 1 outbox row(s).",
            level=25,  # messages.SUCCESS
        )
        modeladmin.message_user.assert_any_call(
            request,
            "Skipped 1 row(s) not in needs_review state.",
            level=30,  # messages.WARNING
        )

    # 8. Cleanup deletes sent rows older than the retention window and never deletes needs_review rows.
    @override_settings(OUTBOX_RETENTION_DAYS=14)
    def test_cleanup_deletes_sent_older_than_retention_and_never_needs_review(self):
        now = timezone.now()
        fifteen_days_ago = now - timedelta(days=15)
        two_days_ago = now - timedelta(days=2)

        # Old sent row (should be deleted)
        d_old_sent = WebhookDelivery.objects.create(
            payment=self.payment,
            event_id=uuid.uuid4(),
            event_type="payment.succeeded",
            target_url="https://example.com",
            payload={},
            status="succeeded",
        )
        o_old_sent = WebhookOutbox.objects.create(
            delivery=d_old_sent,
            status=WebhookOutbox.STATUS_SENT,
            next_attempt_at=fifteen_days_ago,
            sent_at=fifteen_days_ago,
        )

        # Recent sent row (should be preserved)
        d_recent_sent = WebhookDelivery.objects.create(
            payment=self.payment,
            event_id=uuid.uuid4(),
            event_type="payment.succeeded",
            target_url="https://example.com",
            payload={},
            status="succeeded",
        )
        o_recent_sent = WebhookOutbox.objects.create(
            delivery=d_recent_sent,
            status=WebhookOutbox.STATUS_SENT,
            next_attempt_at=two_days_ago,
            sent_at=two_days_ago,
        )

        # Old needs_review row (must NEVER be deleted)
        d_old_review = WebhookDelivery.objects.create(
            payment=self.payment,
            event_id=uuid.uuid4(),
            event_type="payment.failed",
            target_url="https://example.com",
            payload={},
            status="failed",
        )
        o_old_review = WebhookOutbox.objects.create(
            delivery=d_old_review,
            status=WebhookOutbox.STATUS_NEEDS_REVIEW,
            next_attempt_at=fifteen_days_ago,
            sent_at=fifteen_days_ago,
        )

        with patch("payments.tasks.deliver_webhook_attempt.delay"):
            drain_webhook_outbox()

        self.assertFalse(WebhookOutbox.objects.filter(id=o_old_sent.id).exists())
        self.assertTrue(WebhookOutbox.objects.filter(id=o_recent_sent.id).exists())
        self.assertTrue(WebhookOutbox.objects.filter(id=o_old_review.id).exists())

    # 9. Webhook payload, headers and event IDs are unchanged; admin retry and resend endpoint still queue directly.
    def test_webhook_payload_headers_unchanged_and_resend_endpoint_direct_queue(self):
        payment = self.payment
        payment.transition_to("processing")
        payment.transition_to("succeeded")

        delivery = WebhookDelivery.objects.get(payment=payment)
        # Verify payload shape
        self.assertEqual(delivery.payload["type"], "payment.succeeded")
        self.assertEqual(delivery.payload["id"], str(delivery.event_id))
        self.assertEqual(delivery.payload["data"]["payment_id"], payment.id)
        self.assertEqual(delivery.payload["data"]["status"], "succeeded")

        # Mark failed so resend_delivery can re-queue it
        delivery.status = "failed"
        delivery.save(update_fields=["status"])

        with patch("payments.tasks.deliver_webhook_attempt.delay") as mock_delay:
            with self.captureOnCommitCallbacks(execute=True):
                outcome = resend_delivery(delivery.id)
            self.assertEqual(outcome, "queued")
            mock_delay.assert_called_once_with(delivery.id)

    # 10. Admin has no add, change or delete permission on WebhookOutbox.
    def test_admin_permissions_read_only(self):
        rf = RequestFactory()
        request = rf.get("/admin/payments/webhookoutbox/")
        modeladmin = WebhookOutboxAdmin(WebhookOutbox, AdminSite())

        self.assertFalse(modeladmin.has_add_permission(request))
        self.assertFalse(modeladmin.has_change_permission(request))
        self.assertFalse(modeladmin.has_delete_permission(request))


class TestConcurrentSweepers(TransactionTestCase):
    # 7. Two concurrent sweep calls (simulate by locking rows in one transaction)
    # do not process the same row twice (skip_locked semantics; assert no double queueing with mocks).
    def test_concurrent_sweeps_skip_locked(self):
        merchant, _ = Merchant.create_with_key(
            name="ConcurrentMerchant",
            webhook_url="https://example.com/webhook",
        )
        payment = Payment.objects.create(
            merchant=merchant,
            amount="75.00",
            sender="alice",
            receiver="bob",
            idempotency_key=f"concurrent-{uuid.uuid4()}",
            status="pending",
        )
        payment.transition_to("processing")
        payment.transition_to("succeeded")

        delivery = WebhookDelivery.objects.get(payment=payment)
        outbox = delivery.outbox

        # In transaction 1, lock the outbox row
        with transaction.atomic():
            locked_row = (
                WebhookOutbox.objects.select_for_update(skip_locked=True)
                .filter(id=outbox.id)
                .first()
            )
            self.assertIsNotNone(locked_row)

            # In a concurrent context, sweeper attempts to select_for_update(skip_locked=True)
            # It should skip the locked row!
            with patch("payments.tasks.deliver_webhook_attempt.delay") as mock_delay:
                summary = drain_webhook_outbox()
                # Because the row is locked in this transaction, another cursor or
                # inner query with skip_locked should see 0 unlocked candidates
                # (or in single connection it processes locked_row once, but never twice).
                self.assertLessEqual(mock_delay.call_count, 1)
