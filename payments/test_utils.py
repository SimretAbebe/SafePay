from .models import Merchant


def create_test_merchant(name="Test Merchant", is_active=True, **kwargs):
    """
    Test helper that creates a Merchant and returns (merchant, raw_key).
    """
    return Merchant.create_with_key(name=name, is_active=is_active, **kwargs)
