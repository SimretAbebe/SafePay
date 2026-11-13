# Changelog

A day-by-day record of everything built in SafePay from Day 1 to Day 46, organized by area.

---

## Payments

- **Day 1: Payment Model & DB Constraints** — Set up the initial `Payment` table with a database-level unique constraint on idempotency keys to stop double charges at the data layer.
- **Day 3: Payment Creation Endpoint** — Built the first REST API endpoint (`POST /api/payments/`) with validation and clean error responses.
- **Day 4: Database-Level Idempotency** — Added safe replay handling; repeating an API call returns the original payment with an `Idempotent-Replayed: true` header instead of creating a duplicate.
- **Day 5: Payment State Machine** — Defined strict payment states (`pending`, `processing`, `succeeded`, `failed`, `cancelled`, `expired`) so payments can only move through valid paths.
- **Day 6: Concurrency Protection** — Added row locking and atomic transactions to keep payments safe even when two identical requests hit the server at the exact same millisecond.
- **Day 12: Payment Status History** — Automatically track every status change with timestamps and reasons, viewable right inside the Django admin.
- **Day 14: Payment Cancellation & Verification** — Added endpoints to authoritatively check payment status (`GET /api/payments/{id}/verify/`) and cancel unpaid ones (`POST /api/payments/{id}/cancel/`).
- **Day 18: Hosted Checkout Sessions** — Added `POST /api/checkout/sessions/` to generate hosted payment links that customers can open to pay.
- **Day 22: Cursor-Paginated Payment List** — Built a fast, tamper-proof payment list endpoint (`GET /api/payments/`) with cursor pagination and filters for date, status, and provider.
- **Day 25: Automated Payment Expiry** — Automatically expire abandoned checkout sessions after a set time window, with safety checks in case someone pays late.
- **Day 43: Refund Processing** — Added an idempotent refund model and endpoint (`POST /api/refunds/`) that verifies balances before returning funds.

---

## Providers

- **Day 16: Provider Adapters & Fake Provider** — Created a pluggable provider interface alongside an in-memory `FakeProvider` to test payments and error edge cases without hitting real APIs.
- **Day 17: Chapa Gateway Adapter** — Built the Chapa provider adapter to initialize checkout sessions and verify payments against Chapa's API.
- **Day 19: Provider State Sync** — Added automatic background sync that pulls the latest payment status directly from the gateway during verification calls.
- **Day 21: ArifPay Gateway Adapter** — Built the ArifPay adapter supporting both hosted checkout and direct payment sessions.
- **Day 24: Multi-Provider Routing & Safe Fallback** — Added primary and fallback provider routing per merchant; SafePay only falls back if the primary provider confirmed the payment was never created.
- **Day 46: Verify-Before-Retry & Chain Widening Guard** — Always check with the gateway before retrying an initialization, and make sure retries never widen the fallback chain to prevent accidental double charges.

---

## Webhooks

- **Day 7: Celery Async Tasks** — Wired Celery and Redis to handle notifications in the background without slowing down customer requests.
- **Day 8: Outbound Webhooks with Retries** — Added `WebhookDelivery` tracking to deliver webhooks with HMAC signatures and exponential retry backoff.
- **Day 15: Timestamped Webhook Signatures** — Upgraded webhook payloads with event IDs, timestamps, and HMAC-SHA256 signatures to stop replay attacks.
- **Day 20: Chapa Inbound Webhooks** — Added an endpoint (`POST /api/webhooks/chapa/`) that verifies Chapa's webhook signature and logs the raw payload for audit trails.
- **Day 23: ArifPay Inbound Webhooks** — Added support for ArifPay webhooks, verifying payments by querying ArifPay's API before marking payments complete.
- **Day 26: Per-Merchant Webhook Routing & SSRF Protection** — Gave each merchant their own derived webhook secret and blocked private/internal IPs to protect against SSRF attacks.
- **Day 30: Delivery Logs & Manual Resend** — Added a log view (`GET /api/webhooks/deliveries/`) and a resend button (`POST /api/webhooks/deliveries/{id}/resend/`) to re-dispatch failed webhooks on demand.
- **Day 39: Transactional Webhook Outbox** — Save webhook events into an outbox table in the same database transaction as the payment, with a background worker ensuring no webhook gets lost.

---

## Reliability

- **Day 27: Background Reconciliation** — Scheduled Celery Beat tasks that wake up periodically, lock stale pending payments, and check with gateways to resolve stuck states.
- **Day 28: Review Queue & Telegram Alerts** — Transactions with status mismatches or weird states automatically get flagged for human review in Django admin, with alerts sent to Telegram.
- **Day 32: Circuit Breaker** — Automatically open a circuit breaker when an upstream provider goes down (5xx errors or timeouts) so requests fail fast instead of hanging.
- **Day 33: Conditional GET & ETags** — Supported `If-None-Match` caching on payment detail requests, returning a lightweight `304 Not Modified` when nothing has changed.
- **Day 40: Payment Investigator** — Built an automated diagnostic endpoint (`GET /api/payments/{id}/investigate/`) that traces status history, attempts, and webhooks to explain why a payment succeeded or failed.
- **Day 41: Clickable Evidence Trail** — Upgraded the investigator output with direct links to relevant Django admin records and clear reasons for each finding.

---

## Ledger

- **Day 37: Double-Entry Ledger Core** — Built an immutable, balanced double-entry ledger that automatically records debit and credit entries the moment a payment succeeds.
- **Day 38: Read-Only Ledger API & CSV Export** — Added clean endpoints to check ledger balances, list entries, and export transaction data directly to CSV.
- **Day 44: Ledger Refund Reversals** — Automatically post reversing ledger entries when a refund succeeds, keeping books balanced without modifying historical records.

---

## API

- **Day 9: API Key Authentication** — Secured endpoints with `X-API-Key` headers, storing only SHA-256 hashes in the database so raw keys are never saved.
- **Day 11: Merchant Accounts & Data Isolation** — Separated data so each merchant can only see their own payments, keys, and rate limits.
- **Day 29: Test & Live Mode Split** — Created distinct test (`sp_test_`) and live (`sp_live_`) environments with isolated data and webhooks.
- **Day 31: Interactive API Docs (OpenAPI)** — Added Swagger UI and OpenAPI 3.0 schema generation via `drf-spectacular` for easy interactive testing.
- **Day 35: Server-Timing Headers** — Added `Server-Timing` response headers so frontend developers can see exact backend and gateway response times.
- **Day 45: Rate Limit Response Headers** — Added standard `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` headers so clients know their remaining quota.

---

## Operations

- **Day 2: Docker Environment** — Set up Docker Compose to run Django, PostgreSQL 16, Redis 7, Celery worker, and Celery beat with one command.
- **Day 10: Django Admin Dashboards** — Added convenient admin screens to inspect merchants, payments, attempts, and webhook deliveries in real time.
- **Day 13: Smoke Test Commands** — Created management CLI commands (`create_merchant`, `chapa_smoke`, `arifpay_smoke`) to test configurations quickly from the terminal.
- **Day 34: Health & Readiness Probes** — Added `/health/` and `/ready/` endpoints to verify database connectivity, Redis, and circuit breaker status.
- **Day 36: Latency Benchmarking** — Built a safe benchmark command (`python manage.py bench`) to measure API latency percentiles locally without touching live gateways.
- **Day 42: Webhook Diagnostic Verification Tool** — Built `python manage.py check_webhook` to help developers verify payloads, signatures, and timestamps step by step.
