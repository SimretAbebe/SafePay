"""
payments/investigator.py

Pure functions to explain a payment's history and current status in plain English.
Read-only engine shared by both the API endpoint and the Django admin view.
"""

from typing import Any, Dict, List, Optional


def build_investigation(payment) -> Dict[str, Any]:
    """
    Build a plain-English explanation of one payment's lifecycle and current health.
    Uses existing records only. Never touches external providers or modifies state.
    Never returns secrets, keys, signatures, URLs, error traces, or customer PII.
    """
    timeline: List[Dict[str, Any]] = []

    # 1. Payment created
    timeline.append({
        "at": payment.created_at.isoformat(),
        "event": "Payment created",
        "source": {
            "type": "payment",
            "source_id": payment.id,
        },
        "_seq": 0,
    })

    # 2. PaymentStatusHistory
    for history in payment.status_history.all():
        timeline.append({
            "at": history.changed_at.isoformat(),
            "event": f"Status changed from {history.from_status} to {history.to_status}",
            "source": {
                "type": "status_change",
                "source_id": history.id,
            },
            "_seq": 10,
        })

    # 3. ProviderAttempt
    # outcomes: created, failed_not_created, failed_unknown, skipped_circuit_open
    attempts = list(payment.provider_attempts.all())
    for attempt in attempts:
        if attempt.outcome == "created":
            event_text = f"Provider {attempt.provider} created a checkout link"
        elif attempt.outcome == "failed_not_created":
            event_text = f"Provider {attempt.provider} could not be reached, so the next provider was tried"
        elif attempt.outcome == "skipped_circuit_open":
            event_text = f"Provider {attempt.provider} was skipped because its circuit was open"
        elif attempt.outcome == "failed_unknown":
            event_text = f"Provider {attempt.provider} failed; not retried, the outcome is unknown"
        else:
            event_text = f"Provider {attempt.provider} attempt recorded"

        timeline.append({
            "at": attempt.created_at.isoformat(),
            "event": event_text,
            "source": {
                "type": "provider_attempt",
                "source_id": attempt.id,
            },
            "_seq": 20,
        })

    # 4. Reconciliation check if last_synced_at is set
    if payment.last_synced_at:
        timeline.append({
            "at": payment.last_synced_at.isoformat(),
            "event": "Re-checked with the provider",
            "source": {
                "type": "reconciliation",
                "source_id": payment.id,
            },
            "_seq": 30,
        })

    # 5. WebhookDelivery and WebhookOutbox
    deliveries = list(payment.webhook_deliveries.all())
    for delivery in deliveries:
        # WebhookDelivery events:
        # "Merchant notified" when delivered (succeeded)
        # "Merchant notification queued" when pending
        # "Merchant notification failed after N attempts" when dead (failed)
        if delivery.status == "succeeded":
            delivery_event = "Merchant notified"
            delivery_at = (delivery.updated_at or delivery.created_at).isoformat()
        elif delivery.status == "failed":
            delivery_event = f"Merchant notification failed after {delivery.attempt_count} attempts"
            delivery_at = (delivery.updated_at or delivery.created_at).isoformat()
        else:
            delivery_event = "Merchant notification queued"
            delivery_at = delivery.created_at.isoformat()

        timeline.append({
            "at": delivery_at,
            "event": delivery_event,
            "source": {
                "type": "webhook_delivery",
                "source_id": delivery.id,
            },
            "_seq": 40,
        })

        # WebhookOutbox row (if exists)
        outbox = getattr(delivery, "outbox", None)
        if outbox is not None:
            if outbox.status == "pending":
                outbox_event = "Notification saved, waiting for the queue"
                outbox_at = outbox.created_at.isoformat()
            elif outbox.status == "sent":
                outbox_event = "Notification sent to the queue"
                outbox_at = (outbox.sent_at or outbox.created_at).isoformat()
            elif outbox.status == "needs_review":
                outbox_event = "Notification needs review"
                outbox_at = outbox.created_at.isoformat()
            else:
                outbox_event = f"Notification outbox status: {outbox.status}"
                outbox_at = outbox.created_at.isoformat()

            timeline.append({
                "at": outbox_at,
                "event": outbox_event,
                "source": {
                    "type": "webhook_outbox",
                    "source_id": outbox.id,
                },
                "_seq": 50,
            })

    # Sort timeline: oldest first by ISO timestamp, with stable secondary tie-break
    timeline.sort(key=lambda item: (item["at"], item["_seq"]))
    # Remove internal _seq key while keeping at, event, source
    cleaned_timeline = [
        {
            "at": item["at"],
            "event": item["event"],
            "source": item["source"],
        }
        for item in timeline
    ]

    # --- Findings & Next Step ---
    findings: List[Dict[str, Any]] = []
    next_step: Optional[str] = None

    # Check for late payment scenario:
    # An expired payment that the data shows was later reported paid (e.g. mismatch review on an expired payment)
    is_expired = payment.status == "expired" or any(h.to_status == "expired" for h in payment.status_history.all())
    is_late_paid_mismatch = is_expired and payment.needs_review and payment.review_reason == "mismatch"

    reason_map = {
        "unknown_outcome": "the provider could not be reached repeatedly",
        "unsupported_status": "the provider reported a status SafePay does not support",
        "mismatch": "SafePay and the provider disagree about a finished payment",
        "cannot_verify": "the provider cannot verify this payment without a transaction id",
    }

    # Helper: evidence for flagged payment
    # All provider attempts for this payment
    attempt_evidence = [
        {"type": "provider_attempt", "source_id": att.id}
        for att in attempts
    ]

    if payment.needs_review:
        plain_reason = reason_map.get(payment.review_reason, payment.review_reason or "manual review required")
        findings.append({
            "text": f"Flagged for review: {plain_reason}",
            "basis": "known",
            "evidence": attempt_evidence,
        })
        if is_late_paid_mismatch:
            findings.append({
                "text": "Payment was reported paid after it had already expired.",
                "basis": "known",
                "evidence": attempt_evidence,
            })

        # Determine next_step based on primary review reason
        if payment.review_reason == "unknown_outcome":
            next_step = "Check the provider's dashboard, then use 'Check with provider now' in the admin."
        elif payment.review_reason == "mismatch":
            next_step = "Compare both sides, then decide and record the outcome manually."
        elif payment.review_reason == "unsupported_status":
            next_step = "Inspect provider logs and determine the final payment status manually."
        elif payment.review_reason == "cannot_verify":
            next_step = "Look up the transaction in the provider portal to resolve verification."
        else:
            next_step = "Review this payment in the admin and resolve any discrepancy."

    # sync_attempts above 3 while still pending
    if payment.status == "pending" and payment.sync_attempts > 3:
        findings.append({
            "text": f"Re-checked {payment.sync_attempts} times and still pending",
            "basis": "known",
            "evidence": [
                {"type": "reconciliation", "source_id": payment.id}
            ] if payment.last_synced_at else [],
        })
        if not next_step:
            next_step = "Check if the customer abandoned checkout or if provider webhook failed."

    # A dead webhook delivery
    dead_deliveries = [d for d in deliveries if d.status == "failed"]
    for dead_d in dead_deliveries:
        findings.append({
            "text": f"Merchant notification failed after {dead_d.attempt_count} attempts",
            "basis": "known",
            "evidence": [
                {"type": "webhook_delivery", "source_id": dead_d.id}
            ],
        })
        if not next_step:
            next_step = "Check merchant webhook target URL and retry delivery from the admin."

    # A needs_review outbox row
    outbox_review_rows = [
        getattr(d, "outbox", None)
        for d in deliveries
        if getattr(d, "outbox", None) and d.outbox.status == "needs_review"
    ]
    if outbox_review_rows:
        findings.append({
            "text": "Notification outbox needs review",
            "basis": "known",
            "evidence": [
                {"type": "webhook_outbox", "source_id": ob.id}
                for ob in outbox_review_rows
            ],
        })
        if not next_step:
            next_step = "Inspect Celery broker connectivity and requeue outbox items from the admin."

    # STEP 3: KNOWN AND UNKNOWN FINDINGS
    # Unknown: the last recorded event is not a final state (not succeeded, failed, cancelled or expired)
    # and no provider confirmation exists.
    # Specifically: A pending payment with sync attempts and no provider confirmation shows "unknown"
    # and the last-recorded-event line; a succeeded payment shows no unknown line.
    final_states = {"succeeded", "failed", "cancelled", "expired"}
    is_terminal = payment.status in final_states
    has_provider_confirmation = bool(payment.last_synced_at) or any(
        att.outcome == "created" and payment.status in final_states
        for att in attempts
    )

    if not is_terminal and not has_provider_confirmation and payment.sync_attempts > 0:
        last_event_text = cleaned_timeline[-1]["event"] if cleaned_timeline else "Payment created"
        findings.append({
            "text": f"Last recorded event: {last_event_text}. The provider has not confirmed the outcome.",
            "basis": "unknown",
            "evidence": [],
        })

    # --- Summary ---
    # Built from fixed templates only:
    # "Expired, but the provider later reported it paid. Needs review." for the late-payment case
    # "Paid and confirmed. Merchant notified once." for succeeded with a delivered webhook
    # "Still pending with the provider." for pending
    # "Expired without payment." for expired without findings
    if is_late_paid_mismatch:
        summary = "Expired, but the provider later reported it paid. Needs review."
    elif payment.status == "succeeded":
        has_delivered_webhook = any(d.status == "succeeded" for d in deliveries)
        if has_delivered_webhook:
            summary = "Paid and confirmed. Merchant notified once."
        else:
            summary = "Paid and confirmed."
    elif payment.status == "pending":
        summary = "Still pending with the provider."
    elif payment.status == "expired":
        if not findings:
            summary = "Expired without payment."
        else:
            summary = "Expired without payment."
    elif payment.status == "failed":
        summary = "Payment failed."
    elif payment.status == "cancelled":
        summary = "Payment cancelled."
    else:
        summary = f"Payment status is {payment.status}."

    return {
        "payment_id": payment.id,
        "status": payment.status,
        "summary": summary,
        "timeline": cleaned_timeline,
        "findings": findings,
        "next_step": next_step,
    }
