"""Protocol-neutral contracts shared by provider adapters."""

from .base import (
    ProtocolAdapter,
    ProviderAdapter,
    ProviderErrorMetadata,
    discover_model_ids,
    extract_provider_error,
)
from .chat_completions import ChatCompletionsAdapter
from .responses import ResponsesAdapter

__all__ = [
    "ChatCompletionsAdapter",
    "ProtocolAdapter",
    "ProviderAdapter",
    "ProviderErrorMetadata",
    "ResponsesAdapter",
    "discover_model_ids",
    "extract_provider_error",
]
