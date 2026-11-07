"""
payments/management/commands/check_webhook.py

Developer diagnostic tool to verify a received SafePay webhook step-by-step:
1. Parse body JSON
2. Verify HMAC-SHA256 signature against derived merchant secret
3. Verify timestamp freshness
4. Check for replay against a local seen-store
5. Confirm payment status via GET /api/payments/<id>/verify/
"""

import json
import os
import sys
import time
from typing import Any, Dict, Optional
import urllib.error
import urllib.request

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from payments.models import Merchant
from payments.webhook_secrets import derive_merchant_secret
from payments.webhook_verification import verify_webhook


class Command(BaseCommand):
    help = "Verify and inspect a SafePay webhook payload step-by-step."

    def add_arguments(self, parser):
        parser.add_argument(
            "--body",
            dest="body_path",
            required=True,
            help="Path to the file containing the raw, unmodified webhook body bytes.",
        )
        parser.add_argument(
            "--timestamp",
            required=True,
            help="Value from the X-SafePay-Timestamp header.",
        )
        parser.add_argument(
            "--signature",
            required=True,
            help="Value from the X-SafePay-Signature header.",
        )
        parser.add_argument(
            "--merchant-id",
            type=int,
            required=True,
            help="Database ID of the merchant receiving the webhook.",
        )
        parser.add_argument(
            "--api-key-env",
            default="SAFEPAY_API_KEY",
            help="Name of the environment variable holding the merchant's API key (default: SAFEPAY_API_KEY).",
        )
        parser.add_argument(
            "--tolerance",
            type=int,
            default=300,
            help="Tolerance window in seconds for timestamp freshness (default: 300).",
        )
        parser.add_argument(
            "--seen-store",
            dest="seen_store_path",
            default=None,
            help="Optional path to a JSON file recording seen event IDs for replay checking.",
        )
        parser.add_argument(
            "--base-url",
            default="http://127.0.0.1:8000",
            help="Base URL for the SafePay API (default: http://127.0.0.1:8000).",
        )

    def handle(self, *args, **options):
        body_path = options["body_path"]
        timestamp_str = options["timestamp"]
        signature_str = options["signature"]
        merchant_id = options["merchant_id"]
        api_key_env = options["api_key_env"]
        tolerance = options["tolerance"]
        seen_store_path = options["seen_store_path"]
        base_url = options["base_url"].rstrip("/")

        # 1. Read Raw Body as Bytes
        if not os.path.exists(body_path):
            self.stderr.write(self.style.ERROR(f"Body file not found: {body_path}"))
            sys.exit(1)

        try:
            with open(body_path, "rb") as f:
                raw_body = f.read()
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Failed to read body file: {e}"))
            sys.exit(1)

        failed_checks = []

        # -------------------------------------------------------------
        # STEP 1: Parse
        # -------------------------------------------------------------
        parsed_payload: Optional[Dict[str, Any]] = None
        try:
            decoded_text = raw_body.decode("utf-8")
            data = json.loads(decoded_text)
            if not isinstance(data, dict):
                raise ValueError("Payload is not a JSON object")
            required_keys = ("id", "type", "livemode", "data")
            missing = [k for k in required_keys if k not in data]
            if missing:
                raise ValueError(f"Missing required fields: {', '.join(missing)}")
            parsed_payload = data
            self.stdout.write(self.style.SUCCESS("[PASS] Parse: body is valid JSON with required fields"))
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f"[FAIL] Parse: body is not valid JSON ({exc})"))
            self.stdout.write(self.style.ERROR("\nExecution stopped due to parse failure."))
            sys.exit(1)

        # Retrieve Merchant & Derive Secret
        try:
            merchant = Merchant.objects.get(id=merchant_id)
        except Merchant.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"Merchant with ID {merchant_id} does not exist."))
            sys.exit(1)

        livemode = bool(parsed_payload.get("livemode", False))
        try:
            derived_secret = derive_merchant_secret(merchant, livemode=livemode)
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Error deriving merchant secret: {e}"))
            sys.exit(1)

        # -------------------------------------------------------------
        # STEP 2: Signature
        # -------------------------------------------------------------
        # Use verify_webhook without tolerance (we check tolerance explicitly in Step 3)
        sig_valid = verify_webhook(
            secret=derived_secret,
            timestamp=timestamp_str,
            signature=signature_str,
            body=raw_body,
            tolerance_seconds=None,
        )

        if sig_valid:
            self.stdout.write(self.style.SUCCESS("[PASS] Signature matches the merchant's secret"))
        else:
            self.stdout.write(self.style.ERROR("[FAIL] Signature does NOT match"))
            failed_checks.append("Signature")

        # -------------------------------------------------------------
        # STEP 3: Timestamp
        # -------------------------------------------------------------
        try:
            ts_int = int(timestamp_str)
            age = int(abs(time.time() - ts_int))
            if age <= tolerance:
                self.stdout.write(
                    self.style.SUCCESS(f"[PASS] Timestamp: age is {age}s (within {tolerance}s tolerance)")
                )
            else:
                self.stdout.write(
                    self.style.ERROR(f"[FAIL] Timestamp: age is {age}s (exceeds {tolerance}s tolerance)")
                )
                failed_checks.append("Timestamp")
        except (ValueError, TypeError):
            self.stdout.write(self.style.ERROR("[FAIL] Timestamp: invalid integer timestamp"))
            failed_checks.append("Timestamp")

        # -------------------------------------------------------------
        # STEP 4: Replay Check
        # -------------------------------------------------------------
        event_id = str(parsed_payload.get("id"))
        if seen_store_path:
            seen_ids = set()
            if os.path.exists(seen_store_path):
                try:
                    with open(seen_store_path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                        if isinstance(loaded, list):
                            seen_ids = set(loaded)
                except Exception:
                    seen_ids = set()

            if event_id in seen_ids:
                self.stdout.write(
                    self.style.WARNING("[INFO] Replay: Event id seen before: a receiver must ignore this")
                )
            else:
                self.stdout.write(self.style.SUCCESS("[PASS] Replay: New event id"))
                seen_ids.add(event_id)
                try:
                    with open(seen_store_path, "w", encoding="utf-8") as f:
                        json.dump(list(seen_ids), f, indent=2)
                except Exception as e:
                    self.stderr.write(self.style.WARNING(f"Could not update seen-store: {e}"))
        else:
            self.stdout.write(self.style.NOTICE("[INFO] Replay check skipped (no --seen-store given)"))

        # -------------------------------------------------------------
        # STEP 5: Verify via SafePay API
        # -------------------------------------------------------------
        payment_data = parsed_payload.get("data", {})
        payment_id = payment_data.get("payment_id") or payment_data.get("id")

        api_key = os.environ.get(api_key_env)
        if not api_key:
            self.stdout.write(
                self.style.ERROR(f"[FAIL] Verify: Environment variable '{api_key_env}' is not set or empty")
            )
            failed_checks.append("Verify")
        elif not payment_id:
            self.stdout.write(self.style.ERROR("[FAIL] Verify: No payment ID found in webhook data"))
            failed_checks.append("Verify")
        else:
            verify_url = f"{base_url}/api/payments/{payment_id}/verify/"
            req = urllib.request.Request(
                verify_url,
                headers={"X-API-Key": api_key, "User-Agent": "SafePay-CheckWebhook/1.0"},
                method="GET",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    resp_body = resp.read().decode("utf-8")
                    resp_json = json.loads(resp_body)
                    status_confirmed = resp_json.get("status", "unknown")
                    self.stdout.write(
                        self.style.SUCCESS(f"[PASS] Verify: SafePay confirms payment {payment_id} is {status_confirmed}")
                    )
            except urllib.error.HTTPError as http_err:
                self.stdout.write(
                    self.style.ERROR(f"[FAIL] Verify: SafePay verification returned HTTP {http_err.code}")
                )
                failed_checks.append("Verify")
            except Exception as err:
                self.stdout.write(
                    self.style.ERROR(f"[FAIL] Verify: Could not connect to SafePay verification endpoint ({err})")
                )
                failed_checks.append("Verify")

        # -------------------------------------------------------------
        # Summary & Exit
        # -------------------------------------------------------------
        self.stdout.write("\n" + "=" * 40)
        if not failed_checks:
            self.stdout.write(self.style.SUCCESS("All checks passed"))
            sys.exit(0)
        else:
            self.stdout.write(self.style.ERROR(f"Failed checks: {', '.join(failed_checks)}"))
            sys.exit(1)

