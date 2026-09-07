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
        self.assertEqual(payment.status, "pending")  # default value

    def test_duplicate_idempotency_key_is_rejected_by_the_database(self):
        """
        This is the actual proof the unique constraint works -- not just
        that the model is defined correctly, but that PostgreSQL itself
        refuses a second row with the same idempotency_key.
        """
        Payment.objects.create(
            idempotency_key="duplicate-key",
            amount=100.00,
            sender="alice",
            receiver="bob",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Payment.objects.create(
                    idempotency_key="duplicate-key",  # same key again
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
 
    def test_sending_the_same_key_twice_currently_creates_two_payments(self):
        payload = {
            "idempotency_key": "same-key-sent-twice",
            "amount": "500.00",
            "sender": "alice",
            "receiver": "bob",
        }
 
        first_response = self.client.post("/api/payments/", payload)
        self.assertEqual(first_response.status_code, 201)
 
        second_response = self.client.post("/api/payments/", payload)
        self.assertNotEqual(second_response.status_code, 201)
 