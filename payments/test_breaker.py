import uuid
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payments import breaker
from payments.models import Payment, ProviderAttempt
from payments.providers import ProviderError
from payments.providers.fake import FakeProvider
from payments.providers.registry import _registry as _provider_registry
from payments.test_utils import create_test_merchant

URL = reverse("create_checkout_session")


class MockClock:
    def __init__(self, start=1000.0):
        self.current = start

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current += seconds


class _NamedFake(FakeProvider):
    def __init__(self, name):
        super().__init__()
        self.name = name

    def initialize(self, payment, customer=None):
        if getattr(self.__class__, "_force_error", False):
            raise ProviderError(
                "FakeProvider forced error (test knob)",
                not_created=getattr(self.__class__, "_force_error_not_created", False),
                infrastructure=getattr(self.__class__, "_force_error_infrastructure", False),
            )
        ref = f"{self.name}_{uuid.uuid4().hex}"
        FakeProvider._store[ref] = {"status": "pending"}
        from payments.providers.base import InitResult
        return InitResult(provider_reference=ref, checkout_url=f"https://fake.local/pay/{ref}")


def _make_named_provider(name):
    cls = type(f"_NamedFake_{name}", (_NamedFake,), {
        "_force_error": False,
        "_force_error_not_created": False,
        "_force_error_infrastructure": False,
    })
    p = cls(name)
    _provider_registry[name] = p

    def cleanup():
        _provider_registry.pop(name, None)

    return p, cleanup


