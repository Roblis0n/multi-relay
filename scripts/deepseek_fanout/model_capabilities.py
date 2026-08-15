"""Verified model and reasoning capability selection."""

from __future__ import annotations

from dataclasses import dataclass


EFFORT_PREFERENCE = (
    "max",
    "xhigh",
    "high",
    "medium",
    "low",
    "minimal",
)


@dataclass(frozen=True)
class ModelSelection:
    """A provider model and effort accepted by both endpoints."""

    requested_model: str
    resolved_model: str
    reasoning_effort: str | None
    effort_source: str


def resolve_effort(
    codex_values: set[str],
    provider_values: set[str],
) -> tuple[str | None, str]:
    """Choose the highest verified common effort or omit the setting."""

    common = codex_values.intersection(provider_values)
    for effort in EFFORT_PREFERENCE:
        if effort in common:
            return effort, "verified_intersection"
    return None, "provider_default"
