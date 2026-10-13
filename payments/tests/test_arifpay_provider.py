import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.core.cache import cache
from rest_framework.test import APIClient

from payments.models import InboundWebhookEvent, Payment
from payments.providers import (
    ArifPayProvider,
    InitResult,
    ProviderError,
    ProviderStatusResult,
    UnsupportedProviderStatus,
    get_provider,
)
from payments.test_utils import create_test_merchant


class DummyPayment:
    def __init__(
        self,
        amount=Decimal("150.00"),
        customer_email="customer@example.com",
        customer_phone="+251911223344",
        item_name="Order 123",
    ):
        self.amount = amount
        self.customer_email = customer_email
        self.customer_phone = customer_phone
        self.item_name = item_name


# ---------------------------------------------------------------------------
# Provider configuration tests
# ---------------------------------------------------------------------------

@override_settings(
    ARIFPAY_API_KEY="test-arifpay-merchant-key",
    ARIFPAY_SANDBOX=True,
    ARIFPAY_BASE_URL="https://gateway.arifpay.org/api",
    ARIFPAY_DEFAULT_CURRENCY="ETB",
    ARIFPAY_REQUEST_TIMEOUT=10,
    ARIFPAY_SUCCESS_URL="https://example.com/success",
    ARIFPAY_ERROR_URL="https://example.com/error",
    ARIFPAY_CANCEL_URL="https://example.com/cancel",
    ARIFPAY_NOTIFY_URL="https://example.com/notify",
)
class ArifPayProviderConfigTests(SimpleTestCase):
    def setUp(self):
        self.provider = ArifPayProvider()

    def test_get_provider_registry(self):
        provider = get_provider("arifpay")
        self.assertIsInstance(provider, ArifPayProvider)
        self.assertEqual(provider.name, "arifpay")

    @override_settings(ARIFPAY_API_KEY="")
    def test_missing_api_key_raises_provider_error(self):
        with self.assertRaises(ProviderError) as ctx:
            self.provider.initialize(DummyPayment())
        self.assertIn("not configured", str(ctx.exception))

    @override_settings(ARIFPAY_API_KEY="arif-sandbox-portal-key")
    def test_portal_key_rejected_for_hosted_checkout(self):
        """Portal simulator key must not be accepted for hosted checkout initialize()."""
        with self.assertRaises(ProviderError) as ctx:
            self.provider.initialize(DummyPayment())
        err = str(ctx.exception)
        self.assertIn("arif-sandbox-portal-key", err)
        self.assertIn("hosted checkout", err)
        self.assertIn("developer.arifpay.net", err)

    @override_settings(ARIFPAY_SANDBOX=True, ARIFPAY_BASE_URL="")
    def test_sandbox_default_base_url(self):
        config = self.provider._get_config()
        self.assertEqual(config["base_url"], "https://gateway.arifpay.org/api")
        self.assertTrue(config["sandbox"])

    @override_settings(ARIFPAY_SANDBOX=False, ARIFPAY_BASE_URL="")
    def test_production_default_base_url(self):
        config = self.provider._get_config()
        self.assertEqual(config["base_url"], "https://api.arifpay.org/api")
        self.assertFalse(config["sandbox"])

    @override_settings(ARIFPAY_BASE_URL="https://custom-gateway.arifpay.org")
    def test_custom_base_url_normalized_with_api(self):
        config = self.provider._get_config()
        self.assertEqual(config["base_url"], "https://custom-gateway.arifpay.org/api")


# ---------------------------------------------------------------------------
# Hosted checkout (initialize / verify / cancel)
# ---------------------------------------------------------------------------

