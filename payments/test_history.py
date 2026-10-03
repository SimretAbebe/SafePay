from django.test import TestCase
from .models import Payment, PaymentStatusHistory, InvalidStateTransition


class PaymentStatusHistoryTests(TestCase):

    def setUp(self):
        self.payment = Payment.objects.create(
            idempotency_key="history-test-key-001",
            amount=150.00,
            sender="alice",
            receiver="bob",
        )

    def test_new_payment_has_no_history_initially(self):
        """A freshly created payment starts with an empty status history."""
        self.assertEqual(self.payment.status_history.count(), 0)

    def test_status_change_creates_history_record(self):
        """A valid transition records old_status, new_status, and timestamp."""
        self.payment.transition_to("processing")

        history = self.payment.status_history.all()
        self.assertEqual(history.count(), 1)

        record = history.first()
        self.assertEqual(record.payment, self.payment)
        self.assertEqual(record.from_status, "pending")
        self.assertEqual(record.to_status, "processing")
        self.assertIsNotNone(record.changed_at)

    def test_multiple_transitions_create_ordered_history(self):
        """History entries are always ordered chronologically, oldest first."""
        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")

        history = list(self.payment.status_history.all())
        self.assertEqual(len(history), 2)

        # First transition: pending -> processing
        self.assertEqual(history[0].from_status, "pending")
        self.assertEqual(history[0].to_status, "processing")

        # Second transition: processing -> succeeded
        self.assertEqual(history[1].from_status, "processing")
        self.assertEqual(history[1].to_status, "succeeded")

        # Ordering guarantee: history[0] timestamp <= history[1] timestamp
        self.assertLessEqual(history[0].changed_at, history[1].changed_at)

    def test_invalid_transition_creates_no_history_record(self):
        """An invalid transition raises InvalidStateTransition and creates no history record."""
        with self.assertRaises(InvalidStateTransition):
            self.payment.transition_to("succeeded")  # cannot jump straight from pending to succeeded

        self.assertEqual(self.payment.status_history.count(), 0)

    def test_failed_transition_after_valid_transition_creates_no_extra_record(self):
        """If a transition fails after valid transitions, only valid transitions exist in history."""
        self.payment.transition_to("processing")
        self.assertEqual(self.payment.status_history.count(), 1)

        with self.assertRaises(InvalidStateTransition):
            self.payment.transition_to("pending")  # cannot go backwards from processing to pending

        self.assertEqual(self.payment.status_history.count(), 1)
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "processing")

    def test_history_str_representation(self):
        """PaymentStatusHistory string representation includes key and status change."""
        self.payment.transition_to("processing")
        record = self.payment.status_history.first()
        self.assertIn("history-test-key-001", str(record))
        self.assertIn("pending", str(record))
        self.assertIn("processing", str(record))
