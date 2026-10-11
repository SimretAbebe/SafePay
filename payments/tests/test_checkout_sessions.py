from decimal import Decimal
from unittest.mock import patch
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payments.models import Payment, PaymentStatusHistory
from payments.providers import FakeProvider, ProviderError, get_provider
from payments.test_utils import create_test_merchant


@override_settings(ENABLE_FAKE_PROVIDER=True, DEFAULT_PROVIDER="fake")
class CheckoutSessionTests(TestCase):
    def setUp(self):
        cache.clear()
        FakeProvider.reset()
        self.client = APIClient()
        self.merchant_a, self.key_a = create_test_merchant("Merchant A")
        self.merchant_b, self.key_b = create_test_merchant("Merchant B")
        self.url = reverse("create_checkout_session")

    def tearDown(self):
        cache.clear()
        FakeProvider.reset()

    # 1. A valid request returns 201, creates a Payment owned by the merchant with provider,
    #    provider_reference and checkout_url saved, status still "pending", and the response
    #    contains checkout_url but not provider_reference.
    def test_valid_request_creates_checkout_session(self):
        payload = {
            "amount": "150.00",
            "reference": "ORDER-1001",
            "return_url": "https://example.com/checkout/complete",
            "provider": "fake",
        }
        response = self.client.post(
            self.url,
            payload,
            format="json",
            HTTP_X_API_KEY=self.key_a,
            HTTP_IDEMPOTENCY_KEY="valid-session-key-1",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data

        self.assertIn("id", data)
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["provider"], "fake")
        self.assertEqual(data["reference"], "ORDER-1001")
        self.assertTrue(data["checkout_url"].startswith("https://fake.local/pay/"))
        self.assertIn("created_at", data)
        self.assertNotIn("provider_reference", data)

        payment = Payment.objects.get(pk=data["id"])
        self.assertEqual(payment.merchant, self.merchant_a)
        self.assertEqual(payment.status, "pending")
        self.assertEqual(payment.provider, "fake")
        self.assertTrue(payment.provider_reference.startswith("fake_"))
        self.assertEqual(payment.checkout_url, data["checkout_url"])
        self.assertEqual(payment.merchant_reference, "ORDER-1001")
        self.assertEqual(payment.return_url, "https://example.com/checkout/complete")

    # 2. Missing Idempotency-Key header -> 400; empty -> 400.
    def test_missing_or_empty_idempotency_key_header(self):
        payload = {"amount": "100.00", "provider": "fake"}

        # Missing header
        res_missing = self.client.post(
            self.url,
            payload,
            format="json",
            HTTP_X_API_KEY=self.key_a,
        )
        self.assertEqual(res_missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Idempotency-Key", str(res_missing.data))

        # Empty header
        res_empty = self.client.post(
            self.url,
            payload,
            format="json",
            HTTP_X_API_KEY=self.key_a,
            HTTP_IDEMPOTENCY_KEY="   ",
        )
        self.assertEqual(res_empty.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Idempotency-Key", str(res_empty.data))

    # 3. The same Idempotency-Key sent twice returns 200 with the same id and checkout_url,
    #    exactly one Payment row exists, and the provider's initialize ran exactly once.
    def test_same_idempotency_key_returns_200_and_runs_initialize_once(self):
        payload = {"amount": "200.00", "reference": "INV-2", "provider": "fake"}
        key = "idemp-duplicate-test-key"

        with patch.object(FakeProvider, "initialize", wraps=FakeProvider().initialize) as mock_init:
            res1 = self.client.post(
                self.url,
                payload,
                format="json",
                HTTP_X_API_KEY=self.key_a,
                HTTP_IDEMPOTENCY_KEY=key,
            )
            self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

            res2 = self.client.post(
                self.url,
                payload,
                format="json",
                HTTP_X_API_KEY=self.key_a,
                HTTP_IDEMPOTENCY_KEY=key,
            )
            self.assertEqual(res2.status_code, status.HTTP_200_OK)

            self.assertEqual(res1.data["id"], res2.data["id"])
            self.assertEqual(res1.data["checkout_url"], res2.data["checkout_url"])
            self.assertEqual(res2.data["status"], "pending")
            self.assertEqual(res2.data["reference"], "INV-2")

            # Provider initialized exactly once
            self.assertEqual(mock_init.call_count, 1)

            # Exactly one Payment row exists
            self.assertEqual(
                Payment.objects.filter(merchant=self.merchant_a, idempotency_key=key).count(),
                1,
            )

    # 4. The same Idempotency-Key used by two different merchants creates two separate payments.
    def test_same_idempotency_key_different_merchants(self):
        shared_key = "shared-idempotency-key"
        payload_a = {"amount": "50.00", "provider": "fake"}
        payload_b = {"amount": "75.00", "provider": "fake"}

        res_a = self.client.post(
            self.url,
            payload_a,
            format="json",
            HTTP_X_API_KEY=self.key_a,
            HTTP_IDEMPOTENCY_KEY=shared_key,
        )
        self.assertEqual(res_a.status_code, status.HTTP_201_CREATED)

        res_b = self.client.post(
            self.url,
            payload_b,
            format="json",
            HTTP_X_API_KEY=self.key_b,
            HTTP_IDEMPOTENCY_KEY=shared_key,
        )
        self.assertEqual(res_b.status_code, status.HTTP_201_CREATED)

        self.assertNotEqual(res_a.data["id"], res_b.data["id"])

        pay_a = Payment.objects.get(pk=res_a.data["id"])
        pay_b = Payment.objects.get(pk=res_b.data["id"])
        self.assertEqual(pay_a.merchant, self.merchant_a)
        self.assertEqual(pay_b.merchant, self.merchant_b)
        self.assertEqual(pay_a.idempotency_key, shared_key)
        self.assertEqual(pay_b.idempotency_key, shared_key)

    # 5. Missing or wrong API key -> 403.
    def test_missing_or_wrong_api_key(self):
        payload = {"amount": "100.00", "provider": "fake"}

        # Missing API Key
        res_missing = self.client.post(
            self.url,
            payload,
            format="json",
            HTTP_IDEMPOTENCY_KEY="auth-key-test-1",
        )
        self.assertEqual(res_missing.status_code, status.HTTP_403_FORBIDDEN)

        # Wrong API Key
        res_wrong = self.client.post(
            self.url,
            payload,
            format="json",
            HTTP_X_API_KEY="invalid-merchant-api-key",
            HTTP_IDEMPOTENCY_KEY="auth-key-test-2",
        )
        self.assertEqual(res_wrong.status_code, status.HTTP_403_FORBIDDEN)

    # 6. Invalid amount (0, negative, 3 decimals, not a number), invalid return_url,
    #    and unknown provider -> 400, and no Payment row is created for a 400.
    def test_invalid_inputs_return_400_and_no_payment_created(self):
        initial_count = Payment.objects.count()

        invalid_cases = [
            {"amount": "0.00", "provider": "fake"},
            {"amount": "-10.00", "provider": "fake"},
            {"amount": "10.123", "provider": "fake"},
            {"amount": "not-a-number", "provider": "fake"},
            {"amount": "10.00", "return_url": "ftp://example.com/bad", "provider": "fake"},
            {"amount": "10.00", "return_url": "invalid-url", "provider": "fake"},
            {"amount": "10.00", "provider": "nonexistent_provider"},
        ]

        for i, payload in enumerate(invalid_cases):
            with self.subTest(case=i, payload=payload):
                res = self.client.post(
                    self.url,
                    payload,
                    format="json",
                    HTTP_X_API_KEY=self.key_a,
                    HTTP_IDEMPOTENCY_KEY=f"invalid-input-case-{i}",
                )
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        # Assert no Payment row is created for a 400
        self.assertEqual(Payment.objects.count(), initial_count)

    # 7. Provider failure: with a provider that raises ProviderError, the response is 502,
    #    the payment stays pending with no provider_reference/checkout_url, the response body
    #    contains no provider error text; then a retry with the same key and a working provider
    #    returns 201 and calls the provider again.
    def test_provider_failure_returns_502_and_retry_succeeds(self):
        payload = {"amount": "120.00", "provider": "fake"}
        key = "fail-and-retry-key"
        provider_secret_error = "Provider internal failure: SECRET_CHAPA_KEY_XYZ"

        with patch.object(
            FakeProvider,
            "initialize",
            side_effect=ProviderError(provider_secret_error),
        ) as mock_fail:
            res_fail = self.client.post(
                self.url,
                payload,
                format="json",
                HTTP_X_API_KEY=self.key_a,
                HTTP_IDEMPOTENCY_KEY=key,
            )
            self.assertEqual(res_fail.status_code, status.HTTP_502_BAD_GATEWAY)
            self.assertEqual(
                res_fail.data.get("detail"),
                "Payment provider unavailable. Retry with the same Idempotency-Key.",
            )
            # Response body must not contain provider error text
            self.assertNotIn("SECRET_CHAPA_KEY_XYZ", str(res_fail.content))
            self.assertNotIn("Provider internal failure", str(res_fail.content))

            # Payment exists, pending, with no provider_reference / checkout_url
            payment = Payment.objects.get(merchant=self.merchant_a, idempotency_key=key)
            self.assertEqual(payment.status, "pending")
            self.assertFalse(payment.provider_reference)
            self.assertFalse(payment.checkout_url)

        # Retry with a working provider
        res_retry = self.client.post(
            self.url,
            payload,
            format="json",
            HTTP_X_API_KEY=self.key_a,
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(res_retry.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res_retry.data["checkout_url"].startswith("https://fake.local/pay/"))

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertTrue(payment.provider_reference.startswith("fake_"))
        self.assertEqual(payment.checkout_url, res_retry.data["checkout_url"])

    # 8. Customer fields reach the provider's initialize but are not stored on the Payment
    #    (assert no email is saved anywhere on the model).
    def test_customer_fields_reach_provider_not_stored_on_payment(self):
        payload = {
            "amount": "80.00",
            "customer_email": "customer@example.com",
            "customer_first_name": "Abebe",
            "customer_last_name": "Bikila",
            "provider": "fake",
        }
        res = self.client.post(
            self.url,
            payload,
            format="json",
            HTTP_X_API_KEY=self.key_a,
            HTTP_IDEMPOTENCY_KEY="customer-data-key-1",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        payment = Payment.objects.get(pk=res.data["id"])

        # Customer fields reach the provider's initialize
        provider_record = FakeProvider._store.get(payment.provider_reference)
        self.assertIsNotNone(provider_record)
        self.assertIsNotNone(provider_record.get("customer"))
        self.assertEqual(provider_record["customer"]["email"], "customer@example.com")
        self.assertEqual(provider_record["customer"]["first_name"], "Abebe")
        self.assertEqual(provider_record["customer"]["last_name"], "Bikila")

        # Assert no email field is on the Payment model schema
        model_field_names = [f.name for f in payment._meta.fields]
        self.assertNotIn("email", model_field_names)
        self.assertNotIn("customer_email", model_field_names)
        self.assertNotIn("customer_first_name", model_field_names)
        self.assertNotIn("customer_last_name", model_field_names)
        self.assertFalse(hasattr(payment, "email"))
        self.assertFalse(hasattr(payment, "customer_email"))

    # 9. The throttle applies per merchant (clear the cache in setUp).
    def test_throttle_applies_per_merchant(self):
        cache.clear()
        with override_settings(PAYMENT_THROTTLE_RATE="2/minute"):
            # Recreate client to reload throttle rate
            client = APIClient()

            # Merchant A request 1
            res1 = client.post(
                self.url,
                {"amount": "10.00", "provider": "fake"},
                format="json",
                HTTP_X_API_KEY=self.key_a,
                HTTP_IDEMPOTENCY_KEY="throttle-a-1",
            )
            self.assertEqual(res1.status_code, status.HTTP_201_CREATED)

            # Merchant A request 2
            res2 = client.post(
                self.url,
                {"amount": "10.00", "provider": "fake"},
                format="json",
                HTTP_X_API_KEY=self.key_a,
                HTTP_IDEMPOTENCY_KEY="throttle-a-2",
            )
            self.assertEqual(res2.status_code, status.HTTP_201_CREATED)

            # Merchant A request 3 -> throttled (429)
            res3 = client.post(
                self.url,
                {"amount": "10.00", "provider": "fake"},
                format="json",
                HTTP_X_API_KEY=self.key_a,
                HTTP_IDEMPOTENCY_KEY="throttle-a-3",
            )
            self.assertEqual(res3.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

            # Merchant B is NOT throttled (throttle applies per merchant)
            res_b = client.post(
                self.url,
                {"amount": "10.00", "provider": "fake"},
                format="json",
                HTTP_X_API_KEY=self.key_b,
                HTTP_IDEMPOTENCY_KEY="throttle-b-1",
            )
            self.assertEqual(res_b.status_code, status.HTTP_201_CREATED)

    # 10. cancel_payment on a payment with a provider returns 409 and changes nothing
    #     (no history row); on a payment without a provider, a pending cancel still works as before.
    def test_cancel_payment_guard_for_provider_payments(self):
        # Case A: Payment with provider
        res_provider = self.client.post(
            self.url,
            {"amount": "100.00", "provider": "fake"},
            format="json",
            HTTP_X_API_KEY=self.key_a,
            HTTP_IDEMPOTENCY_KEY="cancel-guard-provider-pay",
        )
        self.assertEqual(res_provider.status_code, status.HTTP_201_CREATED)
        provider_pay_id = res_provider.data["id"]

        cancel_url_provider = reverse("cancel_payment", kwargs={"pk": provider_pay_id})
        res_cancel = self.client.post(
            cancel_url_provider,
            HTTP_X_API_KEY=self.key_a,
        )
        self.assertEqual(res_cancel.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            res_cancel.data.get("detail"),
            "Cancelling provider payments is not supported yet.",
        )

        # Changes nothing: status still pending, no history rows created
        pay_after = Payment.objects.get(pk=provider_pay_id)
        self.assertEqual(pay_after.status, "pending")
        self.assertEqual(PaymentStatusHistory.objects.filter(payment=pay_after).count(), 0)

        # Case B: Payment without provider (legacy/direct flow)
        res_legacy = self.client.post(
            reverse("create-payment"),
            {
                "amount": "50.00",
                "sender": "alice",
                "receiver": "bob",
                "idempotency_key": "legacy-payment-key-1",
            },
            format="json",
            HTTP_X_API_KEY=self.key_a,
        )
        self.assertEqual(res_legacy.status_code, status.HTTP_201_CREATED)
        legacy_pay_id = res_legacy.data["id"]

        cancel_url_legacy = reverse("cancel_payment", kwargs={"pk": legacy_pay_id})
        res_cancel_legacy = self.client.post(
            cancel_url_legacy,
            HTTP_X_API_KEY=self.key_a,
        )
        self.assertEqual(res_cancel_legacy.status_code, status.HTTP_200_OK)
        self.assertEqual(res_cancel_legacy.data.get("status"), "cancelled")

        legacy_pay_after = Payment.objects.get(pk=legacy_pay_id)
        self.assertEqual(legacy_pay_after.status, "cancelled")
        self.assertEqual(
            PaymentStatusHistory.objects.filter(payment=legacy_pay_after, to_status="cancelled").count(),
            1,
        )

    # 11. Another merchant cannot read this session's payment via payment_detail (404).
    def test_merchant_isolation_payment_detail(self):
        res = self.client.post(
            self.url,
            {"amount": "99.00", "provider": "fake"},
            format="json",
            HTTP_X_API_KEY=self.key_a,
            HTTP_IDEMPOTENCY_KEY="isolation-test-key",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        pay_id = res.data["id"]

        detail_url = reverse("payment_detail", kwargs={"pk": pay_id})

        # Merchant A can read
        res_a = self.client.get(detail_url, HTTP_X_API_KEY=self.key_a)
        self.assertEqual(res_a.status_code, status.HTTP_200_OK)

        # Merchant B cannot read -> 404
        res_b = self.client.get(detail_url, HTTP_X_API_KEY=self.key_b)
        self.assertEqual(res_b.status_code, status.HTTP_404_NOT_FOUND)