@override_settings(
    ARIFPAY_API_KEY="test-arifpay-merchant-key",
    ARIFPAY_SANDBOX=True,
    ARIFPAY_BASE_URL="https://gateway.arifpay.org/api",
    ARIFPAY_DEFAULT_CURRENCY="ETB",
    ARIFPAY_REQUEST_TIMEOUT=10,
    ARIFPAY_SUCCESS_URL="https://example.com/success",
    ARIFPAY_ERROR_URL="https://example.com/error",
    ARIFPAY_CANCEL_URL="https://example.com/cancel",
    ARIFPAY_NOTIFY_URL="https://example.com/notify",
)
class ArifPayHostedCheckoutTests(SimpleTestCase):
    def setUp(self):
        self.provider = ArifPayProvider()

    @patch("payments.providers.arifpay.requests.post")
    def test_initialize_request_construction_and_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "error": False,
            "msg": "success",
            "data": {
                "sessionId": "sess-uuid-12345",
                "paymentUrl": "https://gateway.arifpay.org/checkout/sess-uuid-12345",
            },
        }
        mock_post.return_value = mock_response

        payment = DummyPayment(amount=Decimal("200.00"))
        result = self.provider.initialize(payment)

        self.assertIsInstance(result, InitResult)
        self.assertEqual(result.provider_reference, "sess-uuid-12345")
        self.assertEqual(result.checkout_url, "https://gateway.arifpay.org/checkout/sess-uuid-12345")

        _, call_kwargs = mock_post.call_args
        sent_headers = call_kwargs["headers"]
        self.assertEqual(sent_headers["x-arifpay-key"], "test-arifpay-merchant-key")
        self.assertEqual(sent_headers["Content-Type"], "application/json")

        sent_payload = call_kwargs["json"]
        self.assertEqual(sent_payload["items"][0]["price"], 200.0)
        self.assertEqual(sent_payload["items"][0]["name"], "Order 123")
        self.assertEqual(sent_payload["currency"], "ETB")
        self.assertEqual(sent_payload["email"], "customer@example.com")
        self.assertEqual(sent_payload["successUrl"], "https://example.com/success")
        self.assertIn("expireDate", sent_payload)
        self.assertTrue(sent_payload["nonce"].startswith("sp-"))

    @patch("payments.providers.arifpay.requests.post")
    def test_initialize_test_api_key_error_gives_helpful_message(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"error": True, "msg": "Test API Key found"}
        mock_post.return_value = mock_response

        with self.assertRaises(ProviderError) as ctx:
            self.provider.initialize(DummyPayment())
        err = str(ctx.exception)
        self.assertIn("Test API Key found", err)
        self.assertIn("developer.arifpay.net", err)
        # API key must be redacted in the error message
        self.assertNotIn("test-arifpay-merchant-key", err)

    @patch("payments.providers.arifpay.requests.get")
    def test_verify_success_maps_to_succeeded(self, mock_get):
        ref = "sess-uuid-12345"
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "error": False,
                "data": {"sessionId": ref, "transaction": {"transactionStatus": "SUCCESS"}},
            },
        )
        result = self.provider.verify(ref)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.provider_reference, ref)

    @patch("payments.providers.arifpay.requests.get")
    def test_verify_all_statuses(self, mock_get):
        for native, expected in [
            ("PENDING", "pending"),
            ("FAILED", "failed"),
            ("CANCELLED", "cancelled"),
            ("CANCELED", "cancelled"),
            ("EXPIRED", "expired"),
        ]:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda s=native: {
                    "error": False,
                    "data": {"transaction": {"transactionStatus": s}},
                },
            )
            result = self.provider.verify("sess-abc")
            self.assertEqual(result.status, expected, f"Expected '{expected}' for native '{native}'")


# ---------------------------------------------------------------------------
# Sandbox PSS direct transfer
# ---------------------------------------------------------------------------

