import hashlib
import hmac
import json
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from payments.models import Merchant, Payment
from payments.providers import (
    FakeProvider,
    InitResult,
    ProviderError,
    ProviderEvent,
    ProviderStatusResult,
    get_provider,
    validate_safepay_status,
)

class FakeNoCancelProvider(FakeProvider):
    name: str = "fake_no_cancel"
    supports_cancel: bool = False


class ProviderFrameworkTests(TestCase):
    """Compact test suite covering the required provider‑adapter behavior.

    It keeps the nine original checks but in a more concise form.
    """

    def setUp(self) -> None:
        FakeProvider.reset()
        self.provider = FakeProvider()
        self.merchant = Merchant.objects.create(
            name="Test Merchant",
            api_key_hash=Merchant.hash_key("test-key"),
            key_prefix="testpref",
        )

    # 1. initialize returns reference & URL
    def test_initialize(self):
        init = self.provider.initialize(payment=None)
        self.assertIsInstance(init, InitResult)
        self.assertTrue(init.provider_reference.startswith("fake_"))
        self.assertEqual(
            init.checkout_url,
            f"https://fake.local/pay/{init.provider_reference}",
        )

    # 2. verify maps fake statuses to SafePay statuses
    def test_verify_maps_statuses(self):
        ref = self.provider.initialize(payment=None).provider_reference
        self.assertEqual(self.provider.verify(ref).status, "pending")
        mapping = {
            "paid": "succeeded",
            "processing": "processing",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        for fake_status, safepay_status in mapping.items():
            FakeProvider.simulate(ref, fake_status)
            self.assertEqual(self.provider.verify(ref).status, safepay_status)

    # 3. unknown status raises ProviderError
    def test_map_status_unknown_raises(self):
        for bad in ["unknown", "partial", "", "refunded", "reversed"]:
            with self.assertRaises(ProviderError):
                self.provider.map_status(bad)

    # 4. cancel works only while pending
    def test_cancel_behaviour(self):
        ref = self.provider.initialize(payment=None).provider_reference
        cancelled = self.provider.cancel(ref)
        self.assertIsInstance(cancelled, ProviderStatusResult)
        self.assertEqual(cancelled.status, "cancelled")
        with self.assertRaises(ProviderError):
            self.provider.cancel(ref)  # already cancelled
        # non‑pending case
        ref2 = self.provider.initialize(payment=None).provider_reference
        FakeProvider.simulate(ref2, "paid")
        with self.assertRaises(ProviderError):
            self.provider.cancel(ref2)

        # FakeNoCancelProvider
        no_cancel_provider = FakeNoCancelProvider()
        ref3 = no_cancel_provider.initialize(payment=None).provider_reference
        with self.assertRaisesMessage(ProviderError, "does not support cancelling"):
            no_cancel_provider.cancel(ref3)

    # 5. webhook signature verification
    @override_settings(FAKE_PROVIDER_SECRET="test-secret")
    def test_webhook_signature(self):
        payload = {"reference": "fake_123", "status": "paid", "event_id": "evt1"}
        body = json.dumps(payload).encode("utf-8")
        sig = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        # valid webhook
        ev = self.provider.parse_webhook({"X-Fake-Signature": sig}, body)
        self.assertIsInstance(ev, ProviderEvent)
        self.assertEqual(ev.provider, "fake")
        self.assertEqual(ev.status, "succeeded")
        # missing signature
        with self.assertRaises(ProviderError):
            self.provider.parse_webhook({}, body)
        # wrong signature
        with self.assertRaises(ProviderError):
            self.provider.parse_webhook({"X-Fake-Signature": "bad"}, body)
        # tampered payload with original signature
        bad_body = json.dumps({"reference": "fake_123", "status": "failed"}).encode("utf-8")
        with self.assertRaises(ProviderError):
            self.provider.parse_webhook({"X-Fake-Signature": sig}, bad_body)

    # 6. registry lookup behaviour
    def test_registry_lookup(self):
        with override_settings(ENABLE_FAKE_PROVIDER=True):
            p = get_provider("fake")
            self.assertIsInstance(p, FakeProvider)
        with self.assertRaises(ProviderError):
            get_provider("nope")
        with override_settings(DEBUG=False, ENABLE_FAKE_PROVIDER=False):
            with self.assertRaises(ProviderError):
                get_provider("fake")

    # 7. conditional unique constraint on (provider, provider_reference)
    def test_unique_constraint(self):
        # first payment with provider data – succeeds
        p1 = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="k1",
            amount=Decimal("10"),
            sender="A",
            receiver="B",
            provider="fake",
            provider_reference="ref-1",
        )
        self.assertIsNotNone(p1.pk)
        # duplicate should raise IntegrityError
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Payment.objects.create(
                    merchant=self.merchant,
                    idempotency_key="k2",
                    amount=Decimal("5"),
                    sender="C",
                    receiver="D",
                    provider="fake",
                    provider_reference="ref-1",
                )
        # payments without provider info can duplicate freely
        for i in range(2):
            p = Payment.objects.create(
                merchant=self.merchant,
                idempotency_key=f"none-{i}",
                amount=Decimal("1"),
                sender="X",
                receiver="Y",
                provider=None,
                provider_reference=None,
            )
            self.assertIsNotNone(p.pk)

    # 8. legacy payment creation (no provider fields) still works
    def test_legacy_creation(self):
        p = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="legacy",
            amount=Decimal("75"),
            sender="OldSender",
            receiver="OldReceiver",
        )
        p.refresh_from_db()
        self.assertIsNone(p.provider)
        self.assertIsNone(p.provider_reference)
        self.assertIsNone(p.checkout_url)
        self.assertIsNone(p.provider_status)
        self.assertEqual(p.status, "pending")

    def test_confirm_from_webhook_trusts_verify(self):
        ref = self.provider.initialize(payment=None).provider_reference
        # Real state is pending. Webhook claims paid.
        payload = {"reference": ref, "status": "paid", "event_id": "evt1"}
        body = json.dumps(payload).encode("utf-8")
        sig = hmac.new(b"fake-secret-for-tests", body, hashlib.sha256).hexdigest()
        
        with override_settings(FAKE_PROVIDER_SECRET="fake-secret-for-tests"):
            result = self.provider.confirm_from_webhook({"X-Fake-Signature": sig}, body)
            self.assertEqual(result.status, "pending")
            
            # Tampered body raises error
            bad_body = json.dumps({"reference": ref, "status": "failed"}).encode("utf-8")
            with self.assertRaises(ProviderError):
                self.provider.confirm_from_webhook({"X-Fake-Signature": sig}, bad_body)

    def test_status_expired(self):
        from payments.models import InvalidStateTransition
        
        # pending -> expired is allowed and records history
        p1 = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-1",
            amount=Decimal("10"),
            sender="A",
            receiver="B",
        )
        p1.transition_to("expired")
        self.assertEqual(p1.status, "expired")
        self.assertTrue(p1.status_history.filter(to_status="expired").exists())
        
        # processing -> expired is rejected
        p2 = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="exp-2",
            amount=Decimal("10"),
            sender="A",
            receiver="B",
            status="processing"
        )
        with self.assertRaises(InvalidStateTransition):
            p2.transition_to("expired")
            
        # expired has no exits
        with self.assertRaises(InvalidStateTransition):
            p1.transition_to("failed")

    def test_provider_status_field(self):
        p = Payment.objects.create(
            merchant=self.merchant,
            idempotency_key="ps-1",
            amount=Decimal("10"),
            sender="A",
            receiver="B",
            provider_status="INITIALIZED_BY_USER"
        )
        p.refresh_from_db()
        self.assertEqual(p.provider_status, "INITIALIZED_BY_USER")
