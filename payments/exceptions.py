"""Exception handling with rate limit header support."""
from rest_framework.views import exception_handler as drf_exception_handler


def custom_exception_handler(exc, context):
    """
    Custom exception handler that wraps DRF's default exception handler
    and attaches rate limit headers (X-RateLimit-Limit, X-RateLimit-Remaining,
    X-RateLimit-Reset) to the error response when rate limit information is present
    on the request (e.g. 429 Throttled responses or other errors).
    """
    response = drf_exception_handler(exc, context)

    if response is not None and context is not None and response.status_code != 404:
        request = context.get("request")
        if request is not None:
            rate_limit_info = getattr(request, "_rate_limit_info", None)
            if rate_limit_info:
                limit = rate_limit_info["limit"]
                remaining = rate_limit_info["remaining"]
                reset = rate_limit_info["reset"]
                response.headers["X-RateLimit-Limit"] = str(limit)
                response.headers["X-RateLimit-Remaining"] = str(remaining)
                response.headers["X-RateLimit-Reset"] = str(reset)

    return response

