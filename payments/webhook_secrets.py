import base64
import hashlib
import hmac

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def derive_merchant_secret(merchant, version=None, livemode=False) -> str:
    """
    Derives a deterministic, versioned webhook signing secret for a merchant.

    HMAC-SHA256 is computed over 'merchant:{merchant.id}:v{version}' keyed with
    the master settings.WEBHOOK_SECRET, formatted as 'whsec_' followed by
    unpadded base64url encoding.

    NOTE: Changing the master secret (settings.WEBHOOK_SECRET) invalidates every
    merchant secret derived from it.
    """
    master_secret = getattr(settings, "WEBHOOK_SECRET", None)
    if not master_secret:
        raise ImproperlyConfigured("WEBHOOK_SECRET must be set to derive merchant secrets.")

    if version is None:
        version = merchant.webhook_secret_version

    if livemode:
        message = f"merchant:{merchant.id}:live:v{version}".encode("utf-8")
    else:
        message = f"merchant:{merchant.id}:v{version}".encode("utf-8")

    key = master_secret.encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    b64_str = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"whsec_{b64_str}"
