from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from .test_utils import create_test_merchant


class PaymentThrottlingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.merchant, self.api_key = create_test_merchant("Throttle Test Merchant")
        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        self.url = reverse("create-payment")

    def tearDown(self):
        cache.clear()

    @override_settings(PAYMENT_THROTTLE_RATE="3/minute")
    def test_requests_exceeding_rate_limit_return_429(self):
        # First 3 requests within limit should succeed
        for i in range(3):
            payload = {
                "amount": "50.00",
                "sender": "alice",
                "receiver": "bob",
                "idempotency_key": f"throttle-key-{i}",
            }
            response = self.client.post(self.url, payload, format="json")
            self.assertEqual(
                response.status_code,
                status.HTTP_201_CREATED,
                f"Request {i+1} failed with status {response.status_code}",
            )

        # 4th request exceeds rate limit (3/minute) -> 429 Too Many Requests
        payload = {
            "amount": "50.00",
            "sender": "alice",
            "receiver": "bob",
            "idempotency_key": "throttle-key-exceeded",
        }
        exceeded_response = self.client.post(self.url, payload, format="json")
        self.assertEqual(
            exceeded_response.status_code,
            status.HTTP_429_TOO_MANY_REQUESTS,
        )
        self.assertIn("detail", exceeded_response.data)
        self.assertIn("Request was throttled", str(exceeded_response.data["detail"]))
