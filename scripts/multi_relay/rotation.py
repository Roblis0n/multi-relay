"""Sticky and timed target rotation over secret-free runtime state."""

from __future__ import annotations

import hashlib
import json
import math
import random
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Callable, Protocol

from .catalog import Catalog, CredentialRef, TargetPool
from .errors import ManagerError
from .failure import FailureClass, NormalizedFailure
from .selection import (
    CredentialAvailability,
    SelectionRequirements,
    SelectionResult,
    TargetSelector,
)
from .state import (
    PoolRuntimeState,
    RUNTIME_STATE_SCHEMA_VERSION,
    RuntimeState,
    RuntimeStateStore,
    TargetRuntimeState,
)


class Clock(Protocol):
    def now_utc(self) -> datetime: ...

    def monotonic(self) -> float: ...


class RandomSource(Protocol):
    def random(self) -> float: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True)
class _MonotonicHold:
    target_id: str
    deadline: float


def catalog_fingerprint(catalog: Catalog) -> str:
    """Return a deterministic hash of the complete secret-free catalog."""

    payload = json.dumps(
        catalog.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _utc_text(value: datetime) -> str:
    selected = value.astimezone(UTC)
    timespec = "microseconds" if selected.microsecond else "seconds"
    return selected.isoformat(timespec=timespec).replace("+00:00", "Z")


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        selected = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if selected.tzinfo is None:
        return None
    return selected.astimezone(UTC)


def _always_available(credential: CredentialRef) -> bool:
    del credential
    return True


class RotationController:
    """Coordinate selection, cooldowns, policy timing, and generation CAS."""

    def __init__(
        self,
        catalog: Catalog,
        store: RuntimeStateStore,
        *,
        selector: TargetSelector | None = None,
        clock: Clock | None = None,
        random_source: RandomSource | None = None,
        credential_available: CredentialAvailability | None = None,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.selector = selector or TargetSelector()
        self.clock = clock or SystemClock()
        self.random_source = random_source or random.SystemRandom()
        self.credential_available = credential_available or _always_available
        self.catalog_hash = catalog_fingerprint(catalog)
        self._valid_targets = {
            pool.id: pool.targets for pool in self.catalog.pools
        }
        self._hold_lock = threading.Lock()
        self._monotonic_holds: dict[str, _MonotonicHold] = {}

    def _now(self) -> datetime:
        selected = self.clock.now_utc()
        if selected.tzinfo is None:
            raise ManagerError(
                "invalid_clock",
                "Clock.now_utc() must return a timezone-aware datetime.",
            )
        return selected.astimezone(UTC)

    def _load_reconciled(self) -> RuntimeState:
        while True:
            current = self.store.load(self.catalog_hash)
            reconciled = current.reconcile(
                self.catalog_hash,
                self._valid_targets,
            )
            if reconciled is current:
                return current
            if self.store.compare_and_swap(current.generation, reconciled):
                return reconciled

    def _remember_hold(
        self,
        pool_id: str,
        target_id: str,
        seconds: float,
    ) -> None:
        with self._hold_lock:
            self._monotonic_holds[pool_id] = _MonotonicHold(
                target_id=target_id,
                deadline=self.clock.monotonic() + max(0.0, seconds),
            )

    def _forget_hold(self, pool_id: str) -> None:
        with self._hold_lock:
            self._monotonic_holds.pop(pool_id, None)

    def _effective_state(
        self,
        state: RuntimeState,
        pool: TargetPool,
        now: datetime,
    ) -> tuple[RuntimeState, bool]:
        pool_state = state.pools.get(pool.id)
        if (
            pool.strategy != "timed"
            or pool_state is None
            or pool_state.active_target_id is None
        ):
            return state, False

        tick = self.clock.monotonic()
        with self._hold_lock:
            local = self._monotonic_holds.get(pool.id)
            if local is not None and local.target_id != pool_state.active_target_id:
                self._monotonic_holds.pop(pool.id, None)
                local = None
            if local is not None and tick >= local.deadline:
                self._monotonic_holds.pop(pool.id, None)
                local = None

        if local is not None:
            remaining = local.deadline - tick
            effective_pool = replace(
                pool_state,
                hold_until=_utc_text(now + timedelta(seconds=remaining)),
            )
            pools = dict(state.pools)
            pools[pool.id] = effective_pool
            return replace(state, pools=pools), True

        persisted = _timestamp(pool_state.hold_until)
        if persisted is not None and now < persisted:
            remaining = (persisted - now).total_seconds()
            self._remember_hold(pool.id, pool_state.active_target_id, remaining)
            return state, True
        if pool_state.hold_until is not None and persisted is None:
            # An invalid persisted deadline must not cause an early failback.
            duration = float(pool.duration_seconds or 0)
            self._remember_hold(pool.id, pool_state.active_target_id, duration)
            effective_pool = replace(
                pool_state,
                hold_until=_utc_text(now + timedelta(seconds=duration)),
            )
            pools = dict(state.pools)
            pools[pool.id] = effective_pool
            return replace(state, pools=pools), True
        return state, False

    def _snapshot_selection(
        self,
        state: RuntimeState,
        pool_id: str,
        requirements: SelectionRequirements,
        *,
        excluded_target_ids: frozenset[str] = frozenset(),
    ) -> tuple[SelectionResult, bool, datetime]:
        now = self._now()
        pool = self.catalog.pool(pool_id)
        effective, hold_active = self._effective_state(state, pool, now)
        result = self.selector.select(
            self.catalog,
            pool.id,
            effective,
            requirements,
            now=now,
            credential_available=self.credential_available,
            excluded_target_ids=excluded_target_ids,
        )
        return replace(result, generation=state.generation), hold_active, now

    @staticmethod
    def _pool_with_selection(
        pool_state: PoolRuntimeState,
        target_id: str | None,
        now: datetime,
        hold_until: datetime | None,
    ) -> PoolRuntimeState:
        if target_id is None:
            return PoolRuntimeState(
                active_target_id=None,
                selected_at=None,
                hold_until=None,
                targets=pool_state.targets,
            )
        return PoolRuntimeState(
            active_target_id=target_id,
            selected_at=_utc_text(now),
            hold_until=_utc_text(hold_until) if hold_until is not None else None,
            targets=pool_state.targets,
        )

    def _next_probe_delay(
        self,
        pool: TargetPool,
        pool_state: PoolRuntimeState,
        now: datetime,
    ) -> float:
        primary_health = pool_state.targets.get(pool.targets[0])
        retry_at = _timestamp(primary_health.retry_at) if primary_health else None
        if retry_at is not None and retry_at > now:
            return (retry_at - now).total_seconds()
        return float(pool.duration_seconds or 0)

    def select(
        self,
        pool_id: str,
        requirements: SelectionRequirements,
    ) -> SelectionResult:
        """Select a target and persist a policy change when one is required."""

        while True:
            current = self._load_reconciled()
            result, hold_active, now = self._snapshot_selection(
                current,
                pool_id,
                requirements,
            )
            pool = self.catalog.pool(pool_id)
            current_pool = current.pools.get(pool.id, PoolRuntimeState.empty())
            selected = result.selected_target_id
            if selected is None:
                return result

            switched = selected != current_pool.active_target_id
            refresh_fallback = (
                pool.strategy == "timed"
                and not hold_active
                and selected == current_pool.active_target_id
                and selected != pool.targets[0]
            )
            if not switched and not refresh_fallback:
                return result

            if pool.strategy == "timed":
                delay = (
                    float(pool.duration_seconds or 0)
                    if switched
                    else self._next_probe_delay(pool, current_pool, now)
                )
                hold_until = now + timedelta(seconds=delay)
            else:
                delay = 0.0
                hold_until = None
            pools = dict(current.pools)
            pools[pool.id] = self._pool_with_selection(
                current_pool,
                selected,
                now,
                hold_until,
            )
            replacement = RuntimeState(
                schema_version=RUNTIME_STATE_SCHEMA_VERSION,
                catalog_hash=self.catalog_hash,
                generation=current.generation + 1,
                pools=pools,
            )
            if self.store.compare_and_swap(current.generation, replacement):
                if pool.strategy == "timed":
                    self._remember_hold(pool.id, selected, delay)
                else:
                    self._forget_hold(pool.id)
                return replace(
                    result,
                    generation=replacement.generation,
                    changed=True,
                )

    @staticmethod
    def _cooldown_seconds(
        pool: TargetPool,
        failure: NormalizedFailure,
    ) -> int:
        kind = failure.failure_class
        if kind is FailureClass.QUOTA_EXHAUSTED:
            return pool.cooldown.quota_seconds
        if kind is FailureClass.RATE_LIMITED:
            retry_after = failure.retry.retry_after_seconds or 0.0
            return max(
                pool.cooldown.rate_limit_seconds,
                int(math.ceil(retry_after)),
            )
        if kind is FailureClass.AUTH_INVALID:
            return pool.cooldown.auth_seconds
        return pool.cooldown.provider_seconds

    @staticmethod
    def _updated_health(
        previous: TargetRuntimeState | None,
        failure: NormalizedFailure,
        retry_at: datetime,
    ) -> TargetRuntimeState:
        return TargetRuntimeState(
            status="cooldown",
            reason=failure.failure_class.value,
            retry_at=_utc_text(retry_at),
            failure_count=(previous.failure_count if previous is not None else 0) + 1,
        )

    def _apply_failure_health(
        self,
        state: RuntimeState,
        pool: TargetPool,
        target_id: str,
        failure: NormalizedFailure,
        now: datetime,
    ) -> tuple[dict[str, PoolRuntimeState], frozenset[str]]:
        pools = dict(state.pools)
        failed_target = self.catalog.target(target_id)
        affected_by_pool: dict[str, set[str]] = {pool.id: {target_id}}
        if (
            failure.retry.disable_credential
            and failed_target.credential_id is not None
        ):
            for candidate_pool in self.catalog.pools:
                for candidate_id in candidate_pool.targets:
                    candidate = self.catalog.target(candidate_id)
                    if (
                        candidate.provider_id == failed_target.provider_id
                        and candidate.credential_id == failed_target.credential_id
                    ):
                        affected_by_pool.setdefault(candidate_pool.id, set()).add(
                            candidate_id
                        )

        for affected_pool_id, target_ids in affected_by_pool.items():
            existing = pools.get(affected_pool_id, PoolRuntimeState.empty())
            target_states = dict(existing.targets)
            affected_pool = self.catalog.pool(affected_pool_id)
            retry_at = now + timedelta(
                seconds=self._cooldown_seconds(affected_pool, failure)
            )
            for affected_target_id in target_ids:
                target_states[affected_target_id] = self._updated_health(
                    target_states.get(affected_target_id),
                    failure,
                    retry_at,
                )
            pools[affected_pool_id] = replace(existing, targets=target_states)
        return pools, frozenset(affected_by_pool.get(pool.id, set()))

    def record_failure(
        self,
        pool_id: str,
        target_id: str,
        failure: NormalizedFailure,
        *,
        expected_generation: int,
        requirements: SelectionRequirements,
    ) -> SelectionResult:
        """Record one failover-safe failure and advance at most one generation."""

        if (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation < 0
        ):
            raise ManagerError(
                "invalid_generation",
                "expected_generation must be a non-negative integer.",
            )
        pool = self.catalog.pool(pool_id)
        if target_id not in pool.targets:
            raise ManagerError(
                "unknown_pool_target",
                f"Target {target_id} does not belong to pool {pool.id}.",
                {"pool": pool.id, "target": target_id},
            )
        current = self._load_reconciled()
        if current.generation != expected_generation:
            result, _, _ = self._snapshot_selection(
                current,
                pool.id,
                requirements,
            )
            return replace(result, changed=False)
        if not failure.retry.failover_allowed or failure.committed:
            result, _, _ = self._snapshot_selection(
                current,
                pool.id,
                requirements,
            )
            return replace(result, changed=False)

        now = self._now()
        pools, affected_current = self._apply_failure_health(
            current,
            pool,
            target_id,
            failure,
            now,
        )
        provisional = replace(current, pools=pools)
        result = self.selector.select(
            self.catalog,
            pool.id,
            provisional,
            requirements,
            now=now,
            credential_available=self.credential_available,
            excluded_target_ids=affected_current,
        )
        selected = result.selected_target_id
        pool_state = pools.get(pool.id, PoolRuntimeState.empty())
        active_was_affected = pool_state.active_target_id in affected_current
        delay = 0.0
        if selected is not None and selected != pool_state.active_target_id:
            if pool.strategy == "timed":
                delay = float(pool.duration_seconds or 0)
                hold_until = now + timedelta(seconds=delay)
            else:
                hold_until = None
            pools[pool.id] = self._pool_with_selection(
                pool_state,
                selected,
                now,
                hold_until,
            )
        elif selected is None and active_was_affected:
            pools[pool.id] = self._pool_with_selection(
                pool_state,
                None,
                now,
                None,
            )

        replacement = RuntimeState(
            schema_version=RUNTIME_STATE_SCHEMA_VERSION,
            catalog_hash=self.catalog_hash,
            generation=current.generation + 1,
            pools=pools,
        )
        if not self.store.compare_and_swap(current.generation, replacement):
            latest = self._load_reconciled()
            latest_result, _, _ = self._snapshot_selection(
                latest,
                pool.id,
                requirements,
            )
            return replace(latest_result, changed=False)

        if selected is not None and selected != pool_state.active_target_id:
            if pool.strategy == "timed":
                self._remember_hold(pool.id, selected, delay)
            else:
                self._forget_hold(pool.id)
        elif selected is None and active_was_affected:
            self._forget_hold(pool.id)
        return replace(
            result,
            generation=replacement.generation,
            changed=True,
        )

    def rotate_pool(
        self,
        pool_id: str,
        *,
        expected_generation: int,
        requirements: SelectionRequirements,
    ) -> SelectionResult:
        """Manually move to the next healthy candidate with one CAS."""

        pool = self.catalog.pool(pool_id)
        current = self._load_reconciled()
        if current.generation != expected_generation:
            result, _, _ = self._snapshot_selection(
                current,
                pool.id,
                requirements,
            )
            return replace(result, changed=False)
        pool_state = current.pools.get(pool.id, PoolRuntimeState.empty())
        excluded = (
            frozenset({pool_state.active_target_id})
            if pool_state.active_target_id is not None
            else frozenset()
        )
        now = self._now()
        result = self.selector.select(
            self.catalog,
            pool.id,
            current,
            requirements,
            now=now,
            credential_available=self.credential_available,
            excluded_target_ids=excluded,
        )
        selected = result.selected_target_id
        if selected is None:
            return result
        if pool.strategy == "timed":
            delay = float(pool.duration_seconds or 0)
            hold_until = now + timedelta(seconds=delay)
        else:
            delay = 0.0
            hold_until = None
        pools = dict(current.pools)
        pools[pool.id] = self._pool_with_selection(
            pool_state,
            selected,
            now,
            hold_until,
        )
        replacement = RuntimeState(
            schema_version=RUNTIME_STATE_SCHEMA_VERSION,
            catalog_hash=self.catalog_hash,
            generation=current.generation + 1,
            pools=pools,
        )
        if not self.store.compare_and_swap(current.generation, replacement):
            latest = self._load_reconciled()
            latest_result, _, _ = self._snapshot_selection(
                latest,
                pool.id,
                requirements,
            )
            return replace(latest_result, changed=False)
        if pool.strategy == "timed":
            self._remember_hold(pool.id, selected, delay)
        else:
            self._forget_hold(pool.id)
        return replace(
            result,
            generation=replacement.generation,
            changed=True,
        )

    def reset_pool(self, pool_id: str) -> RuntimeState:
        """Clear sticky/timed choice and health for one pool."""

        self._load_reconciled()
        reset = self.store.reset_pool(pool_id, self.catalog_hash)
        self._forget_hold(pool_id)
        return reset

    def jittered_delay(
        self,
        base_seconds: float,
        *,
        jitter_ratio: float = 0.2,
    ) -> float:
        """Return an injectable deterministic retry delay for later attempt loops."""

        if not math.isfinite(base_seconds) or base_seconds < 0:
            raise ManagerError("invalid_retry_delay", "Retry delay must be finite and non-negative.")
        if not math.isfinite(jitter_ratio) or not 0 <= jitter_ratio <= 1:
            raise ManagerError("invalid_retry_delay", "Jitter ratio must be between zero and one.")
        sample = self.random_source.random()
        if not math.isfinite(sample) or not 0 <= sample <= 1:
            raise ManagerError("invalid_random_source", "Random source must return a value from zero to one.")
        return base_seconds * (1 - jitter_ratio + (2 * jitter_ratio * sample))


__all__ = [
    "Clock",
    "RandomSource",
    "RotationController",
    "SystemClock",
    "catalog_fingerprint",
]
