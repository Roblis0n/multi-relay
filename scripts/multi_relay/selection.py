"""Pure, host-neutral execution target filtering and ordering."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from .catalog import (
    CAPABILITIES,
    HOSTS,
    TRUST_LEVELS,
    Catalog,
    CredentialRef,
    TargetPool,
)
from .errors import ManagerError
from .state import PoolRuntimeState, RuntimeState


CredentialAvailability = Callable[[CredentialRef], bool]


@dataclass(frozen=True)
class SelectionRequirements:
    """Request constraints evaluated without performing any I/O."""

    host: str
    required_capabilities: frozenset[str] = frozenset()
    context_tokens: int | None = None
    required_trust: str = "standard"

    def __post_init__(self) -> None:
        if self.host not in HOSTS:
            raise ManagerError(
                "invalid_selection_requirements",
                f"Unsupported host: {self.host}.",
                {"host": self.host},
            )
        unsupported = self.required_capabilities - CAPABILITIES
        if unsupported:
            raise ManagerError(
                "invalid_selection_requirements",
                "Selection requires unsupported capabilities.",
                {"capabilities": sorted(unsupported)},
            )
        if (
            self.context_tokens is not None
            and (
                isinstance(self.context_tokens, bool)
                or not isinstance(self.context_tokens, int)
                or self.context_tokens < 0
            )
        ):
            raise ManagerError(
                "invalid_selection_requirements",
                "context_tokens must be a non-negative integer or null.",
            )
        if self.required_trust not in TRUST_LEVELS:
            raise ManagerError(
                "invalid_selection_requirements",
                f"Unsupported trust requirement: {self.required_trust}.",
            )


@dataclass(frozen=True)
class TargetRejection:
    target_id: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"target_id": self.target_id, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class SelectionResult:
    """Selected target plus complete safe diagnostics for rejected candidates."""

    selected_target_id: str | None
    generation: int
    rejections: tuple[TargetRejection, ...] = ()
    changed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "selected_target_id": self.selected_target_id,
            "generation": self.generation,
            "changed": self.changed,
            "rejections": [item.to_dict() for item in self.rejections],
        }


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _candidate_order(
    pool: TargetPool,
    state: PoolRuntimeState,
    now: datetime,
) -> tuple[str, ...]:
    targets = pool.targets
    active = state.active_target_id
    if active not in targets:
        return targets
    active_index = targets.index(active)
    if pool.strategy == "sticky":
        return targets[active_index:] + targets[:active_index]

    hold_until = _timestamp(state.hold_until)
    # Invalid persisted timing is treated conservatively: keep the current target.
    if state.hold_until is not None and hold_until is None:
        return targets[active_index:] + targets[:active_index]
    if hold_until is not None and now < hold_until:
        return targets[active_index:] + targets[:active_index]
    if active == targets[0]:
        return targets
    return (targets[0], active) + tuple(
        target_id for target_id in targets[1:] if target_id != active
    )


class TargetSelector:
    """Filter and order targets using only immutable inputs and snapshots."""

    def select(
        self,
        catalog: Catalog,
        pool_id: str,
        state: RuntimeState,
        requirements: SelectionRequirements,
        *,
        now: datetime,
        credential_available: CredentialAvailability,
        excluded_target_ids: frozenset[str] = frozenset(),
    ) -> SelectionResult:
        if now.tzinfo is None:
            raise ManagerError(
                "invalid_selection_requirements",
                "Selection time must include a timezone.",
            )
        selected_now = now.astimezone(UTC)
        pool = catalog.pool(pool_id)
        pool_state = state.pools.get(pool.id, PoolRuntimeState.empty())
        order = _candidate_order(pool, pool_state, selected_now)
        required = pool.required_capabilities | requirements.required_capabilities
        rejections_by_id: dict[str, TargetRejection] = {}
        eligible: set[str] = set()

        for target_id in order:
            target = catalog.target(target_id)
            provider = catalog.provider(target.provider_id)
            reasons: list[str] = []
            if not pool.enabled:
                reasons.append("pool_disabled")
            if requirements.host not in pool.host_compatibility:
                reasons.append("pool_host_incompatible")
            if target_id in excluded_target_ids:
                reasons.append("manually_skipped")
            if not target.enabled:
                reasons.append("target_disabled")
            if not provider.enabled:
                reasons.append("provider_disabled")
            if requirements.host not in target.host_compatibility:
                reasons.append("host_incompatible")
            if not required.issubset(target.capabilities):
                reasons.append("capability_missing")
            if (
                requirements.context_tokens is not None
                and target.context_window is not None
                and requirements.context_tokens > target.context_window
            ):
                reasons.append("context_exceeded")
            if requirements.required_trust == "high" and target.trust != "high":
                reasons.append("trust_too_low")

            if target.credential_id is not None:
                credential = catalog.credential(
                    target.credential_id,
                    provider_id=target.provider_id,
                )
                if not credential.enabled:
                    reasons.append("credential_disabled")
                elif not credential_available(credential):
                    reasons.append("credential_unavailable")

            health = pool_state.targets.get(target_id)
            if health is not None and health.status == "cooldown":
                retry_at = _timestamp(health.retry_at)
                if health.retry_at is None or retry_at is None or selected_now < retry_at:
                    reasons.append("cooldown")

            if reasons:
                rejections_by_id[target_id] = TargetRejection(
                    target_id=target_id,
                    reasons=tuple(dict.fromkeys(reasons)),
                )
            else:
                eligible.add(target_id)

        selected = next((target_id for target_id in order if target_id in eligible), None)
        rejections = tuple(
            rejections_by_id[target_id]
            for target_id in order
            if target_id in rejections_by_id
        )
        return SelectionResult(
            selected_target_id=selected,
            generation=state.generation,
            rejections=rejections,
        )


__all__ = [
    "CredentialAvailability",
    "SelectionRequirements",
    "SelectionResult",
    "TargetRejection",
    "TargetSelector",
]
