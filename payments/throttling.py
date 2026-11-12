import math
from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle


def record_throttle_rate_limit(request, throttle, allowed: bool):
    """
    Helper to calculate rate limit bucket metadata and record it on the request.
    If multiple throttles run on the same request, records the most restrictive one.
    """
    if not hasattr(throttle, "num_requests") or not hasattr(throttle, "duration"):
        return

    limit = throttle.num_requests
    duration = throttle.duration
    history = getattr(throttle, "history", [])
    now = getattr(throttle, "now", None)
    if now is None:
        now = throttle.timer()

    if allowed:
        # Request succeeded throttle check and was recorded into history
        req_count = len(history)
    else:
        # Request failed throttle check; history len is at least num_requests
        req_count = len(history)

    remaining = max(0, limit - req_count)

    if history:
        reset = max(0, int(math.ceil(duration - (now - history[-1]))))
    else:
        reset = 0

    candidate = {
        "limit": limit,
        "remaining": remaining,
        "reset": reset,
        "scope": getattr(throttle, "scope", ""),
    }

    current = getattr(request, "_rate_limit_info", None)
    if current is None:
        request._rate_limit_info = candidate
    else:
        # Most restrictive bucket: lowest remaining, then lowest limit
        if (candidate["remaining"] < current["remaining"]) or (
            candidate["remaining"] == current["remaining"] and candidate["limit"] < current["limit"]
        ):
            request._rate_limit_info = candidate

    # Mirror onto the underlying Django HttpRequest so Django middleware can read it.
    # DRF wraps the Django HttpRequest in rest_framework.request.Request; attributes
    # set on the wrapper are NOT visible on the underlying _request object.
    underlying = getattr(request, "_request", None)
    if underlying is not None and underlying is not request:
        underlying._rate_limit_info = request._rate_limit_info


class BaseMerchantRateThrottle(SimpleRateThrottle):
    """Base throttle for merchant-scoped endpoints that records rate limit headers."""

    def allow_request(self, request, view):
        allowed = super().allow_request(request, view)
        record_throttle_rate_limit(request, self, allowed)
        return allowed


class PaymentRateThrottle(BaseMerchantRateThrottle):
    """
    Limits the rate of API calls to create payments.
    Throttles by merchant id when present, otherwise falls back to IP address.
    """
    scope = "payments"

    def get_rate(self):
        if hasattr(settings, "PAYMENT_THROTTLE_RATE"):
            return settings.PAYMENT_THROTTLE_RATE
        return getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_THROTTLE_RATES", {}).get(self.scope, "60/minute")

    def get_cache_key(self, request, view):
        merchant = getattr(request, "merchant", None)
        if merchant:
            livemode = getattr(request, "livemode", False)
            ident = f"{merchant.id}:{'live' if livemode else 'test'}"
        else:
            ident = self.get_ident(request)

        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }


class PaymentReadRateThrottle(BaseMerchantRateThrottle):
    """
    Limits the rate of API calls to read payments.
    Throttles by merchant id and mode when present, otherwise falls back to IP address.
    Separate bucket from payment creation (scope "payment_reads").
    """
    scope = "payment_reads"

    def get_rate(self):
        if hasattr(settings, "PAYMENT_READ_THROTTLE_RATE"):
            return settings.PAYMENT_READ_THROTTLE_RATE
        return getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_THROTTLE_RATES", {}).get(self.scope, "120/minute")

    def get_cache_key(self, request, view):
        merchant = getattr(request, "merchant", None)
        if merchant:
            livemode = getattr(request, "livemode", False)
            ident = f"{merchant.id}:{'live' if livemode else 'test'}"
        else:
            ident = self.get_ident(request)

        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }


class WebhookResendThrottle(BaseMerchantRateThrottle):
    """
    Limits the rate of webhook resend actions.
    Separate bucket from reads (scope "webhook_resend").
    Defaults to 10/minute via settings.WEBHOOK_RESEND_THROTTLE_RATE.
    """
    scope = "webhook_resend"

    def get_rate(self):
        if hasattr(settings, "WEBHOOK_RESEND_THROTTLE_RATE"):
            return settings.WEBHOOK_RESEND_THROTTLE_RATE
        return getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_THROTTLE_RATES", {}).get(self.scope, "10/minute")

    def get_cache_key(self, request, view):
        merchant = getattr(request, "merchant", None)
        if merchant:
            livemode = getattr(request, "livemode", False)
            ident = f"{merchant.id}:{'live' if livemode else 'test'}"
        else:
            ident = self.get_ident(request)

        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }


class HealthCheckThrottle(SimpleRateThrottle):
    """
    Limits the rate of health and readiness probe calls by IP address.
    Excluded from rate limit response headers.
    """
    scope = "health"

    def get_rate(self):
        if hasattr(settings, "HEALTH_THROTTLE_RATE"):
            return settings.HEALTH_THROTTLE_RATE
        return getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_THROTTLE_RATES", {}).get(self.scope, "120/minute")

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }


class LedgerExportRateThrottle(BaseMerchantRateThrottle):
    """
    Limits the rate of ledger CSV export calls.
    Throttles by merchant id and mode when present, otherwise falls back to IP address.
    Separate bucket from reads (scope "ledger_export").
    Defaults to 10/minute via settings.LEDGER_EXPORT_THROTTLE_RATE.
    """
    scope = "ledger_export"

    def get_rate(self):
        if hasattr(settings, "LEDGER_EXPORT_THROTTLE_RATE"):
            return settings.LEDGER_EXPORT_THROTTLE_RATE
        return getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_THROTTLE_RATES", {}).get(self.scope, "10/minute")

    def get_cache_key(self, request, view):
        merchant = getattr(request, "merchant", None)
        if merchant:
            livemode = getattr(request, "livemode", False)
            ident = f"{merchant.id}:{'live' if livemode else 'test'}"
        else:
            ident = self.get_ident(request)

        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }
