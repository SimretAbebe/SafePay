import base64
import hashlib
import hmac
import json
import logging
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from payments.admin import MerchantAdmin
from payments.models import Merchant, Payment, WebhookDelivery
from payments.providers import (
    InitResult,
    PaymentProvider,
    ProviderStatusResult,
    register_provider,
)
from payments.providers.fake import FakeProvider
from payments.providers.registry import _registry
from payments.services import notify_status_change
from payments.tasks import deliver_webhook_attempt
from payments.test_utils import create_test_merchant
from payments.webhook_secrets import derive_merchant_secret
from payments.webhook_verification import verify_webhook


class LiveCapableDouble(FakeProvider):
    name = "live_double"
    supports_livemode = True


class TestOnlyDouble(FakeProvider):
    name = "test_double"
    supports_livemode = False


class DummyMessageRequest:
    def __init__(self):
        self._messages = []

    def get_messages(self):
        return self._messages


class LiveModeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.factory = RequestFactory()

        # Register test doubles
        register_provider(LiveCapableDouble())
        register_provider(TestOnlyDouble())

        # Merchant A: live_enabled=True
        self.merchant_a, self.test_key_a = create_test_merchant(
            name="Merchant A",
            webhook_url="https://example.com/test-webhook",
            live_webhook_url="https://example.com/live-webhook",
            live_enabled=True,
            default_provider="live_double",
        )
        with override_settings(LIVE_MODE_AVAILABLE=True):
            self.live_key_a = self.merchant_a.create_live_key()

        # Merchant B: live_enabled=False
        self.merchant_b, self.test_key_b = create_test_merchant(
            name="Merchant B",
            webhook_url="https://example.com/merchant-b-webhook",
            live_enabled=False,
            default_provider="test_double",
        )

    def tearDown(self):
        cache.clear()
        _registry.pop("live_double", None)
        _registry.pop("test_double", None)

    # 1. A legacy (unprefixed) key still authenticates as test mode, and payments get livemode False.
    def test_legacy_key_authenticates_as_test_mode(self):
        merchant_legacy = Merchant.objects.create(
            name="Legacy Shop",
            api_key_hash=Merchant.hash_key("legacy-raw-key-1234567890"),
            key_prefix="legacy-r",
            is_active=True,
        )
        response = self.client.post(
            reverse("create-payment"),
            {
                "amount": "100.00",
                "sender": "alice",
                "receiver": "Legacy Shop",
                "idempotency_key": "legacy-key-test-1",
            },
            format="json",
            HTTP_X_API_KEY="legacy-raw-key-1234567890",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(response.data["livemode"])

        payment = Payment.objects.get(id=response.data["id"])
        self.assertFalse(payment.livemode)
        self.assertEqual(payment.merchant, merchant_legacy)

    # 2. regenerate_test_key returns an sp_test_ key that works and invalidates the old test key;
    #    only hashes are stored (assert the raw key is in no model field).
    def test_regenerate_test_key(self):
        old_test_key = self.test_key_a
        new_test_key = self.merchant_a.regenerate_test_key()

        self.assertTrue(new_test_key.startswith("sp_test_"))
        self.assertNotEqual(old_test_key, new_test_key)

        # Old key is invalidated
        old_resp = self.client.post(
            reverse("create-payment"),
            {
                "amount": "50.00",
                "sender": "alice",
                "receiver": "Merchant A",
                "idempotency_key": "old-key-attempt",
            },
            format="json",
            HTTP_X_API_KEY=old_test_key,
        )
        self.assertEqual(old_resp.status_code, status.HTTP_403_FORBIDDEN)

        # New key works
        new_resp = self.client.post(
            reverse("create-payment"),
            {
                "amount": "50.00",
                "sender": "alice",
                "receiver": "Merchant A",
                "idempotency_key": "new-key-attempt",
            },
            format="json",
            HTTP_X_API_KEY=new_test_key,
        )
        self.assertEqual(new_resp.status_code, status.HTTP_201_CREATED)
        self.assertFalse(new_resp.data["livemode"])

        # Check raw key is not stored in any model field
        self.merchant_a.refresh_from_db()
        for field in self.merchant_a._meta.fields:
            value = getattr(self.merchant_a, field.name)
            self.assertNotEqual(value, new_test_key)
            self.assertNotEqual(value, old_test_key)
            if isinstance(value, str):
                self.assertNotIn(new_test_key, value)

    # 3. A live key is rejected with 403 when LIVE_MODE_AVAILABLE is False;
    #    rejected with 403 when live_enabled is False even if the global switch is on;
    #    no payment is created in either case.
    def test_live_key_rejected_when_gated_off(self):
        initial_count = Payment.objects.count()

        # Switch 1 off: LIVE_MODE_AVAILABLE = False, merchant.live_enabled = True
        with override_settings(LIVE_MODE_AVAILABLE=False):
            resp1 = self.client.post(
                reverse("create-payment"),
                {
                    "amount": "75.00",
                    "sender": "alice",
                    "receiver": "Merchant A",
                    "idempotency_key": "gated-off-attempt-1",
                },
                format="json",
                HTTP_X_API_KEY=self.live_key_a,
            )
            self.assertEqual(resp1.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(resp1.data["detail"], "Live mode is not available yet.")
            self.assertEqual(Payment.objects.count(), initial_count)

        # Switch 2 off: LIVE_MODE_AVAILABLE = True, merchant.live_enabled = False
        self.merchant_a.live_enabled = False
        self.merchant_a.save()
        with override_settings(LIVE_MODE_AVAILABLE=True):
            resp2 = self.client.post(
                reverse("create-payment"),
                {
                    "amount": "75.00",
                    "sender": "alice",
                    "receiver": "Merchant A",
                    "idempotency_key": "gated-off-attempt-2",
                },
                format="json",
                HTTP_X_API_KEY=self.live_key_a,
            )
            self.assertEqual(resp2.status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(resp2.data["detail"], "Live mode is not enabled for this account.")
            self.assertEqual(Payment.objects.count(), initial_count)

    # 4. create_live_key raises when live_enabled is False or LIVE_MODE_AVAILABLE is False;
    #    works when both are true.
    def test_create_live_key_requirements(self):
        with override_settings(LIVE_MODE_AVAILABLE=False):
            with self.assertRaises(ValueError):
                self.merchant_a.create_live_key()

        with override_settings(LIVE_MODE_AVAILABLE=True):
            with self.assertRaises(ValueError):
                self.merchant_b.create_live_key()

            # Works when both are true
            key = self.merchant_a.create_live_key()
            self.assertTrue(key.startswith("sp_live_"))

    # 5. With both switches on, a live key authenticates and creates a payment with livemode True
    #    using the live-capable double; with no live-capable provider the response is 400
    #    and NO payment is created.
    def test_live_key_creates_livemode_payment_or_400(self):
        with override_settings(LIVE_MODE_AVAILABLE=True):
            # Live capable provider
            resp = self.client.post(
                reverse("create_checkout_session"),
                {
                    "amount": "200.00",
                    "provider": "live_double",
                },
                format="json",
                HTTP_X_API_KEY=self.live_key_a,
                HTTP_IDEMPOTENCY_KEY="live-checkout-1",
            )
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
            self.assertTrue(resp.data["livemode"])
            payment = Payment.objects.get(id=resp.data["id"])
            self.assertTrue(payment.livemode)

            # No live-capable provider
            count_before = Payment.objects.count()
            resp_no_live = self.client.post(
                reverse("create_checkout_session"),
                {
                    "amount": "200.00",
                    "provider": "test_double",
                    "allow_fallback": False,
                },
                format="json",
                HTTP_X_API_KEY=self.live_key_a,
                HTTP_IDEMPOTENCY_KEY="live-checkout-no-provider",
            )
            self.assertEqual(resp_no_live.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(resp_no_live.data["detail"], "No live-capable provider is configured.")
            self.assertEqual(Payment.objects.count(), count_before)

    # 6. Data separation: a payment made with a test key returns 404 for a live key on
    #    payment_detail, verify and cancel, and the reverse; another merchant is still 404.
    def test_data_separation_between_test_and_live(self):
        with override_settings(LIVE_MODE_AVAILABLE=True):
            # Test payment
            resp_test = self.client.post(
                reverse("create-payment"),
                {
                    "amount": "10.00",
                    "sender": "alice",
                    "receiver": "Merchant A",
                    "idempotency_key": "sep-test-pay",
                },
                format="json",
                HTTP_X_API_KEY=self.test_key_a,
            )
            test_pay_id = resp_test.data["id"]

            # Live payment
            resp_live = self.client.post(
                reverse("create-payment"),
                {
                    "amount": "20.00",
                    "sender": "alice",
                    "receiver": "Merchant A",
                    "idempotency_key": "sep-live-pay",
                },
                format="json",
                HTTP_X_API_KEY=self.live_key_a,
            )
            live_pay_id = resp_live.data["id"]

            # Live key cannot access test payment
            self.assertEqual(
                self.client.get(reverse("payment_detail", args=[test_pay_id]), HTTP_X_API_KEY=self.live_key_a).status_code,
                status.HTTP_404_NOT_FOUND,
            )
            self.assertEqual(
                self.client.get(reverse("verify_payment", args=[test_pay_id]), HTTP_X_API_KEY=self.live_key_a).status_code,
                status.HTTP_404_NOT_FOUND,
            )
            self.assertEqual(
                self.client.post(reverse("cancel_payment", args=[test_pay_id]), HTTP_X_API_KEY=self.live_key_a).status_code,
                status.HTTP_404_NOT_FOUND,
            )

            # Test key cannot access live payment
            self.assertEqual(
                self.client.get(reverse("payment_detail", args=[live_pay_id]), HTTP_X_API_KEY=self.test_key_a).status_code,
                status.HTTP_404_NOT_FOUND,
            )
            self.assertEqual(
                self.client.get(reverse("verify_payment", args=[live_pay_id]), HTTP_X_API_KEY=self.test_key_a).status_code,
                status.HTTP_404_NOT_FOUND,
            )
            self.assertEqual(
                self.client.post(reverse("cancel_payment", args=[live_pay_id]), HTTP_X_API_KEY=self.test_key_a).status_code,
                status.HTTP_404_NOT_FOUND,
            )

            # Merchant B cannot access either
            self.assertEqual(
                self.client.get(reverse("payment_detail", args=[test_pay_id]), HTTP_X_API_KEY=self.test_key_b).status_code,
                status.HTTP_404_NOT_FOUND,
            )
            self.assertEqual(
                self.client.get(reverse("payment_detail", args=[live_pay_id]), HTTP_X_API_KEY=self.test_key_b).status_code,
                status.HTTP_404_NOT_FOUND,
            )

    # 7. The same Idempotency-Key used once in test mode and once in live mode creates
    #    two separate payments; reusing it in the same mode returns the original.
    def test_same_idempotency_key_test_and_live(self):
        with override_settings(LIVE_MODE_AVAILABLE=True):
            shared_idemp = "idemp-shared-mode-key"

            resp_test_1 = self.client.post(
                reverse("create-payment"),
                {
                    "amount": "11.00",
                    "sender": "alice",
                    "receiver": "Merchant A",
                    "idempotency_key": shared_idemp,
                },
                format="json",
                HTTP_X_API_KEY=self.test_key_a,
            )
            self.assertEqual(resp_test_1.status_code, status.HTTP_201_CREATED)
            test_id = resp_test_1.data["id"]

            resp_live_1 = self.client.post(
                reverse("create-payment"),
                {
                    "amount": "22.00",
                    "sender": "alice",
                    "receiver": "Merchant A",
                    "idempotency_key": shared_idemp,
                },
                format="json",
                HTTP_X_API_KEY=self.live_key_a,
            )
            self.assertEqual(resp_live_1.status_code, status.HTTP_201_CREATED)
            live_id = resp_live_1.data["id"]

            self.assertNotEqual(test_id, live_id)

            # Reusing in test mode returns original test payment
            resp_test_2 = self.client.post(
                reverse("create-payment"),
                {
                    "amount": "11.00",
                    "sender": "alice",
                    "receiver": "Merchant A",
                    "idempotency_key": shared_idemp,
                },
                format="json",
                HTTP_X_API_KEY=self.test_key_a,
            )
            self.assertEqual(resp_test_2.status_code, status.HTTP_200_OK)
            self.assertEqual(resp_test_2.data["id"], test_id)

            # Reusing in live mode returns original live payment
            resp_live_2 = self.client.post(
                reverse("create-payment"),
                {
                    "amount": "22.00",
                    "sender": "alice",
                    "receiver": "Merchant A",
                    "idempotency_key": shared_idemp,
                },
                format="json",
                HTTP_X_API_KEY=self.live_key_a,
            )
            self.assertEqual(resp_live_2.status_code, status.HTTP_200_OK)
            self.assertEqual(resp_live_2.data["id"], live_id)

    # 8. The fake provider is refused in live mode even when configured as a fallback.
    def test_fake_provider_refused_in_live_mode(self):
        with override_settings(LIVE_MODE_AVAILABLE=True):
            self.merchant_a.default_provider = "fake"
            self.merchant_a.fallback_providers = ["fake"]
            self.merchant_a.save()

            count_before = Payment.objects.count()
            resp = self.client.post(
                reverse("create_checkout_session"),
                {
                    "amount": "100.00",
                    "allow_fallback": True,
                },
                format="json",
                HTTP_X_API_KEY=self.live_key_a,
                HTTP_IDEMPOTENCY_KEY="fake-live-refuse-key",
            )
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(resp.data["detail"], "No live-capable provider is configured.")
            self.assertEqual(Payment.objects.count(), count_before)

    # 9. Throttle buckets are separate: exhausting the test bucket does not throttle
    #    the live key (clear the cache in setUp).
    @override_settings(PAYMENT_THROTTLE_RATE="2/minute", LIVE_MODE_AVAILABLE=True)
    def test_throttle_buckets_separate(self):
        cache.clear()

        # 1st test request -> ok
        r1 = self.client.post(
            reverse("create-payment"),
            {"amount": "1.00", "sender": "a", "receiver": "b", "idempotency_key": "t1"},
            format="json",
            HTTP_X_API_KEY=self.test_key_a,
        )
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)

        # 2nd test request -> ok
        r2 = self.client.post(
            reverse("create-payment"),
            {"amount": "1.00", "sender": "a", "receiver": "b", "idempotency_key": "t2"},
            format="json",
            HTTP_X_API_KEY=self.test_key_a,
        )
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)

        # 3rd test request -> throttled
        r3 = self.client.post(
            reverse("create-payment"),
            {"amount": "1.00", "sender": "a", "receiver": "b", "idempotency_key": "t3"},
            format="json",
            HTTP_X_API_KEY=self.test_key_a,
        )
        self.assertEqual(r3.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

        # Live key request -> NOT throttled
        r_live = self.client.post(
            reverse("create-payment"),
            {"amount": "1.00", "sender": "a", "receiver": "b", "idempotency_key": "l1"},
            format="json",
            HTTP_X_API_KEY=self.live_key_a,
        )
        self.assertEqual(r_live.status_code, status.HTTP_201_CREATED)

    # 10. Webhooks: a test payment's event goes to webhook_url and verifies with the test secret;
    #     a live payment's event goes to live_webhook_url and verifies ONLY with the live-derived secret;
    #     a live payment with no live_webhook_url sends nothing; the payload contains the correct "livemode" value.
    @patch("payments.tasks.is_target_safe", return_value=True)
    @patch("requests.post")
    def test_webhooks_test_and_live(self, mock_post, mock_safe):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        # 1. Test payment webhook
        test_payment = Payment.objects.create(
            merchant=self.merchant_a,
            livemode=False,
            amount="50.00",
            sender="alice",
            receiver="Merchant A",
            idempotency_key="webhook-test-pay",
            status="pending",
        )
        test_payment.transition_to("processing")
        test_payment.transition_to("succeeded")
        test_delivery = notify_status_change(test_payment)
        self.assertIsNotNone(test_delivery)
        self.assertEqual(test_delivery.target_url, self.merchant_a.webhook_url)
        self.assertFalse(test_delivery.payload["livemode"])

        deliver_webhook_attempt(test_delivery.id)
        mock_post.assert_called()
        headers = mock_post.call_args[1]["headers"]
        test_sig = headers["X-SafePay-Signature"]
        timestamp = headers["X-SafePay-Timestamp"]
        delivered_body = mock_post.call_args[1]["data"]

        # Verifies with test secret
        test_secret = derive_merchant_secret(self.merchant_a, livemode=False)
        self.assertTrue(verify_webhook(test_secret, timestamp, test_sig, delivered_body))
        # Does NOT verify with live secret
        live_secret = derive_merchant_secret(self.merchant_a, livemode=True)
        self.assertFalse(verify_webhook(live_secret, timestamp, test_sig, delivered_body))

        # 2. Live payment webhook
        mock_post.reset_mock()
        live_payment = Payment.objects.create(
            merchant=self.merchant_a,
            livemode=True,
            amount="50.00",
            sender="alice",
            receiver="Merchant A",
            idempotency_key="webhook-live-pay",
            status="pending",
        )
        live_payment.transition_to("processing")
        live_payment.transition_to("succeeded")
        live_delivery = notify_status_change(live_payment)
        self.assertIsNotNone(live_delivery)
        self.assertEqual(live_delivery.target_url, self.merchant_a.live_webhook_url)
        self.assertTrue(live_delivery.payload["livemode"])

        deliver_webhook_attempt(live_delivery.id)
        mock_post.assert_called()
        headers = mock_post.call_args[1]["headers"]
        live_sig = headers["X-SafePay-Signature"]
        timestamp = headers["X-SafePay-Timestamp"]
        delivered_body_live = mock_post.call_args[1]["data"]

        # Verifies with live secret ONLY
        self.assertTrue(verify_webhook(live_secret, timestamp, live_sig, delivered_body_live))
        self.assertFalse(verify_webhook(test_secret, timestamp, live_sig, delivered_body_live))

        # 3. Live payment with no live_webhook_url sends nothing
        self.merchant_a.live_webhook_url = None
        self.merchant_a.save()
        live_payment_no_url = Payment.objects.create(
            merchant=self.merchant_a,
            livemode=True,
            amount="50.00",
            sender="alice",
            receiver="Merchant A",
            idempotency_key="webhook-live-no-url",
            status="pending",
        )
        live_payment_no_url.transition_to("processing")
        live_payment_no_url.transition_to("succeeded")
        result = notify_status_change(live_payment_no_url)
        self.assertIsNone(result)

    # 11. Regression: derive_merchant_secret(merchant) for test mode equals the Day 19 formula's value.
    def test_derive_merchant_secret_regression(self):
        master_secret = settings.WEBHOOK_SECRET
        message = f"merchant:{self.merchant_a.id}:v{self.merchant_a.webhook_secret_version}".encode("utf-8")
        digest = hmac.new(master_secret.encode("utf-8"), message, hashlib.sha256).digest()
        day19_value = f"whsec_{base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')}"

        self.assertEqual(derive_merchant_secret(self.merchant_a), day19_value)
        self.assertEqual(derive_merchant_secret(self.merchant_a, livemode=False), day19_value)

        # And live derivation is distinct
        self.assertNotEqual(derive_merchant_secret(self.merchant_a, livemode=True), day19_value)

    # 12. Admin actions: both key actions require exactly one merchant, show the key once,
    #     and the key never appears in captured logs.
    def test_admin_actions(self):
        merchant_admin = MerchantAdmin(Merchant, AdminSite())

        request = self.factory.get("/")
        # Setup message framework on request
        setattr(request, "session", {})
        messages_storage = FallbackStorage(request)
        setattr(request, "_messages", messages_storage)

        # 1. Regenerate test key: require exactly 1 merchant
        merchant_admin.regenerate_test_key_action(request, Merchant.objects.none())
        msg = list(messages_storage)[-1]
        self.assertEqual(msg.level, messages.ERROR)
        self.assertIn("exactly one merchant", msg.message)

        with self.assertLogs("payments", level="INFO") as captured:
            logging.getLogger("payments").info("admin test start")
            merchant_admin.regenerate_test_key_action(request, Merchant.objects.filter(id=self.merchant_a.id))
            raw_key_test = None
            for m in messages_storage:
                if "New test API key" in m.message:
                    raw_key_test = m.message.split(": ")[1].split(" ")[0]
            self.assertIsNotNone(raw_key_test)
            self.assertTrue(raw_key_test.startswith("sp_test_"))
            for log_msg in captured.output:
                self.assertNotIn(raw_key_test, log_msg)

        # 2. Create live key: require exactly 1 merchant and both gates
        merchant_admin.create_live_key_action(request, Merchant.objects.none())
        msg = list(messages_storage)[-1]
        self.assertEqual(msg.level, messages.ERROR)

        # Refused if live_enabled is False
        merchant_admin.create_live_key_action(request, Merchant.objects.filter(id=self.merchant_b.id))
        msg = list(messages_storage)[-1]
        self.assertEqual(msg.level, messages.ERROR)

        # Works with both gates on
        with override_settings(LIVE_MODE_AVAILABLE=True):
            with self.assertLogs("payments", level="INFO") as captured:
                logging.getLogger("payments").info("admin live test start")
                merchant_admin.create_live_key_action(request, Merchant.objects.filter(id=self.merchant_a.id))
                raw_key_live = None
                for m in messages_storage:
                    if "Live API key" in m.message:
                        raw_key_live = m.message.split(": ")[1].split(" ")[0]
                self.assertIsNotNone(raw_key_live)
                self.assertTrue(raw_key_live.startswith("sp_live_"))
                for log_msg in captured.output:
                    self.assertNotIn(raw_key_live, log_msg)

    # 13. Checkout and payment_detail responses include "livemode"; verify's body is unchanged.
    def test_response_shapes_include_livemode(self):
        with override_settings(LIVE_MODE_AVAILABLE=True):
            resp_checkout = self.client.post(
                reverse("create_checkout_session"),
                {
                    "amount": "120.00",
                    "provider": "live_double",
                },
                format="json",
                HTTP_X_API_KEY=self.live_key_a,
                HTTP_IDEMPOTENCY_KEY="shape-checkout-1",
            )
            self.assertEqual(resp_checkout.status_code, status.HTTP_201_CREATED)
            self.assertIn("livemode", resp_checkout.data)
            self.assertTrue(resp_checkout.data["livemode"])

            pay_id = resp_checkout.data["id"]

            resp_detail = self.client.get(
                reverse("payment_detail", args=[pay_id]),
                HTTP_X_API_KEY=self.live_key_a,
            )
            self.assertEqual(resp_detail.status_code, status.HTTP_200_OK)
            self.assertIn("livemode", resp_detail.data)
            self.assertTrue(resp_detail.data["livemode"])

            resp_verify = self.client.get(
                reverse("verify_payment", args=[pay_id]),
                HTTP_X_API_KEY=self.live_key_a,
            )
            self.assertEqual(resp_verify.status_code, status.HTTP_200_OK)
            self.assertEqual(set(resp_verify.data.keys()), {"id", "status", "updated_at"})
