import contextvars
from contextlib import contextmanager
import logging
import time
from typing import Optional

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)
slow_logger = logging.getLogger("safepay.slow")


class RequestTimer:
    """Accumulates database and provider execution times for a single request."""

    def __init__(self):
        self.start_time: float = 0.0
        self.db_time: float = 0.0
        self.db_queries: int = 0
        self.provider_time: float = 0.0
        self.provider_called: bool = False

    def record_db_query(self, duration: float) -> None:
        self.db_time += duration
        self.db_queries += 1

    def record_provider_time(self, duration: float) -> None:
        self.provider_time += duration
        self.provider_called = True


current_timer: contextvars.ContextVar[Optional[RequestTimer]] = contextvars.ContextVar(
    "current_timer", default=None
)


@contextmanager
def provider_timer():
    """
    Context manager that measures elapsed time for provider calls.
    Adds the elapsed duration to the current request's timer if active;
    no-op when invoked outside of a request.
    """
    timer = current_timer.get()
    start = time.perf_counter()
    try:
        yield
    finally:
        duration = time.perf_counter() - start
        if timer is not None:
            timer.record_provider_time(duration)


class ServerTimingMiddleware:
    """
    Measures database query duration & count, provider call duration, and total request time.
    Attaches the Server-Timing header if enabled and logs slow requests above SLOW_REQUEST_MS.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        timer = RequestTimer()
        token = current_timer.set(timer)
        timer.start_time = time.perf_counter()

        def db_wrapper(execute, sql, params, many, context):
            q_start = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                q_duration = time.perf_counter() - q_start
                timer.record_db_query(q_duration)

        try:
            with connection.execute_wrapper(db_wrapper):
                response = self.get_response(request)
        finally:
            current_timer.reset(token)

        total_duration = time.perf_counter() - timer.start_time
        total_ms = total_duration * 1000.0
        db_ms = timer.db_time * 1000.0
        provider_ms = timer.provider_time * 1000.0
        app_ms = max(0.0, total_ms - db_ms - (provider_ms if timer.provider_called else 0.0))

        # Check slow request logging
        slow_request_ms = getattr(settings, "SLOW_REQUEST_MS", 800)
        path = getattr(request, "path", "")
        if path not in ("/health/", "/ready/") and total_ms > slow_request_ms:
            rid = getattr(request, "request_id", "-")
            status_code = getattr(response, "status_code", 0)
            slow_logger.warning(
                "Slow request [%s] %s %s status=%s total=%.1fms db=%.1fms (%d queries) provider=%.1fms",
                rid,
                request.method,
                path,
                status_code,
                round(total_ms, 1),
                round(db_ms, 1),
                timer.db_queries,
                round(provider_ms, 1),
            )

        # Server-Timing header
        if getattr(settings, "SERVER_TIMING_ENABLED", False):
            parts = [
                f'db;dur={round(db_ms, 1)};desc="{timer.db_queries} queries"',
            ]
            if timer.provider_called:
                parts.append(f'provider;dur={round(provider_ms, 1)}')
            parts.append(f'app;dur={round(app_ms, 1)}')
            parts.append(f'total;dur={round(total_ms, 1)}')
            response["Server-Timing"] = ", ".join(parts)

        return response

