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
    retry_after: Optional[int] = None


def build_provider_chain(
    merchant,
    requested_provider: Optional[str],
    allow_fallback: bool,
    payment: Payment,
    livemode: bool = False,
) -> list:
    """Build ordered list of provider candidates respecting fallback and safety rules."""
    primary = (
        requested_provider
        or (merchant.default_provider or None)
        or getattr(settings, "DEFAULT_PROVIDER", "chapa")
    )

    uncertain_attempt = payment.provider_attempts.filter(
        outcome=ProviderAttempt.OUTCOME_FAILED_UNKNOWN
    ).first()
    if uncertain_attempt:
        chain = [uncertain_attempt.provider]
    else:
        chain = [primary]

        if allow_fallback:
            fallbacks = list(merchant.fallback_providers or [])
            for name in fallbacks:
                if name in chain:
                    continue
                if name not in _provider_registry:
                    continue
                chain.append(name)

    if livemode:
        live_chain = []
        for name in chain:
            try:
                p = get_provider(name)
                if getattr(p, "supports_livemode", False):
                    live_chain.append(name)
            except ProviderError:
                continue
        chain = live_chain

    return chain


def start_checkout(payment: Payment, customer: Optional[dict], chain: list) -> RoutingResult:
    """Attempt session initialization across provider chain; records ProviderAttempt for each."""
    from . import breaker

    skipped_retry_afters = []

    for provider_name in chain:
        action = breaker.allow_request(provider_name, livemode=payment.livemode)
        if action == "skip":
            ProviderAttempt.objects.create(
                payment=payment,
                provider=provider_name,
                outcome=ProviderAttempt.OUTCOME_SKIPPED_CIRCUIT_OPEN,
                error_type="CircuitBreakerOpen",
            )
            skipped_retry_afters.append(breaker.retry_after(provider_name, livemode=payment.livemode))
            logger.info(
                "Checkout provider '%s' circuit open for payment %s; skipping",
                provider_name, payment.pk,
            )
            continue

        try:
            provider = get_provider(provider_name)
        except ProviderError:
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

            if getattr(exc, "infrastructure", False):
                breaker.record_failure(provider_name, livemode=payment.livemode)

            if outcome == ProviderAttempt.OUTCOME_FAILED_UNKNOWN:
                logger.warning(
                    "Checkout provider '%s' failed with uncertain outcome for payment %s",
                    provider_name, payment.pk,
                )
                min_retry = min(skipped_retry_afters) if skipped_retry_afters else None
                return RoutingResult(success=False, retry_after=min_retry)

            logger.info(
                "Checkout provider '%s' failed (not_created=True) for payment %s; trying next",
                provider_name, payment.pk,
            )
            continue

        breaker.record_success(provider_name, livemode=payment.livemode)
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

    min_retry = min(skipped_retry_afters) if skipped_retry_afters else None
    return RoutingResult(success=False, retry_after=min_retry)
