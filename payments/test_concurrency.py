import threading

from django.db import connection
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from .models import Payment


class PaymentConcurrencyTests(TransactionTestCase):

    def _fire_identical_requests(self, payload, num_threads):
        """
        Launch num_threads threads that all send the SAME request.
        """
        status_codes = []
        barrier = threading.Barrier(num_threads)

        def worker():
            try:
                client = APIClient()  # each thread gets its own client
                barrier.wait()        # everyone starts together
                response = client.post("/api/payments/", payload, format="json")
                status_codes.append(response.status_code)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        return status_codes

    def test_same_key_sent_simultaneously_creates_exactly_one_payment(self):
        payload = {
            "idempotency_key": "concurrent-key-001",
            "amount": "500.00",
            "sender": "alice",
            "receiver": "bob",
        }

        status_codes = self._fire_identical_requests(payload, num_threads=10)

        # The database is the source of truth: exactly one row.
        row_count = Payment.objects.filter(
            idempotency_key="concurrent-key-001"
        ).count()
        self.assertEqual(row_count, 1, f"Expected 1 payment row, found {row_count}")

        # Every client must get a clean success response, either 201
        # (I created it) or 200 (it already existed). Anything else --
        # a 400 or 500 -- means the API mishandled the race.
        self.assertTrue(
            all(code in (200, 201) for code in status_codes),
            f"Unexpected status codes under concurrency: {sorted(status_codes)}",
        )

        # Exactly one request should have actually created the payment.
        self.assertEqual(
            status_codes.count(201), 1,
            f"Expected exactly one 201, got: {sorted(status_codes)}",
        )

    def test_repeated_rounds_of_concurrent_requests(self):
        for round_number in range(15):
            key = f"stress-key-{round_number}"
            payload = {
                "idempotency_key": key,
                "amount": "100.00",
                "sender": "alice",
                "receiver": "bob",
            }

            status_codes = self._fire_identical_requests(payload, num_threads=8)

            self.assertEqual(
                Payment.objects.filter(idempotency_key=key).count(), 1,
                f"Round {round_number}: wrong number of rows",
            )
            self.assertTrue(
                all(code in (200, 201) for code in status_codes),
                f"Round {round_number}: bad status codes {sorted(status_codes)}",
            )