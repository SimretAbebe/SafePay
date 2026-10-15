"""Tests for provider choice per merchant and safe fallback logic."""
from contextlib import contextmanager
import json
import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
import requests
from rest_framework import status
from rest_framework.test import APIClient

from payments.admin import MerchantAdminForm
from payments.models import Payment, ProviderAttempt
from payments.providers import ProviderError
from payments.providers.arifpay import ArifPayProvider
from payments.providers.chapa import ChapaProvider
from payments.providers.fake import FakeProvider
from payments.providers.registry import _registry as _provider_registry
from payments.test_utils import create_test_merchant

URL = reverse("create_checkout_session")


# ---------------------------------------------------------------------------
# Test Double & Dynamic Registry Helpers
# ---------------------------------------------------------------------------

class _NamedFake(FakeProvider):
    """FakeProvider double with configurable identity and error behavior."""

    def __init__(self, name):
        super().__init__()
        self.name = name

    def initialize(self, payment, customer=None):
        if getattr(self.__class__, "_force_error", False):
            raise ProviderError(
                "FakeProvider forced error (test knob)",
                not_created=getattr(self.__class__, "_force_error_not_created", False),
            )
        ref = f"{self.name}_{uuid.uuid4().hex}"
        FakeProvider._store[ref] = {"status": "pending"}
        from payments.providers.base import InitResult
        return InitResult(provider_reference=ref, checkout_url=f"https://fake.local/pay/{ref}")


def _make_fallback_provider(name):
    """Register an isolated FakeProvider clone with its own class state."""
    cls = type(f"_NamedFake_{name}", (_NamedFake,), {
        "_force_error": False,
        "_force_error_not_created": False,
    })
    p = cls(name)
    _provider_registry[name] = p

    def cleanup():
        _provider_registry.pop(name, None)

    return p, cleanup


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------

