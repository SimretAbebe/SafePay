"""
Template Provider Adapter for SafePay.

This is a starter template for adding a new payment provider adapter to SafePay.
Copy this file, rename the class and filename, and implement the five required
methods following the official API documentation of your provider.

DO NOT register this template in payments/providers/registry.py.
"""

from typing import Any, Optional

from .base import (
    InitResult,
    PaymentProvider,
    ProviderError,
    ProviderEvent,
    ProviderStatusResult,
)


class TemplateProvider(PaymentProvider):
    """
    Starter template for implementing a new payment provider in SafePay.
    """

    name: str = "your_provider_name"
    supports_cancel: bool = False
    supports_livemode: bool = False
    supports_refund: bool = False

    def initialize(self, payment: Any, customer: Optional[dict] = None) -> InitResult:
        """
        Job 1: Initialize a checkout session with the payment provider.

        TODO:
        - Call provider's checkout/session API to obtain a checkout URL and a provider reference.
        - SAFETY RULE: If the network call times out or fails with an unknown outcome,
          raise ProviderError(..., not_created=False). Only set not_created=True if you are
          certain the provider never registered the payment attempt (so SafePay can safely fallback).
        - SAFETY RULE: Never log or return secret keys or tokens.
        """
        raise ProviderError("initialize not implemented yet")

    def verify(self, provider_reference: str) -> ProviderStatusResult:
        """
        Job 2: Query the provider's API for the current payment status.

        TODO:
        - Query the provider's verification/inquiry endpoint using provider_reference.
        - Map native provider status to SafePay status using self.map_status().
        - SAFETY RULE: If the status is ambiguous or verification fails due to network error,
          raise ProviderError. Never guess payment success.
        - SAFETY RULE: Return raw payload without sensitive credentials or customer PII.
        """
        raise ProviderError("verify not implemented yet")

    def cancel(self, provider_reference: str) -> ProviderStatusResult:
        """
        Job 3: Request session cancellation with the provider.

        TODO:
        - Call the provider's cancel endpoint if supported (and set supports_cancel = True).
        - If cancellation is not supported by the provider, raise ProviderError.
        - SAFETY RULE: Verify with the provider that the session is truly cancelled
          so that a late payment cannot succeed unnoticed.
        """
        raise ProviderError("cancel not implemented yet")

    def parse_webhook(self, headers: dict, body: bytes) -> ProviderEvent:
        """
        Job 4: Verify authenticity and parse the incoming webhook event.

        TODO:
        - Verify signature/HMAC using provider's documented algorithm and secret.
        - Extract the event identifier, provider reference, and native status.
        - SAFETY RULE: verify before trusting any webhook. SafePay's confirm_from_webhook
          always calls self.verify() to confirm state from the provider directly.
        - SAFETY RULE: If signature verification fails, raise ProviderError (or ProviderSignatureError).
          Never process an unauthenticated webhook.
        """
        raise ProviderError("parse_webhook not implemented yet")

    def map_status(self, provider_status: str) -> str:
        """
        Translate the provider's native status string to a canonical SafePay status:
        "pending", "processing", "succeeded", "failed", "cancelled", or "expired".

        TODO:
        - Maintain an explicit mapping dictionary.
        - SAFETY RULE: unknown status must raise, never guess. If provider sends an
          unmapped status, raise UnsupportedProviderStatus or ProviderError.
        - Always return validate_safepay_status(safepay_status).
        """
        raise ProviderError("map_status not implemented yet")
