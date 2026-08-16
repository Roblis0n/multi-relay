"""Atomic multi-file transactions with recoverable byte-for-byte rollback."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Mapping

from .errors import ManagerError

fcntl: Any = None
try:
    import fcntl as _fcntl
except ImportError:  # Windows
    pass
else:
    fcntl = _fcntl

msvcrt: Any = None
try:
    import msvcrt as _msvcrt
except ImportError:  # macOS
    pass
else:
    msvcrt = _msvcrt


@dataclass(frozen=True)
class InstallPlan:
    """A recoverable mutation set with best-effort content preconditions."""

    files: dict[Path, bytes]
    removals: tuple[Path, ...]
    manifest: dict[str, Any] | None
    backup_dir: Path
    preconditions: dict[Path, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ChangePlan:
    """Host-neutral validated changes that can be merged before one commit."""

    files: Mapping[Path, bytes] = field(default_factory=dict)
    removals: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def combine(cls, plans: Iterable["ChangePlan"]) -> "ChangePlan":
        files: dict[Path, bytes] = {}
        removals: list[Path] = []
        warnings: list[str] = []
        details: dict[str, Any] = {}
        for plan in plans:
            overlap = set(files).intersection(plan.files)
            conflicting = [
                path for path in overlap if files[path] != plan.files[path]
            ]
            if conflicting or set(plan.files).intersection(removals) or set(plan.removals).intersection(files):
                raise ManagerError(
                    "invalid_plan",
                    "Combined change plans contain conflicting host writes.",
                    {"paths": [str(path) for path in sorted(conflicting, key=str)]},
                )
            files.update(plan.files)
            removals.extend(plan.removals)
            warnings.extend(plan.warnings)
            details.update(plan.details)
        return cls(
            files=files,
            removals=tuple(dict.fromkeys(removals)),
            warnings=tuple(warnings),
            details=details,
        )

    def to_install_plan(
        self,
        *,
        manifest: dict[str, Any] | None,
        backup_dir: Path,
        preconditions: Mapping[Path, str] | None = None,
    ) -> InstallPlan:
        return InstallPlan(
            files=dict(self.files),
            removals=self.removals,
            manifest=manifest,
            backup_dir=backup_dir,
            preconditions=dict(preconditions or {}),
        )


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    existed: bool
    data: bytes
    mode: int | None


@dataclass(frozen=True)
class TransactionResult:
    snapshots: tuple[FileSnapshot, ...]
    backup_dir: Path


def transaction_target_paths(
    files: Mapping[Path, bytes],
    removals: Iterable[Path],
    manifest_path: Path,
) -> tuple[Path, ...]:
    """Return the canonical target order used by snapshots and backup filenames."""

    return tuple(
        sorted(
            set(files).union(removals).union({manifest_path}),
            key=str,
        )
    )


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write bytes through a same-directory temporary file and atomic replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _snapshot(path: Path) -> FileSnapshot:
    if not path.exists():
        return FileSnapshot(path=path, existed=False, data=b"", mode=None)
    if not path.is_file():
        raise ManagerError("unsafe_target", f"Transaction target is not a file: {path}")
    stat = path.stat()
    return FileSnapshot(
        path=path,
        existed=True,
        data=path.read_bytes(),
        mode=stat.st_mode & 0o777,
    )


def _write_backup(backup_dir: Path, snapshots: tuple[FileSnapshot, ...]) -> None:
    backup_dir.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    for index, snapshot in enumerate(snapshots):
        backup_file: str | None = None
        checksum: str | None = None
        if snapshot.existed:
            backup_file = f"{index:04d}.bin"
            checksum = hashlib.sha256(snapshot.data).hexdigest()
            atomic_write(backup_dir / backup_file, snapshot.data)
        records.append(
            {
                "path": str(snapshot.path),
                "existed": snapshot.existed,
                "mode": snapshot.mode,
                "backup_file": backup_file,
                "sha256": checksum,
            }
        )
    atomic_write(
        backup_dir / "snapshot.json",
        (json.dumps({"files": records}, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _restore(snapshots: tuple[FileSnapshot, ...]) -> None:
    failures: list[str] = []
    for snapshot in reversed(snapshots):
        try:
            if snapshot.existed:
                atomic_write(snapshot.path, snapshot.data, snapshot.mode or 0o600)
                if snapshot.mode is not None:
                    os.chmod(snapshot.path, snapshot.mode)
            elif snapshot.path.exists():
                if not snapshot.path.is_file():
                    raise OSError("rollback target changed into a directory")
                snapshot.path.unlink()
        except OSError:
            failures.append(str(snapshot.path))
    if failures:
        raise ManagerError(
            "rollback_failed",
            "Rollback could not restore every managed file.",
            {"paths": failures},
        )


def execute_install_plan(
    plan: InstallPlan,
    manifest_path: Path,
    *,
    writer: Callable[[Path, bytes, int], None] = atomic_write,
) -> TransactionResult:
    """Apply candidate files and manifest or restore the exact pre-state."""

    if manifest_path in plan.files or manifest_path in plan.removals:
        raise ManagerError("invalid_plan", "The manifest is managed separately from plan targets.")
    targets = transaction_target_paths(plan.files, plan.removals, manifest_path)
    unknown_preconditions = set(plan.preconditions) - set(targets)
    if unknown_preconditions:
        raise ManagerError(
            "invalid_plan",
            "Transaction preconditions must reference transaction targets.",
            {"paths": [str(path) for path in sorted(unknown_preconditions, key=str)]},
        )
    snapshots = tuple(_snapshot(path) for path in targets)
    try:
        _write_backup(plan.backup_dir, snapshots)
    except Exception:
        raise ManagerError(
            "backup_failed",
            "Could not create the pre-install backup.",
        ) from None

    for path, expected in sorted(plan.preconditions.items(), key=lambda item: str(item[0])):
        try:
            current = path.read_bytes()
        except OSError:
            current = None
        if (
            not isinstance(expected, str)
            or current is None
            or hashlib.sha256(current).hexdigest() != expected
        ):
            raise ManagerError(
                "transaction_precondition_failed",
                "A managed file changed after backup and before replacement; no install writes were made.",
                {"path": str(path), "backup": str(plan.backup_dir)},
            )

    try:
        for path in sorted(plan.removals, key=str):
            if path.exists():
                if not path.is_file():
                    raise OSError("removal target is not a file")
                path.unlink()
        for path, data in sorted(plan.files.items(), key=lambda item: str(item[0])):
            writer(path, data, 0o600)
            if path.read_bytes() != data:
                raise OSError("post-write verification failed")
        if plan.manifest is None:
            if manifest_path.exists():
                if not manifest_path.is_file():
                    raise OSError("manifest target is not a file")
                manifest_path.unlink()
        else:
            manifest_payload = dict(plan.manifest)
            manifest_payload["backup"] = str(plan.backup_dir)
            manifest_payload["transaction_targets"] = [str(path) for path in targets]
            manifest_bytes = (
                json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            writer(manifest_path, manifest_bytes, 0o600)
            if manifest_path.read_bytes() != manifest_bytes:
                raise OSError("manifest verification failed")
    except Exception:
        try:
            _restore(snapshots)
        except ManagerError:
            raise
        raise ManagerError(
            "transaction_failed",
            "Managed files could not be installed; the previous state was restored.",
            {"backup": str(plan.backup_dir)},
        ) from None
    return TransactionResult(snapshots=snapshots, backup_dir=plan.backup_dir)


def rollback_transaction(result: TransactionResult) -> None:
    """Restore a completed transaction, including a newly written manifest."""

    _restore(result.snapshots)


def _try_lock(handle: BinaryIO) -> bool:
    if os.name == "nt":
        if msvcrt is None:
            return False
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    if fcntl is None:
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def operation_lock(lock_path: Path, timeout_seconds: float = 5.0) -> Iterator[None]:
    """Serialize manager mutations across processes."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout_seconds
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise ManagerError(
                    "operation_in_progress",
                "Another Multi Relay configuration operation is in progress.",
                )
            time.sleep(0.02)
        try:
            yield
        finally:
            _unlock(handle)
