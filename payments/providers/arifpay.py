import json
import logging
import uuid
import datetime
from typing import Any, Dict, Optional

import requests
import requests.exceptions
from django.conf import settings

from .base import (
    InitResult,
    PaymentProvider,
    ProviderError,
    ProviderEvent,
    ProviderSignatureError,
    ProviderStatusResult,
    UnsupportedProviderStatus,
    validate_safepay_status,
)

logger = logging.getLogger(__name__)


class ArifPayProvider(PaymentProvider):

    name: str = "arifpay"
    supports_cancel: bool = False
    # TODO: Live credentials and legal review are required first before enabling live mode.
    supports_livemode: bool = False

    # The portal simulator key is ONLY valid for the sandbox PSS endpoint.
    # It must never be used for the hosted checkout endpoint in production.
    SANDBOX_PORTAL_KEY = "arif-sandbox-portal-key"

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _get_config(self) -> Dict[str, Any]:
        """Read all ArifPay settings from Django settings (populated from .env)."""
        is_sandbox = bool(getattr(settings, "ARIFPAY_SANDBOX", True))

        # Modern ArifPay endpoints use the .net domain.
        default_base = (
            "https://gateway.arifpay.net/api"
            if is_sandbox
            else "https://api.arifpay.net/api"
        )
        raw_base = getattr(settings, "ARIFPAY_BASE_URL", None) or default_base
        base_url = str(raw_base).rstrip("/")
        if not base_url.endswith("/api"):
            base_url = f"{base_url}/api"

        return {
            "api_key": getattr(settings, "ARIFPAY_API_KEY", "").strip(),
            "sandbox": is_sandbox,
            "base_url": base_url,
            "currency": getattr(settings, "ARIFPAY_DEFAULT_CURRENCY", "ETB"),
            "return_url": getattr(settings, "ARIFPAY_RETURN_URL", None),
            "success_url": getattr(settings, "ARIFPAY_SUCCESS_URL", None),
            "error_url": getattr(settings, "ARIFPAY_ERROR_URL", None),
            "cancel_url": getattr(settings, "ARIFPAY_CANCEL_URL", None),
            "notify_url": getattr(settings, "ARIFPAY_NOTIFY_URL", None),
            "timeout": int(getattr(settings, "ARIFPAY_REQUEST_TIMEOUT", 10)),
            "sandbox_sim_mode": getattr(settings, "ARIFPAY_SANDBOX_SIM_MODE", "SUCCESS"),
            "sandbox_fee_source": getattr(settings, "ARIFPAY_SANDBOX_FEE_SOURCE", "CUSTOMER"),
        }

    def _require_merchant_key(self, config: Dict[str, Any]) -> str:
        """Return the merchant API key, refusing portal simulator keys."""
        api_key = str(config.get("api_key", "")).strip().strip("'\"")
        if not api_key:
            # No key configured: request was never attempted.
            raise ProviderError("ArifPay API key is not configured", not_created=True, infrastructure=False)
        if api_key == self.SANDBOX_PORTAL_KEY:
            raise ProviderError(
                "ARIFPAY_API_KEY is set to the sandbox portal simulator key "
                f"('{self.SANDBOX_PORTAL_KEY}'). That key only works with the "
                "sandbox PSS direct-transfer endpoint — it cannot create hosted "
                "checkout sessions. To use hosted checkout (production flow), set "
                "ARIFPAY_API_KEY to a merchant API key from developer.arifpay.net.",
                not_created=True,
                infrastructure=False,
            )
        return api_key

    def _sanitize_error(self, message: str, api_key: Optional[str] = None) -> str:
        if api_key:
            message = message.replace(api_key, "[REDACTED]")
        return message

    def _build_callback_urls(self, config: Dict[str, Any]) -> Dict[str, str]:
        return {
            "successUrl": config["success_url"] or config["return_url"] or "http://localhost:8000/payments/success/",
            "errorUrl": config["error_url"] or config["return_url"] or "http://localhost:8000/payments/error/",
            "cancelUrl": config["cancel_url"] or config["return_url"] or "http://localhost:8000/payments/cancel/",
            "notifyUrl": config["notify_url"] or "http://localhost:8000/webhooks/arifpay/",
        }

    def _build_items(self, payment: Any, amount: float) -> list:
        return [
            {
                "name": getattr(payment, "item_name", None) or "Payment",
                "quantity": 1,
                "price": amount,
            }
        ]

    def _add_customer_fields(self, payload: Dict[str, Any], payment: Any, customer: Optional[dict]) -> None:
        if customer:
            if customer.get("email"):
                payload["email"] = customer["email"]
            if customer.get("phone"):
                payload["phone"] = customer["phone"]
        else:
            email = getattr(payment, "customer_email", None)
            phone = getattr(payment, "customer_phone", None)
            if email:
                payload["email"] = email
            if phone:
                payload["phone"] = phone

    # -------------------------------------------------------------------------
    # PaymentProvider interface — HOSTED CHECKOUT (production flow)
    # -------------------------------------------------------------------------

    def initialize(self, payment: Any, customer: Optional[dict] = None) -> InitResult:
        """Create an ArifPay hosted checkout session.

        This is the **production flow**: SafePay calls ArifPay's
        ``POST /api/checkout/session`` endpoint, receives a ``paymentUrl``, and
        returns it as ``checkout_url`` so the merchant can redirect the customer.

        **Requires a real merchant API key** from developer.arifpay.net.
        The portal sandbox key (``arif-sandbox-portal-key``) does NOT work here —
        use the ``arifpay_smoke init --pss`` command to test sandbox PSS transfers.
        """
        config = self._get_config()
        api_key = self._require_merchant_key(config)

        nonce = f"sp-{uuid.uuid4().hex}"
        amount = float(getattr(payment, "amount", 0))
        expire_date = (
            datetime.datetime.utcnow() + datetime.timedelta(days=1)
        ).strftime("%Y-%m-%dT%H:%M:%S.000")

        payload: Dict[str, Any] = {
            "nonce": nonce,
            "items": self._build_items(payment, amount),
            "currency": config["currency"],
            "expireDate": expire_date,
            **self._build_callback_urls(config),
        }
        self._add_customer_fields(payload, payment, customer)

        headers = {"x-arifpay-key": api_key, "Content-Type": "application/json"}

        try:
            response = requests.post(
                f"{config['base_url']}/checkout/session",
                json=payload,
                headers=headers,
                timeout=config["timeout"],
            )
        except requests.exceptions.ConnectTimeout as exc:
            raise ProviderError(
                self._sanitize_error(f"ArifPay initialize connect timeout: {exc}", api_key),
                not_created=True,
                infrastructure=True,
            )
        except requests.exceptions.ConnectionError as exc:
            raise ProviderError(
                self._sanitize_error(f"ArifPay initialize connection error: {exc}", api_key),
                not_created=True,
                infrastructure=True,
            )
        except requests.exceptions.ReadTimeout as exc:
            raise ProviderError(
                self._sanitize_error(f"ArifPay initialize read timeout: {exc}", api_key),
                not_created=False,
                infrastructure=True,
            )
        except requests.RequestException as exc:
            raise ProviderError(
                self._sanitize_error(f"ArifPay initialize request failed: {exc}", api_key),
                not_created=False,
                infrastructure=True,
            )

        try:
            data_json = response.json()
        except Exception:
            is_infra = getattr(response, "status_code", 0) >= 500
            raise ProviderError(
                self._sanitize_error(
                    f"ArifPay initialize returned non-JSON (status {response.status_code})",
                    api_key,
                ),
                not_created=False,
                infrastructure=is_infra,
            )

        if response.status_code >= 400 and response.status_code < 500:
            raw_msg = data_json.get("msg") if isinstance(data_json, dict) else None
            if raw_msg and "Test API Key" in str(raw_msg):
                detail = (
                    f"{raw_msg}. The key you supplied is a sandbox portal simulator key "
                    "that only works with the direct PSS endpoint. Hosted checkout "
                    "requires a merchant API key from developer.arifpay.net."
                )
            else:
                detail = raw_msg or f"HTTP {response.status_code}"
            raise ProviderError(
                self._sanitize_error(f"ArifPay initialize failed: {detail}", api_key),
                not_created=True,
                infrastructure=False,
            )

        if response.status_code >= 500:
            raw_msg = data_json.get("msg") if isinstance(data_json, dict) else None
            detail = raw_msg or f"HTTP {response.status_code}"
            raise ProviderError(
                self._sanitize_error(f"ArifPay initialize failed: {detail}", api_key),
                not_created=False,
                infrastructure=True,
            )

        if data_json.get("error"):
            raw_msg = data_json.get("msg") if isinstance(data_json, dict) else None
            detail = raw_msg or "application error"
            raise ProviderError(
                self._sanitize_error(f"ArifPay initialize failed: {detail}", api_key),
                not_created=False,
                infrastructure=False,
            )

        data = data_json.get("data", {})
        session_id = data.get("sessionId")
        checkout_url = data.get("paymentUrl")
        if not session_id or not checkout_url:
            raise ProviderError(
                "ArifPay initialize response missing sessionId or paymentUrl",
                not_created=False,
                infrastructure=False,
            )

        return InitResult(provider_reference=session_id, checkout_url=checkout_url)

    def sandbox_pss_transfer(
        self,
        payment: Any,
        customer: Optional[dict] = None,
        sim_mode: str = "SUCCESS",
        fee_source: str = "CUSTOMER",
    ) -> ProviderStatusResult:
        """Execute a sandbox direct PSS transfer (sandbox-only, not production).

        Uses the ArifPay sandbox PSS endpoint:
        ``POST https://developer.arifpay.org/api/sandbox/c2b/pss/direct/transfer``

        This endpoint accepts the portal simulator key (``arif-sandbox-portal-key``).
        It **does not** produce a hosted checkout URL — it simulates an immediate
        card payment and returns a transaction result.

        This method is intentionally **not** part of the ``PaymentProvider`` abstract
        interface because it is a sandbox-only testing utility, not a production flow.
        It is called exclusively by the ``arifpay_smoke`` management command.
        """
        config = self._get_config()

        if not config["sandbox"]:
            raise ProviderError(
                "sandbox_pss_transfer() can only be called when ARIFPAY_SANDBOX=True"
            )

        api_key = str(config.get("api_key", "")).strip().strip("'\"")
        if not api_key:
            raise ProviderError("ArifPay API key is not configured")

        nonce = f"sp-{uuid.uuid4().hex}"
        amount = float(getattr(payment, "amount", 0))

        payload: Dict[str, Any] = {
            "nonce": nonce,
            "items": self._build_items(payment, amount),
            "currency": config["currency"],
            **self._build_callback_urls(config),
        }
        self._add_customer_fields(payload, payment, customer)

        headers = {
            "x-arifpay-key": api_key,
            "Content-Type": "application/json",
            "x-arifpay-sim-mode": sim_mode.upper(),
            "x-arifpay-fee-source": fee_source.upper(),
        }

        endpoint = "https://gateway.arifpay.org/api/sandbox/c2b/pss/direct/transfer"

        logger.info("ArifPay sandbox PSS transfer: POST %s (sim_mode=%s)", endpoint, sim_mode)

        try:
            response = requests.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=config["timeout"],
            )
        except requests.RequestException as exc:
            raise ProviderError(f"ArifPay sandbox PSS request failed: {exc}")

        try:
            data_json = response.json()
        except Exception:
            raise ProviderError(
                f"ArifPay sandbox PSS returned non-JSON (status {response.status_code})"
            )

        if response.status_code != 200 or data_json.get("error"):
            raw_msg = data_json.get("msg") if isinstance(data_json, dict) else None
            raise ProviderError(f"ArifPay sandbox PSS failed: {raw_msg or 'HTTP ' + str(response.status_code)}")

        # Extract the session/transaction reference from the PSS response.
        data = data_json.get("data", data_json)
        session_id = (
            data.get("sessionId")
            or data.get("uuid")
            or (data.get("transaction") or {}).get("transactionId")
        )
        if not session_id and isinstance(data.get("transaction"), dict):
            cs = data["transaction"].get("checkoutSession") or {}
            session_id = cs.get("uuid") or str(cs.get("id", ""))

        if not session_id:
            raise ProviderError("ArifPay sandbox PSS response missing session/transaction reference")

        transaction = data.get("transaction", {})
        provider_status = (
            transaction.get("transactionStatus") if isinstance(transaction, dict) else None
        ) or data_json.get("msg", "PENDING")

        safepay_status = self.map_status(provider_status) if provider_status in (
            "SUCCESS", "PENDING", "FAILED", "CANCELLED", "CANCELED", "EXPIRED"
        ) else "pending"

        return ProviderStatusResult(
            provider_reference=str(session_id),
            status=safepay_status,
            raw=data_json,
        )

    def verify(self, provider_reference: str) -> ProviderStatusResult:
        """Verify a checkout session's current status via the ArifPay API."""
        config = self._get_config()
        api_key = self._require_merchant_key(config)
        if not provider_reference:
            raise ProviderError("Provider reference (sessionId) is required for verification")

        url = f"{config['base_url']}/checkout/session/{provider_reference}"
        headers = {"x-arifpay-key": api_key}
        try:
            response = requests.get(url, headers=headers, timeout=config["timeout"])
        except requests.RequestException as exc:
            raise ProviderError(
                self._sanitize_error(f"ArifPay verify request failed: {exc}", api_key)
            )

        try:
            data_json = response.json()
        except Exception:
            raise ProviderError(
                self._sanitize_error(
                    f"ArifPay verify returned non-JSON (status {response.status_code})",
                    api_key,
                )
            )

        if response.status_code != 200 or data_json.get("error"):
            detail = data_json.get("msg") or f"HTTP {response.status_code}"
            raise ProviderError(
                self._sanitize_error(f"ArifPay verify failed: {detail}", api_key)
            )

        data = data_json.get("data", {})
        transaction = data.get("transaction", {})
        provider_status = transaction.get("transactionStatus") if isinstance(transaction, dict) else None
        if not provider_status:
            raise ProviderError("ArifPay verify response missing transactionStatus")

        return ProviderStatusResult(
            provider_reference=provider_reference,
            status=self.map_status(provider_status),
            raw=data,
        )

    def cancel(self, provider_reference: str) -> ProviderStatusResult:
        """Cancel a hosted checkout session.

        ArifPay does not expose a programmatic cancel endpoint. Sessions exit
        via user cancellation on the hosted payment interface or session expiry.
        """
        raise ProviderError("ArifPay does not support programmatic session cancellation")

    # -------------------------------------------------------------------------
    # Webhook helpers
    # -------------------------------------------------------------------------

    def _get_header(self, headers: Any, key: str) -> Optional[str]:
        if headers is None:
            return None
        if hasattr(headers, "get"):
            val = headers.get(key)
            if val is not None:
                return str(val)
            key_lower = key.lower()
            if hasattr(headers, "items"):
                for k, v in headers.items():
                    if k.lower() == key_lower:
                        return str(v)
        return None

    def parse_webhook(self, headers: dict, body: bytes) -> ProviderEvent:
        """Parse an ArifPay webhook payload.

        ArifPay does not publish a webhook signature scheme, so we parse the
        payload and record it. The webhook view subsequently calls
        ``sync_payment_from_provider`` which calls ``verify()`` to confirm the
        authoritative status from the ArifPay API.
        """
        try:
            payload = json.loads(
                body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
            )
        except Exception as exc:
            raise ProviderError(f"Malformed ArifPay webhook JSON: {exc}")

        if not isinstance(payload, dict):
            raise ProviderError("ArifPay webhook payload must be a JSON object")

        # Support both the wrapped {"data": {...}} envelope and flat payloads.
        data_block = payload.get("data") if isinstance(payload.get("data"), dict) else payload

        provider_reference = (
            payload.get("sessionId")
            or payload.get("uuid")
            or data_block.get("sessionId")
            or data_block.get("uuid")
        )
        if not provider_reference and isinstance(data_block.get("transaction"), dict):
            cs = data_block["transaction"].get("checkoutSession")
            if isinstance(cs, dict):
                provider_reference = cs.get("uuid") or str(cs.get("id", "")) or None
            if not provider_reference:
                provider_reference = data_block["transaction"].get("transactionId")

        if not provider_reference:
            raise ProviderError("ArifPay webhook missing sessionId/uuid")

        transaction = payload.get("transaction") or data_block.get("transaction") or {}
        raw_status = (
            transaction.get("transactionStatus") if isinstance(transaction, dict) else None
        ) or payload.get("status") or data_block.get("status")

        if raw_status is None:
            raise UnsupportedProviderStatus(
                "Webhook payload missing transactionStatus",
                provider_reference=str(provider_reference),
                event_type=None,
            )

        try:
            mapped_status = self.map_status(raw_status)
        except ProviderError as exc:
            raise UnsupportedProviderStatus(
                f"Unsupported status '{raw_status}' in webhook",
                provider_reference=str(provider_reference),
                event_type=None,
            ) from exc

        return ProviderEvent(
            provider=self.name,
            provider_reference=str(provider_reference),
            status=mapped_status,
            event_id=None,
            raw=payload,
        )

    def map_status(self, provider_status: str) -> str:
        """Map ArifPay native statuses to SafePay statuses."""
        if not provider_status:
            raise UnsupportedProviderStatus("unsupported provider status")

        normalized = str(provider_status).strip().lower()
        if normalized in ("canceled", "cancelled"):
            normalized = "cancelled"

        mapping = {
            "pending": "pending",
            "success": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "expired": "expired",
        }

        if normalized not in mapping:
            raise UnsupportedProviderStatus(
                f"unsupported provider status: '{provider_status}'"
            )

        return validate_safepay_status(mapping[normalized])
