# SafePay Code Map

This document outlines the top-level repository structure and the key modules inside `payments/`.

---

## Top-Level Repository Structure

- **`safepay/`**: Django project configuration root (`settings.py`, `urls.py`, `celery.py`, `wsgi.py`, `asgi.py`).
- **`payments/`**: Main Django application implementing the payments engine, provider adapters, double-entry ledger, webhook deliveries, and API endpoints.
- **`docs/`**: Technical guides, operational runbooks, and developer documentation.
- **`.github/`**: GitHub Actions workflows for continuous integration (CI).
- **`thunder-tests/`**: Thunder Client API request test collections.

---

## Core Modules in `payments/`

### 1. Data Models (`payments/models.py`)
- **`Merchant`**: Merchant accounts with separate test (`sp_test_`) and live (`sp_live_`) API keys, routing preferences, and webhook configuration.
- **`Payment`**: Core transaction model managing idempotency, state transitions (`pending`, `processing`, `succeeded`, `failed`, `cancelled`, `expired`), and provider references.
- **`PaymentStatusHistory`**: Append-only log of every status transition for every payment.
- **`ProviderAttempt`**: Records each provider checkout attempt, error flags, and circuit breaker outcomes.
- **`WebhookDelivery`**: Tracks outbound webhook HTTP attempts, responses, and delivery status.
- **`InboundWebhookEvent`**: Deduped log of received provider webhook notifications.
- **`ReviewQueueProxy`**: Admin proxy for flagged transactions requiring manual intervention.

### 2. Views & API Endpoints (`payments/views.py`, `payments/urls.py`)
- **`CheckoutSessionView`** (`POST /api/checkout/sessions/`): Initiates hosted checkout sessions idempotently.
- **`PaymentDetailView`** (`GET /api/payments/{id}/`): Retrieves payment status with conditional ETag caching.
- **`PaymentVerifyView`** (`POST /api/payments/{id}/verify/`): Actively queries the provider for verified status.
- **`PaymentCancelView`** (`POST /api/payments/{id}/cancel/`): Cancels a pending checkout session.
- **`RefundCreateView`** (`POST /api/payments/{id}/refunds/`): Issues partial or full refunds on succeeded payments.
- **`ProviderListView`** (`GET /api/providers/`): Read-only listing of registered provider capabilities.
- **`InboundWebhookView`** (`POST /api/webhooks/{provider}/`): Ingests provider callbacks with authenticity verification.

### 3. Provider Adapters (`payments/providers/`)
- **`base.py`**: Defines abstract `PaymentProvider` interface (`initialize`, `verify`, `cancel`, `parse_webhook`, `map_status`) and standard `ProviderError` hierarchies.
- **`registry.py`**: Central provider registry managing discovery and retrieval by name (`fake`, `chapa`, `arifpay`).
- **`fake.py`**: In-memory test provider for unit tests and local simulation.
- **`chapa.py`**: Chapa payment provider integration with verify-before-retry semantics.
- **`arifpay.py`**: ArifPay payment provider integration.
- **`template_provider.py`**: Starter template for building new provider plugins safely.

### 4. Double-Entry Ledger (`payments/ledger/`)
- **`models.py`**: `LedgerAccount`, `LedgerJournal`, and `LedgerEntry` tables implementing double-entry accounting invariants (debits equal credits).
- **`services.py`**: Atomic posting functions (`post_payment_succeeded`, `post_refund_succeeded`) that record balance movements upon terminal events.
- **`views.py`**: Read-only ledger balance queries and CSV exports.

### 5. Webhooks & Outbox Delivery (`payments/webhook_*.py`, `payments/services.py`)
- **`webhook_verification.py`**: Verifies HMAC signatures and replay protection timestamps.
- **`webhook_secrets.py`**: Handles webhook secret rotation across versioned secrets.
- **`webhook_urls.py`**: Enforces strict URL validation (blocks internal IPs and SSRF targets).
- **`WebhookOutbox`**: Transactional outbox ensuring reliable webhook dispatch even during downstream network partitions.

### 6. Investigation & Simulator (`payments/investigator.py`, `payments/simulator.py`)
- **`investigator.py`**: Pure functions (`build_investigation`) assembling an audit timeline and plain-English summary of any payment.
- **`simulator.py`**: Staff-only sandbox (`/simulator/`) with "Pay", "Fail", and "Never answer" actions to test flows safely.

### 7. Background Tasks & Middleware
- **`tasks.py`**: Celery tasks for periodic reconciliation sweeps, payment expiry sweeps, outbox retries, and review alerts.
- **`breaker.py`**: In-memory and cache-backed circuit breaker guarding failing external providers.
- **`middleware.py`**: Enforces request IDs, Server-Timing metrics, slow request logging, and rate limiting.

### 8. Management Commands (`payments/management/commands/`)
- **`create_merchant`**: Generates a new merchant with API key pairs.
- **`chapa_smoke` & `arifpay_smoke`**: CLI utilities to smoke-test provider integrations.
- **`bench`**: Performance benchmarking suite measuring RPS, p95 latencies, and DB query counts.
- **`ledger_verify`**: Audits double-entry ledger invariants across all journals and entries.

### 9. Test Suite (`payments/test_*.py`, `payments/tests/`)
- Extensive automated test suite covering auth, throttling, webhook security, ledger balance verification, circuit breaker behavior, reconciliation, server timing, and livemode boundaries.
