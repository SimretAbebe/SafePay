from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

SAFEPAY_STATUSES = frozenset({
    "pending",
    "processing",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
})


class ProviderError(Exception):
    """Base exception for payment provider failures."""
    pass


@dataclass
class InitResult:
    provider_reference: str
    checkout_url: str


@dataclass
class ProviderStatusResult:
    provider_reference: str
    status: str
    raw: dict


@dataclass
class ProviderEvent:
    provider: str
    provider_reference: str
    status: str
    event_id: Optional[str]
    raw: dict


def validate_safepay_status(status: str) -> str:
    """
    Validates that a status string is one of SafePay's valid statuses.
    Raises ValueError otherwise.
    """
    if status not in SAFEPAY_STATUSES:
        raise ValueError(
            f"Invalid SafePay status: '{status}'. "
            f"Allowed statuses: {sorted(SAFEPAY_STATUSES)}"
        )
    return status


class PaymentProvider(ABC):
    """Abstract base class for all payment providers.
    
    TODO: If a payment is cancelled in SafePay while the provider session can 
    still be paid, a later 'succeeded' event must NOT silently overwrite it.
    """
    name: str
    supports_cancel: bool = True

    @abstractmethod
    def initialize(self, payment: Any) -> InitResult:
        """Initialize a payment with the provider and obtain checkout info."""
        pass

    @abstractmethod
    def verify(self, provider_reference: str) -> ProviderStatusResult:
        """Query the provider for current payment status."""
        pass

    @abstractmethod
    def cancel(self, provider_reference: str) -> ProviderStatusResult:
        """Request payment cancellation with the provider."""
        pass

    @abstractmethod
    def parse_webhook(self, headers: dict, body: bytes) -> ProviderEvent:
        """
        Verify authenticity and parse webhook payload.
        Must raise ProviderError if signature or payload is invalid.
        
        Note: Provider adapters whose webhook authenticity mechanism is not confirmed 
        must be allowed to implement parse_webhook as 'parse only, mark authenticity 
        as unconfirmed', with a clear TODO comment, and rely on confirm_from_webhook 
        to make it safe. Do not invent a signature scheme.
        """
        pass

    def confirm_from_webhook(self, headers: dict, body: bytes) -> ProviderStatusResult:
        """
        1. Calls parse_webhook (which verifies authenticity if applicable).
        2. Takes the provider_reference from the event.
        3. Calls self.verify(provider_reference).
        4. Returns the ProviderStatusResult from verify.
        
        The state decided must come from verify(), never from the webhook body.
        """
        event = self.parse_webhook(headers, body)
        return self.verify(event.provider_reference)

    @abstractmethod
    def map_status(self, provider_status: str) -> str:
        """
        Translate the provider's native status into a SafePay status.
        Unknown values must raise ProviderError, never guess.
        """
        pass
