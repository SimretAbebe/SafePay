from django.db import IntegrityError, transaction
from django.test import TestCase

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