@override_settings(
    ENABLE_FAKE_PROVIDER=True,
    DEFAULT_PROVIDER="fake",
)
class ProviderRoutingTests(TestCase):
    """Tests 1–14 covering merchant routing, fallback, and failure safety."""

    def setUp(self):
        cache.clear()
        FakeProvider.reset()
        self.client = APIClient()

        self.fake2, self._cleanup_fake2 = _make_fallback_provider("fake2")
        self.fake3, self._cleanup_fake3 = _make_fallback_provider("fake3")

        self.merchant_a, self.key_a = create_test_merchant("Merchant A")
        self.merchant_a.default_provider = "fake2"
        self.merchant_a.fallback_providers = ["fake3"]
        self.merchant_a.save()

        self.merchant_b, self.key_b = create_test_merchant("Merchant B")

    def tearDown(self):
        cache.clear()
        FakeProvider.reset()
        self._cleanup_fake2()
        self._cleanup_fake3()

    # Helpers

    @contextmanager
    def fault(self, provider, not_created: bool = False):
        """Inject an initialize() error into a test double with guaranteed cleanup."""
        provider.__class__._force_error = True
        provider.__class__._force_error_not_created = not_created
        try:
            yield
        finally:
            provider.__class__._force_error = False
            provider.__class__._force_error_not_created = False

    def _post(self, api_key, idemp_key=None, **payload):
        """Helper to POST to checkout session endpoint."""
        payload.setdefault("amount", "100.00")
        headers = {
            "HTTP_X_API_KEY": api_key,
            "HTTP_IDEMPOTENCY_KEY": idemp_key or f"ikey-{uuid.uuid4().hex}",
        }
        return self.client.post(URL, payload, format="json", **headers)

    def _assert_admin_form_invalid(self, default_provider="fake", fallback_providers=None):
        form = MerchantAdminForm(
            data={
                "name": "Test",
                "is_active": True,
                "default_provider": default_provider,
                "fallback_providers": json.dumps(fallback_providers or []),
            },
            instance=self.merchant_a,
        )
        self.assertFalse(form.is_valid())
        return form

    def _check_classification(self, provider_cls, expected_not_created: bool, post_side_effect=None, post_return=None, **settings_kwargs):
        with override_settings(**settings_kwargs):
            with patch("requests.post", side_effect=post_side_effect, return_value=post_return):
                with self.assertRaises(ProviderError) as cm:
                    provider_cls().initialize(_MockPayment())
                self.assertEqual(cm.exception.not_created, expected_not_created)

    # Test 1: Provider selection precedence

    def test_1a_merchant_default_used_when_no_provider_in_request(self):
        resp = self._post(self.key_a)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["provider"], "fake2")

    def test_1b_request_provider_overrides_merchant_default(self):
        resp = self._post(self.key_a, provider="fake")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["provider"], "fake")

    def test_1c_settings_default_used_when_neither_merchant_nor_request_provides_provider(self):
        resp = self._post(self.key_b)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["provider"], "fake")

    # Test 2: Primary succeeds

    def test_2_primary_succeeds_no_fallback_called(self):
        resp = self._post(self.key_a)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        payment = Payment.objects.get(pk=resp.data["id"])
        attempts = list(payment.provider_attempts.all())
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].outcome, ProviderAttempt.OUTCOME_CREATED)
        self.assertEqual(attempts[0].provider, "fake2")

    # Test 3: Primary fails not_created=True, fallback succeeds

    def test_3_primary_fails_not_created_fallback_succeeds(self):
        with self.fault(self.fake2, not_created=True):
            resp = self._post(self.key_a)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["provider"], "fake3")

        payment = Payment.objects.get(pk=resp.data["id"])
        self.assertEqual(payment.provider, "fake3")
        self.assertIsNotNone(payment.checkout_url)

        attempts = list(payment.provider_attempts.all())
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0].outcome, ProviderAttempt.OUTCOME_FAILED_NOT_CREATED)
        self.assertEqual(attempts[0].provider, "fake2")
        self.assertEqual(attempts[1].outcome, ProviderAttempt.OUTCOME_CREATED)
        self.assertEqual(attempts[1].provider, "fake3")

    # Test 4: Primary fails not_created=False (uncertain)

    def test_4_primary_fails_uncertain_no_fallback_called(self):
        idemp = f"t4-{uuid.uuid4().hex}"
        with self.fault(self.fake2, not_created=False):
            resp = self._post(self.key_a, idemp_key=idemp)
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertNotIn("fake", resp.data["detail"])

        payment = Payment.objects.get(merchant=self.merchant_a, idempotency_key=idemp)
        self.assertEqual(payment.status, "pending")
        self.assertIsNone(payment.checkout_url)

        attempts = list(payment.provider_attempts.all())
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].outcome, ProviderAttempt.OUTCOME_FAILED_UNKNOWN)
        self.assertEqual(attempts[0].provider, "fake2")

    # Test 5: Retry after uncertain failure retries same provider only

    def test_5_retry_after_uncertain_failure_uses_same_provider(self):
        idemp = f"t5-{uuid.uuid4().hex}"
        with self.fault(self.fake2, not_created=False):
            resp1 = self._post(self.key_a, idemp_key=idemp)
        self.assertEqual(resp1.status_code, status.HTTP_502_BAD_GATEWAY)

        # Retry succeeds with same provider
        resp2 = self._post(self.key_a, idemp_key=idemp)
        self.assertEqual(resp2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp2.data["provider"], "fake2")

        payment = Payment.objects.get(pk=resp2.data["id"])
        outcomes = [a.outcome for a in payment.provider_attempts.all()]
        self.assertIn(ProviderAttempt.OUTCOME_FAILED_UNKNOWN, outcomes)
        self.assertIn(ProviderAttempt.OUTCOME_CREATED, outcomes)
        for a in payment.provider_attempts.all():
            self.assertEqual(a.provider, "fake2")

    # Test 6: All providers fail not_created=True

    def test_6_all_providers_fail_not_created_attempts_persist(self):
        idemp = f"t6-{uuid.uuid4().hex}"
        with self.fault(self.fake2, not_created=True), self.fault(self.fake3, not_created=True):
            resp = self._post(self.key_a, idemp_key=idemp)
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)

        payment = Payment.objects.get(merchant=self.merchant_a, idempotency_key=idemp)
        self.assertEqual(payment.status, "pending")
        attempts = list(payment.provider_attempts.all())
        self.assertEqual(len(attempts), 2)
        for attempt in attempts:
            self.assertEqual(attempt.outcome, ProviderAttempt.OUTCOME_FAILED_NOT_CREATED)

    # Test 7: Merchant with no fallback

    def test_7_no_fallback_configured_502_no_other_provider(self):
        FakeProvider.set_error(not_created=True)
        try:
            idemp = f"t7-{uuid.uuid4().hex}"
            resp = self._post(self.key_b, idemp_key=idemp)
            self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)

            payment = Payment.objects.get(merchant=self.merchant_b, idempotency_key=idemp)
            attempts = list(payment.provider_attempts.all())
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].provider, "fake")
        finally:
            FakeProvider.clear_error()

    # Test 8: allow_fallback handling

    def test_8a_allow_fallback_false_disables_fallback(self):
        idemp = f"t8a-{uuid.uuid4().hex}"
        with self.fault(self.fake2, not_created=True):
            resp = self._post(self.key_a, idemp_key=idemp, allow_fallback=False)
        self.assertEqual(resp.status_code, status.HTTP_502_BAD_GATEWAY)

        payment = Payment.objects.get(merchant=self.merchant_a, idempotency_key=idemp)
        attempts = list(payment.provider_attempts.all())
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].provider, "fake2")

    def test_8b_allow_fallback_string_yes_returns_400(self):
        resp = self.client.post(
            URL,
            {"amount": "100.00", "allow_fallback": "yes"},
            format="json",
            HTTP_X_API_KEY=self.key_a,
            HTTP_IDEMPOTENCY_KEY="t8b-key",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # Test 9: Idempotent repeat after fallback

    def test_9_repeat_after_fallback_returns_200_same_session(self):
        idemp = f"t9-{uuid.uuid4().hex}"
        with self.fault(self.fake2, not_created=True):
            resp1 = self._post(self.key_a, idemp_key=idemp)
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp1.data["provider"], "fake3")

        with patch.object(self.fake2, "initialize", wraps=self.fake2.initialize) as m2, \
             patch.object(self.fake3, "initialize", wraps=self.fake3.initialize) as m3:
            resp2 = self._post(self.key_a, idemp_key=idemp)

        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertEqual(resp2.data["checkout_url"], resp1.data["checkout_url"])
        m2.assert_not_called()
        m3.assert_not_called()

    # Test 10: Validation

    def test_10a_unknown_provider_in_request_returns_400(self):
        idemp = f"t10-{uuid.uuid4().hex}"
        resp = self._post(self.key_a, idemp_key=idemp, provider="nonexistent_xyz")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Payment.objects.filter(merchant=self.merchant_a, idempotency_key=idemp).exists())

    def test_10b_admin_form_rejects_unknown_default_provider(self):
        form = self._assert_admin_form_invalid(default_provider="does_not_exist")
        self.assertIn("does_not_exist", str(form.errors))

    def test_10c_admin_form_rejects_duplicates_in_fallback(self):
        self._assert_admin_form_invalid(fallback_providers=["fake3", "fake3"])

    def test_10d_admin_form_rejects_default_repeated_in_fallback(self):
        self._assert_admin_form_invalid(default_provider="fake", fallback_providers=["fake"])

    # Test 11: Attempt log sanitization

    def test_11_attempt_rows_store_only_class_names(self):
        with self.fault(self.fake2, not_created=True):
            resp = self._post(self.key_a)
        payment = Payment.objects.get(pk=resp.data["id"])
        for attempt in payment.provider_attempts.all():
            if attempt.error_type:
                self.assertNotIn(" ", attempt.error_type)
                for secret_kw in ("key", "secret", "token", "auth", "bearer", "password"):
                    self.assertNotIn(secret_kw, attempt.error_type.lower())

    # Test 12: Provider adapter classification tests

    def test_12_chapa_connection_error_is_not_created_true(self):
        self._check_classification(ChapaProvider, True, post_side_effect=requests.exceptions.ConnectionError("refused"))

    def test_12_chapa_connect_timeout_is_not_created_true(self):
        self._check_classification(ChapaProvider, True, post_side_effect=requests.exceptions.ConnectTimeout())

    def test_12_chapa_http_401_is_not_created_true(self):
        self._check_classification(ChapaProvider, True, post_return=_MockResponse(401, {"message": "Unauthorized"}), CHAPA_SECRET_KEY="test-key")

    def test_12_chapa_missing_key_is_not_created_true(self):
        self._check_classification(ChapaProvider, True, CHAPA_SECRET_KEY="")

    def test_12_chapa_read_timeout_is_not_created_false(self):
        self._check_classification(ChapaProvider, False, post_side_effect=requests.exceptions.ReadTimeout(), CHAPA_SECRET_KEY="test-key")

    def test_12_chapa_http_500_is_not_created_false(self):
        self._check_classification(ChapaProvider, False, post_return=_MockResponse(500, {"message": "server error"}), CHAPA_SECRET_KEY="test-key")

    def test_12_chapa_invalid_json_is_not_created_false(self):
        self._check_classification(ChapaProvider, False, post_return=_MockResponse(200, bad_json=True), CHAPA_SECRET_KEY="test-key")

    def test_12_chapa_missing_checkout_url_is_not_created_false(self):
        self._check_classification(ChapaProvider, False, post_return=_MockResponse(200, {"status": "success", "data": {}}), CHAPA_SECRET_KEY="test-key")

    def test_12_arifpay_connection_error_is_not_created_true(self):
        self._check_classification(ArifPayProvider, True, post_side_effect=requests.exceptions.ConnectionError("refused"), ARIFPAY_API_KEY="test-key")

    def test_12_arifpay_connect_timeout_is_not_created_true(self):
        self._check_classification(ArifPayProvider, True, post_side_effect=requests.exceptions.ConnectTimeout(), ARIFPAY_API_KEY="test-key")

    def test_12_arifpay_http_401_is_not_created_true(self):
        self._check_classification(ArifPayProvider, True, post_return=_MockResponse(401, {"msg": "Unauthorized", "error": True}), ARIFPAY_API_KEY="test-key")

    def test_12_arifpay_missing_key_is_not_created_true(self):
        self._check_classification(ArifPayProvider, True, ARIFPAY_API_KEY="")

    def test_12_arifpay_read_timeout_is_not_created_false(self):
        self._check_classification(ArifPayProvider, False, post_side_effect=requests.exceptions.ReadTimeout(), ARIFPAY_API_KEY="test-key")

    def test_12_arifpay_http_500_is_not_created_false(self):
        self._check_classification(ArifPayProvider, False, post_return=_MockResponse(500, {"msg": "Internal Server Error"}), ARIFPAY_API_KEY="test-key")

    def test_12_arifpay_invalid_json_is_not_created_false(self):
        self._check_classification(ArifPayProvider, False, post_return=_MockResponse(200, bad_json=True), ARIFPAY_API_KEY="test-key")

    def test_12_arifpay_missing_checkout_fields_is_not_created_false(self):
        self._check_classification(ArifPayProvider, False, post_return=_MockResponse(200, {"data": {}}), ARIFPAY_API_KEY="test-key")

    # Test 13: Merchant isolation

    def test_13_merchant_isolation(self):
        FakeProvider.set_error(not_created=True)
        try:
            idemp_b = f"t13b-{uuid.uuid4().hex}"
            resp_b = self._post(self.key_b, idemp_key=idemp_b)
            self.assertEqual(resp_b.status_code, status.HTTP_502_BAD_GATEWAY)

            payment_b = Payment.objects.get(merchant=self.merchant_b, idempotency_key=idemp_b)
            attempt_providers = [a.provider for a in payment_b.provider_attempts.all()]
            for p in attempt_providers:
                self.assertNotIn(p, ["fake2", "fake3"])
        finally:
            FakeProvider.clear_error()

    # Test 14: Existing checkout smoke-test

    def test_14_existing_checkout_still_works(self):
        resp = self._post(self.key_a)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn("checkout_url", resp.data)
        self.assertIn("provider", resp.data)


# ---------------------------------------------------------------------------
# Test Stubs
# ---------------------------------------------------------------------------

class _MockPayment:
    id = None
    amount = "100.00"
    return_url = None
    customer_email = None
    customer_first_name = None
    customer_last_name = None
    item_name = None


class _MockResponse:
    def __init__(self, status_code=200, data=None, bad_json=False):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not JSON")
        return self._data
