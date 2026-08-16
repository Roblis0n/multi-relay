"""Protocol-neutral contracts shared by provider adapters."""

from .base import (
    ProtocolAdapter,
    ProviderAdapter,
    ProviderErrorMetadata,
    discover_model_ids,
    extract_provider_error,
)
from .anthropic_messages import (
    AnthropicInboundAdapter,
    AnthropicOutboundRenderer,
    AnthropicUpstreamAdapter,
)
from .chat_completions import ChatCompletionsAdapter
from .responses import ResponsesAdapter

__all__ = [
    "AnthropicInboundAdapter",
    "AnthropicOutboundRenderer",
    "AnthropicUpstreamAdapter",
    "ChatCompletionsAdapter",
    "ProtocolAdapter",
    "ProviderAdapter",
    "ProviderErrorMetadata",
    "ResponsesAdapter",
    "discover_model_ids",
    "extract_provider_error",
]
