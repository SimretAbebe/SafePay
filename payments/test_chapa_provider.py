from decimal import Decimal
from unittest.mock import MagicMock, patch
import requests
from django.test import SimpleTestCase, TestCase, override_settings

from payments.models import Payment
from payments.providers import (
    ChapaProvider,
    InitResult,
    ProviderError,
    ProviderStatusResult,
    get_provider,
)


class DummyPayment:
    def __init__(self, amount=Decimal("150.00"), customer_email="customer@example.com", first_name="Abebe", last_name="Bikila"):
        self.amount = amount
        self.customer_email = customer_email
        self.customer_first_name = first_name
        self.customer_last_name = last_name


@override_settings(
    CHAPA_SECRET_KEY="test-secret-123",
    CHAPA_BASE_URL="https://api.chapa.co/v1",
    CHAPA_DEFAULT_CURRENCY="ETB",
    CHAPA_REQUEST_TIMEOUT=10,
)
class ChapaProviderTests(SimpleTestCase):
    def setUp(self):
        self.provider = ChapaProvider()

    # 1. initialize sends right URL, bearer header, amount, currency, unique tx_ref, returns reference and checkout_url
    @patch("payments.providers.chapa.requests.post")
    def test_initialize_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": "Hosted Link",
            "status": "success",
            "data": {
                "checkout_url": "https://checkout.chapa.co/checkout/payment/12345"
            },
        }
        mock_post.return_value = mock_response

        payment = DummyPayment(amount=Decimal("250.50"))
        result = self.provider.initialize(payment)

        self.assertIsInstance(result, InitResult)
        self.assertTrue(result.provider_reference.startswith("sp-"))
        self.assertEqual(len(result.provider_reference), 35)
        self.assertEqual(
            result.checkout_url, "https://checkout.chapa.co/checkout/payment/12345"
        )

        mock_post.assert_called_once()
        call_args, call_kwargs = mock_post.call_args
        self.assertEqual(call_args[0], "https://api.chapa.co/v1/transaction/initialize")

        sent_headers = call_kwargs["headers"]
        self.assertEqual(sent_headers["Authorization"], "Bearer test-secret-123")
        self.assertEqual(sent_headers["Content-Type"], "application/json")

        sent_payload = call_kwargs["json"]
        self.assertEqual(sent_payload["amount"], "250.50")
        self.assertEqual(sent_payload["currency"], "ETB")
        self.assertEqual(sent_payload["tx_ref"], result.provider_reference)
        self.assertEqual(sent_payload["email"], "customer@example.com")
        self.assertEqual(sent_payload["first_name"], "Abebe")
        self.assertEqual(sent_payload["last_name"], "Bikila")

    # 2. Two initialize calls produce two different tx_refs
    @patch("payments.providers.chapa.requests.post")
    def test_two_initialize_calls_produce_different_tx_refs(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "success",
            "data": {"checkout_url": "https://checkout.chapa.co/pay"},
        }
        mock_post.return_value = mock_response

        payment1 = DummyPayment()
        payment2 = DummyPayment()

        res1 = self.provider.initialize(payment1)
        res2 = self.provider.initialize(payment2)

        self.assertNotEqual(res1.provider_reference, res2.provider_reference)
        self.assertTrue(res1.provider_reference.startswith("sp-"))
        self.assertTrue(res2.provider_reference.startswith("sp-"))

    # 3. verify with data.status "success" -> SafePay "succeeded"
    @patch("payments.providers.chapa.requests.get")
    def test_verify_data_status_success_maps_to_succeeded(self, mock_get):
        ref = "sp-11112222333344445555666677778888"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": "Payment details",
            "status": "success",
            "data": {
                "status": "success",
                "tx_ref": ref,
                "amount": 100,
                "currency": "ETB",
            },
        }
        mock_get.return_value = mock_response

        result = self.provider.verify(ref)

        self.assertIsInstance(result, ProviderStatusResult)
        self.assertEqual(result.provider_reference, ref)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.raw["status"], "success")

        mock_get.assert_called_once_with(
            f"https://api.chapa.co/v1/transaction/verify/{ref}",
            headers={"Authorization": "Bearer test-secret-123"},
            timeout=10,
        )

    # 4. THE KEY TEST: outer status "success" but data.status "pending" -> SafePay "pending" (not succeeded)
    @patch("payments.providers.chapa.requests.get")
    def test_verify_outer_success_with_data_status_pending(self, mock_get):
        ref = "sp-pending-test-ref-123456789012"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": "Payment details",
            "status": "success",  # Outer status only indicates API call worked
            "data": {
                "status": "pending",  # Payment state is pending
                "tx_ref": ref,
                "amount": 100,
            },
        }
        mock_get.return_value = mock_response

        result = self.provider.verify(ref)

        self.assertEqual(result.status, "pending")
        self.assertNotEqual(result.status, "succeeded")

    # 5. verify where data.tx_ref differs from requested reference -> ProviderError
    @patch("payments.providers.chapa.requests.get")
    def test_verify_reference_mismatch_raises_provider_error(self, mock_get):
        ref = "sp-expected-ref"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "success",
            "data": {
                "status": "success",
                "tx_ref": "sp-different-ref",
            },
        }
        mock_get.return_value = mock_response

        with self.assertRaises(ProviderError) as ctx:
            self.provider.verify(ref)

        self.assertIn("reference mismatch", str(ctx.exception).lower())

    # 6. map_status: pending, success, failed, cancelled map correctly (including uppercase);
    #    refunded, reversed and unknown values raise ProviderError
    def test_map_status(self):
        # Valid mappings (both lowercase and uppercase)
        self.assertEqual(self.provider.map_status("pending"), "pending")
        self.assertEqual(self.provider.map_status("PENDING"), "pending")
        self.assertEqual(self.provider.map_status("success"), "succeeded")
        self.assertEqual(self.provider.map_status("SUCCESS"), "succeeded")
        self.assertEqual(self.provider.map_status("failed"), "failed")
        self.assertEqual(self.provider.map_status("FAILED"), "failed")
        self.assertEqual(self.provider.map_status("cancelled"), "cancelled")
        self.assertEqual(self.provider.map_status("CANCELLED"), "cancelled")

        # Unsupported statuses
        for bad_status in ["refunded", "REFUNDED", "reversed", "REVERSED", "unknown", "chargeback"]:
            with self.subTest(bad_status=bad_status):
                with self.assertRaises(ProviderError) as ctx:
                    self.provider.map_status(bad_status)
                self.assertIn("unsupported provider status", str(ctx.exception).lower())

    # 7. HTTP 401/500, a timeout, invalid JSON, and missing checkout_url raise ProviderError,
    #    and message does not contain the secret key
    @patch("payments.providers.chapa.requests.post")
    def test_initialize_errors_do_not_leak_secret(self, mock_post):
        payment = DummyPayment()
        secret = "test-secret-123"

        # Case 1: HTTP 401
        res_401 = MagicMock()
        res_401.status_code = 401
        res_401.json.return_value = {"message": "Invalid authentication token", "status": "failed"}
        mock_post.return_value = res_401
        with self.assertRaises(ProviderError) as ctx:
            self.provider.initialize(payment)
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn("Bearer", str(ctx.exception))

        # Case 2: HTTP 500
        res_500 = MagicMock()
        res_500.status_code = 500
        res_500.json.return_value = {"message": "Internal server error"}
        mock_post.return_value = res_500
        with self.assertRaises(ProviderError) as ctx:
            self.provider.initialize(payment)
        self.assertNotIn(secret, str(ctx.exception))

        # Case 3: requests.Timeout
        mock_post.side_effect = requests.Timeout(f"Timeout occurred connecting with {secret}")
        with self.assertRaises(ProviderError) as ctx:
            self.provider.initialize(payment)
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIn("timed out", str(ctx.exception).lower())
        mock_post.side_effect = None

        # Case 4: Invalid JSON
        res_bad_json = MagicMock()
        res_bad_json.status_code = 200
        res_bad_json.json.side_effect = ValueError("No JSON object could be decoded")
        mock_post.return_value = res_bad_json
        with self.assertRaises(ProviderError) as ctx:
            self.provider.initialize(payment)
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIn("invalid json", str(ctx.exception).lower())

        # Case 5: Missing checkout_url
        res_missing_url = MagicMock()
        res_missing_url.status_code = 200
        res_missing_url.json.return_value = {
            "status": "success",
            "data": {},
        }
        mock_post.return_value = res_missing_url
        with self.assertRaises(ProviderError) as ctx:
            self.provider.initialize(payment)
        self.assertNotIn(secret, str(ctx.exception))
        self.assertIn("missing checkout_url", str(ctx.exception).lower())

    # 8. Empty CHAPA_SECRET_KEY raises ProviderError("Chapa is not configured")
    @override_settings(CHAPA_SECRET_KEY="")
    def test_empty_secret_key_raises_provider_error(self):
        with self.assertRaises(ProviderError) as ctx_init:
            self.provider.initialize(DummyPayment())
        self.assertEqual(str(ctx_init.exception), "Chapa is not configured")

        with self.assertRaises(ProviderError) as ctx_verify:
            self.provider.verify("sp-123456")
        self.assertEqual(str(ctx_verify.exception), "Chapa is not configured")

    # 9. cancel() and parse_webhook() raise "not implemented yet" ProviderError
    def test_cancel_and_parse_webhook_raise_not_implemented(self):
        with self.assertRaises(ProviderError) as ctx_cancel:
            self.provider.cancel("sp-123456")
        self.assertEqual(str(ctx_cancel.exception), "Chapa cancel is not implemented yet")

        with self.assertRaises(ProviderError) as ctx_webhook:
            self.provider.parse_webhook(headers={}, body=b"{}")
        self.assertEqual(
            str(ctx_webhook.exception), "Chapa webhook parsing is not implemented yet"
        )

    # 10. get_provider("chapa") returns a ChapaProvider
    def test_get_provider_chapa(self):
        provider = get_provider("chapa")
        self.assertIsInstance(provider, ChapaProvider)
        self.assertEqual(provider.name, "chapa")
        self.assertFalse(provider.supports_cancel)

    # 11. initialize sends customer dict and payment.return_url
    @patch("payments.providers.chapa.requests.post")
    def test_initialize_with_customer_dict_and_return_url(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "success",
            "data": {"checkout_url": "https://checkout.chapa.co/pay/123"},
        }
        mock_post.return_value = mock_response

        class SimplePayment:
            amount = Decimal("300.00")
            return_url = "https://merchant.example.com/callback"

        customer = {
            "email": "dict_customer@example.com",
            "first_name": "Haile",
            "last_name": "Gebrselassie",
        }
        result = self.provider.initialize(SimplePayment(), customer=customer)
        self.assertEqual(result.checkout_url, "https://checkout.chapa.co/pay/123")

        call_args, call_kwargs = mock_post.call_args
        sent_payload = call_kwargs["json"]
        self.assertEqual(sent_payload["email"], "dict_customer@example.com")
        self.assertEqual(sent_payload["first_name"], "Haile")
        self.assertEqual(sent_payload["last_name"], "Gebrselassie")
        self.assertEqual(sent_payload["return_url"], "https://merchant.example.com/callback")

    # 12. initialize falls back to settings.CHAPA_RETURN_URL if payment.return_url is None
    @override_settings(CHAPA_RETURN_URL="https://default.example.com/fallback")
    @patch("payments.providers.chapa.requests.post")
    def test_initialize_fallback_to_settings_return_url(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "success",
            "data": {"checkout_url": "https://checkout.chapa.co/pay/123"},
        }
        mock_post.return_value = mock_response

        class SimplePayment:
            amount = Decimal("100.00")
            return_url = None

        self.provider.initialize(SimplePayment())
        call_args, call_kwargs = mock_post.call_args
        sent_payload = call_kwargs["json"]
        self.assertEqual(sent_payload["return_url"], "https://default.example.com/fallback")



