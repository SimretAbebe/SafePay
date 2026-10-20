# Runbook: Review Queue

## What is the review queue?

The review queue collects payments that SafePay's reconciliation system could
not resolve automatically.  A payment enters the queue when the `needs_review`
flag is set to `True` during reconciliation.  It stays there until a human
investigates and uses the admin action **"Clear review flag and re-check"**.

You can see the queue in the Django admin under **Review queue**.

---

## Review reasons

| Reason code | What it means | What to do |
|---|---|---|
| `unknown_outcome` | SafePay exhausted all retry attempts and still could not reach the provider to confirm the payment status. | 1. Check the provider dashboard for this payment's reference.  2. If the status is clear, use **"Check with provider now"** to let SafePay try once more.  3. If the provider is still unreachable, escalate. |
| `unsupported_status` | The provider returned a payment status that SafePay does not know how to map (e.g. "refunded"). | 1. Look at the `provider_status` field.  2. Decide the correct SafePay outcome manually.  3. Record it (transition the payment via a management command or the admin) and clear the flag. |
| `mismatch` | SafePay's current status and the provider's status disagree about a finished payment (e.g. SafePay says "cancelled" but the provider says "paid"). | 1. Compare both sides carefully.  2. Determine which is correct.  3. If the provider is right, escalate so the payment can be corrected safely.  4. Clear the flag after resolution. |

---

## How alerts work

- **Grouped, one-shot alerts** — When one or more payments enter the queue,
  SafePay sends a single message listing up to 10 payments (with a "+N more"
  suffix if there are more).  Each payment is alerted **exactly once**; a
  second run of the alert task will not re-send it.

- **Cooldown** — After the first alert fires, there is a configurable cooldown
  period (`REVIEW_ALERT_COOLDOWN_SECONDS`, default 5 minutes) during which new
  flaggings do **not** trigger an immediate alert.  The beat safety-net task
  (`review_alert_sweep`) picks up any un-alerted payments every
  `REVIEW_ALERT_SWEEP_SECONDS` (default 15 minutes).

- **Daily digest** — A summary runs once a day at `REVIEW_DIGEST_HOUR_UTC`
  (default 06:00 UTC) and reports the total queue size, counts per reason, and
  the age of the oldest item.

- **Channels** — Alerts are always written to the Python logger at WARNING
  level.  If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are configured, the
  same text is also sent to Telegram.

---

## How to clear a review flag

1. Open the **Review queue** in the Django admin.
2. Select the payment(s) you have investigated.
3. Choose the action **"Clear review flag and re-check"**.
4. SafePay will reset `needs_review`, `needs_review_at`, `review_alerted_at`,
   and `sync_attempts` to zero, set `next_sync_at` to now, and the next
   reconciliation sweep will re-check the payment with the provider.

---

## Note

Customer-facing refunds are **out of scope** for this queue.  If a payment
needs to be refunded, follow the separate refund process.
