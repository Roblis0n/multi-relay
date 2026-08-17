"""Canonical product identity and migration-only legacy names."""

from __future__ import annotations


PRODUCT_NAME = "Multi Relay"
REPOSITORY_NAME = "multi-relay"
PACKAGE_NAME = "multi_relay"
CLI_NAME = "multi-relay"
STATE_DIRECTORY_NAME = "multi-relay"
OWNERSHIP_MARKER = "MULTI-RELAY"
MANAGEMENT_PREFIX = "/_multi-relay"
PRODUCT_VERSION = "1.0.0"
USER_AGENT = f"{CLI_NAME}/{PRODUCT_VERSION}"

# Read-only migration inputs. New state and public output must never use these.
LEGACY_STATE_DIRECTORY_NAMES = (
    "codex-multi-relay",
    "codex-deepseek-relay",
    "codex-deepseek-subagent",
)


__all__ = [
    "CLI_NAME",
    "LEGACY_STATE_DIRECTORY_NAMES",
    "MANAGEMENT_PREFIX",
    "OWNERSHIP_MARKER",
    "PACKAGE_NAME",
    "PRODUCT_NAME",
    "PRODUCT_VERSION",
    "REPOSITORY_NAME",
    "STATE_DIRECTORY_NAME",
    "USER_AGENT",
]
