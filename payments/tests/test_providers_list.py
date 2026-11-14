"""Tests for GET /api/providers/ endpoint."""
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payments.models import Merchant, Payment, ProviderAttempt, Refund, WebhookDelivery
from payments.providers.registry import PROVIDER_REGISTRY, is_fake_provider_enabled
from payments.test_utils import create_test_merchant


class ProvidersListEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.merchant, self.raw_key = create_test_merchant("ProviderListMerchant")
        self.url = reverse("list_providers")
        # Ensure registry does not retain temporary test doubles left behind by other test suites
        for key in list(PROVIDER_REGISTRY.keys()):
            if key not in ("chapa", "arifpay", "fake"):
                PROVIDER_REGISTRY.pop(key, None)

    def tearDown(self):
        cache.clear()

    def _auth_headers(self, raw_key=None):
        return {"HTTP_X_API_KEY": raw_key or self.raw_key}

    @override_settings(ENABLE_FAKE_PROVIDER=True)
    def test_returns_200_with_expected_names_when_fake_enabled(self):
        """Returns 200 with arifpay, chapa, fake sorted by name."""
        resp = self.client.get(self.url, **self._auth_headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        data = resp.json()
        names = [item["name"] for item in data]
        self.assertEqual(names, ["arifpay", "chapa", "fake"])
        self.assertEqual(names, sorted(names))

        # Check expected attributes for each provider
        arifpay = next(p for p in data if p["name"] == "arifpay")
        self.assertEqual(arifpay["supports_refund"], False)
        self.assertEqual(arifpay["supports_cancel"], False)
        self.assertEqual(arifpay["supports_livemode"], False)

        chapa = next(p for p in data if p["name"] == "chapa")
        self.assertEqual(chapa["supports_refund"], False)
        self.assertEqual(chapa["supports_cancel"], False)
        self.assertEqual(chapa["supports_livemode"], False)

        fake = next(p for p in data if p["name"] == "fake")
        self.assertEqual(fake["supports_refund"], True)
        self.assertEqual(fake["supports_cancel"], True)
        self.assertEqual(fake["supports_livemode"], False)

    @override_settings(DEBUG=False, ENABLE_FAKE_PROVIDER=False)
    def test_excludes_fake_provider_when_disabled(self):
        """Excludes fake provider when ENABLE_FAKE_PROVIDER is off and DEBUG is False."""
        resp = self.client.get(self.url, **self._auth_headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        data = resp.json()
        names = [item["name"] for item in data]
        self.assertEqual(names, ["arifpay", "chapa"])
        self.assertNotIn("fake", names)

    def test_requires_api_key_403(self):
        """Returns 403 when no key is provided, and when an invalid key is provided."""
        # Missing key
        resp_no_key = self.client.get(self.url)
        self.assertEqual(resp_no_key.status_code, status.HTTP_403_FORBIDDEN)

        # Invalid key
        resp_bad_key = self.client.get(self.url, HTTP_X_API_KEY="sp_test_invalidkey123")
        self.assertEqual(resp_bad_key.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(ENABLE_FAKE_PROVIDER=True)
    def test_response_keys_are_exactly_the_four_listed(self):
        """Every item has exactly: name, supports_refund, supports_cancel, supports_livemode."""
        expected_keys = {"name", "supports_refund", "supports_cancel", "supports_livemode"}
        resp = self.client.get(self.url, **self._auth_headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        data = resp.json()
        self.assertGreater(len(data), 0)
        for item in data:
            self.assertEqual(set(item.keys()), expected_keys)
            self.assertIsInstance(item["name"], str)
            self.assertIsInstance(item["supports_refund"], bool)
            self.assertIsInstance(item["supports_cancel"], bool)
            self.assertIsInstance(item["supports_livemode"], bool)

    @override_settings(ENABLE_FAKE_PROVIDER=True)
    def test_no_sensitive_or_metadata_strings_in_body(self):
        """No string containing 'secret', 'key', 'http', or 'token' appears in the body."""
        resp = self.client.get(self.url, **self._auth_headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        raw_body = resp.content.decode("utf-8").lower()
        forbidden_substrings = ["secret", "key", "http", "token"]
        for forbidden in forbidden_substrings:
            self.assertNotIn(
                forbidden,
                raw_body,
                f"Forbidden substring '{forbidden}' found in response body: {raw_body}",
            )

    @override_settings(ENABLE_FAKE_PROVIDER=True)
    def test_endpoint_writes_nothing(self):
        """The endpoint is strictly read-only and writes nothing to the database."""
        # Capture counts across relevant models before request
        merchants_count = Merchant.objects.count()
        payments_count = Payment.objects.count()
        refunds_count = Refund.objects.count()
        attempts_count = ProviderAttempt.objects.count()
        deliveries_count = WebhookDelivery.objects.count()

        resp = self.client.get(self.url, **self._auth_headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        self.assertEqual(Merchant.objects.count(), merchants_count)
        self.assertEqual(Payment.objects.count(), payments_count)
        self.assertEqual(Refund.objects.count(), refunds_count)
        self.assertEqual(ProviderAttempt.objects.count(), attempts_count)
        self.assertEqual(WebhookDelivery.objects.count(), deliveries_count)

    @override_settings(ENABLE_FAKE_PROVIDER=True)
    def test_reads_throttle_applied(self):
        """PaymentReadRateThrottle is active and rate-limit headers are present."""
        resp = self.client.get(self.url, **self._auth_headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("X-RateLimit-Limit", resp.headers)
        self.assertIn("X-RateLimit-Remaining", resp.headers)
        self.assertIn("X-RateLimit-Reset", resp.headers)

