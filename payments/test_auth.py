from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status

from .test_utils import create_test_merchant


class APIKeyAuthenticationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.merchant, self.api_key = create_test_merchant("Auth Test Merchant")
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

    def test_request_with_valid_api_key_succeeds(self):
        response = self.client.post(
            self.url,
            self.payload,
            format="json",
            HTTP_X_API_KEY=self.api_key
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["idempotency_key"], "auth-test-key-1")
