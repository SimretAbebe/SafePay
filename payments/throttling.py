from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle


class PaymentRateThrottle(SimpleRateThrottle):
    """
    Limits the rate of API calls to create payments.
    Throttles by API key (X-API-Key) if provided, otherwise falls back to IP address.
    """
    scope = "payments"

    def get_rate(self):
        if hasattr(settings, "PAYMENT_THROTTLE_RATE"):
            return settings.PAYMENT_THROTTLE_RATE
        return getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_THROTTLE_RATES", {}).get(self.scope, "60/minute")

    def get_cache_key(self, request, view):
        api_key = request.headers.get("X-API-Key")
        if api_key:
            ident = api_key
        else:
            ident = self.get_ident(request)

        return self.cache_format % {
            "scope": self.scope,
            "ident": ident,
        }
