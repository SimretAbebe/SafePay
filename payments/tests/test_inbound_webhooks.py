import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from payments.models import InboundWebhookEvent, Payment, PaymentStatusHistory, WebhookDelivery
from payments.providers import ArifPayProvider, ChapaProvider, ProviderError, ProviderStatusResult
from payments.test_utils import create_test_merchant


@override_settings(
    CHAPA_WEBHOOK_SECRET="test-webhook-secret-xyz",
    CHAPA_SECRET_KEY="test-chapa-key-123",
)
class InboundWebhookTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.mock_webhook_patcher = patch("payments.tasks.deliver_webhook_attempt.delay")
        self.mock_webhook_delay = self.mock_webhook_patcher.start()
        self.client = APIClient()
        self.merchant, self.api_key = create_test_merchant(
            "Webhook Merchant",
            webhook_url="https://merchant.example.com/webhook",
        )
        self.url = reverse("chapa_webhook")
        self.secret = "test-webhook-secret-xyz"

    def tearDown(self):
        self.mock_webhook_patcher.stop()
        cache.clear()

    def _sign_body(self, body_bytes: bytes, secret: str = None) -> str:
        s = (secret if secret is not None else self.secret).encode("utf-8")
        return hmac.new(s, body_bytes, hashlib.sha256).hexdigest()

    def _sign_secret(self, secret: str = None) -> str:
        s = (secret if secret is not None else self.secret).encode("utf-8")
        return hmac.new(s, s, hashlib.sha256).hexdigest()

    def _create_payment(self, tx_ref: str, status: str = "pending") -> Payment:
        return Payment.objects.create(
            merchant=self.merchant,
            idempotency_key=f"idem-{tx_ref}",
            amount=Decimal("100.00"),
            sender="customer@example.com",
            receiver="SafePay",
            provider="chapa",
            provider_reference=tx_ref,
            status=status,
        )

    # 1. Valid x-chapa-signature, verify (mocked) says success: 200, the payment becomes succeeded
    #    with exactly two history rows (pending->processing, processing->succeeded), exactly one
    #    WebhookDelivery, one InboundWebhookEvent with outcome processed_updated.
    @patch.object(ChapaProvider, "verify")
    def test_valid_signature_verify_success(self, mock_verify):
        tx_ref = "sp-valid-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        mock_verify.return_value = ProviderStatusResult(
            provider_reference=tx_ref,
            status="succeeded",
            raw={"status": "success", "tx_ref": tx_ref},
        )

        payload = {"event": "charge.success", "tx_ref": tx_ref, "status": "success"}
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = self._sign_body(body_bytes)

        response = self.client.post(
            self.url,
            data=body_bytes,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE=sig,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")

        histories = list(PaymentStatusHistory.objects.filter(payment=payment).order_by("changed_at"))
        self.assertEqual(len(histories), 2)
        self.assertEqual(histories[0].from_status, "pending")
        self.assertEqual(histories[0].to_status, "processing")
        self.assertEqual(histories[1].from_status, "processing")
        self.assertEqual(histories[1].to_status, "succeeded")

        deliveries = WebhookDelivery.objects.filter(payment=payment)
        self.assertEqual(deliveries.count(), 1)

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertTrue(event.signature_valid)
        self.assertEqual(event.outcome, "processed_updated")
        self.assertEqual(event.provider, "chapa")
        self.assertEqual(event.provider_reference, tx_ref)
        self.assertEqual(event.event_type, "charge.success")

    # 2. Wrong signature: 401, payment unchanged, verify NOT called,
    #    one InboundWebhookEvent with signature_valid False and outcome signature_rejected.
    @patch.object(ChapaProvider, "verify")
    def test_wrong_signature_rejected(self, mock_verify):
        tx_ref = "sp-wrong-sig-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        payload = {"event": "charge.success", "tx_ref": tx_ref, "status": "success"}
        body_bytes = json.dumps(payload).encode("utf-8")
        wrong_sig = "a" * 64

        response = self.client.post(
            self.url,
            data=body_bytes,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE=wrong_sig,
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Invalid signature."})

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        mock_verify.assert_not_called()

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertFalse(event.signature_valid)
        self.assertEqual(event.outcome, "signature_rejected")

    # 3. No signature headers at all: 401, same assertions.
    @patch.object(ChapaProvider, "verify")
    def test_no_signature_headers_rejected(self, mock_verify):
        tx_ref = "sp-no-sig-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        payload = {"event": "charge.success", "tx_ref": tx_ref, "status": "success"}
        body_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(
            self.url,
            data=body_bytes,
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Invalid signature."})

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        mock_verify.assert_not_called()

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertFalse(event.signature_valid)
        self.assertEqual(event.outcome, "signature_rejected")

    # 4. KEY TEST: a correctly signed webhook whose body says "success" while verify (mocked)
    #    says pending: 200, payment stays pending, no history, no WebhookDelivery.
    @patch.object(ChapaProvider, "verify")
    def test_signed_body_success_while_verify_pending(self, mock_verify):
        tx_ref = "sp-pending-verify-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        mock_verify.return_value = ProviderStatusResult(
            provider_reference=tx_ref,
            status="pending",
            raw={"status": "pending", "tx_ref": tx_ref},
        )

        payload = {"event": "charge.success", "tx_ref": tx_ref, "status": "success"}
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = self._sign_body(body_bytes)

        response = self.client.post(
            self.url,
            data=body_bytes,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE=sig,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertEqual(PaymentStatusHistory.objects.filter(payment=payment).count(), 0)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 0)

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertTrue(event.signature_valid)
        self.assertEqual(event.outcome, "processed_unchanged")

    # 5. Signed body says success but verify says failed: the payment follows verify and ends failed.
    @patch.object(ChapaProvider, "verify")
    def test_signed_body_success_but_verify_failed(self, mock_verify):
        tx_ref = "sp-failed-verify-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        mock_verify.return_value = ProviderStatusResult(
            provider_reference=tx_ref,
            status="failed",
            raw={"status": "failed", "tx_ref": tx_ref},
        )

        payload = {"event": "charge.success", "tx_ref": tx_ref, "status": "success"}
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = self._sign_body(body_bytes)

        response = self.client.post(
            self.url,
            data=body_bytes,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE=sig,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        payment.refresh_from_db()
        self.assertEqual(payment.status, "failed")

        histories = list(PaymentStatusHistory.objects.filter(payment=payment).order_by("changed_at"))
        self.assertEqual(len(histories), 2)
        self.assertEqual(histories[0].to_status, "processing")
        self.assertEqual(histories[1].to_status, "failed")

        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 1)

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().outcome, "processed_updated")

    # 6. The same valid webhook sent twice: only one set of history rows and one WebhookDelivery
    #    in total; second outcome processed_unchanged.
    @patch.object(ChapaProvider, "verify")
    def test_valid_webhook_sent_twice_idempotent(self, mock_verify):
        tx_ref = "sp-twice-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        mock_verify.return_value = ProviderStatusResult(
            provider_reference=tx_ref,
            status="succeeded",
            raw={"status": "success", "tx_ref": tx_ref},
        )

        payload = {"event": "charge.success", "tx_ref": tx_ref, "status": "success"}
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = self._sign_body(body_bytes)

        # First call
        res1 = self.client.post(
            self.url,
            data=body_bytes,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE=sig,
        )
        self.assertEqual(res1.status_code, 200)

        # Second call
        res2 = self.client.post(
            self.url,
            data=body_bytes,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE=sig,
        )
        self.assertEqual(res2.status_code, 200)

        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")
        self.assertEqual(PaymentStatusHistory.objects.filter(payment=payment).count(), 2)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 1)

        events = list(InboundWebhookEvent.objects.order_by("received_at"))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].outcome, "processed_updated")
        self.assertEqual(events[1].outcome, "processed_unchanged")

    # 7. Unknown tx_ref: 200 ignored, outcome unknown_reference, nothing changed.
    @patch.object(ChapaProvider, "verify")
    def test_unknown_tx_ref_ignored(self, mock_verify):
        tx_ref = "sp-unknown-999"
        payload = {"event": "charge.success", "tx_ref": tx_ref, "status": "success"}
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = self._sign_body(body_bytes)

        response = self.client.post(
            self.url,
            data=body_bytes,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE=sig,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})
        mock_verify.assert_not_called()

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertTrue(event.signature_valid)
        self.assertEqual(event.outcome, "unknown_reference")
        self.assertEqual(event.provider_reference, tx_ref)

    # 8. A refunded or unknown status payload (signed): 200 ignored, outcome ignored_unsupported, nothing changed.
    @patch.object(ChapaProvider, "verify")
    def test_refunded_or_unknown_status_ignored(self, mock_verify):
        tx_ref = "sp-refunded-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        # Test A: refunded
        payload_refunded = {"event": "charge.refunded", "tx_ref": tx_ref, "status": "refunded"}
        body_bytes = json.dumps(payload_refunded).encode("utf-8")
        sig = self._sign_body(body_bytes)

        response = self.client.post(
            self.url,
            data=body_bytes,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE=sig,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        mock_verify.assert_not_called()

        event = InboundWebhookEvent.objects.latest("received_at")
        self.assertTrue(event.signature_valid)
        self.assertEqual(event.outcome, "ignored_unsupported")

        # Test B: unknown status
        payload_unknown = {"event": "charge.partial", "tx_ref": tx_ref, "status": "partially_paid"}
        body_bytes_unk = json.dumps(payload_unknown).encode("utf-8")
        sig_unk = self._sign_body(body_bytes_unk)

        response_unk = self.client.post(
            self.url,
            data=body_bytes_unk,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE=sig_unk,
        )

        self.assertEqual(response_unk.status_code, 200)
        self.assertEqual(response_unk.json(), {"status": "ignored"})

        event_unk = InboundWebhookEvent.objects.latest("received_at")
        self.assertTrue(event_unk.signature_valid)
        self.assertEqual(event_unk.outcome, "ignored_unsupported")

    # 9. verify raises ProviderError: 503, payment unchanged, outcome verify_failed,
    #    no provider error text in the response.
    @patch.object(ChapaProvider, "verify")
    def test_verify_raises_provider_error(self, mock_verify):
        tx_ref = "sp-error-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        mock_verify.side_effect = ProviderError("Internal sensitive network timeout failure at chapa host")

        payload = {"event": "charge.success", "tx_ref": tx_ref, "status": "success"}
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = self._sign_body(body_bytes)

        response = self.client.post(
            self.url,
            data=body_bytes,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE=sig,
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Temporarily unable to confirm. Retry later."})
        self.assertNotIn("chapa host", response.content.decode("utf-8"))
        self.assertNotIn("Internal sensitive network", response.content.decode("utf-8"))

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertTrue(event.signature_valid)
        self.assertEqual(event.outcome, "verify_failed")

    # 10. The Chapa-Signature header alone (HMAC of the secret) is accepted for authenticity;
    #     and a body re-serialized as compact JSON with a matching x-chapa-signature is accepted.
    @patch.object(ChapaProvider, "verify")
    def test_chapa_signature_header_and_compact_reserialized_matching(self, mock_verify):
        # Subtest 1: Chapa-Signature header alone
        tx_ref_1 = "sp-chapa-sig-only"
        payment_1 = self._create_payment(tx_ref=tx_ref_1, status="pending")

        mock_verify.return_value = ProviderStatusResult(
            provider_reference=tx_ref_1,
            status="succeeded",
            raw={"status": "success", "tx_ref": tx_ref_1},
        )

        payload_1 = {"event": "charge.success", "tx_ref": tx_ref_1, "status": "success"}
        body_1 = json.dumps(payload_1).encode("utf-8")
        chapa_sig = self._sign_secret()

        res1 = self.client.post(
            self.url,
            data=body_1,
            content_type="application/json",
            HTTP_CHAPA_SIGNATURE=chapa_sig,
        )
        self.assertEqual(res1.status_code, 200)
        payment_1.refresh_from_db()
        self.assertEqual(payment_1.status, "succeeded")

        # Subtest 2: Compact re-serialized JSON match for x-chapa-signature
        tx_ref_2 = "sp-compact-match"
        payment_2 = self._create_payment(tx_ref=tx_ref_2, status="pending")

        mock_verify.return_value = ProviderStatusResult(
            provider_reference=tx_ref_2,
            status="succeeded",
            raw={"status": "success", "tx_ref": tx_ref_2},
        )

        # Raw body has pretty-printed spacing
        payload_2 = {"event": "charge.success", "tx_ref": tx_ref_2, "status": "success"}
        raw_body_formatted = b'{\n  "event": "charge.success",\n  "tx_ref": "sp-compact-match",\n  "status": "success"\n}'
        # Signature is calculated against compact JSON
        compact_json = json.dumps(payload_2, separators=(",", ":")).encode("utf-8")
        compact_sig = self._sign_body(compact_json)

        res2 = self.client.post(
            self.url,
            data=raw_body_formatted,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE=compact_sig,
        )
        self.assertEqual(res2.status_code, 200)
        payment_2.refresh_from_db()
        self.assertEqual(payment_2.status, "succeeded")

    # 11. Empty CHAPA_WEBHOOK_SECRET: 503, nothing processed, outcome not_configured.
    @override_settings(CHAPA_WEBHOOK_SECRET="")
    @patch.object(ChapaProvider, "verify")
    def test_empty_chapa_webhook_secret_not_configured(self, mock_verify):
        tx_ref = "sp-empty-secret-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        payload = {"event": "charge.success", "tx_ref": tx_ref, "status": "success"}
        body_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(
            self.url,
            data=body_bytes,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE="any-sig",
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Webhook receiver is not configured."})

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        mock_verify.assert_not_called()

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertFalse(event.signature_valid)
        self.assertEqual(event.outcome, "not_configured")

    # 12. InboundWebhookEvent stores only a sha256 hash of the body (assert the raw body text
    #     and the signature value are not saved anywhere on the row).
    @patch.object(ChapaProvider, "verify")
    def test_inbound_webhook_event_stores_only_sha256_hash(self, mock_verify):
        tx_ref = "sp-hash-test-1"
        self._create_payment(tx_ref=tx_ref, status="pending")

        mock_verify.return_value = ProviderStatusResult(
            provider_reference=tx_ref,
            status="succeeded",
            raw={"status": "success", "tx_ref": tx_ref},
        )

        secret_text = "sensitive_credit_card_data_xyz_987"
        payload = {
            "event": "charge.success",
            "tx_ref": tx_ref,
            "status": "success",
            "extra": secret_text,
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = self._sign_body(body_bytes)

        response = self.client.post(
            self.url,
            data=body_bytes,
            content_type="application/json",
            HTTP_X_CHAPA_SIGNATURE=sig,
        )
        self.assertEqual(response.status_code, 200)

        event = InboundWebhookEvent.objects.first()
        self.assertIsNotNone(event)
        expected_hash = hashlib.sha256(body_bytes).hexdigest()
        self.assertEqual(event.body_sha256, expected_hash)

        # Inspect all fields on InboundWebhookEvent model
        field_names = [f.name for f in InboundWebhookEvent._meta.fields]
        self.assertNotIn("body", field_names)
        self.assertNotIn("raw_body", field_names)
        self.assertNotIn("signature", field_names)
        self.assertNotIn("headers", field_names)

        # Ensure raw secret string and signature are not present in string representation or field values
        for field_name in field_names:
            field_val = str(getattr(event, field_name))
            self.assertNotIn(secret_text, field_val)
            self.assertNotIn(sig, field_val)

        self.assertNotIn(secret_text, str(event))
        self.assertNotIn(sig, str(event))

    # 13. A GET request to the URL returns 405.
    def test_get_request_returns_405(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)


# ---------------------------------------------------------------------------
# ArifPay Inbound Webhook Tests (Parity with Chapa)
# ---------------------------------------------------------------------------

@override_settings(
    ARIFPAY_API_KEY="test-arifpay-merchant-key",
    ARIFPAY_SANDBOX=True,
)
class ArifPayInboundWebhookTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.mock_webhook_patcher = patch("payments.tasks.deliver_webhook_attempt.delay")
        self.mock_webhook_delay = self.mock_webhook_patcher.start()
        self.client = APIClient()
        self.merchant, self.api_key = create_test_merchant(
            "ArifPay Webhook Merchant",
            webhook_url="https://merchant.example.com/webhook",
        )
        self.url = reverse("arifpay_webhook")

    def tearDown(self):
        self.mock_webhook_patcher.stop()
        cache.clear()

    def _create_payment(self, tx_ref: str, status: str = "pending") -> Payment:
        return Payment.objects.create(
            merchant=self.merchant,
            idempotency_key=f"idem-{tx_ref}",
            amount=Decimal("100.00"),
            sender="customer@example.com",
            receiver="SafePay",
            provider="arifpay",
            provider_reference=tx_ref,
            status=status,
        )

    # 1. Valid webhook, out-of-band verify says success: 200, payment becomes succeeded,
    #    two history rows, exactly one WebhookDelivery, outcome processed_updated.
    @patch.object(ArifPayProvider, "verify")
    def test_valid_webhook_verify_success(self, mock_verify):
        tx_ref = "sp-arif-valid-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        mock_verify.return_value = ProviderStatusResult(
            provider_reference=tx_ref,
            status="succeeded",
            raw={"transaction": {"transactionStatus": "SUCCESS"}, "sessionId": tx_ref},
        )

        payload = {"sessionId": tx_ref, "transaction": {"transactionStatus": "SUCCESS"}}
        body_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(self.url, data=body_bytes, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        payment.refresh_from_db()
        self.assertEqual(payment.status, "succeeded")

        histories = list(PaymentStatusHistory.objects.filter(payment=payment).order_by("changed_at"))
        self.assertEqual(len(histories), 2)
        self.assertEqual(histories[0].from_status, "pending")
        self.assertEqual(histories[0].to_status, "processing")
        self.assertEqual(histories[1].from_status, "processing")
        self.assertEqual(histories[1].to_status, "succeeded")

        deliveries = WebhookDelivery.objects.filter(payment=payment)
        self.assertEqual(deliveries.count(), 1)

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertTrue(event.signature_valid)
        self.assertEqual(event.outcome, "processed_updated")
        self.assertEqual(event.provider, "arifpay")
        self.assertEqual(event.provider_reference, tx_ref)

    # 2. Webhook payload says success while out-of-band verify says pending:
    #    200, payment stays pending, no history, no WebhookDelivery, outcome processed_unchanged.
    @patch.object(ArifPayProvider, "verify")
    def test_webhook_success_while_verify_pending(self, mock_verify):
        tx_ref = "sp-arif-pending-verify-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        mock_verify.return_value = ProviderStatusResult(
            provider_reference=tx_ref,
            status="pending",
            raw={"transaction": {"transactionStatus": "PENDING"}, "sessionId": tx_ref},
        )

        payload = {"sessionId": tx_ref, "transaction": {"transactionStatus": "SUCCESS"}}
        body_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(self.url, data=body_bytes, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertEqual(PaymentStatusHistory.objects.filter(payment=payment).count(), 0)
        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 0)

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertTrue(event.signature_valid)
        self.assertEqual(event.outcome, "processed_unchanged")

    # 3. Webhook body says success but out-of-band verify says failed:
    #    payment follows verify and ends failed.
    @patch.object(ArifPayProvider, "verify")
    def test_webhook_success_but_verify_failed(self, mock_verify):
        tx_ref = "sp-arif-failed-verify-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        mock_verify.return_value = ProviderStatusResult(
            provider_reference=tx_ref,
            status="failed",
            raw={"transaction": {"transactionStatus": "FAILED"}, "sessionId": tx_ref},
        )

        payload = {"sessionId": tx_ref, "transaction": {"transactionStatus": "SUCCESS"}}
        body_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(self.url, data=body_bytes, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

        payment.refresh_from_db()
        self.assertEqual(payment.status, "failed")

        histories = list(PaymentStatusHistory.objects.filter(payment=payment).order_by("changed_at"))
        self.assertEqual(len(histories), 2)
        self.assertEqual(histories[0].to_status, "processing")
        self.assertEqual(histories[1].to_status, "failed")

        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 1)

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().outcome, "processed_updated")

    # 4. Valid webhook sent twice: idempotent, second outcome processed_unchanged.
    @patch.object(ArifPayProvider, "verify")
    def test_valid_webhook_sent_twice_idempotent(self, mock_verify):
        tx_ref = "sp-arif-twice-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        mock_verify.return_value = ProviderStatusResult(
            provider_reference=tx_ref,
            status="succeeded",
            raw={"transaction": {"transactionStatus": "SUCCESS"}, "sessionId": tx_ref},
        )

        payload = {"sessionId": tx_ref, "transaction": {"transactionStatus": "SUCCESS"}}
        body_bytes = json.dumps(payload).encode("utf-8")

        response1 = self.client.post(self.url, data=body_bytes, content_type="application/json")
        self.assertEqual(response1.status_code, 200)

        response2 = self.client.post(self.url, data=body_bytes, content_type="application/json")
        self.assertEqual(response2.status_code, 200)

        events = list(InboundWebhookEvent.objects.filter(provider="arifpay").order_by("received_at"))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].outcome, "processed_updated")
        self.assertEqual(events[1].outcome, "processed_unchanged")

        self.assertEqual(WebhookDelivery.objects.filter(payment=payment).count(), 1)
        self.assertEqual(PaymentStatusHistory.objects.filter(payment=payment).count(), 2)

    # 5. Unknown reference: 200, outcome unknown_reference, payment table untouched.
    def test_unknown_reference_ignored(self):
        payload = {"sessionId": "sp-arif-unknown-999", "transaction": {"transactionStatus": "SUCCESS"}}
        body_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(self.url, data=body_bytes, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertTrue(event.signature_valid)
        self.assertEqual(event.outcome, "unknown_reference")
        self.assertEqual(event.provider, "arifpay")
        self.assertEqual(event.provider_reference, "sp-arif-unknown-999")

    # 6. Verify raises ProviderError (5xx/timeout): 503, outcome verify_failed.
    @patch.object(ArifPayProvider, "verify")
    def test_verify_provider_error_returns_503(self, mock_verify):
        tx_ref = "sp-arif-down-1"
        payment = self._create_payment(tx_ref=tx_ref, status="pending")

        mock_verify.side_effect = ProviderError("ArifPay verify service unreachable")

        payload = {"sessionId": tx_ref, "transaction": {"transactionStatus": "SUCCESS"}}
        body_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(self.url, data=body_bytes, content_type="application/json")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Temporarily unable to confirm. Retry later."})

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertEqual(PaymentStatusHistory.objects.filter(payment=payment).count(), 0)

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertTrue(event.signature_valid)
        self.assertEqual(event.outcome, "verify_failed")
        self.assertEqual(event.provider, "arifpay")

    # 7. Unsupported status: 200, outcome ignored_unsupported.
    def test_unsupported_status_ignored(self):
        tx_ref = "sp-arif-unsupported-1"
        self._create_payment(tx_ref=tx_ref, status="pending")

        payload = {"sessionId": tx_ref, "transaction": {"transactionStatus": "UNKNOWN_CUSTOM_STATUS"}}
        body_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(self.url, data=body_bytes, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ignored"})

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertTrue(event.signature_valid)
        self.assertEqual(event.outcome, "ignored_unsupported")
        self.assertEqual(event.provider, "arifpay")
        self.assertEqual(event.provider_reference, tx_ref)

    # 8. Malformed JSON: 400, outcome invalid_payload.
    def test_malformed_json_returns_400(self):
        response = self.client.post(self.url, data=b"bad-json-payload", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "Invalid webhook payload."})

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertFalse(event.signature_valid)
        self.assertEqual(event.outcome, "invalid_payload")
        self.assertEqual(event.provider, "arifpay")

    # 9. Missing sessionId: 400, outcome invalid_payload.
    def test_missing_session_id_returns_400(self):
        payload = {"transaction": {"transactionStatus": "SUCCESS"}}
        body_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(self.url, data=body_bytes, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "Invalid webhook payload."})

        events = InboundWebhookEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertFalse(event.signature_valid)
        self.assertEqual(event.outcome, "invalid_payload")

    # 10. InboundWebhookEvent stores only sha256 hash of the body.
    @patch.object(ArifPayProvider, "verify")
    def test_inbound_webhook_event_stores_only_sha256_hash(self, mock_verify):
        tx_ref = "sp-arif-hash-1"
        self._create_payment(tx_ref=tx_ref, status="pending")

        mock_verify.return_value = ProviderStatusResult(
            provider_reference=tx_ref,
            status="succeeded",
            raw={"transaction": {"transactionStatus": "SUCCESS"}, "sessionId": tx_ref},
        )

        secret_text = "confidential_arifpay_customer_info_456"
        payload = {
            "sessionId": tx_ref,
            "transaction": {"transactionStatus": "SUCCESS"},
            "extra": secret_text,
        }
        body_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(self.url, data=body_bytes, content_type="application/json")
        self.assertEqual(response.status_code, 200)

        event = InboundWebhookEvent.objects.first()
        self.assertIsNotNone(event)
        expected_hash = hashlib.sha256(body_bytes).hexdigest()
        self.assertEqual(event.body_sha256, expected_hash)

        field_names = [f.name for f in InboundWebhookEvent._meta.fields]
        self.assertNotIn("body", field_names)
        self.assertNotIn("raw_body", field_names)

        for field_name in field_names:
            field_val = str(getattr(event, field_name))
            self.assertNotIn(secret_text, field_val)
        self.assertNotIn(secret_text, str(event))

    # 11. GET request returns 405 Method Not Allowed.
    def test_get_request_returns_405(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

