# SafePay Provider Plugin Guide

This guide explains how to build a new payment provider adapter for SafePay using the starter template (`payments/providers/template_provider.py`).

---

## 1. What "Plug In" Means

Plugging in an adapter means writing a Python class that subclasses `PaymentProvider` and strictly adheres to SafePay's four core jobs and safety invariants.

Passing all software checks and tests **does not mean** the provider is legally licensed, certified, or approved for moving real money. SafePay enforces strict technical contracts; regulatory compliance, merchant agreements, and payment licensing remain external operational requirements.

---

## 2. The Four Jobs

Every provider adapter is responsible for four discrete tasks:

1. **`initialize(payment, customer=None)`**: Creates a hosted checkout session with the provider. Returns an `InitResult` containing the `provider_reference` and the customer-facing `checkout_url`.
2. **`verify(provider_reference)`**: Queries the provider's authoritative inquiry API for the ground-truth payment status. Returns a `ProviderStatusResult`.
3. **`cancel(provider_reference)`**: Requests session cancellation with the provider if supported (`supports_cancel = True`). If the provider does not support cancellation, this must raise `ProviderError`.
4. **`parse_webhook(headers, body)`**: Validates webhook signature authenticity and parses incoming notification payloads into a structured `ProviderEvent`. Remember: SafePay **never trusts a webhook alone**; `confirm_from_webhook` will always trigger an out-of-band `verify()` call to confirm status directly.

---

## 3. The Safety Rules

Payment systems require defensive engineering. Every adapter must follow these non-negotiable rules:

- **Never mark paid from a redirect or webhook alone:** Customers can manipulate return URLs, and webhooks can be replayed or delayed. Status must always be checked via `verify()`.
- **An unknown outcome is never a failure:** If a network timeout or HTTP 5xx occurs during `initialize()`, the payment might have been created on the provider's servers. Do not set `not_created=True` unless the provider explicitly confirms no session was opened. Never trigger an automatic fallback if a duplicate charge could occur.
- **Unknown statuses must raise, never guess:** In `map_status()`, any status value not explicitly documented and handled must raise `UnsupportedProviderStatus` or `ProviderError`. Never map an unknown status to `pending`, `failed`, or `succeeded`.
- **Secrets never appear in logs or responses:** API keys, webhook secrets, authentication tokens, and customer credentials must never be included in return dictionaries, log statements, or exception messages.

---

## 4. Before Real Money

Before flipping `supports_livemode = True` and processing real transactions:

1. **Legal & Regulatory Approval:** Obtain proper legal authorization and merchant service agreements in each operating country and jurisdiction.
2. **Confirmed Sandbox Access:** Validate the adapter thoroughly against the provider's official sandbox environment across all scenarios (success, user cancellation, insufficient funds, expired sessions).
3. **Confirmed Webhook Security:** Verify webhook authenticity mechanisms against the provider's official documentation (e.g. HMAC-SHA256 signatures, timestamp replay protection).
4. **Keep Live Mode Switched Off:** Keep `supports_livemode = False` until production credentials and end-to-end integration tests are certified.

---

## 5. Next Steps

1. Copy `payments/providers/template_provider.py` to `payments/providers/<provider_name>.py`.
2. Implement the four jobs and `map_status()` according to the provider's official API specifications.
3. Add adapter unit tests to verify state transitions and error handling.
4. Once verified, register the new adapter in `payments/providers/registry.py`.
5. Open a pull request for review.
