"""Secret-free, atomically persisted runtime selection state."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Collection, Mapping

from .catalog import _assert_secret_free
from .errors import ManagerError
from .transaction import atomic_write, operation_lock


RUNTIME_STATE_SCHEMA_VERSION = 1
MAX_RUNTIME_STATE_BYTES = 4 * 1024 * 1024

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_STATE_FIELDS = frozenset({"schema_version", "catalog_hash", "generation", "pools"})
_POOL_FIELDS = frozenset(
    {"active_target_id", "selected_at", "hold_until", "targets"}
)
_TARGET_FIELDS = frozenset(
    {"status", "reason", "retry_at", "failure_count"}
)

StateWriter = Callable[[Path, bytes, int], None]


def _invalid(message: str, *, field: str | None = None) -> ManagerError:
    details = {"field": field} if field is not None else None
    return ManagerError("runtime_state_invalid", message, details)


def _strict_mapping(
    value: object,
    fields: frozenset[str],
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid(f"{label} must be a JSON object.")
    actual = set(value)
    missing = sorted(fields - actual)
    unknown = sorted(actual - fields)
    if missing:
        raise _invalid(f"{label} is missing required fields: {', '.join(missing)}.")
    if unknown:
        raise _invalid(f"{label} contains unsupported fields: {', '.join(unknown)}.")
    return value


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise _invalid(
            f"{field} must use lowercase ASCII letters, digits, underscores, or hyphens.",
            field=field,
        )
    return value


def _optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field)


def _optional_timestamp(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{field} must be a UTC timestamp or null.", field=field)
    # Timestamp semantics are validated by the rotation layer. Keeping parsing out
    # of state loading lets a damaged timestamp recover as an unavailable target.
    return value.strip()


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(f"{field} must be a non-negative integer.", field=field)
    return value


@dataclass(frozen=True)
class TargetRuntimeState:
    """Non-secret health state for one execution target."""

    status: str
    reason: str
    retry_at: str | None
    failure_count: int

    @classmethod
    def from_dict(cls, value: object) -> "TargetRuntimeState":
        data = _strict_mapping(value, _TARGET_FIELDS, "Target runtime state")
        status = data["status"]
        reason = data["reason"]
        if status != "cooldown":
            raise _invalid(
                "Target runtime status must be cooldown.",
                field="target.status",
            )
        if not isinstance(reason, str) or not reason.strip():
            raise _invalid(
                "Target runtime reason must be a non-empty string.",
                field="target.reason",
            )
        return cls(
            status=status,
            reason=reason.strip(),
            retry_at=_optional_timestamp(data["retry_at"], "target.retry_at"),
            failure_count=_nonnegative_integer(
                data["failure_count"],
                "target.failure_count",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        if self.status != "cooldown":
            raise _invalid(
                "Target runtime status must be cooldown.",
                field="target.status",
            )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise _invalid(
                "Target runtime reason must be a non-empty string.",
                field="target.reason",
            )
        return {
            "status": self.status,
            "reason": self.reason.strip(),
            "retry_at": _optional_timestamp(self.retry_at, "target.retry_at"),
            "failure_count": _nonnegative_integer(
                self.failure_count,
                "target.failure_count",
            ),
        }


@dataclass(frozen=True)
class PoolRuntimeState:
    """Current target, policy timing, and target health for one pool."""

    active_target_id: str | None
    selected_at: str | None
    hold_until: str | None
    targets: Mapping[str, TargetRuntimeState]

    def __post_init__(self) -> None:
        object.__setattr__(self, "targets", MappingProxyType(dict(self.targets)))

    @classmethod
    def empty(cls) -> "PoolRuntimeState":
        return cls(
            active_target_id=None,
            selected_at=None,
            hold_until=None,
            targets={},
        )

    @classmethod
    def from_dict(cls, value: object) -> "PoolRuntimeState":
        data = _strict_mapping(value, _POOL_FIELDS, "Pool runtime state")
        raw_targets = data["targets"]
        if not isinstance(raw_targets, Mapping):
            raise _invalid("Pool targets must be a JSON object.", field="pool.targets")
        targets: dict[str, TargetRuntimeState] = {}
        for raw_id, raw_state in raw_targets.items():
            target_id = _identifier(raw_id, "pool.targets")
            targets[target_id] = TargetRuntimeState.from_dict(raw_state)
        active = _optional_identifier(data["active_target_id"], "pool.active_target_id")
        selected_at = _optional_timestamp(data["selected_at"], "pool.selected_at")
        hold_until = _optional_timestamp(data["hold_until"], "pool.hold_until")
        if active is None and (selected_at is not None or hold_until is not None):
            raise _invalid(
                "Pool selection timestamps require an active target.",
                field="pool.active_target_id",
            )
        return cls(
            active_target_id=active,
            selected_at=selected_at,
            hold_until=hold_until,
            targets=targets,
        )

    def to_dict(self) -> dict[str, object]:
        active = _optional_identifier(
            self.active_target_id,
            "pool.active_target_id",
        )
        selected_at = _optional_timestamp(self.selected_at, "pool.selected_at")
        hold_until = _optional_timestamp(self.hold_until, "pool.hold_until")
        if active is None and (selected_at is not None or hold_until is not None):
            raise _invalid(
                "Pool selection timestamps require an active target.",
                field="pool.active_target_id",
            )
        return {
            "active_target_id": active,
            "selected_at": selected_at,
            "hold_until": hold_until,
            "targets": {
                _identifier(target_id, "pool.targets"): state.to_dict()
                for target_id, state in sorted(self.targets.items())
            },
        }


@dataclass(frozen=True)
class RuntimeState:
    """Versioned runtime state, separate from the user-authored catalog."""

    schema_version: int
    catalog_hash: str
    generation: int
    pools: Mapping[str, PoolRuntimeState]

    def __post_init__(self) -> None:
        object.__setattr__(self, "pools", MappingProxyType(dict(self.pools)))

    @classmethod
    def empty(cls, catalog_hash: str) -> "RuntimeState":
        return cls(
            schema_version=RUNTIME_STATE_SCHEMA_VERSION,
            catalog_hash=catalog_hash,
            generation=0,
            pools={},
        )

    @classmethod
    def from_dict(cls, value: object) -> "RuntimeState":
        data = _strict_mapping(value, _STATE_FIELDS, "Runtime state")
        schema = data["schema_version"]
        if schema != RUNTIME_STATE_SCHEMA_VERSION:
            raise ManagerError(
                "unsupported_runtime_state_schema",
                f"Runtime state schema must be {RUNTIME_STATE_SCHEMA_VERSION}.",
                {"schema_version": schema},
            )
        catalog_hash = data["catalog_hash"]
        if not isinstance(catalog_hash, str) or not catalog_hash.strip():
            raise _invalid(
                "catalog_hash must be a non-empty string.",
                field="catalog_hash",
            )
        raw_pools = data["pools"]
        if not isinstance(raw_pools, Mapping):
            raise _invalid("pools must be a JSON object.", field="pools")
        pools: dict[str, PoolRuntimeState] = {}
        for raw_id, raw_state in raw_pools.items():
            pool_id = _identifier(raw_id, "pools")
            pools[pool_id] = PoolRuntimeState.from_dict(raw_state)
        return cls(
            schema_version=RUNTIME_STATE_SCHEMA_VERSION,
            catalog_hash=catalog_hash.strip(),
            generation=_nonnegative_integer(data["generation"], "generation"),
            pools=pools,
        )

    def to_dict(self) -> dict[str, object]:
        if self.schema_version != RUNTIME_STATE_SCHEMA_VERSION:
            raise ManagerError(
                "unsupported_runtime_state_schema",
                f"Runtime state schema must be {RUNTIME_STATE_SCHEMA_VERSION}.",
                {"schema_version": self.schema_version},
            )
        if not isinstance(self.catalog_hash, str) or not self.catalog_hash.strip():
            raise _invalid(
                "catalog_hash must be a non-empty string.",
                field="catalog_hash",
            )
        payload: dict[str, object] = {
            "schema_version": RUNTIME_STATE_SCHEMA_VERSION,
            "catalog_hash": self.catalog_hash.strip(),
            "generation": _nonnegative_integer(self.generation, "generation"),
            "pools": {
                _identifier(pool_id, "pools"): state.to_dict()
                for pool_id, state in sorted(self.pools.items())
            },
        }
        _assert_secret_free(payload)
        return payload

    def reconcile(
        self,
        catalog_hash: str,
        valid_targets_by_pool: Mapping[str, Collection[str]],
    ) -> "RuntimeState":
        """Drop stale references while retaining state for still-valid targets."""

        reconciled: dict[str, PoolRuntimeState] = {}
        for raw_pool_id, raw_target_ids in valid_targets_by_pool.items():
            pool_id = _identifier(raw_pool_id, "pools")
            current = self.pools.get(pool_id)
            if current is None:
                continue
            target_ids = tuple(
                _identifier(item, "pool.targets") for item in raw_target_ids
            )
            valid = set(target_ids)
            targets = {
                target_id: current.targets[target_id]
                for target_id in target_ids
                if target_id in current.targets
            }
            active = (
                current.active_target_id
                if current.active_target_id in valid
                else None
            )
            reconciled[pool_id] = PoolRuntimeState(
                active_target_id=active,
                selected_at=current.selected_at if active is not None else None,
                hold_until=current.hold_until if active is not None else None,
                targets=targets,
            )
        if self.catalog_hash == catalog_hash and dict(self.pools) == reconciled:
            return self
        return RuntimeState(
            schema_version=RUNTIME_STATE_SCHEMA_VERSION,
            catalog_hash=catalog_hash,
            generation=self.generation + 1,
            pools=reconciled,
        )


def runtime_state_bytes(state: RuntimeState) -> bytes:
    """Return deterministic state JSON after the mandatory secret scan."""

    payload = state.to_dict()
    _assert_secret_free(payload)
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class RuntimeStateStore:
    """Atomic compare-and-swap persistence for runtime state."""

    def __init__(
        self,
        path: Path,
        *,
        lock_path: Path | None = None,
        writer: StateWriter = atomic_write,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        self.path = Path(path)
        self.lock_path = (
            Path(lock_path)
            if lock_path is not None
            else self.path.with_suffix(".lock")
        )
        self.writer = writer
        self.lock_timeout_seconds = lock_timeout_seconds

    def _load_unlocked(self, catalog_hash: str) -> RuntimeState:
        try:
            if not self.path.exists():
                return RuntimeState.empty(catalog_hash)
            if not self.path.is_file():
                raise ManagerError(
                    "runtime_state_read_failed",
                    "Runtime state path is not a file.",
                    {"path": str(self.path)},
                )
            if self.path.stat().st_size > MAX_RUNTIME_STATE_BYTES:
                return RuntimeState.empty(catalog_hash)
            raw = self.path.read_bytes()
        except ManagerError:
            raise
        except OSError as exc:
            raise ManagerError(
                "runtime_state_read_failed",
                "Runtime state could not be read.",
                {"path": str(self.path)},
            ) from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return RuntimeState.empty(catalog_hash)
        if isinstance(decoded, Mapping):
            schema = decoded.get("schema_version")
            if (
                isinstance(schema, int)
                and not isinstance(schema, bool)
                and schema > RUNTIME_STATE_SCHEMA_VERSION
            ):
                raise ManagerError(
                    "unsupported_runtime_state_schema",
                    "Runtime state was written by a newer Multi Relay version.",
                    {"schema_version": schema},
                )
        try:
            return RuntimeState.from_dict(decoded)
        except ManagerError as exc:
            if exc.code == "unsupported_runtime_state_schema":
                raise
            return RuntimeState.empty(catalog_hash)

    def load(self, catalog_hash: str) -> RuntimeState:
        """Load one complete atomic snapshot, recovering corrupt known schemas."""

        return self._load_unlocked(catalog_hash)

    def compare_and_swap(
        self,
        expected_generation: int,
        replacement: RuntimeState,
    ) -> bool:
        """Commit replacement only when the on-disk generation still matches."""

        expected = _nonnegative_integer(expected_generation, "expected_generation")
        if replacement.generation != expected + 1:
            raise _invalid(
                "CAS replacement generation must advance exactly once.",
                field="generation",
            )
        payload = runtime_state_bytes(replacement)
        with operation_lock(
            self.lock_path,
            timeout_seconds=self.lock_timeout_seconds,
        ):
            current = self._load_unlocked(replacement.catalog_hash)
            if current.generation != expected:
                return False
            try:
                self.writer(self.path, payload, 0o600)
            except Exception as exc:
                if isinstance(exc, ManagerError):
                    raise
                raise ManagerError(
                    "runtime_state_write_failed",
                    "Runtime state could not be replaced; the previous state remains active.",
                    {"path": str(self.path)},
                ) from exc
        return True

    def reset_pool(self, pool_id: str, catalog_hash: str) -> RuntimeState:
        """Remove one pool shard with a retrying generation CAS."""

        selected_pool = _identifier(pool_id, "pool_id")
        while True:
            current = self.load(catalog_hash)
            if selected_pool not in current.pools:
                return current
            pools = dict(current.pools)
            pools.pop(selected_pool)
            replacement = RuntimeState(
                schema_version=RUNTIME_STATE_SCHEMA_VERSION,
                catalog_hash=catalog_hash,
                generation=current.generation + 1,
                pools=pools,
            )
            if self.compare_and_swap(current.generation, replacement):
                return replacement


__all__ = [
    "MAX_RUNTIME_STATE_BYTES",
    "PoolRuntimeState",
    "RUNTIME_STATE_SCHEMA_VERSION",
    "RuntimeState",
    "RuntimeStateStore",
    "TargetRuntimeState",
    "runtime_state_bytes",
]
