"""
Management command to check deployment readiness checks against SafePay configuration.
Evaluates security and operational deployment requirements and reports PASS/FAIL status.
"""

import os
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Check deployment readiness for production"

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("SafePay Deployment Readiness Review\n"))

        checks = []

        # 1. SECRET_KEY
        secret_key = getattr(settings, "SECRET_KEY", "")
        is_insecure = (
            secret_key.startswith("django-insecure-")
            or len(secret_key) < 50
            or len(set(secret_key)) < 5
        )
        checks.append((
            "SECRET_KEY is strong and not default/insecure",
            not is_insecure,
            "Set SECRET_KEY in environment to a unique, random string with at least 50 characters."
        ))

        # 2. DEBUG
        debug = getattr(settings, "DEBUG", True)
        checks.append((
            "DEBUG is False",
            not debug,
            "Set DEBUG=False in environment."
        ))

        # 3. ALLOWED_HOSTS
        allowed_hosts = getattr(settings, "ALLOWED_HOSTS", [])
        valid_hosts = bool(allowed_hosts and allowed_hosts != ["*"] and "*" not in allowed_hosts)
        checks.append((
            "ALLOWED_HOSTS is explicitly configured (not wildcard or empty)",
            valid_hosts,
            "Set ALLOWED_HOSTS in environment to comma-separated valid domain names (e.g. api.safepay.et)."
        ))

        # 4. SECURE_SSL_REDIRECT
        ssl_redirect = getattr(settings, "SECURE_SSL_REDIRECT", False)
        checks.append((
            "SECURE_SSL_REDIRECT is True",
            bool(ssl_redirect),
            "Set SECURE_SSL_REDIRECT=True in environment."
        ))

        # 5. SESSION_COOKIE_SECURE
        session_cookie_secure = getattr(settings, "SESSION_COOKIE_SECURE", False)
        checks.append((
            "SESSION_COOKIE_SECURE is True",
            bool(session_cookie_secure),
            "Set SESSION_COOKIE_SECURE=True in environment."
        ))

        # 6. CSRF_COOKIE_SECURE
        csrf_cookie_secure = getattr(settings, "CSRF_COOKIE_SECURE", False)
        checks.append((
            "CSRF_COOKIE_SECURE is True",
            bool(csrf_cookie_secure),
            "Set CSRF_COOKIE_SECURE=True in environment."
        ))

        # 7. SECURE_HSTS_SECONDS
        hsts_seconds = getattr(settings, "SECURE_HSTS_SECONDS", 0) or 0
        checks.append((
            "SECURE_HSTS_SECONDS is configured (> 0)",
            hsts_seconds > 0,
            "Set SECURE_HSTS_SECONDS in environment (e.g. 31536000)."
        ))

        # 8. Database Engine
        db_engine = settings.DATABASES["default"]["ENGINE"]
        is_postgres = "postgresql" in db_engine or "psycopg" in db_engine
        checks.append((
            "Database engine is PostgreSQL",
            is_postgres,
            "Configure DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT for PostgreSQL in environment."
        ))

        # 9. Redis Cache & Broker
        redis_broker = getattr(settings, "CELERY_BROKER_URL", "")
        is_redis_broker = redis_broker.startswith("redis://") or redis_broker.startswith("rediss://")
        checks.append((
            "Celery broker is configured with Redis",
            is_redis_broker,
            "Set REDIS_URL in environment."
        ))

        # 10. Webhook signing secret
        webhook_secret = getattr(settings, "WEBHOOK_SECRET", "")
        has_webhook_secret = bool(webhook_secret and webhook_secret != "dev-secret-change-me")
        checks.append((
            "WEBHOOK_SECRET is set and not default",
            has_webhook_secret,
            "Set WEBHOOK_SECRET in environment to a production secret."
        ))

        pass_count = 0
        fail_count = 0

        for label, passed, hint in checks:
            if passed:
                pass_count += 1
                self.stdout.write(f"PASS: {label}")
            else:
                fail_count += 1
                self.stdout.write(f"FAIL: {label}")

        self.stdout.write(f"\nTotal: {pass_count} passed, {fail_count} failed.\n")
