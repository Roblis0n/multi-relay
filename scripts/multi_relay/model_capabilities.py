"""Verified model and reasoning capability selection."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ManagerError


EFFORT_PREFERENCE = (
    "max",
    "xhigh",
    "high",
    "medium",
    "low",
    "minimal",
)
EFFORT_LEVELS = (
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
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


def resolve_target_effort(
    requested_effort: str | None,
    host_values: set[str] | frozenset[str],
    target_values: set[str] | frozenset[str],
) -> tuple[str | None, str]:
    """Resolve an explicit request against host and execution-target support."""

    unsupported = (set(host_values) | set(target_values)) - set(EFFORT_LEVELS)
    if unsupported:
        raise ManagerError(
            "invalid_reasoning_effort",
            "Reasoning capability sets contain unsupported values.",
            {"values": sorted(unsupported)},
        )
    if requested_effort is None:
        return resolve_effort(set(host_values), set(target_values))
    if requested_effort not in EFFORT_LEVELS:
        raise ManagerError(
            "invalid_reasoning_effort",
            f"Unsupported requested reasoning effort: {requested_effort}.",
        )
    common = set(host_values).intersection(target_values)
    if requested_effort in common:
        return requested_effort, "requested_intersection"
    requested_index = EFFORT_LEVELS.index(requested_effort)
    for effort in reversed(EFFORT_LEVELS[:requested_index]):
        if effort in common:
            return effort, "downgraded_intersection"
    return None, "provider_default"


__all__ = [
    "EFFORT_LEVELS",
    "EFFORT_PREFERENCE",
    "ModelSelection",
    "resolve_effort",
    "resolve_target_effort",
]
