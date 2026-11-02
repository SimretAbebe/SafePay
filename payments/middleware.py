import contextvars
import logging
import re
import sys
import time
import uuid

from django.conf import settings
from django.http import HttpResponseServerError

request_id_var = contextvars.ContextVar("request_id", default="-")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")
_logger = logging.getLogger("django.request")


class ServerTimingMiddleware:
    """Measures total request duration using time.perf_counter() and attaches Server-Timing."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        t0 = time.perf_counter()
        response = self.get_response(request)

        if getattr(settings, "SERVER_TIMING_ENABLED", True):
            dur_ms = max(0.0, (time.perf_counter() - t0) * 1000.0)
            if "Server-Timing" not in response.headers or not getattr(settings, "SERVER_TIMING_DETAILED", False):
                response["Server-Timing"] = f"app;dur={dur_ms:.1f}"
        elif "Server-Timing" in response.headers:
            del response["Server-Timing"]

        return response


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get("X-Request-Id") or request.META.get("HTTP_X_REQUEST_ID", "")
        if incoming and _VALID_REQUEST_ID.match(incoming):
            rid = incoming
        else:
            rid = uuid.uuid4().hex

        request.request_id = rid
        token = request_id_var.set(rid)

        try:
            try:
                response = self.get_response(request)
            except Exception:
                _logger.error(
                    "Internal Server Error: %s",
                    request.path,
                    exc_info=sys.exc_info(),
                    extra={"status_code": 500, "request": request},
                )
                response = HttpResponseServerError("Internal Server Error")
            response["X-Request-Id"] = rid
            return response
        finally:
            request_id_var.reset(token)


class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get("-")
        return True
