# SafePay Deployment Stage 1: Preparation & Readiness

This document records the completed preparations in Stage 1 and establishes the prerequisites for Stage 2 (Deployment).

---

## 1. What Is Ready (Stage 1)

1. **Environment Configuration & Security Settings**:
   - Environment variable overrides implemented in `safepay/settings.py` for all deployment-sensitive toggles:
     - `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`
     - `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_HSTS_SECONDS`
   - Complete production environment variable template documented in `.env.production.example`.

2. **Automated Readiness Verification**:
   - Automated deployment readiness review command: `python manage.py check_deploy_ready`.
   - Evaluates key security properties, database connectivity, broker configuration, and webhook secrets.

3. **Application Containerization (Dockerfile)**:
   - Base image: `python:3.11-slim`.
   - Dependency installation: installs `requirements.txt` only (excludes development tools).
   - Migrations are decoupled (not baked into container build steps).
   - *Note on production server*: Currently starts via `CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]`. A production WSGI server (such as Gunicorn or uWSGI) is not yet configured.

4. **Multi-Service Orchestration**:
   - `docker-compose.yml` defining `web`, `celery` worker, `beat` scheduler, `db` (PostgreSQL 16), and `redis` (Redis 7).

---

## 2. What Stage 2 Needs (Deployment Requirements)

Stage 2 represents the actual cloud infrastructure provisioning and deployment, which involves:

1. **Host Selection**:
   - Selecting a target cloud host or managed container runtime (e.g., Render, Railway, AWS ECS, DigitalOcean, or private VPS).
2. **Production Infrastructure Provisioning**:
   - Provisioning managed PostgreSQL 16+ database instance.
   - Provisioning managed Redis 7+ instance for Celery task queuing and caching:
     - `REDIS_URL` configures the Celery background job queue broker and result backend.
     - `REDIS_CACHE_URL` configures application caching (rate limits, circuit breakers, health probes); defaults to `REDIS_URL` if not set separately. In production, both typically point to the same Redis URL.
     - A dedicated Celery background worker process (`celery -A safepay worker`) is required for background jobs (such as outbox webhook dispatch and reconciliation sweeps) to run.
3. **Injecting Production Secrets**:
   - Setting real production values in the hosting platform's environment variables based on `.env.production.example` (strong `SECRET_KEY`, `DEBUG=False`, specific `ALLOWED_HOSTS`, real gateway keys).
4. **Database Migration Step**:
   - Executing `python manage.py migrate` as an explicit release/pre-deploy phase prior to serving traffic.
5. **Production WSGI/ASGI Server**:
   - Adding a production-grade application server (e.g. Gunicorn / Uvicorn) to the container command rather than `runserver`.
6. **Live Endpoint Health Verification**:
   - Requesting `GET /health/` and `GET /ready/` on the public domain and verifying `200 OK` status with database and cache probes passing.

---

## 3. Current Status

> **Important:** **Stage 2 has NOT happened yet.**
> SafePay remains in local preparation and staging test mode. No production cloud infrastructure is currently provisioned, and no live traffic or live transactions are enabled.
