import hmac
import time
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.cache import cache
from django.db import connection

from drf_spectacular.utils import extend_schema
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.response import Response

from .throttling import HealthCheckThrottle

READY_CACHE_KEY = "safepay:health:ready"
RECONCILE_SWEEP_TS_KEY = "safepay:health:reconcile_sweep_ts"


@extend_schema(exclude=True)
@api_view(["GET"])
@authentication_classes([])
@permission_classes([])
@throttle_classes([HealthCheckThrottle])
def liveness_check(request):
    return Response({"status": "ok"})


def _check_database():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return "ok"


def _check_cache():
    key = "safepay:health:probe"
    cache.set(key, "1", timeout=10)
    if cache.get(key) != "1":
        raise RuntimeError("cache read-back mismatch")
    return "ok"


def _check_broker():
    from safepay.celery import app as celery_app

    conn = celery_app.connection()
    try:
        conn.ensure_connection(max_retries=1)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return "ok"


def _get_reconcile_sweep_status():
    try:
        ts = cache.get(RECONCILE_SWEEP_TS_KEY)
    except Exception:
        return "unknown"
    if ts is None:
        return "unknown"
    try:
        stale_seconds = getattr(settings, "HEALTH_RECONCILE_STALE_SECONDS", 300)
        if time.time() - float(ts) <= stale_seconds:
            return "ok"
        return "stale"
    except Exception:
        return "unknown"


@extend_schema(exclude=True)
@api_view(["GET"])
@authentication_classes([])
@permission_classes([])
@throttle_classes([HealthCheckThrottle])
def readiness_check(request):
    token = getattr(settings, "HEALTH_TOKEN", "")
    if token:
        provided = request.headers.get("X-Health-Token") or request.META.get("HTTP_X_HEALTH_TOKEN", "")
        if not provided or not hmac.compare_digest(str(token), str(provided)):
            return Response({"detail": "Forbidden"}, status=403)

    cache_seconds = getattr(settings, "HEALTH_CACHE_SECONDS", 5)
    try:
        cached = cache.get(READY_CACHE_KEY)
    except Exception:
        cached = None

    if cached is not None:
        status_code = 200 if cached.get("status") == "ready" else 503
        return Response(cached, status=status_code)

    timeout = getattr(settings, "HEALTH_CHECK_TIMEOUT_SECONDS", 2)
    checks = {}
    executor = ThreadPoolExecutor(max_workers=3)
    try:
        futures = {
            "database": executor.submit(_check_database),
            "cache": executor.submit(_check_cache),
            "broker": executor.submit(_check_broker),
        }
        deadline = time.monotonic() + timeout
        for name, future in futures.items():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                checks[name] = future.result(timeout=remaining)
            except Exception:
                checks[name] = "fail"
    finally:
        executor.shutdown(wait=False)

    all_ok = all(v == "ok" for v in checks.values())

    if all_ok:
        sweep_status = _get_reconcile_sweep_status()
        body = {
            "status": "ready",
            "checks": checks,
            "jobs": {"reconcile_sweep": sweep_status},
        }
        status_code = 200
    else:
        body = {
            "status": "not_ready",
            "checks": checks,
        }
        status_code = 503

    if cache_seconds > 0:
        try:
            cache.set(READY_CACHE_KEY, body, timeout=cache_seconds)
        except Exception:
            pass

    return Response(body, status=status_code)
