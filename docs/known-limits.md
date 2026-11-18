# Known Limits

This document outlines the current operational boundaries, constraints, and unverified aspects of SafePay as implemented in the codebase.

---

## Mode

- **Test mode only.**
- SafePay is configured and restricted to test mode.
- While the architecture for live mode (separate `sp_live_` key spaces, separate live webhook URLs, and partitioned merchant database records) is fully built into the codebase, live mode is intentionally switched off:
  - `LIVE_MODE_AVAILABLE` defaults to `False` in settings.
  - All payment provider adapters define `supports_livemode = False`.
  - Any request using a live key (`sp_live_...`) is rejected with HTTP `403 Forbidden`.

---

## Providers

- **Fake Provider (`fake`)**: Fully functional in-memory provider used for local simulation, test suites, and manual staff testing via the simulator.
- **Chapa (`chapa`)**:
  - `initialize` (checkout session creation) and `verify` (status inquiry) work against Chapa's test/sandbox API.
  - Session cancellation is not supported by Chapa (`supports_cancel = False`); attempting to cancel an initialized session returns HTTP `409 Conflict`.
- **ArifPay (`arifpay`)**:
  - Sandbox direct transfer (Payment Simulation Service / PSS) is implemented using the sandbox portal key.
  - Hosted checkout session integration is implemented in code but has not yet been verified or tested against a live gateway environment.
  - Session cancellation is not supported by ArifPay (`supports_cancel = False`); attempting to cancel returns HTTP `409 Conflict`.

---

## Refunds

- **Only the fake provider (`fake`) supports refunds** (`supports_refund = True`).
- **Chapa and ArifPay** do not have refund operations enabled (`supports_refund = False`).
- If a merchant requests a refund for a payment processed by Chapa or ArifPay, SafePay records a `Refund` record with status `unsupported` and returns HTTP `409 Conflict` with error code `refund_unsupported`.

---

## Unclear Payments

- SafePay adheres to a strict safety invariant: **payments with unclear outcomes are flagged for human review and never guessed.**
- If a network error, timeout, or ambiguous status occurs during gateway communication:
  - The payment is never automatically marked as `failed` or `succeeded`.
  - The payment is flagged with `needs_review = True` and routed to the staff Review Queue in the Django admin.
  - Periodic background reconciliation tasks lock and query pending payments, but will never guess state without explicit provider verification.

---

## Open Questions

- **Checkout Link Recovery**:
  - If a customer closes their browser tab or loses their checkout URL, Chapa's verification API returns payment state but not the original checkout URL. SafePay cannot recover or re-generate an existing checkout link from Chapa after initial creation. This behaviour needs confirmation against official Chapa partner documentation.
- **Meaning of "used before" for Chapa**:
  - Submitting a session initialization request with a reference that previously reached Chapa returns a `"used before"` validation error. SafePay avoids blind retries by always querying gateway status first (`verify-before-retry`), but exact error codes and idempotency reuse rules need confirmation from Chapa's official specifications.

---

## Not Built

The following capabilities are outside SafePay's current scope and are not built:
- **Real Money Movement**: SafePay does not touch live banking rails, real merchant accounts, or real funds.
- **Legal & Regulatory Licensing**: SafePay has not undergone payment intermediary licensing, legal compliance audits, or central bank regulatory approval in Ethiopia or any other jurisdiction.
- **Multi-Country Support**: SafePay is tailored specifically to Ethiopian payment rails (ETB currency with Chapa and ArifPay); it does not support multi-currency clearing or international gateway routing.
