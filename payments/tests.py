from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Payment


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