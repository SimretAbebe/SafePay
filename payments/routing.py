"""Provider routing and safe fallback for checkout session initialization."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from django.conf import settings

from .models import Payment, ProviderAttempt
from .providers import ProviderError, get_provider
from .providers.registry import _registry as _provider_registry

logger = logging.getLogger(__name__)


@dataclass
class RoutingResult:
    """Result of provider initialization attempt (never raises)."""
    success: bool
    provider_name: Optional[str] = None
    provider_reference: Optional[str] = None
    checkout_url: Optional[str] = None


def build_provider_chain(
    merchant,
    requested_provider: Optional[str],
    allow_fallback: bool,
    payment: Payment,
) -> list:
    """Build ordered list of provider candidates respecting fallback and safety rules."""
    # Determine primary.
    primary = (
        requested_provider
        or (merchant.default_provider or None)
        or getattr(settings, "DEFAULT_PROVIDER", "chapa")
    )

    # Check for a prior uncertain failure.
    uncertain_attempt = payment.provider_attempts.filter(
        outcome=ProviderAttempt.OUTCOME_FAILED_UNKNOWN
    ).first()
    if uncertain_attempt:
        # Retry only the provider that had the uncertain failure.
        return [uncertain_attempt.provider]

    chain = [primary]

    if allow_fallback:
        fallbacks = list(merchant.fallback_providers or [])
        for name in fallbacks:
            if name in chain:
                continue  # skip duplicates
            if name not in _provider_registry:
                # Skip names not registered (safety: don't silently promote
                # an unregistered name).
                continue
            chain.append(name)

    return chain


# ---------------------------------------------------------------------------
# Checkout starter
# ---------------------------------------------------------------------------

def start_checkout(payment: Payment, customer: Optional[dict], chain: list) -> RoutingResult:
    """Attempt session initialization across provider chain; records ProviderAttempt for each."""
    for provider_name in chain:
        try:
            provider = get_provider(provider_name)
        except ProviderError:
            # Provider name is not in the registry — treat as not_created.
            ProviderAttempt.objects.create(
                payment=payment,
                provider=provider_name,
                outcome=ProviderAttempt.OUTCOME_FAILED_NOT_CREATED,
                error_type="ProviderError",
            )
            continue

        try:
            init_result = provider.initialize(payment, customer=customer)
        except ProviderError as exc:
            if exc.not_created:
                outcome = ProviderAttempt.OUTCOME_FAILED_NOT_CREATED
            else:
                outcome = ProviderAttempt.OUTCOME_FAILED_UNKNOWN

            ProviderAttempt.objects.create(
                payment=payment,
                provider=provider_name,
                outcome=outcome,
                error_type=type(exc).__name__,
            )

            if outcome == ProviderAttempt.OUTCOME_FAILED_UNKNOWN:
                # Uncertain — stop immediately; do not try the next provider.
                logger.warning(
                    "Checkout provider '%s' failed with uncertain outcome for payment %s",
                    provider_name, payment.pk,
                )
                return RoutingResult(success=False)

            # Failed but definitely not created — continue to next provider.
            logger.info(
                "Checkout provider '%s' failed (not_created=True) for payment %s; trying next",
                provider_name, payment.pk,
            )
            continue

        # Success.
        ProviderAttempt.objects.create(
            payment=payment,
            provider=provider_name,
            outcome=ProviderAttempt.OUTCOME_CREATED,
            error_type="",
        )
        return RoutingResult(
            success=True,
            provider_name=provider_name,
            provider_reference=init_result.provider_reference,
            checkout_url=init_result.checkout_url,
        )

    # Chain exhausted without success.
    return RoutingResult(success=False)
