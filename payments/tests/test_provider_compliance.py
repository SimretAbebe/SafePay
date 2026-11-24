from decimal import Decimal
from unittest.mock import MagicMock, patch
import requests
from django.test import SimpleTestCase, override_settings

from payments.providers import (
    ArifPayProvider,
    ChapaProvider,
    FakeProvider,
    ProviderError,
    UnsupportedProviderStatus,
)


class DummyPayment:
    def __init__(self, amount=Decimal("100.00"), customer_email="customer@example.com"):
        self.amount = amount
        self.customer_email = customer_email
        self.customer_first_name = "Test"
        self.customer_last_name = "User"
        self.customer_phone = "+251911223344"
        self.item_name = "Test Item"


@override_settings(
    CHAPA_SECRET_KEY="test-chapa-secret-key-123",
    CHAPA_WEBHOOK_SECRET="test-chapa-webhook-secret",
    CHAPA_BASE_URL="https://api.chapa.co/v1",
    CHAPA_DEFAULT_CURRENCY="ETB",
    ARIFPAY_API_KEY="test-arifpay-secret-key-456",
    ARIFPAY_SANDBOX=True,
    ARIFPAY_BASE_URL="https://gateway.arifpay.net/api",
    ARIFPAY_DEFAULT_CURRENCY="ETB",
    ENABLE_FAKE_PROVIDER=True,
)
class ProviderComplianceTests(SimpleTestCase):
    """
    Reusable compliance and contract test suite running across all registered providers:
    FakeProvider, ChapaProvider, and ArifPayProvider.
    """

    def setUp(self):
        self.providers = [
            FakeProvider(),
            ChapaProvider(),
            ArifPayProvider(),
        ]

    def test_unknown_status_raises_unsupported_provider_status(self):
        """(1) An unknown status raises UnsupportedProviderStatus for every provider."""
        unknown_status = "UNKNOWN_OR_BOGUS_STATUS"
        for provider in self.providers:
            with self.subTest(provider=provider.name):
                with self.assertRaises((UnsupportedProviderStatus, ProviderError)) as ctx:
                    provider.map_status(unknown_status)
                self.assertIsInstance(
                    ctx.exception,
                    UnsupportedProviderStatus,
                    f"Provider {provider.name} raised {type(ctx.exception)} instead of UnsupportedProviderStatus",
                )

    def test_cancel_raises_provider_error_when_supports_cancel_is_false(self):
        """(2) cancel raises ProviderError when supports_cancel is False."""
        for provider in self.providers:
            with self.subTest(provider=provider.name):
                if not provider.supports_cancel:
                    with self.assertRaises(ProviderError):
                        provider.cancel("test-reference-123")
                else:
                    # For providers supporting cancel (e.g. FakeProvider), cancel succeeds or is supported
                    self.assertTrue(provider.supports_cancel)

    def test_refund_support_is_false(self):
        """(3) refund support is False for real payment providers (Chapa and ArifPay)."""
        for provider in self.providers:
            with self.subTest(provider=provider.name):
                if provider.name in ("chapa", "arifpay"):
                    self.assertFalse(
                        provider.supports_refund,
                        f"Provider {provider.name} should have supports_refund=False until gateway refund API is implemented",
                    )
                elif provider.name == "fake":
                    # FakeProvider is our test knob supporting refund simulations
                    self.assertTrue(provider.supports_refund)

    @patch("payments.providers.chapa.requests.post")
    @patch("payments.providers.arifpay.requests.post")
    def test_exception_message_does_not_contain_secret_key(self, mock_arif_post, mock_chapa_post):
        """(4) An exception message does not contain the provider's secret key or API key."""
        chapa_key = "test-chapa-secret-key-123"
        arif_key = "test-arifpay-secret-key-456"

        # Simulate provider network error containing the raw secret
        mock_chapa_post.side_effect = requests.RequestException(f"Connection failed with token {chapa_key}")
        mock_arif_post.side_effect = requests.RequestException(f"Connection failed with key {arif_key}")

        chapa = ChapaProvider()
        with self.assertRaises(ProviderError) as ctx_chapa:
            chapa.initialize(DummyPayment())
        self.assertNotIn(chapa_key, str(ctx_chapa.exception))

        arif = ArifPayProvider()
        with self.assertRaises(ProviderError) as ctx_arif:
            arif.initialize(DummyPayment())
        self.assertNotIn(arif_key, str(ctx_arif.exception))
