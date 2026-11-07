"""
payments/test_check_webhook.py

Unit and integration tests for the `check_webhook` management command.
Tests:
1. A correctly signed body with a fresh timestamp passes parse, signature and timestamp; exit code 0 when verify returns status.
2. A changed body fails the signature check; message never contains either signature value.
3. Timestamp older than tolerance fails; within tolerance passes.
4. Replay: first run records ID in seen-store; second run reports 'seen before'.
5. Without --seen-store, replay check is skipped.
6. Missing API key environment variable fails verify clearly; other checks still print.
7. Verify call is made to correct path with X-API-Key header; key never appears in stdout/stderr.
8. Secret never appears in output.
9. Body that is not valid JSON stops run with clear message.
10. All existing tests pass.
"""

import io
import json
import os
import tempfile
import time
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch
import urllib.error

from django.core.management import call_command
from django.test import TestCase, override_settings

from payments.models import Merchant, Payment
from payments.tasks import _sign_payload
from payments.webhook_secrets import derive_merchant_secret


class CheckWebhookCommandTests(TestCase):
    def setUp(self):
        self.merchant, self.raw_key = Merchant.create_with_key("TestWebhookMerchant")
        self.payment = Payment.objects.create(
            merchant=self.merchant,
            livemode=False,
            amount=Decimal("250.00"),
            sender="alice",
            receiver="bob",
            provider="fake",
            idempotency_key=f"check-webhook-{uuid.uuid4()}",
            status="succeeded",
        )
        self.event_id = str(uuid.uuid4())
        self.payload_dict = {
            "id": self.event_id,
            "type": "payment.succeeded",
            "livemode": False,
            "data": {
                "payment_id": self.payment.id,
                "idempotency_key": self.payment.idempotency_key,
                "status": "succeeded",
                "amount": "250.00",
            },
        }
        self.payload_bytes = json.dumps(self.payload_dict).encode("utf-8")
        self.secret = derive_merchant_secret(self.merchant, livemode=False)
        self.timestamp = str(int(time.time()))
        self.signature = _sign_payload(self.timestamp, self.payload_bytes, secret=self.secret)

    def _write_temp_file(self, content: bytes) -> str:
        f = tempfile.NamedTemporaryFile(delete=False)
        f.write(content)
        f.close()
        self.addCleanup(lambda: os.path.exists(f.name) and os.unlink(f.name))
        return f.name

    @patch("urllib.request.urlopen")
    def test_correctly_signed_fresh_body_passes(self, mock_urlopen):
        # Mock verify HTTP response
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": self.payment.id, "status": "succeeded"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        body_file = self._write_temp_file(self.payload_bytes)
        out = io.StringIO()
        err = io.StringIO()

        with patch.dict(os.environ, {"SAFEPAY_API_KEY": self.raw_key}):
            with self.assertRaises(SystemExit) as cm:
                call_command(
                    "check_webhook",
                    body_path=body_file,
                    timestamp=self.timestamp,
                    signature=self.signature,
                    merchant_id=self.merchant.id,
                    stdout=out,
                    stderr=err,
                )
            self.assertEqual(cm.exception.code, 0)

        output = out.getvalue()
        self.assertIn("[PASS] Parse", output)
        self.assertIn("[PASS] Signature matches", output)
        self.assertIn("[PASS] Timestamp", output)
        self.assertIn("[PASS] Verify: SafePay confirms payment", output)
        self.assertIn("All checks passed", output)

    @patch("urllib.request.urlopen")
    def test_changed_body_fails_signature_and_does_not_leak_signature(self, mock_urlopen):
        tampered_dict = dict(self.payload_dict)
        tampered_dict["data"]["amount"] = "999.00"
        tampered_bytes = json.dumps(tampered_dict).encode("utf-8")
        body_file = self._write_temp_file(tampered_bytes)

        out = io.StringIO()
        err = io.StringIO()

        with patch.dict(os.environ, {"SAFEPAY_API_KEY": self.raw_key}):
            with self.assertRaises(SystemExit) as cm:
                call_command(
                    "check_webhook",
                    body_path=body_file,
                    timestamp=self.timestamp,
                    signature=self.signature,
                    merchant_id=self.merchant.id,
                    stdout=out,
                    stderr=err,
                )
            self.assertEqual(cm.exception.code, 1)

        output = out.getvalue()
        self.assertIn("[FAIL] Signature does NOT match", output)
        self.assertIn("Failed checks: Signature", output)
        # Verify no signature values appear
        self.assertNotIn(self.signature, output)

    @patch("urllib.request.urlopen")
    def test_timestamp_tolerance_check(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": self.payment.id, "status": "succeeded"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        # 1. Expired timestamp (e.g. 500s ago with 300s tolerance)
        old_ts = str(int(time.time()) - 500)
        old_sig = _sign_payload(old_ts, self.payload_bytes, secret=self.secret)
        body_file = self._write_temp_file(self.payload_bytes)

        out = io.StringIO()
        with patch.dict(os.environ, {"SAFEPAY_API_KEY": self.raw_key}):
            with self.assertRaises(SystemExit) as cm:
                call_command(
                    "check_webhook",
                    body_path=body_file,
                    timestamp=old_ts,
                    signature=old_sig,
                    merchant_id=self.merchant.id,
                    tolerance=300,
                    stdout=out,
                )
            self.assertEqual(cm.exception.code, 1)
        self.assertIn("[FAIL] Timestamp: age is", out.getvalue())
        self.assertIn("exceeds 300s tolerance", out.getvalue())

        # 2. Fresh timestamp passes
        fresh_ts = str(int(time.time()) - 10)
        fresh_sig = _sign_payload(fresh_ts, self.payload_bytes, secret=self.secret)
        out2 = io.StringIO()
        with patch.dict(os.environ, {"SAFEPAY_API_KEY": self.raw_key}):
            with self.assertRaises(SystemExit) as cm2:
                call_command(
                    "check_webhook",
                    body_path=body_file,
                    timestamp=fresh_ts,
                    signature=fresh_sig,
                    merchant_id=self.merchant.id,
                    tolerance=300,
                    stdout=out2,
                )
            self.assertEqual(cm2.exception.code, 0)
        self.assertIn("[PASS] Timestamp: age is", out2.getvalue())

    @patch("urllib.request.urlopen")
    def test_replay_check_records_and_detects(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": self.payment.id, "status": "succeeded"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        seen_store_file = tempfile.NamedTemporaryFile(delete=False)
        seen_store_file.close()
        self.addCleanup(lambda: os.path.exists(seen_store_file.name) and os.unlink(seen_store_file.name))

        body_file = self._write_temp_file(self.payload_bytes)

        # First run: records the ID
        out1 = io.StringIO()
        with patch.dict(os.environ, {"SAFEPAY_API_KEY": self.raw_key}):
            with self.assertRaises(SystemExit) as cm1:
                call_command(
                    "check_webhook",
                    body_path=body_file,
                    timestamp=self.timestamp,
                    signature=self.signature,
                    merchant_id=self.merchant.id,
                    seen_store_path=seen_store_file.name,
                    stdout=out1,
                )
            self.assertEqual(cm1.exception.code, 0)
        self.assertIn("[PASS] Replay: New event id", out1.getvalue())

        # Second run: detects replay
        out2 = io.StringIO()
        with patch.dict(os.environ, {"SAFEPAY_API_KEY": self.raw_key}):
            with self.assertRaises(SystemExit) as cm2:
                call_command(
                    "check_webhook",
                    body_path=body_file,
                    timestamp=self.timestamp,
                    signature=self.signature,
                    merchant_id=self.merchant.id,
                    seen_store_path=seen_store_file.name,
                    stdout=out2,
                )
            self.assertEqual(cm2.exception.code, 0)
        self.assertIn("[INFO] Replay: Event id seen before: a receiver must ignore this", out2.getvalue())

    @patch("urllib.request.urlopen")
    def test_without_seen_store_replay_is_skipped(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": self.payment.id, "status": "succeeded"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        body_file = self._write_temp_file(self.payload_bytes)
        out = io.StringIO()
        with patch.dict(os.environ, {"SAFEPAY_API_KEY": self.raw_key}):
            with self.assertRaises(SystemExit):
                call_command(
                    "check_webhook",
                    body_path=body_file,
                    timestamp=self.timestamp,
                    signature=self.signature,
                    merchant_id=self.merchant.id,
                    stdout=out,
                )
        self.assertIn("[INFO] Replay check skipped (no --seen-store given)", out.getvalue())

    def test_missing_api_key_env_var_fails_verify_clearly(self):
        body_file = self._write_temp_file(self.payload_bytes)
        out = io.StringIO()

        # Ensure env var is absent
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as cm:
                call_command(
                    "check_webhook",
                    body_path=body_file,
                    timestamp=self.timestamp,
                    signature=self.signature,
                    merchant_id=self.merchant.id,
                    api_key_env="SAFEPAY_API_KEY",
                    stdout=out,
                )
            self.assertEqual(cm.exception.code, 1)

        output = out.getvalue()
        self.assertIn("[PASS] Parse", output)
        self.assertIn("[PASS] Signature", output)
        self.assertIn("[FAIL] Verify: Environment variable 'SAFEPAY_API_KEY' is not set or empty", output)

    @patch("urllib.request.urlopen")
    def test_verify_call_headers_path_and_key_not_leaked(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": self.payment.id, "status": "succeeded"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        body_file = self._write_temp_file(self.payload_bytes)
        out = io.StringIO()
        err = io.StringIO()

        custom_key = "sp_test_SECRET_API_KEY_12345678"
        with patch.dict(os.environ, {"CUSTOM_ENV_KEY": custom_key}):
            with self.assertRaises(SystemExit) as cm:
                call_command(
                    "check_webhook",
                    body_path=body_file,
                    timestamp=self.timestamp,
                    signature=self.signature,
                    merchant_id=self.merchant.id,
                    api_key_env="CUSTOM_ENV_KEY",
                    base_url="http://safepay.local",
                    stdout=out,
                    stderr=err,
                )
            self.assertEqual(cm.exception.code, 0)

        # Check call arguments
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, f"http://safepay.local/api/payments/{self.payment.id}/verify/")
        self.assertEqual(req.headers.get("X-api-key"), custom_key)

        # Assert key is never leaked in output
        combined_output = out.getvalue() + err.getvalue()
        self.assertNotIn(custom_key, combined_output)

    @patch("urllib.request.urlopen")
    def test_secret_never_leaked_in_output(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": self.payment.id, "status": "succeeded"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        body_file = self._write_temp_file(self.payload_bytes)
        out = io.StringIO()
        err = io.StringIO()

        with patch.dict(os.environ, {"SAFEPAY_API_KEY": self.raw_key}):
            with self.assertRaises(SystemExit):
                call_command(
                    "check_webhook",
                    body_path=body_file,
                    timestamp=self.timestamp,
                    signature=self.signature,
                    merchant_id=self.merchant.id,
                    stdout=out,
                    stderr=err,
                )

        combined = out.getvalue() + err.getvalue()
        self.assertNotIn(self.secret, combined)

    def test_invalid_json_body_stops_run(self):
        body_file = self._write_temp_file(b"{invalid-json-body-here")
        out = io.StringIO()

        with self.assertRaises(SystemExit) as cm:
            call_command(
                "check_webhook",
                body_path=body_file,
                timestamp=self.timestamp,
                signature=self.signature,
                merchant_id=self.merchant.id,
                stdout=out,
            )
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[FAIL] Parse: body is not valid JSON", out.getvalue())
        self.assertIn("Execution stopped due to parse failure", out.getvalue())