@override_settings(
    ARIFPAY_API_KEY="arif-sandbox-portal-key",
    ARIFPAY_SANDBOX=True,
    ARIFPAY_DEFAULT_CURRENCY="ETB",
    ARIFPAY_REQUEST_TIMEOUT=10,
    ARIFPAY_SUCCESS_URL="https://example.com/success",
    ARIFPAY_ERROR_URL="https://example.com/error",
    ARIFPAY_CANCEL_URL="https://example.com/cancel",
    ARIFPAY_NOTIFY_URL="https://example.com/notify",
)
class ArifPaySandboxPSSTests(SimpleTestCase):
    def setUp(self):
        self.provider = ArifPayProvider()

    @patch("payments.providers.arifpay.requests.post")
    def test_sandbox_pss_transfer_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "error": False,
            "msg": "Sandbox payment successful",
            "data": {
                "transaction": {
                    "transactionStatus": "SUCCESS",
                    "checkoutSession": {"uuid": "SIM-SESSION-ABC"},
                },
                "sessionId": "SIM-SESSION-ABC",
            },
        }
        mock_post.return_value = mock_response

        result = self.provider.sandbox_pss_transfer(DummyPayment())

        self.assertIsInstance(result, ProviderStatusResult)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.provider_reference, "SIM-SESSION-ABC")

        call_args, call_kwargs = mock_post.call_args
        self.assertIn("gateway.arifpay.org/api/sandbox/c2b/pss/direct/transfer", call_args[0])
        sent_headers = call_kwargs["headers"]
        self.assertEqual(sent_headers["x-arifpay-key"], "arif-sandbox-portal-key")
        self.assertEqual(sent_headers["x-arifpay-sim-mode"], "SUCCESS")
        self.assertEqual(sent_headers["x-arifpay-fee-source"], "CUSTOMER")

    @patch("payments.providers.arifpay.requests.post")
    def test_sandbox_pss_fail_sim_mode(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "error": False,
            "msg": "Sandbox payment failed",
            "data": {
                "transaction": {
                    "transactionStatus": "FAILED",
                    "checkoutSession": {"uuid": "SIM-FAIL-ABC"},
                },
                "sessionId": "SIM-FAIL-ABC",
            },
        }
        mock_post.return_value = mock_response

        result = self.provider.sandbox_pss_transfer(DummyPayment(), sim_mode="FAIL")

        self.assertEqual(result.status, "failed")
        _, call_kwargs = mock_post.call_args
        self.assertEqual(call_kwargs["headers"]["x-arifpay-sim-mode"], "FAIL")

    @override_settings(ARIFPAY_SANDBOX=False)
    def test_sandbox_pss_rejected_in_production(self):
        with self.assertRaises(ProviderError) as ctx:
            self.provider.sandbox_pss_transfer(DummyPayment())
        self.assertIn("ARIFPAY_SANDBOX=True", str(ctx.exception))

    def test_portal_key_allowed_for_pss_not_checkout(self):
        """Portal key is accepted for PSS but rejected for initialize()."""
        # initialize() must raise
        with self.assertRaises(ProviderError) as ctx:
            self.provider.initialize(DummyPayment())
        self.assertIn("portal simulator key", str(ctx.exception))


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

@override_settings(ARIFPAY_API_KEY="test-key", ARIFPAY_SANDBOX=True)
class ArifPayStatusMappingTests(SimpleTestCase):
    def setUp(self):
        self.provider = ArifPayProvider()

    def test_status_mapping(self):
        cases = [
            ("SUCCESS", "succeeded"),
            ("PENDING", "pending"),
            ("FAILED", "failed"),
            ("CANCELLED", "cancelled"),
            ("CANCELED", "cancelled"),
            ("EXPIRED", "expired"),
        ]
        for native, expected in cases:
            with self.subTest(native=native):
                self.assertEqual(self.provider.map_status(native), expected)

    def test_unknown_status_raises(self):
        with self.assertRaises(UnsupportedProviderStatus):
            self.provider.map_status("REFUNDED")

    def test_empty_status_raises(self):
        with self.assertRaises(UnsupportedProviderStatus):
            self.provider.map_status("")


# ---------------------------------------------------------------------------
# Webhook parsing
# ---------------------------------------------------------------------------

