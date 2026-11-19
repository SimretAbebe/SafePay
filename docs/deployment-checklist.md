# Deployment Checklist & Readiness Review

This checklist documents the deployment readiness checks evaluated by `python manage.py check_deploy_ready`.
SafePay is configured for test mode by default. Prior to deploying to a staging or production environment, ensure all items below are configured.

---

## Deployment Readiness Checks

| Check | Status | Required Environment Variable & Note |
|---|---|---|
| `SECRET_KEY is strong and not default/insecure` | PASS | `SECRET_KEY`: Set to a unique, random string of 50+ characters. |
| `DEBUG is False` | FAIL | `DEBUG=False`: Must be disabled in production to prevent information disclosure. |
| `ALLOWED_HOSTS is explicitly configured (not wildcard or empty)` | PASS | `ALLOWED_HOSTS`: Set to comma-separated domains (e.g. `api.safepay.et,safepay.et`). Wildcards (`*`) must not be used in production. |
| `SECURE_SSL_REDIRECT is True` | FAIL | `SECURE_SSL_REDIRECT=True`: Redirects all non-HTTPS HTTP traffic to HTTPS. |
| `SESSION_COOKIE_SECURE is True` | FAIL | `SESSION_COOKIE_SECURE=True`: Ensures session cookies are only transmitted over HTTPS connections. |
| `CSRF_COOKIE_SECURE is True` | FAIL | `CSRF_COOKIE_SECURE=True`: Ensures CSRF protection cookies are only transmitted over HTTPS connections. |
| `SECURE_HSTS_SECONDS is configured (> 0)` | FAIL | `SECURE_HSTS_SECONDS=31536000`: Enables HTTP Strict Transport Security (HSTS) headers. |
| `Database engine is PostgreSQL` | PASS | `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`: Database engine configured with PostgreSQL. |
| `Celery broker is configured with Redis` | PASS | `REDIS_URL`: Redis broker configured for Celery task dispatch and caching. |
| `WEBHOOK_SECRET is set and not default` | PASS | `WEBHOOK_SECRET`: Production webhook signing secret. |

---

## Summary of Environment Variables Required for Production

Before deploying SafePay to production, set the following environment variables:

```bash
# Security & Debugging
DEBUG=False
SECRET_KEY=generate-a-strong-random-50-character-key
ALLOWED_HOSTS=api.safepay.et,safepay.et

# HTTPS & Cookie Protection
SECURE_SSL_REDIRECT=True
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
SECURE_HSTS_SECONDS=31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS=True
SECURE_HSTS_PRELOAD=True

# Database & Broker
DB_NAME=safepay
DB_USER=safepay_user
DB_PASSWORD=strong-db-password
DB_HOST=your-db-host.internal
DB_PORT=5432
REDIS_URL=redis://your-redis-host.internal:6379/0

# Webhooks & Core API
WEBHOOK_SECRET=strong-webhook-signing-secret
SAFE_PAY_API_KEY=production-admin-api-key
```
