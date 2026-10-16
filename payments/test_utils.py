from .models import Merchant


def create_test_merchant(name="Test Merchant", is_active=True, webhook_url=None, **kwargs):
    """
    Test helper that creates a Merchant and returns (merchant, raw_key).
    """
    return Merchant.create_with_key(
        name=name,
        is_active=is_active,
        webhook_url=webhook_url,
        **kwargs,
    )