@override_settings(ARIFPAY_API_KEY="test-key", ARIFPAY_SANDBOX=True)
class ArifPayWebhookParsingTests(SimpleTestCase):
    def setUp(self):
        self.provider = ArifPayProvider()

    def test_parse_flat_webhook(self):
        body = json.dumps({
            "sessionId": "sess-hook-99",
            "transaction": {"transactionStatus": "SUCCESS"},
        }).encode()
        event = self.provider.parse_webhook({}, body)
        self.assertEqual(event.provider_reference, "sess-hook-99")
        self.assertEqual(event.status, "succeeded")

    def test_parse_nested_sandbox_webhook(self):
        """Mirrors the actual ArifPay sandbox PSS webhook payload."""
        body = json.dumps({
            "error": False,
            "msg": "Sandbox payment successful",
            "data": {
                "transaction": {
                    "id": 113536,
                    "checkoutSession": {"uuid": "561CF2F2DEAB", "totalAmount": 10},
                    "transactionStatus": "SUCCESS",
                },
                "sessionId": "561CF2F2DEAB",
            },
        }).encode()
        event = self.provider.parse_webhook({}, body)
        self.assertEqual(event.provider_reference, "561CF2F2DEAB")
        self.assertEqual(event.status, "succeeded")

    def test_parse_webhook_missing_session_id_raises(self):
        body = json.dumps({"transaction": {"transactionStatus": "SUCCESS"}}).encode()
        with self.assertRaises(ProviderError):
            self.provider.parse_webhook({}, body)

    def test_parse_webhook_malformed_json_raises(self):
        with self.assertRaises(ProviderError):
            self.provider.parse_webhook({}, b"not-json")


# ---------------------------------------------------------------------------
# Webhook view (integration)
# ---------------------------------------------------------------------------

@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class ArifPayWebhookViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.merchant, _ = create_test_merchant("ArifPay Merchant")
        self.url = reverse("arifpay_webhook")

    def tearDown(self):
        cache.clear()

    @patch.object(ArifPayProvider, "verify")
    def test_webhook_processed_and_idempotent(self, mock_verify):
        mock_verify.return_value = ProviderStatusResult(
            provider_reference="sess-webhook-100",
            status="succeeded",
            raw={"transaction": {"transactionStatus": "SUCCESS"}},
        )
        payment = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="idem-arifpay-webhook-1",
            amount=Decimal("100.00"),
            sender="customer@example.com",
            receiver="SafePay",
            provider="arifpay",
            provider_reference="sess-webhook-100",
            status="pending",
        )
        body = json.dumps({
            "sessionId": "sess-webhook-100",
            "transaction": {"transactionStatus": "SUCCESS"},
        })

        # First delivery
        res1 = self.client.post(self.url, data=body, content_type="application/json")
        self.assertEqual(res1.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")

        events = list(InboundWebhookEvent.objects.filter(
            provider_reference="sess-webhook-100"
        ).order_by("received_at"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].outcome, "processed_updated")

        # Second delivery (idempotent)
        res2 = self.client.post(self.url, data=body, content_type="application/json")
        self.assertEqual(res2.status_code, 200)

        events = list(InboundWebhookEvent.objects.filter(
            provider_reference="sess-webhook-100"
        ).order_by("received_at"))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1].outcome, "processed_unchanged")

    def test_webhook_unknown_reference_is_ignored(self):
        body = json.dumps({
            "sessionId": "non-existent-sess",
            "transaction": {"transactionStatus": "SUCCESS"},
        })
        res = self.client.post(self.url, data=body, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"status": "ignored"})
        event = InboundWebhookEvent.objects.filter(provider_reference="non-existent-sess").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.outcome, "unknown_reference")

    def test_webhook_malformed_json_returns_400(self):
        res = self.client.post(self.url, data="bad-json", content_type="application/json")
        self.assertEqual(res.status_code, 400)


# ---------------------------------------------------------------------------
# Smoke command tests
# ---------------------------------------------------------------------------

