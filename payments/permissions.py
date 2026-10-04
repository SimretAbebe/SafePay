from rest_framework.permissions import BasePermission
from .models import Merchant


class HasAPIKey(BasePermission):
    """
    Allows access only to requests containing a valid X-API-Key header
    belonging to an active merchant.
    """
    message = "Invalid or missing API key."

    def has_permission(self, request, view):
        api_key = request.headers.get("X-API-Key")
        if not api_key:
            return False

        key_hash = Merchant.hash_key(api_key)
        merchant = Merchant.objects.filter(api_key_hash=key_hash, is_active=True).first()
        if not merchant:
            return False

        request.merchant = merchant
        return True
