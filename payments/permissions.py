import secrets
from django.conf import settings
from rest_framework.permissions import BasePermission


class HasAPIKey(BasePermission):
    """
    Allows access only to requests containing a valid X-API-Key header.
    Uses constant-time comparison to prevent timing attacks.
    """
    message = "Invalid or missing API key."

    def has_permission(self, request, view):
        api_key = request.headers.get("X-API-Key")
        if not api_key:
            return False

        expected_key = getattr(settings, "SAFE_PAY_API_KEY", None)
        if not expected_key:
            return False

        return secrets.compare_digest(api_key, expected_key)
