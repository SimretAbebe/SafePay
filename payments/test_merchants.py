from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from .models import Merchant, Payment
from .test_utils import create_test_merchant


class MerchantAccountTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.merchant_a, self.key_a = create_test_merchant("Merchant A")
        self.merchant_b, self.key_b = create_test_merchant("Merchant B")

    def tearDown(self):
        cache.clear()

    def test_valid_merchant_key_creates_payment_belonging_to_merchant(self):
        """1. A valid merchant key can create a payment and the payment belongs to that merchant."""
        response = self.client.post(
            reverse("create-payment"),
            {
                "amount": "150.00",
                "sender": "alice",
                "receiver": "bob",
                "idempotency_key": "merchant-a-key-1",
            },
            format="json",
            HTTP_X_API_KEY=self.key_a,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        payment = Payment.objects.get(idempotency_key="merchant-a-key-1")
        self.assertEqual(payment.merchant, self.merchant_a)

    def test_missing_wrong_or_inactive_key_returns_403(self):
        """2. Missing key returns 403; wrong key returns 403; inactive merchant's key returns 403."""
        payload = {
            "amount": "50.00",
            "sender": "alice",
            "receiver": "bob",
            "idempotency_key": "auth-check-key",
        }
        url = reverse("create-payment")

        # Missing key -> 403
        resp_missing = self.client.post(url, payload, format="json")
        self.assertEqual(resp_missing.status_code, status.HTTP_403_FORBIDDEN)

        # Wrong key -> 403
        resp_wrong = self.client.post(
            url, payload, format="json", HTTP_X_API_KEY="totally-invalid-key"
        )
        self.assertEqual(resp_wrong.status_code, status.HTTP_403_FORBIDDEN)

        # Inactive merchant key -> 403
        inactive_merchant, inactive_key = create_test_merchant(
            "Inactive Merchant", is_active=False
        )
        resp_inactive = self.client.post(
            url, payload, format="json", HTTP_X_API_KEY=inactive_key
        )
        self.assertEqual(resp_inactive.status_code, status.HTTP_403_FORBIDDEN)

    def test_raw_key_is_not_stored_in_database(self):
        """3. Raw key is not stored: the database only has the hash."""
        merchant, raw_key = create_test_merchant("Secure Merchant")
        merchant.refresh_from_db()

        self.assertNotEqual(merchant.api_key_hash, raw_key)
        self.assertEqual(merchant.api_key_hash, Merchant.hash_key(raw_key))
        self.assertNotIn(raw_key, merchant.api_key_hash)
        self.assertEqual(merchant.key_prefix, raw_key[:8])

    def test_merchant_b_cannot_get_merchant_a_payment(self):
        """4. Merchant B cannot GET merchant A's payment (404)."""
        create_resp = self.client.post(
            reverse("create-payment"),
            {
                "amount": "300.00",
                "sender": "alice",
                "receiver": "bob",
                "idempotency_key": "isolation-test-key",
            },
            format="json",
            HTTP_X_API_KEY=self.key_a,
        )
        self.assertEqual(create_resp.status_code, status.HTTP_201_CREATED)
        payment_id = create_resp.data["id"]

        detail_url = reverse("payment_detail", kwargs={"pk": payment_id})

        # Merchant B tries to GET merchant A's payment -> 404 (not 403)
        resp_b = self.client.get(detail_url, HTTP_X_API_KEY=self.key_b)
        self.assertEqual(resp_b.status_code, status.HTTP_404_NOT_FOUND)

        # Merchant A can GET its own payment -> 200
        resp_a = self.client.get(detail_url, HTTP_X_API_KEY=self.key_a)
        self.assertEqual(resp_a.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_a.data["id"], payment_id)

    def test_same_idempotency_key_used_by_different_merchants_creates_separate_payments(self):
        """
        5. Same idempotency_key used by merchant A and merchant B creates
        two separate payments (201 each), and each sees only its own.
        """
        shared_key = "shared-idempotency-key"

        # Merchant A creates payment
        resp_a = self.client.post(
            reverse("create-payment"),
            {
                "amount": "100.00",
                "sender": "alice",
                "receiver": "bob",
                "idempotency_key": shared_key,
            },
            format="json",
            HTTP_X_API_KEY=self.key_a,
        )
        self.assertEqual(resp_a.status_code, status.HTTP_201_CREATED)
        payment_a_id = resp_a.data["id"]

        # Merchant B creates payment with the same idempotency key
        resp_b = self.client.post(
            reverse("create-payment"),
            {
                "amount": "200.00",
                "sender": "charlie",
                "receiver": "dave",
                "idempotency_key": shared_key,
            },
            format="json",
            HTTP_X_API_KEY=self.key_b,
        )
        self.assertEqual(resp_b.status_code, status.HTTP_201_CREATED)
        payment_b_id = resp_b.data["id"]

        # Different database records
        self.assertNotEqual(payment_a_id, payment_b_id)
        self.assertEqual(Payment.objects.filter(idempotency_key=shared_key).count(), 2)

        # Each merchant sees only its own payment
        detail_a = self.client.get(
            reverse("payment_detail", kwargs={"pk": payment_a_id}),
            HTTP_X_API_KEY=self.key_a,
        )
        self.assertEqual(detail_a.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_a.data["amount"], "100.00")

        detail_b = self.client.get(
            reverse("payment_detail", kwargs={"pk": payment_b_id}),
            HTTP_X_API_KEY=self.key_b,
        )
        self.assertEqual(detail_b.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_b.data["amount"], "200.00")

        # Cross-access is 404
        self.assertEqual(
            self.client.get(
                reverse("payment_detail", kwargs={"pk": payment_b_id}),
                HTTP_X_API_KEY=self.key_a,
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            self.client.get(
                reverse("payment_detail", kwargs={"pk": payment_a_id}),
                HTTP_X_API_KEY=self.key_b,
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_same_idempotency_key_sent_twice_by_same_merchant_returns_original(self):
        """6. Same idempotency_key sent twice by the same merchant returns the original payment."""
        payload = {
            "amount": "75.00",
            "sender": "alice",
            "receiver": "bob",
            "idempotency_key": "repeat-key-merchant-a",
        }
        url = reverse("create-payment")

        first_resp = self.client.post(url, payload, format="json", HTTP_X_API_KEY=self.key_a)
        self.assertEqual(first_resp.status_code, status.HTTP_201_CREATED)
        first_id = first_resp.data["id"]

        second_resp = self.client.post(url, payload, format="json", HTTP_X_API_KEY=self.key_a)
        self.assertEqual(second_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(second_resp.data["id"], first_id)

    @override_settings(PAYMENT_THROTTLE_RATE="2/minute")
    def test_throttle_counts_per_merchant_isolated(self):
        """7. Throttle counts per merchant: merchant A hitting the limit does not block merchant B."""
        url = reverse("create-payment")

        # Merchant A sends 2 requests (reaches limit)
        for i in range(2):
            resp = self.client.post(
                url,
                {
                    "amount": "10.00",
                    "sender": "alice",
                    "receiver": "bob",
                    "idempotency_key": f"throttle-a-{i}",
                },
                format="json",
                HTTP_X_API_KEY=self.key_a,
            )
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        # Merchant A 3rd request -> 429
        resp_a_blocked = self.client.post(
            url,
            {
                "amount": "10.00",
                "sender": "alice",
                "receiver": "bob",
                "idempotency_key": "throttle-a-exceeded",
            },
            format="json",
            HTTP_X_API_KEY=self.key_a,
        )
        self.assertEqual(resp_a_blocked.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

        # Merchant B is NOT throttled -> 201
        resp_b = self.client.post(
            url,
            {
                "amount": "10.00",
                "sender": "charlie",
                "receiver": "dave",
                "idempotency_key": "throttle-b-allowed",
            },
            format="json",
            HTTP_X_API_KEY=self.key_b,
        )
        self.assertEqual(resp_b.status_code, status.HTTP_201_CREATED)