@override_settings(
    ENABLE_FAKE_PROVIDER=True,
    DEFAULT_PROVIDER="fake",
    BREAKER_ENABLED=True,
    BREAKER_FAILURE_THRESHOLD=5,
    BREAKER_WINDOW_SECONDS=60,
    BREAKER_COOLDOWN_SECONDS=60,
    BREAKER_MAX_COOLDOWN_SECONDS=600,
    BREAKER_PROBE_TTL_SECONDS=30,
)
class CircuitBreakerTests(TestCase):
    def setUp(self):
        cache.clear()
        FakeProvider.reset()
        self.clock = MockClock(1000.0)
        breaker.set_clock(self.clock)
        self.client = APIClient()

        self.fake2, self._cleanup_fake2 = _make_named_provider("fake2")
        self.fake3, self._cleanup_fake3 = _make_named_provider("fake3")

        self.merchant, self.api_key = create_test_merchant("Breaker Merchant")
        self.merchant.default_provider = "fake2"
        self.merchant.fallback_providers = ["fake3"]
        self.merchant.save()

    def tearDown(self):
        breaker.reset_clock()
        cache.clear()
        FakeProvider.reset()
        self._cleanup_fake2()
        self._cleanup_fake3()

    def _post(self, api_key=None, idemp=None, **payload):
        payload.setdefault("amount", "100.00")
        headers = {
            "HTTP_X_API_KEY": api_key or self.api_key,
            "HTTP_IDEMPOTENCY_KEY": idemp or f"bkey-{uuid.uuid4().hex}",
        }
        return self.client.post(URL, payload, format="json", **headers)

    def test_failure_threshold_and_window_expiry(self):
        provider = "fake2"
        for _ in range(4):
            breaker.record_failure(provider, livemode=False)
            self.assertEqual(breaker.state(provider, livemode=False), "closed")

        breaker.record_failure(provider, livemode=False)
        self.assertEqual(breaker.state(provider, livemode=False), "open")

        cache.clear()
        self.clock.current = 2000.0

        for _ in range(4):
            breaker.record_failure(provider, livemode=False)
        self.assertEqual(breaker.state(provider, livemode=False), "closed")

        self.clock.advance(65)
        breaker.record_failure(provider, livemode=False)
        self.assertEqual(breaker.state(provider, livemode=False), "closed")

    def test_only_infrastructure_failures_count(self):
        provider = "fake2"
        self.fake2.__class__._force_error = True
        self.fake2.__class__._force_error_not_created = True
        self.fake2.__class__._force_error_infrastructure = False

        for _ in range(6):
            self._post()

        self.assertEqual(breaker.state(provider, livemode=False), "closed")

        self.fake2.__class__._force_error_infrastructure = True
        for _ in range(5):
            self._post()

        self.assertEqual(breaker.state(provider, livemode=False), "open")

    def test_open_with_fallback_uses_fallback_and_records_skipped_attempt(self):
        for _ in range(5):
            breaker.record_failure("fake2", livemode=False)
        self.assertEqual(breaker.state("fake2", livemode=False), "open")

        with patch.object(self.fake2, "initialize", wraps=self.fake2.initialize) as mock_init:
            resp = self._post()

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["provider"], "fake3")
        mock_init.assert_not_called()

        payment = Payment.objects.get(pk=resp.data["id"])
        attempts = list(payment.provider_attempts.all())
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].provider, "fake2")
        self.assertEqual(attempts[0].outcome, ProviderAttempt.OUTCOME_SKIPPED_CIRCUIT_OPEN)
        self.assertEqual(attempts[1].provider, "fake3")
        self.assertEqual(attempts[1].outcome, ProviderAttempt.OUTCOME_CREATED)

    def test_open_without_fallback_returns_502_and_retry_after(self):
        self.merchant.fallback_providers = []
        self.merchant.save()

        for _ in range(5):
            breaker.record_failure("fake2", livemode=False)
        self.assertEqual(breaker.state("fake2", livemode=False), "open")

        idemp = f"no-fallback-{uuid.uuid4().hex}"
        with patch.object(self.fake2, "initialize", wraps=self.fake2.initialize) as mock_init:
            resp = self._post(idemp=idemp)

        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(
            resp.data["detail"],
            "Payment provider unavailable. Retry with the same Idempotency-Key.",
        )
        self.assertNotIn("fake", resp.data["detail"])
        mock_init.assert_not_called()

        self.assertIn("Retry-After", resp.headers)
        retry_val = int(resp.headers["Retry-After"])
        self.assertTrue(1 <= retry_val <= 60)

        payment = Payment.objects.get(idempotency_key=idemp)
        self.assertEqual(payment.status, "pending")
        self.assertIsNone(payment.checkout_url)

        attempts = list(payment.provider_attempts.all())
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].outcome, ProviderAttempt.OUTCOME_SKIPPED_CIRCUIT_OPEN)

        self.clock.advance(61)
        resp2 = self._post(idemp=idemp)
        self.assertEqual(resp2.status_code, status.HTTP_201_CREATED)

    def test_cooldown_probe_exclusivity_and_exponential_backoff(self):
        for _ in range(5):
            breaker.record_failure("fake2", livemode=False)
        self.assertEqual(breaker.state("fake2", livemode=False), "open")

        self.clock.advance(60)
        self.assertEqual(breaker.state("fake2", livemode=False), "half_open")

        action1 = breaker.allow_request("fake2", livemode=False)
        action2 = breaker.allow_request("fake2", livemode=False)
        self.assertEqual(action1, "probe")
        self.assertEqual(action2, "skip")

        breaker.record_success("fake2", livemode=False)
        self.assertEqual(breaker.state("fake2", livemode=False), "closed")
        self.assertEqual(breaker.allow_request("fake2", livemode=False), "allow")

        for _ in range(5):
            breaker.record_failure("fake2", livemode=False)
        self.assertEqual(breaker.state("fake2", livemode=False), "open")

        self.clock.advance(60)
        self.assertEqual(breaker.allow_request("fake2", livemode=False), "probe")

        breaker.record_failure("fake2", livemode=False)
        self.assertEqual(breaker.state("fake2", livemode=False), "open")
        self.assertTrue(breaker.retry_after("fake2", livemode=False) > 60)

    def test_uncertain_failure_counts_toward_breaker_and_halts_chain(self):
        self.fake2.__class__._force_error = True
        self.fake2.__class__._force_error_not_created = False
        self.fake2.__class__._force_error_infrastructure = True

        idemp = f"uncert-{uuid.uuid4().hex}"
        with patch.object(self.fake3, "initialize") as mock_fake3:
            resp = self._post(idemp=idemp)

        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)
        mock_fake3.assert_not_called()

        payment = Payment.objects.get(idempotency_key=idemp)
        attempts = list(payment.provider_attempts.all())
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].outcome, ProviderAttempt.OUTCOME_FAILED_UNKNOWN)

        for _ in range(4):
            self._post()
        self.assertEqual(breaker.state("fake2", livemode=False), "open")

    def test_provider_and_mode_isolation(self):
        for _ in range(5):
            breaker.record_failure("chapa", livemode=False)

        self.assertEqual(breaker.state("chapa", livemode=False), "open")
        self.assertEqual(breaker.state("arifpay", livemode=False), "closed")
        self.assertEqual(breaker.state("chapa", livemode=True), "closed")

    @patch("payments.alerts.send_alert_message")
    def test_alert_on_open_and_close(self, mock_alert):
        for _ in range(4):
            breaker.record_failure("fake2", livemode=False)
        mock_alert.assert_not_called()

        breaker.record_failure("fake2", livemode=False)
        self.assertEqual(mock_alert.call_count, 1)
        open_msg = mock_alert.call_args[0][0]
        self.assertIn("circuit OPEN", open_msg)
        self.assertIn("fake2 (test)", open_msg)
        self.assertNotIn("customer", open_msg.lower())
        self.assertNotIn("secret", open_msg.lower())

        for _ in range(3):
            self.assertEqual(breaker.allow_request("fake2", livemode=False), "skip")
        self.assertEqual(mock_alert.call_count, 1)

        self.clock.advance(60)
        self.assertEqual(breaker.allow_request("fake2", livemode=False), "probe")
        breaker.record_success("fake2", livemode=False)

        self.assertEqual(mock_alert.call_count, 2)
        close_msg = mock_alert.call_args[0][0]
        self.assertIn("circuit CLOSED", close_msg)
        self.assertIn("fake2 (test)", close_msg)

    @patch("payments.health._check_broker", return_value="ok")
    @patch("payments.health._check_cache", return_value="ok")
    @patch("payments.health._check_database", return_value="ok")
    def test_ready_endpoint_shows_provider_states(self, mock_db, mock_cache, mock_broker):
        for _ in range(5):
            breaker.record_failure("fake", livemode=False)

        resp = self.client.get("/ready/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertIn("providers", data)
        self.assertEqual(data["providers"]["fake"], "open")

    def test_verify_and_cancel_not_gated_by_breaker(self):
        for _ in range(5):
            breaker.record_failure("fake", livemode=False)
        self.assertEqual(breaker.state("fake", livemode=False), "open")

        FakeProvider.simulate("test-ref-1", "pending")
        provider = FakeProvider()

        status_result = provider.verify("test-ref-1")
        self.assertEqual(status_result.status, "pending")

        cancel_result = provider.cancel("test-ref-1")
        self.assertEqual(cancel_result.status, "cancelled")

    @override_settings(BREAKER_ENABLED=False)
    def test_breaker_disabled_behaves_as_always_closed(self):
        for _ in range(10):
            breaker.record_failure("fake2", livemode=False)

        self.assertEqual(breaker.state("fake2", livemode=False), "closed")
        self.assertEqual(breaker.allow_request("fake2", livemode=False), "allow")
        self.assertEqual(breaker.retry_after("fake2", livemode=False), 1)
