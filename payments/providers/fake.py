import hashlib
import hmac
import json
import uuid
from typing import Any, ClassVar, Dict

from django.conf import settings

from .base import (
    InitResult,
    PaymentProvider,
    ProviderError,
    ProviderEvent,
    ProviderStatusResult,
    validate_safepay_status,
)


class FakeProvider(PaymentProvider):
    name: str = "fake"

    STATUS_MAPPING: ClassVar[Dict[str, str]] = {
        "pending": "pending",
        "processing": "processing",
        "paid": "succeeded",
        "failed": "failed",
        "cancelled": "cancelled",
    }

    _store: ClassVar[Dict[str, Dict[str, Any]]] = {}

    @classmethod
    def reset(cls) -> None:
        """Clear the in-memory storage (useful between tests)."""
        cls._store.clear()

    @classmethod
    def simulate(cls, reference: str, provider_status: str) -> None:
        """Set the provider-side status for a given reference."""
        if reference not in cls._store:
            cls._store[reference] = {}
        cls._store[reference]["status"] = provider_status

    def initialize(self, payment: Any) -> InitResult:
        ref = f"fake_{uuid.uuid4().hex}"
        checkout_url = f"https://fake.local/pay/{ref}"
        self._store[ref] = {
            "status": "pending",
            "payment_id": getattr(payment, "id", None),
        }
        return InitResult(
            provider_reference=ref,
            checkout_url=checkout_url,
        )

    def map_status(self, provider_status: str) -> str:
        if provider_status in ("refunded", "reversed"):
            raise ProviderError("unsupported provider status")
        if provider_status not in self.STATUS_MAPPING:
            raise ProviderError(
                f"Unknown fake provider status: '{provider_status}'"
            )
        safepay_status = self.STATUS_MAPPING[provider_status]
        return validate_safepay_status(safepay_status)

    def verify(self, provider_reference: str) -> ProviderStatusResult:
        record = self._store.get(provider_reference)
        if record is None:
            raise ProviderError(
                f"Fake payment with reference '{provider_reference}' not found."
            )
        provider_status = record.get("status", "pending")
        safepay_status = self.map_status(provider_status)
        return ProviderStatusResult(
            provider_reference=provider_reference,
            status=safepay_status,
            raw={"status": provider_status, "reference": provider_reference},
        )

    def cancel(self, provider_reference: str) -> ProviderStatusResult:
        if not getattr(self, "supports_cancel", True):
            raise ProviderError("This provider does not support cancelling an open payment.")

        record = self._store.get(provider_reference)
        if record is None:
            raise ProviderError(
                f"Fake payment with reference '{provider_reference}' not found."
            )
        current_status = record.get("status")
        if current_status != "pending":
            raise ProviderError(
                f"Cannot cancel fake payment '{provider_reference}' in status '{current_status}'. "
                "Fake payments can only be cancelled while pending."
            )
        record["status"] = "cancelled"
        return ProviderStatusResult(
            provider_reference=provider_reference,
            status="cancelled",
            raw={"status": "cancelled", "reference": provider_reference},
        )

    def parse_webhook(self, headers: dict, body: bytes) -> ProviderEvent:
        sig = (
            headers.get("X-Fake-Signature")
            or headers.get("x-fake-signature")
            or headers.get("HTTP_X_FAKE_SIGNATURE")
        )
        if not sig:
            raise ProviderError("Missing X-Fake-Signature header.")

        secret = getattr(settings, "FAKE_PROVIDER_SECRET", "fake-secret-for-tests")
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        expected_sig = hmac.new(secret_bytes, body, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(str(sig), expected_sig):
            raise ProviderError("Invalid webhook signature.")

        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise ProviderError(f"Malformed JSON payload: {exc}") from exc

        if not isinstance(data, dict):
            raise ProviderError("Webhook body must be a JSON object.")

        reference = data.get("reference")
        provider_status = data.get("status")
        event_id = data.get("event_id")

        if not reference or not provider_status:
            raise ProviderError(
                "Webhook payload missing required 'reference' or 'status' field."
            )

        safepay_status = self.map_status(provider_status)

        return ProviderEvent(
            provider=self.name,
            provider_reference=str(reference),
            status=safepay_status,
            event_id=str(event_id) if event_id is not None else None,
            raw=data,
        )
