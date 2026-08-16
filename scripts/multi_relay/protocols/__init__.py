"""Protocol-neutral contracts shared by provider adapters."""

from .base import ProviderAdapter, ProviderErrorMetadata, extract_provider_error

__all__ = [
    "ProviderAdapter",
    "ProviderErrorMetadata",
    "extract_provider_error",
]
