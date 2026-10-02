from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status


class APIKeyAuthenticationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("create-payment")
        self.payload = {
            "amount": "100.00",
            "sender": "alice",
            "receiver": "bob",
            "idempotency_key": "auth-test-key-1",
        }

    def test_request_without_api_key_returns_403(self):
        response = self.client.post(self.url, self.payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_request_with_invalid_api_key_returns_403(self):
        response = self.client.post(
            self.url,
            self.payload,
            format="json",
            HTTP_X_API_KEY="completely-wrong-key"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(SAFE_PAY_API_KEY="test-secret-key-12345")
    def test_request_with_valid_api_key_succeeds(self):
        response = self.client.post(
            self.url,
            self.payload,
            format="json",
            HTTP_X_API_KEY="test-secret-key-12345"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["idempotency_key"], "auth-test-key-1")
