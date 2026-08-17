"""Stable lifecycle imports for Multi Relay."""

from .relay_manager import DEFAULT_CONCURRENCY, SCHEMA_VERSION, RelayManager
from .hosts.claude_code import launch_claude_code

# Preserve the former Python class import while all new code uses RelayManager.
FanoutManager = RelayManager

__all__ = [
    "DEFAULT_CONCURRENCY",
    "FanoutManager",
    "RelayManager",
    "SCHEMA_VERSION",
    "launch_claude_code",
]
