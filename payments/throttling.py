from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle


class PaymentRateThrottle(SimpleRateThrottle):
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


class PaymentReadRateThrottle(SimpleRateThrottle):
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


class WebhookResendThrottle(SimpleRateThrottle):
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