class ChapaSmokeCommandTests(TestCase):
    @override_settings(CHAPA_SECRET_KEY="")
    def test_smoke_init_without_key_prints_error(self):
        from io import StringIO
        from django.core.management import call_command

        err = StringIO()
        call_command("chapa_smoke", "init", stderr=err)
        self.assertIn("CHAPA_SECRET_KEY is not configured", err.getvalue())

    @override_settings(CHAPA_SECRET_KEY="test-key-123")
    @patch("payments.providers.chapa.requests.post")
    def test_smoke_init_creates_payment_and_prints_info(self, mock_post):
        from io import StringIO
        from django.core.management import call_command

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "success",
            "data": {"checkout_url": "https://checkout.chapa.co/pay/sp-smoke-123"},
        }
        mock_post.return_value = mock_response

        out = StringIO()
        call_command("chapa_smoke", "init", "--amount", "150.00", stdout=out)

        output = out.getvalue()
        self.assertIn("Payment ID:", output)
        self.assertIn("Provider Reference:", output)
        self.assertIn("Checkout URL: https://checkout.chapa.co/pay/sp-smoke-123", output)

        payment = Payment.objects.filter(provider="chapa").first()
        self.assertIsNotNone(payment)
        self.assertEqual(payment.amount, Decimal("150.00"))
        self.assertEqual(payment.checkout_url, "https://checkout.chapa.co/pay/sp-smoke-123")

    @override_settings(CHAPA_SECRET_KEY="test-key-123")
    @patch("payments.providers.chapa.requests.get")
    def test_smoke_verify_command(self, mock_get):
        from io import StringIO
        from django.core.management import call_command

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "success",
            "data": {
                "status": "success",
                "tx_ref": "sp-smoke-ref-999",
            },
        }
        mock_get.return_value = mock_response

        out = StringIO()
        call_command("chapa_smoke", "verify", "--ref", "sp-smoke-ref-999", stdout=out)

        output = out.getvalue()
        self.assertIn("SafePay Status: succeeded", output)
        self.assertIn("Provider Native Status: success", output)
