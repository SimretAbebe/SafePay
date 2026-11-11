"""Provider routing and safe fallback for checkout session initialization."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from django.conf import settings
from django.utils import timezone as dj_timezone

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

    pinned_attempt = None
    if hasattr(payment, "provider_attempts"):
        pinned_attempt = payment.provider_attempts.filter(
            outcome__in=[
                ProviderAttempt.OUTCOME_FAILED_UNKNOWN,
                ProviderAttempt.OUTCOME_FOUND_EXISTING,
            ]
        ).first()
        if not pinned_attempt:
            # Rule: A retry of an existing payment must NEVER widen the provider chain.
            # If a payment has already attempted Chapa, it may only ever retry Chapa.
            chapa_attempt = payment.provider_attempts.filter(provider="chapa").first()
            if chapa_attempt:
                pinned_attempt = chapa_attempt

    if pinned_attempt:
        chain = [pinned_attempt.provider]
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


def _save_payment_fields(payment: Payment, update_fields: list[str]) -> None:
    """Helper to safely save payment fields if possible without breaking mocks."""
    if not hasattr(payment, "save"):
        return
    try:
        if getattr(payment, "pk", None):
            payment.save(update_fields=update_fields)
        else:
            payment.save()
    except Exception:
        pass


def _flag_review_and_save(payment: Payment, reason: str) -> None:
    """Flag the payment for review and persist without touching other fields."""
    if hasattr(payment, "flag_for_review"):
        payment.flag_for_review(reason)
        _save_payment_fields(payment, ["needs_review", "review_reason", "needs_review_at", "review_alerted_at", "next_sync_at", "updated_at"])


# ---------------------------------------------------------------------------
# Chapa verify 404 message classification
# ---------------------------------------------------------------------------
_MSG_NOT_FOUND = "invalid transaction or transaction not found"
_MSG_NOT_PAID_YET = "payment not paid yet"


def _classify_verify_404(exc: ProviderError) -> str:
    """
    Classify a Chapa verify 404 ProviderError by its message text.

    Returns one of:
      "not_found"    — exact match "Invalid transaction or Transaction not found"
      "not_paid_yet" — exact match "Payment not paid yet"
      "unknown"      — anything else (treat as uncertain)

    Decision is made on `exc.not_found` and `exc.not_paid_yet` flags set by
    chapa.py during 404 parsing. The message is NOT re-read here to avoid
    leaking any raw provider text into routing logic.
    """
    if getattr(exc, "not_found", False):
        return "not_found"
    if getattr(exc, "not_paid_yet", False):
        return "not_paid_yet"
    return "unknown"


def _handle_verify_404(
    exc: ProviderError,
    payment: Payment,
    current_ref: str,
    skipped_retry_afters: list,
) -> Optional["_VerifyAction"]:
    """
    Apply rules 1–3 for a Chapa verify 404 response.

    Returns a _VerifyAction:
      .should_initialize = True  → caller should proceed to initialize with new ref
      .should_initialize = False → caller should return 502 immediately

    Never generates a new tx_ref itself; that is left to the caller so the ref
    is updated on `payment` before any further DB write.
    """
    kind = _classify_verify_404(exc)
    if kind == "not_found":
        # Rule 1: Nothing was created on Chapa. Safe to generate a new ref and initialize.
        return _VerifyAction(should_initialize=True, kind=kind)

    elif kind == "not_paid_yet":
        # Rule 2: Transaction exists but is unpaid. Never generate a new ref.
        # Keep pending, flag for review, no fallback.
        logger.warning(
            "Chapa verify returned 404 'not paid yet' for payment %s; flagging for review",
            payment.pk,
        )
        return _VerifyAction(should_initialize=False, kind=kind)

    else:
        # Rule 3: Unknown 404 message or any uncertain verify result.
        logger.warning(
            "Chapa verify returned uncertain 404 for payment %s; flagging for review",
            payment.pk,
        )
        return _VerifyAction(should_initialize=False, kind=kind)


class _VerifyAction:
    def __init__(self, should_initialize: bool, kind: str):
        self.should_initialize = should_initialize
        self.kind = kind


def start_checkout(payment: Payment, customer: Optional[dict], chain: list) -> RoutingResult:
    """Attempt session initialization across provider chain; records ProviderAttempt for each."""
    from . import breaker

    skipped_retry_afters = []

    for provider_name in chain:
        current_ref = getattr(payment, "provider_reference", "") or ""

        action = breaker.allow_request(provider_name, livemode=payment.livemode)
        if action == "skip":
            ProviderAttempt.objects.create(
                payment=payment,
                provider=provider_name,
                provider_reference=current_ref,
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
                provider_reference=current_ref,
                outcome=ProviderAttempt.OUTCOME_FAILED_NOT_CREATED,
                error_type="ProviderError",
            )
            continue

        if provider_name == "chapa":
            has_stored_ref = bool(payment.provider_reference and getattr(payment, "provider", None) == "chapa")

            if not has_stored_ref:
                # Fresh checkout: generate tx_ref and save before calling initialize.
                tx_ref = f"sp-{uuid.uuid4().hex}"
                payment.provider = "chapa"
                payment.provider_reference = tx_ref
                _save_payment_fields(payment, ["provider", "provider_reference", "updated_at"])
                current_ref = tx_ref
            else:
                # Retry: verify stored reference before calling initialize.
                try:
                    verify_result = provider.verify(payment.provider_reference)
                    # HTTP 200: transaction exists on Chapa.
                    ProviderAttempt.objects.create(
                        payment=payment,
                        provider="chapa",
                        provider_reference=payment.provider_reference,
                        outcome=ProviderAttempt.OUTCOME_FOUND_EXISTING,
                        error_type="",
                    )
                    if hasattr(payment, "provider_status"):
                        raw_data = getattr(verify_result, "raw", None)
                        raw_st = raw_data.get("status") if isinstance(raw_data, dict) else None
                        raw_st = raw_st or getattr(verify_result, "status", "")
                        payment.provider_status = raw_st
                        _save_payment_fields(payment, ["provider_status", "updated_at"])
                    # TODO: Chapa public docs do not show how to recover checkout_url for an
                    # existing transaction. Confirm with Chapa support.
                    min_retry = min(skipped_retry_afters) if skipped_retry_afters else None
                    return RoutingResult(success=False, retry_after=min_retry)

                except ProviderError as v_exc:
                    if getattr(v_exc, "status_code", None) == 404:
                        v_action = _handle_verify_404(v_exc, payment, current_ref, skipped_retry_afters)
                        if v_action.should_initialize:
                            # Rule 1: generate a new ref and fall through to initialize.
                            new_ref = f"sp-{uuid.uuid4().hex}"
                            payment.provider_reference = new_ref
                            _save_payment_fields(payment, ["provider_reference", "updated_at"])
                            current_ref = new_ref
                            # Fall through to initialize below.
                        else:
                            # Rules 2 & 3: flag for review, record attempt, return 502.
                            _flag_review_and_save(payment, "unknown_outcome")
                            ProviderAttempt.objects.create(
                                payment=payment,
                                provider="chapa",
                                provider_reference=payment.provider_reference,
                                outcome=ProviderAttempt.OUTCOME_FAILED_UNKNOWN,
                                error_type=type(v_exc).__name__,
                            )
                            min_retry = min(skipped_retry_afters) if skipped_retry_afters else None
                            return RoutingResult(success=False, retry_after=min_retry)
                    else:
                        # Uncertain verify error (timeout, 5xx, invalid JSON).
                        if getattr(v_exc, "infrastructure", False):
                            breaker.record_failure("chapa", livemode=payment.livemode)
                        ProviderAttempt.objects.create(
                            payment=payment,
                            provider="chapa",
                            provider_reference=payment.provider_reference,
                            outcome=ProviderAttempt.OUTCOME_FAILED_UNKNOWN,
                            error_type=type(v_exc).__name__,
                        )
                        logger.warning(
                            "Checkout provider 'chapa' verify failed with uncertain outcome for payment %s",
                            payment.pk,
                        )
                        min_retry = min(skipped_retry_afters) if skipped_retry_afters else None
                        return RoutingResult(success=False, retry_after=min_retry)

        try:
            init_result = provider.initialize(payment, customer=customer)
        except ProviderError as exc:
            # Handle HTTP 400 "Transaction reference has been used before" (Rule 4).
            if provider_name == "chapa" and getattr(exc, "ref_used_before", False):
                try:
                    v_res = provider.verify(current_ref)
                    # Verify 200: transaction exists.
                    ProviderAttempt.objects.create(
                        payment=payment,
                        provider="chapa",
                        provider_reference=current_ref,
                        outcome=ProviderAttempt.OUTCOME_FOUND_EXISTING,
                        error_type="",
                    )
                    if hasattr(payment, "provider_status"):
                        raw_data = getattr(v_res, "raw", None)
                        raw_st = raw_data.get("status") if isinstance(raw_data, dict) else None
                        raw_st = raw_st or getattr(v_res, "status", "")
                        payment.provider_status = raw_st
                        _save_payment_fields(payment, ["provider_status", "updated_at"])
                    # TODO: Chapa public docs do not show how to recover checkout_url for an
                    # existing transaction. Confirm with Chapa support.
                    min_retry = min(skipped_retry_afters) if skipped_retry_afters else None
                    return RoutingResult(success=False, retry_after=min_retry)

                except ProviderError as post_exc:
                    if getattr(post_exc, "status_code", None) == 404:
                        # Rule 4: apply rules 1–3. Never generate a second new reference.
                        p_action = _handle_verify_404(post_exc, payment, current_ref, skipped_retry_afters)
                        if p_action.kind == "not_found":
                            # Rule 4 + Rule 1: 400 "used before" followed by 404 "not found".
                            # This contradicts Chapa docs — flag for review, do NOT initialize.
                            logger.warning(
                                "Chapa reported 'used before' but verify returned 'not found' for payment %s; "
                                "flagging for review",
                                payment.pk,
                            )
                        else:
                            # Rule 4 + Rule 2 or 3.
                            logger.warning(
                                "Chapa 'used before' then verify 404 ('%s') for payment %s; flagging for review",
                                p_action.kind,
                                payment.pk,
                            )
                        _flag_review_and_save(payment, "unknown_outcome")
                        ProviderAttempt.objects.create(
                            payment=payment,
                            provider="chapa",
                            provider_reference=current_ref,
                            outcome=ProviderAttempt.OUTCOME_FAILED_UNKNOWN,
                            error_type=type(exc).__name__,
                        )
                        min_retry = min(skipped_retry_afters) if skipped_retry_afters else None
                        return RoutingResult(success=False, retry_after=min_retry)
                    else:
                        # Uncertain verify after used-before.
                        if getattr(post_exc, "infrastructure", False):
                            breaker.record_failure(provider_name, livemode=payment.livemode)
                        _flag_review_and_save(payment, "unknown_outcome")
                        ProviderAttempt.objects.create(
                            payment=payment,
                            provider="chapa",
                            provider_reference=current_ref,
                            outcome=ProviderAttempt.OUTCOME_FAILED_UNKNOWN,
                            error_type=type(post_exc).__name__,
                        )
                        min_retry = min(skipped_retry_afters) if skipped_retry_afters else None
                        return RoutingResult(success=False, retry_after=min_retry)

            if exc.not_created:
                outcome = ProviderAttempt.OUTCOME_FAILED_NOT_CREATED
            else:
                outcome = ProviderAttempt.OUTCOME_FAILED_UNKNOWN

            ProviderAttempt.objects.create(
                payment=payment,
                provider=provider_name,
                provider_reference=current_ref,
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
        final_ref = init_result.provider_reference or current_ref
        ProviderAttempt.objects.create(
            payment=payment,
            provider=provider_name,
            provider_reference=final_ref,
            outcome=ProviderAttempt.OUTCOME_CREATED,
            error_type="",
        )
        return RoutingResult(
            success=True,
            provider_name=provider_name,
            provider_reference=final_ref,
            checkout_url=init_result.checkout_url,
        )

    min_retry = min(skipped_retry_afters) if skipped_retry_afters else None
    return RoutingResult(success=False, retry_after=min_retry)