class ArifPaySmokeCommandTests(TestCase):
    @override_settings(ARIFPAY_API_KEY="")
    def test_smoke_no_key_prints_error(self):
        from io import StringIO
        from django.core.management import call_command
        err = StringIO()
        call_command("arifpay_smoke", "init", stderr=err)
        self.assertIn("ARIFPAY_API_KEY is not set", err.getvalue())

    @override_settings(
        ARIFPAY_API_KEY="arif-sandbox-portal-key",
        ARIFPAY_SANDBOX=True,
        ARIFPAY_SUCCESS_URL="https://example.com/success",
        ARIFPAY_ERROR_URL="https://example.com/error",
        ARIFPAY_CANCEL_URL="https://example.com/cancel",
        ARIFPAY_NOTIFY_URL="https://example.com/notify",
    )
    @patch("payments.providers.arifpay.requests.post")
    def test_smoke_pss_mode_success(self, mock_post):
        from io import StringIO
        from django.core.management import call_command
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "error": False,
                "data": {
                    "sessionId": "SIM-SMOKE-123",
                    "transaction": {"transactionStatus": "SUCCESS"},
                },
            },
        )
        out = StringIO()
        call_command("arifpay_smoke", "init", "--pss", "--amount", "100.00", stdout=out)
        output = out.getvalue()
        self.assertIn("Sandbox PSS transfer completed", output)
        self.assertIn("SIM-SMOKE-123", output)

        payment = Payment.objects.filter(provider="arifpay").first()
        self.assertIsNotNone(payment)
        self.assertEqual(payment.provider_reference, "SIM-SMOKE-123")

    @override_settings(ARIFPAY_API_KEY="arif-sandbox-portal-key", ARIFPAY_SANDBOX=True)
    def test_smoke_hosted_checkout_with_portal_key_shows_clear_error(self):
        """Running init WITHOUT --pss while using portal key should fail with clear message."""
        from io import StringIO
        from django.core.management import call_command
        err = StringIO()
        call_command("arifpay_smoke", "init", stderr=err)
        self.assertIn("portal simulator key", err.getvalue())

    @override_settings(
        ARIFPAY_API_KEY="test-merchant-key",
        ARIFPAY_SANDBOX=True,
        ARIFPAY_SUCCESS_URL="https://example.com/success",
        ARIFPAY_ERROR_URL="https://example.com/error",
        ARIFPAY_CANCEL_URL="https://example.com/cancel",
        ARIFPAY_NOTIFY_URL="https://example.com/notify",
    )
    @patch("payments.providers.arifpay.requests.post")
    def test_smoke_hosted_checkout_success(self, mock_post):
        from io import StringIO
        from django.core.management import call_command
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "error": False,
                "data": {
                    "sessionId": "sess-smoke-456",
                    "paymentUrl": "https://gateway.arifpay.org/pay/sess-smoke-456",
                },
            },
        )
        out = StringIO()
        call_command("arifpay_smoke", "init", "--amount", "100.00", stdout=out)
        output = out.getvalue()
        self.assertIn("hosted checkout session created", output)
        self.assertIn("sess-smoke-456", output)
        self.assertIn("https://gateway.arifpay.org/pay/sess-smoke-456", output)

    @override_settings(
        ARIFPAY_API_KEY="test-merchant-key",
        ARIFPAY_SANDBOX=True,
    )
    @patch("payments.providers.arifpay.requests.get")
    def test_smoke_verify_command(self, mock_get):
        from io import StringIO
        from django.core.management import call_command
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "error": False,
                "data": {
                    "sessionId": "sess-smoke-456",
                    "transaction": {"transactionStatus": "SUCCESS"},
                },
            },
        )
        out = StringIO()
        call_command("arifpay_smoke", "verify", "--ref", "sess-smoke-456", stdout=out)
        output = out.getvalue()
        self.assertIn("SafePay Status        : succeeded", output)
        self.assertIn("sess-smoke-456", output)
