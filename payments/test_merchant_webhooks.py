import hashlib
import hmac
import json
import logging
import socket
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase, override_settings

from payments.admin import MerchantAdmin, MerchantAdminForm
from payments.models import Merchant, Payment, WebhookDelivery
from payments.services import notify_status_change
from payments.tasks import deliver_webhook_attempt
from payments.test_utils import create_test_merchant
from payments.webhook_secrets import derive_merchant_secret
from payments.webhook_urls import is_target_safe, validate_webhook_url
from payments.webhook_verification import verify_webhook


def _mock_public_dns(hostname, *args, **kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
    ]


def _mock_private_dns(hostname, *args, **kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
    ]


class MerchantWebhookTests(TestCase):

    def setUp(self):
        self.merchant_a, self.key_a = create_test_merchant(
            name="Merchant Alpha",
            webhook_url="https://alpha.example.com/webhook",
        )
        self.merchant_b, self.key_b = create_test_merchant(
            name="Merchant Beta",
            webhook_url="https://beta.example.com/webhook",
        )

    # 1. Merchant A (with a webhook URL) gets a delivery targeting A's URL,
    # and the signature verifies with A's derived secret via verify_webhook and FAILS with B's secret.
    @patch("payments.webhook_urls.socket.getaddrinfo", side_effect=_mock_public_dns)
    @patch("payments.tasks.requests.post")
    def test_merchant_a_delivery_targets_a_url_and_verifies_with_a_secret_only(
        self, mock_post, mock_dns
    ):
        mock_post.return_value = MagicMock(status_code=200)

        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="pay-a-1",
            amount=100.00,
            sender="alice",
            receiver="bob",
        )
        payment.transition_to("processing")
        payment.transition_to("succeeded")

        webhook = notify_status_change(payment)
        webhook.refresh_from_db()

        self.assertEqual(webhook.target_url, self.merchant_a.webhook_url)
        self.assertEqual(webhook.status, "succeeded")

        call_args = mock_post.call_args
        self.assertEqual(call_args.args[0], self.merchant_a.webhook_url)

        headers = call_args.kwargs["headers"]
        data = call_args.kwargs["data"]
        timestamp = headers["X-SafePay-Timestamp"]
        signature = headers["X-SafePay-Signature"]

        secret_a = derive_merchant_secret(self.merchant_a)
        secret_b = derive_merchant_secret(self.merchant_b)

        # Signature must verify with secret_a
        self.assertTrue(
            verify_webhook(
                secret=secret_a,
                timestamp=timestamp,
                signature=signature,
                body=data,
            )
        )
        # Signature must FAIL with secret_b
        self.assertFalse(
            verify_webhook(
                secret=secret_b,
                timestamp=timestamp,
                signature=signature,
                body=data,
            )
        )

    # 2. Two merchants each get their own events only: A's events never target B's URL and vice versa.
    @patch("payments.webhook_urls.socket.getaddrinfo", side_effect=_mock_public_dns)
    @patch("payments.tasks.requests.post")
    def test_two_merchants_get_their_own_events_only(self, mock_post, mock_dns):
        mock_post.return_value = MagicMock(status_code=200)

        payment_a = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="pay-a-2",
            amount=50.00,
            sender="alice",
            receiver="bob",
        )
        payment_a.transition_to("cancelled")
        delivery_a = notify_status_change(payment_a)

        payment_b = Payment.objects.create(
            merchant=self.merchant_b,
            idempotency_key="pay-b-2",
            amount=75.00,
            sender="carol",
            receiver="dave",
        )
        payment_b.transition_to("cancelled")
        delivery_b = notify_status_change(payment_b)

        self.assertEqual(delivery_a.target_url, self.merchant_a.webhook_url)
        self.assertEqual(delivery_b.target_url, self.merchant_b.webhook_url)
        self.assertNotEqual(delivery_a.target_url, delivery_b.target_url)

        posted_urls = [call.args[0] for call in mock_post.call_args_list]
        self.assertEqual(posted_urls, [self.merchant_a.webhook_url, self.merchant_b.webhook_url])

    # 3. A merchant with no webhook_url: the payment transition works, no WebhookDelivery exists,
    # nothing is sent, no fallback to the global URL.
    @patch("payments.tasks.deliver_webhook_attempt.delay")
    def test_merchant_with_no_webhook_url_creates_no_delivery_and_no_fallback(self, mock_delay):
        merchant_no_url, _ = create_test_merchant(name="Shop No URL", webhook_url=None)
        payment = Payment.objects.create(
            merchant=merchant_no_url,
            idempotency_key="pay-nourl-1",
            amount=40.00,
            sender="alice",
            receiver="bob",
        )
        payment.transition_to("processing")
        payment.transition_to("succeeded")

        with self.assertLogs("payments.services", level="INFO") as cm:
            result = notify_status_change(payment)

        self.assertIsNone(result)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 0)
        mock_delay.assert_not_called()
        self.assertTrue(any(f"merchant {merchant_no_url.id}" in msg for msg in cm.output))

    # 4. A legacy payment (merchant None) still uses the global URL and global secret.
    @patch("payments.webhook_urls.socket.getaddrinfo", side_effect=_mock_public_dns)
    @patch("payments.tasks.requests.post")
    def test_legacy_payment_uses_global_url_and_global_secret(self, mock_post, mock_dns):
        mock_post.return_value = MagicMock(status_code=200)

        legacy_payment = Payment.objects.create(
            merchant=None,
            idempotency_key="legacy-pay-1",
            amount=120.00,
            sender="alice",
            receiver="bob",
        )
        legacy_payment.transition_to("cancelled")

        delivery = notify_status_change(legacy_payment)
        delivery.refresh_from_db()

        self.assertEqual(delivery.target_url, settings.WEBHOOK_TARGET_URL)
        self.assertEqual(delivery.status, "succeeded")

        call_args = mock_post.call_args
        self.assertEqual(call_args.args[0], settings.WEBHOOK_TARGET_URL)

        headers = call_args.kwargs["headers"]
        data = call_args.kwargs["data"]
        timestamp = headers["X-SafePay-Timestamp"]
        signature = headers["X-SafePay-Signature"]

        # Verifies with global WEBHOOK_SECRET
        self.assertTrue(
            verify_webhook(
                secret=settings.WEBHOOK_SECRET,
                timestamp=timestamp,
                signature=signature,
                body=data,
            )
        )

    # 5. derive_merchant_secret is deterministic, differs per merchant, and differs after a version bump;
    # after rotation the old secret no longer verifies new deliveries; no secret is stored in any database field.
    @patch("payments.webhook_urls.socket.getaddrinfo", side_effect=_mock_public_dns)
    @patch("payments.tasks.requests.post")
    def test_derive_merchant_secret_properties_and_no_db_storage(self, mock_post, mock_dns):
        mock_post.return_value = MagicMock(status_code=200)

        # Deterministic
        secret_a1 = derive_merchant_secret(self.merchant_a)
        secret_a2 = derive_merchant_secret(self.merchant_a)
        self.assertEqual(secret_a1, secret_a2)
        self.assertTrue(secret_a1.startswith("whsec_"))

        # Differs per merchant
        secret_b = derive_merchant_secret(self.merchant_b)
        self.assertNotEqual(secret_a1, secret_b)

        # Differs after version bump
        secret_a_v2 = derive_merchant_secret(self.merchant_a, version=2)
        self.assertNotEqual(secret_a1, secret_a_v2)

        # Check rotation invalidation on new deliveries
        self.merchant_a.webhook_secret_version = 2
        self.merchant_a.save(update_fields=["webhook_secret_version"])

        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="pay-rot-1",
            amount=88.00,
            sender="alice",
            receiver="bob",
        )
        payment.transition_to("cancelled")
        delivery = notify_status_change(payment)

        call_args = mock_post.call_args
        headers = call_args.kwargs["headers"]
        data = call_args.kwargs["data"]

        # Old secret v1 fails
        self.assertFalse(
            verify_webhook(
                secret=secret_a1,
                timestamp=headers["X-SafePay-Timestamp"],
                signature=headers["X-SafePay-Signature"],
                body=data,
            )
        )
        # New secret v2 succeeds
        self.assertTrue(
            verify_webhook(
                secret=secret_a_v2,
                timestamp=headers["X-SafePay-Timestamp"],
                signature=headers["X-SafePay-Signature"],
                body=data,
            )
        )

        # Assert no secret is stored in any Merchant database field
        self.merchant_a.refresh_from_db()
        for field in Merchant._meta.get_fields():
            if field.concrete and not field.is_relation:
                val = getattr(self.merchant_a, field.name)
                self.assertNotIn("whsec_", str(val))
                self.assertNotEqual(val, secret_a1)
                self.assertNotEqual(val, secret_a_v2)

    # 6. validate_webhook_url: accepts a public https URL (resolver mocked to a public IP);
    # rejects http, file:// and ftp://, URLs with credentials, loopback and localhost,
    # 10.x, 172.16.x, 192.168.x, 169.254.169.254, ::1, 0.0.0.0, an IPv4-mapped private IPv6 address,
    # a hostname that resolves to a private IP, a hostname with mixed public and private answers,
    # an unresolvable hostname, and an overlong URL. With ALLOW_PRIVATE_WEBHOOK_TARGETS=True loopback http is accepted.
    def test_validate_webhook_url_validation_matrix(self):
        # Public HTTPS URL accepted
        validate_webhook_url("https://safe-endpoint.example.com/webhook", resolver=_mock_public_dns)

        # Rejects non-https schemes
        for bad_url in [
            "http://safe-endpoint.example.com/webhook",
            "file:///etc/passwd",
            "ftp://ftp.example.com/files",
        ]:
            with self.assertRaises(ValidationError, msg=f"Should reject {bad_url}"):
                validate_webhook_url(bad_url, resolver=_mock_public_dns)

        # Rejects credentials
        for cred_url in [
            "https://user:pass@safe-endpoint.example.com/webhook",
            "https://user@safe-endpoint.example.com/webhook",
        ]:
            with self.assertRaises(ValidationError, msg=f"Should reject {cred_url}"):
                validate_webhook_url(cred_url, resolver=_mock_public_dns)

        # Rejects private/loopback IP-literals
        disallowed_literals = [
            "https://127.0.0.1/webhook",
            "https://localhost/webhook",
            "https://10.0.0.1/webhook",
            "https://172.16.0.1/webhook",
            "https://192.168.1.1/webhook",
            "https://169.254.169.254/latest/meta-data/",
            "https://[::1]/webhook",
            "https://0.0.0.0/webhook",
            "https://[::ffff:127.0.0.1]/webhook",
            "https://[::ffff:10.0.0.1]/webhook",
        ]
        for bad_lit in disallowed_literals:
            with self.assertRaises(ValidationError, msg=f"Should reject literal {bad_lit}"):
                validate_webhook_url(bad_lit)

        # Rejects hostname resolving to private IP
        with self.assertRaises(ValidationError):
            validate_webhook_url("https://evil.internal.corp/hook", resolver=_mock_private_dns)

        # Rejects mixed public and private answers
        def _mock_mixed_dns(host, port=None):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
            ]

        with self.assertRaises(ValidationError):
            validate_webhook_url("https://mixed.example.com/hook", resolver=_mock_mixed_dns)

        # Rejects unresolvable hostname
        def _mock_failing_dns(host, port=None):
            raise socket.gaierror("Name or service not known")

        with self.assertRaises(ValidationError):
            validate_webhook_url("https://unresolvable.invalid/hook", resolver=_mock_failing_dns)

        # Rejects overlong URL (> 500 characters)
        overlong_url = "https://example.com/" + ("a" * 500)
        with self.assertRaises(ValidationError):
            validate_webhook_url(overlong_url, resolver=_mock_public_dns)

        # With ALLOW_PRIVATE_WEBHOOK_TARGETS=True loopback http is accepted
        with override_settings(ALLOW_PRIVATE_WEBHOOK_TARGETS=True):
            validate_webhook_url("http://127.0.0.1:9001/hook", resolver=_mock_private_dns)
            validate_webhook_url("http://localhost:8000/hook", resolver=_mock_private_dns)

    # 7. Delivery-time recheck: a URL that was fine at save time but now resolves to a private IP
    # is NOT sent (assert requests.post not called), the delivery is failed with "blocked_target",
    # and no retry is scheduled.
    @patch("payments.tasks.deliver_webhook_attempt.apply_async")
    @patch("payments.webhook_urls.socket.getaddrinfo", side_effect=_mock_private_dns)
    @patch("payments.tasks.requests.post")
    def test_delivery_time_recheck_blocks_private_ip_and_no_retry(
        self, mock_post, mock_dns, mock_apply_async
    ):
        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="pay-rebinding-1",
            amount=50.00,
            sender="alice",
            receiver="bob",
        )
        payment.transition_to("cancelled")

        delivery = WebhookDelivery.objects.create(
            payment=payment,
            event_type="payment.cancelled",
            target_url="https://alpha.example.com/webhook",
            payload={"event": "cancelled"},
        )

        with self.assertLogs("payments.tasks", level="WARNING") as cm:
            deliver_webhook_attempt(delivery.id)

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "failed")
        self.assertEqual(delivery.last_error, "blocked_target")
        mock_post.assert_not_called()
        mock_apply_async.assert_not_called()
        self.assertTrue(any("blocked" in msg and "failed safety checks" in msg for msg in cm.output))

    # 8. A 302 response is not followed (assert only one request is made) and is treated as a failed
    # attempt that follows the normal retry rules.
    @patch("payments.tasks.deliver_webhook_attempt.apply_async")
    @patch("payments.webhook_urls.socket.getaddrinfo", side_effect=_mock_public_dns)
    @patch("payments.tasks.requests.post")
    def test_302_redirect_is_not_followed_and_treated_as_failed_attempt(
        self, mock_post, mock_dns, mock_apply_async
    ):
        mock_response = MagicMock(status_code=302)
        mock_post.return_value = mock_response

        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="pay-redirect-1",
            amount=60.00,
            sender="alice",
            receiver="bob",
        )
        payment.transition_to("cancelled")

        delivery = WebhookDelivery.objects.create(
            payment=payment,
            event_type="payment.cancelled",
            target_url="https://alpha.example.com/webhook",
            payload={"event": "cancelled"},
        )

        deliver_webhook_attempt(delivery.id)
        delivery.refresh_from_db()

        self.assertEqual(mock_post.call_count, 1)
        self.assertFalse(mock_post.call_args.kwargs.get("allow_redirects", True))
        self.assertEqual(delivery.last_response_code, 302)
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(delivery.status, "pending")
        mock_apply_async.assert_called_once()

    # 9. Admin: the form rejects an unsafe URL; "Rotate webhook secret" increments the version and shows
    # the secret once; "Show webhook secret" works for exactly one merchant and errors otherwise;
    # the secret never appears in captured logs.
    @patch("payments.webhook_urls.socket.getaddrinfo", side_effect=_mock_private_dns)
    def test_admin_form_and_actions(self, mock_dns):
        # Admin form rejects unsafe URL
        form = MerchantAdminForm(
            data={
                "name": "Admin Test Shop",
                "webhook_url": "http://127.0.0.1:8000/hook",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("webhook_url", form.errors)

        admin_site = AdminSite()
        merchant_admin = MerchantAdmin(Merchant, admin_site)
        factory = RequestFactory()
        request = factory.post("/admin/payments/merchant/")

        mock_messages = []

        def fake_message_user(req, msg, level=messages.INFO):
            mock_messages.append((msg, level))

        merchant_admin.message_user = fake_message_user

        # "Show webhook secret" with exactly 1 merchant
        qs_one = Merchant.objects.filter(id=self.merchant_a.id)
        with self.assertNoLogs(level="INFO"):
            merchant_admin.show_webhook_secret(request, qs_one)

        self.assertEqual(len(mock_messages), 1)
        msg_text, msg_level = mock_messages[0]
        secret_a = derive_merchant_secret(self.merchant_a)
        self.assertIn(secret_a, msg_text)
        self.assertEqual(msg_level, messages.SUCCESS)

        # "Show webhook secret" with 2 merchants -> error
        mock_messages.clear()
        qs_two = Merchant.objects.filter(id__in=[self.merchant_a.id, self.merchant_b.id])
        merchant_admin.show_webhook_secret(request, qs_two)
        self.assertEqual(len(mock_messages), 1)
        self.assertIn("exactly one", mock_messages[0][0])
        self.assertEqual(mock_messages[0][1], messages.ERROR)

        # "Rotate webhook secret" with exactly 1 merchant
        mock_messages.clear()
        v_before = self.merchant_a.webhook_secret_version
        with self.assertNoLogs(level="INFO"):
            merchant_admin.rotate_webhook_secret(request, qs_one)

        self.merchant_a.refresh_from_db()
        self.assertEqual(self.merchant_a.webhook_secret_version, v_before + 1)
        new_secret = derive_merchant_secret(self.merchant_a)
        self.assertEqual(len(mock_messages), 1)
        self.assertIn(new_secret, mock_messages[0][0])
        self.assertEqual(mock_messages[0][1], messages.SUCCESS)

    # 10. Retry behavior with a per-merchant secret: the event id is identical across retries,
    # the timestamp differs, and each attempt verifies with the merchant's secret.
    @patch("payments.tasks.time.time")
    @patch("payments.webhook_urls.socket.getaddrinfo", side_effect=_mock_public_dns)
    @patch("payments.tasks.requests.post")
    def test_retry_behavior_with_per_merchant_secret(self, mock_post, mock_dns, mock_time):
        mock_time.side_effect = [1700000000, 1700000010, 1700000020]
        mock_post.side_effect = [
            MagicMock(status_code=500),
            MagicMock(status_code=500),
            MagicMock(status_code=200),
        ]

        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="pay-retry-seq-1",
            amount=99.00,
            sender="alice",
            receiver="bob",
        )
        payment.transition_to("cancelled")

        delivery = notify_status_change(payment)
        delivery.refresh_from_db()

        self.assertEqual(delivery.status, "succeeded")
        self.assertEqual(delivery.attempt_count, 3)
        self.assertEqual(mock_post.call_count, 3)

        secret_a = derive_merchant_secret(self.merchant_a)
        attempts_headers = [call.kwargs["headers"] for call in mock_post.call_args_list]

        # Event ID constant across attempts
        for headers in attempts_headers:
            self.assertEqual(headers["X-SafePay-Event-Id"], str(delivery.event_id))

        # Fresh timestamp and valid signature on each attempt
        timestamps = [headers["X-SafePay-Timestamp"] for headers in attempts_headers]
        self.assertEqual(timestamps, ["1700000000", "1700000010", "1700000020"])

        for call in mock_post.call_args_list:
            headers = call.kwargs["headers"]
            body = call.kwargs["data"]
            ts = headers["X-SafePay-Timestamp"]
            self.assertTrue(
                verify_webhook(
                    secret=secret_a,
                    timestamp=ts,
                    signature=headers["X-SafePay-Signature"],
                    body=body,
                    now=float(ts),
                )
            )

    # 11. The webhook payload and header names are unchanged (compare to the existing webhook tests).
    @patch("payments.webhook_urls.socket.getaddrinfo", side_effect=_mock_public_dns)
    @patch("payments.tasks.requests.post")
    def test_webhook_payload_and_header_names_unchanged(self, mock_post, mock_dns):
        mock_post.return_value = MagicMock(status_code=200)

        payment = Payment.objects.create(
            merchant=self.merchant_a,
            idempotency_key="pay-headers-1",
            amount=150.00,
            sender="alice",
            receiver="bob",
        )
        payment.transition_to("processing")
        payment.transition_to("succeeded")

        delivery = notify_status_change(payment)
        delivery.refresh_from_db()

        # Check payload keys
        self.assertIn("id", delivery.payload)
        self.assertIn("type", delivery.payload)
        self.assertIn("data", delivery.payload)
        self.assertEqual(delivery.payload["id"], str(delivery.event_id))
        self.assertEqual(delivery.payload["type"], "payment.succeeded")

        data = delivery.payload["data"]
        self.assertEqual(data["payment_id"], payment.id)
        self.assertEqual(data["idempotency_key"], payment.idempotency_key)
        self.assertEqual(data["status"], "succeeded")
        self.assertEqual(data["amount"], str(payment.amount))

        # Check headers
        headers = mock_post.call_args.kwargs["headers"]
        self.assertIn("Content-Type", headers)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("X-SafePay-Event-Id", headers)
        self.assertIn("X-SafePay-Timestamp", headers)
        self.assertIn("X-SafePay-Signature", headers)
