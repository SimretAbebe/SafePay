from typing import Dict

from django.conf import settings

from .base import PaymentProvider, ProviderError
from .fake import FakeProvider

_registry: Dict[str, PaymentProvider] = {}


def register_provider(provider: PaymentProvider) -> None:
    """Register a PaymentProvider instance with the registry."""
    if not isinstance(provider, PaymentProvider):
        raise TypeError(f"Expected PaymentProvider instance, got {type(provider)}")
    _registry[provider.name] = provider


def clear_registry() -> None:
    """Clear all registered providers."""
    _registry.clear()


def is_fake_provider_enabled() -> bool:
    return bool(
        getattr(settings, "DEBUG", False)
        or getattr(settings, "ENABLE_FAKE_PROVIDER", False)
    )


def get_provider(name: str) -> PaymentProvider:
    """
    Retrieve a registered payment provider by name.
    Raises ProviderError if the provider is unknown or not enabled.
    """
    if name == "fake":
        if is_fake_provider_enabled():
            if "fake" not in _registry:
                _registry["fake"] = FakeProvider()
        else:
            # If disabled via settings, ensure fake is not exposed
            _registry.pop("fake", None)

    if name not in _registry:
        available = sorted(_registry.keys())
        raise ProviderError(
            f"Payment provider '{name}' is not registered or unavailable. "
            f"Available providers: {available}"
        )

    return _registry[name]


# Initialize default registry on module import if enabled
if is_fake_provider_enabled():
    _registry["fake"] = FakeProvider()
