import logging
import uuid
from typing import Any, Dict, Optional
import requests
from django.conf import settings

from .base import (
    InitResult,
    PaymentProvider,
    ProviderError,
    ProviderEvent,
    ProviderStatusResult,
    validate_safepay_status,
)

logger = logging.getLogger(__name__)


class ChapaProvider(PaymentProvider):
    name: str = "chapa"
    supports_cancel: bool = False

    def _get_config(self) -> Dict[str, Any]:
        """Read configuration lazily from Django settings at call time."""
        secret_key = getattr(settings, "CHAPA_SECRET_KEY", "") or ""
        base_url = (getattr(settings, "CHAPA_BASE_URL", "https://api.chapa.co/v1") or "https://api.chapa.co/v1").rstrip("/")
        currency = getattr(settings, "CHAPA_DEFAULT_CURRENCY", "ETB") or "ETB"
        return_url = getattr(settings, "CHAPA_RETURN_URL", None)
        timeout = getattr(settings, "CHAPA_REQUEST_TIMEOUT", 10)
        try:
            timeout = int(timeout)
        except (ValueError, TypeError):
            timeout = 10

        return {
            "secret_key": secret_key,
            "base_url": base_url,
            "currency": currency,
            "return_url": return_url,
            "timeout": timeout,
        }

    def _require_secret_key(self, config: Dict[str, Any]) -> str:
        secret = str(config.get("secret_key", "")).strip().strip("'\"")
        if not secret:
            raise ProviderError("Chapa is not configured")
        return secret

    def _sanitize_error(self, message: str, secret_key: Optional[str] = None) -> str:
        if secret_key:
            message = message.replace(secret_key, "[REDACTED]")
        return message

    def initialize(self, payment: Any, customer: Optional[dict] = None) -> InitResult:
        """Create a Chapa transaction and return the provider reference and checkout URL."""
        config = self._get_config()
        secret_key = self._require_secret_key(config)

        tx_ref = f"sp-{uuid.uuid4().hex}"
        amount = str(getattr(payment, "amount", "100.00"))
        payload: Dict[str, Any] = {
            "amount": amount,
            "currency": config["currency"],
            "tx_ref": tx_ref,
        }

        # Customer fields priority: customer dict > payment attributes
        email = (customer.get("email") if customer else None) or getattr(payment, "customer_email", None)
        first_name = (customer.get("first_name") if customer else None) or getattr(payment, "customer_first_name", None)
        last_name = (customer.get("last_name") if customer else None) or getattr(payment, "customer_last_name", None)

        if email:
            payload["email"] = email
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name

        # return_url: payment.return_url fallback to config['return_url']
        ret_url = getattr(payment, "return_url", None) or config.get("return_url")
        if ret_url:
            payload["return_url"] = ret_url

        headers = {
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                f"{config['base_url']}/transaction/initialize",
                json=payload,
                headers=headers,
                timeout=config["timeout"],
            )
        except requests.Timeout as e:
            raise ProviderError(self._sanitize_error(f"Chapa initialize request timed out: {type(e).__name__}", secret_key))
        except requests.RequestException as e:
            raise ProviderError(self._sanitize_error(f"Chapa initialize network error: {type(e).__name__}", secret_key))

        try:
            data_json = response.json()
        except Exception:
            raise ProviderError(self._sanitize_error(f"Chapa initialize returned invalid JSON (HTTP {response.status_code})", secret_key))

        if response.status_code != 200:
            err_msg = data_json.get("message") if isinstance(data_json, dict) else None
            detail = err_msg or f"HTTP {response.status_code}"
            raise ProviderError(self._sanitize_error(f"Chapa initialize failed: {detail}", secret_key))

        if not isinstance(data_json, dict) or data_json.get("status") != "success":
            msg = data_json.get("message", "Unknown error") if isinstance(data_json, dict) else "Invalid response"
            raise ProviderError(self._sanitize_error(f"Chapa initialize unsuccessful: {msg}", secret_key))

        data = data_json.get("data")
        if not isinstance(data, dict):
            raise ProviderError(self._sanitize_error("Chapa initialize response missing data object", secret_key))

        checkout_url = data.get("checkout_url")
        if not checkout_url:
            raise ProviderError(self._sanitize_error("Chapa initialize response missing checkout_url", secret_key))

        return InitResult(provider_reference=tx_ref, checkout_url=checkout_url)

    def verify(self, provider_reference: str) -> ProviderStatusResult:
        config = self._get_config()
        secret_key = self._require_secret_key(config)

        if not provider_reference:
            raise ProviderError("Provider reference is required for verification")

        url = f"{config['base_url']}/transaction/verify/{provider_reference}"
        headers = {
            "Authorization": f"Bearer {secret_key}",
        }

        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=config["timeout"],
            )
        except requests.Timeout as e:
            raise ProviderError(self._sanitize_error(f"Chapa verify request timed out: {type(e).__name__}", secret_key))
        except requests.RequestException as e:
            raise ProviderError(self._sanitize_error(f"Chapa verify network error: {type(e).__name__}", secret_key))

        try:
            data_json = response.json()
        except Exception:
            raise ProviderError(
                self._sanitize_error(
                    f"Chapa verify returned invalid JSON (HTTP {response.status_code})",
                    secret_key,
                )
            )

        if response.status_code != 200:
            err_msg = data_json.get("message") if isinstance(data_json, dict) else None
            detail = err_msg or f"HTTP {response.status_code}"
            raise ProviderError(
                self._sanitize_error(f"Chapa verify failed: {detail}", secret_key)
            )

        if not isinstance(data_json, dict) or data_json.get("status") != "success":
            msg = data_json.get("message", "Unknown error") if isinstance(data_json, dict) else "Invalid response"
            raise ProviderError(
                self._sanitize_error(f"Chapa verify unsuccessful: {msg}", secret_key)
            )

        data = data_json.get("data")
        if not isinstance(data, dict):
            raise ProviderError("Chapa verify response missing data object")

        if data.get("tx_ref") != provider_reference:
            raise ProviderError(
                f"Chapa verification reference mismatch: expected {provider_reference}, got {data.get('tx_ref')}"
            )

        provider_status = data.get("status")
        if not provider_status:
            raise ProviderError("Chapa verify response missing payment status")

        safepay_status = self.map_status(provider_status)

        return ProviderStatusResult(
            provider_reference=provider_reference,
            status=safepay_status,
            raw=data,
        )

    def map_status(self, provider_status: str) -> str:
        if not provider_status:
            raise ProviderError("unsupported provider status")

        status_normalized = str(provider_status).strip().lower()

        # Specific unsupported statuses per spec
        if status_normalized in ("refunded", "reversed"):
            raise ProviderError(f"unsupported provider status: '{provider_status}'")

        status_mapping = {
            "pending": "pending",
            "success": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "failed/cancelled": "cancelled",
        }

        if status_normalized not in status_mapping:
            raise ProviderError(f"unsupported provider status: '{provider_status}'")

        mapped_status = status_mapping[status_normalized]
        return validate_safepay_status(mapped_status)

    def cancel(self, provider_reference: str) -> ProviderStatusResult:
        # TODO: Verify Chapa cancel/void API endpoint and supported workflows at developer.chapa.co
        raise ProviderError("Chapa cancel is not implemented yet")

    def parse_webhook(self, headers: dict, body: bytes) -> ProviderEvent:
        # TODO: Verify exact signature header (e.g. Chapa-Signature / x-chapa-signature) and verification method from official docs at developer.chapa.co first. Do not invent a signature scheme.
        raise ProviderError("Chapa webhook parsing is not implemented yet")
