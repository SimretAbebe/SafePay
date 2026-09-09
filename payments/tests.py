from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Payment
from .models import Payment, InvalidStateTransition


class PaymentModelTests(TestCase):

    def test_creating_a_payment_works(self):
        payment = Payment.objects.create(
            idempotency_key="test-key-001",
            amount=500.00,
            sender="alice",
            receiver="bob",
        )
        self.assertEqual(payment.status, "pending")

    def test_duplicate_idempotency_key_is_rejected_by_the_database(self):
        Payment.objects.create(
            idempotency_key="duplicate-key",
            amount=100.00,
            sender="alice",
            receiver="bob",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Payment.objects.create(
                    idempotency_key="duplicate-key",
                    amount=999.00,
                    sender="charlie",
                    receiver="dave",
                )


class PaymentAPITests(TestCase):

    def setUp(self):
        self.client = APIClient()

    def test_create_payment_via_api_succeeds(self):
        response = self.client.post("/api/payments/", {
            "idempotency_key": "api-key-001",
            "amount": "250.00",
            "sender": "alice",
            "receiver": "bob",
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "pending")

    def test_create_payment_missing_amount_is_rejected(self):
        response = self.client.post("/api/payments/", {
            "idempotency_key": "api-key-002",
            "sender": "alice",
            "receiver": "bob",
        })
        self.assertEqual(response.status_code, 400)

    def test_sending_the_same_key_twice_returns_the_same_payment(self):
        payload = {
            "idempotency_key": "same-key-sent-twice",
            "amount": "500.00",
            "sender": "alice",
            "receiver": "bob",
        }

        first_response = self.client.post("/api/payments/", payload)
        self.assertEqual(first_response.status_code, 201)
        first_id = first_response.data["id"]

        second_response = self.client.post("/api/payments/", payload)
        self.assertEqual(second_response.status_code, 200)  # not 201 -- nothing new was created
        self.assertEqual(second_response.data["id"], first_id)  # same record

        # Confirm only ONE row actually exists in the database
        count = Payment.objects.filter(idempotency_key="same-key-sent-twice").count()
        self.assertEqual(count, 1)

    def test_sending_different_amounts_with_same_key_still_returns_original(self):
        first_payload = {
            "idempotency_key": "amount-mismatch-key",
            "amount": "100.00",
            "sender": "alice",
            "receiver": "bob",
        }
        self.client.post("/api/payments/", first_payload)

        second_payload = {
            "idempotency_key": "amount-mismatch-key",
            "amount": "999999.00",  # different amount, same key
            "sender": "alice",
            "receiver": "bob",
        }
        response = self.client.post("/api/payments/", second_payload)

        self.assertEqual(response.data["amount"], "100.00")  # original amount, not the new one


class PaymentStateMachineTests(TestCase):

    def setUp(self):
        self.payment = Payment.objects.create(
            idempotency_key="state-test-key",
            amount=500.00,
            sender="alice",
            receiver="bob",
        )

    def test_new_payment_starts_pending(self):
        self.assertEqual(self.payment.status, "pending")

    def test_pending_to_processing_is_allowed(self):
        self.payment.transition_to("processing")
        self.assertEqual(self.payment.status, "processing")

    def test_processing_to_succeeded_is_allowed(self):
        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")
        self.assertEqual(self.payment.status, "succeeded")

    def test_processing_to_failed_is_allowed(self):
        self.payment.transition_to("processing")
        self.payment.transition_to("failed")
        self.assertEqual(self.payment.status, "failed")

    def test_pending_directly_to_succeeded_is_blocked(self):
        """A payment can't skip straight to succeeded -- it must pass
        through processing first."""
        with self.assertRaises(InvalidStateTransition):
            self.payment.transition_to("succeeded")

    def test_succeeded_back_to_pending_is_blocked(self):
        """The real-world case this whole state machine exists to
        prevent: un-completing a payment that already delivered money."""
        self.payment.transition_to("processing")
        self.payment.transition_to("succeeded")

        with self.assertRaises(InvalidStateTransition):
            self.payment.transition_to("pending")

    def test_failed_to_processing_is_blocked(self):
        """A failed payment is terminal -- retrying requires a NEW
        payment/idempotency key, not resurrecting the old one."""
        self.payment.transition_to("processing")
        self.payment.transition_to("failed")

        with self.assertRaises(InvalidStateTransition):
            self.payment.transition_to("processing")

    def test_invalid_transition_does_not_change_the_status(self):
        """Confirm a failed transition attempt leaves the payment
        exactly as it was -- no partial or corrupted state."""
        try:
            self.payment.transition_to("succeeded")  # invalid from pending
        except InvalidStateTransition:
            pass

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, "pending")