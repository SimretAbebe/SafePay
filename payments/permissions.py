from django.conf import settings
from rest_framework.exceptions import PermissionDenied
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

        test_merchant = Merchant.objects.filter(api_key_hash=key_hash, is_active=True).first()
        if test_merchant:
            request.merchant = test_merchant
            request.livemode = False
            return True

        live_merchant = Merchant.objects.filter(live_api_key_hash=key_hash, is_active=True).first()
        if live_merchant:
            request.merchant = live_merchant
            request.livemode = True

            if not getattr(settings, "LIVE_MODE_AVAILABLE", False):
                raise PermissionDenied("Live mode is not available yet.")

            if not live_merchant.live_enabled:
                raise PermissionDenied("Live mode is not enabled for this account.")

            return True

        return False
