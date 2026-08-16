"""Ownership-checked migration from the former single-agent installation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .catalog import (
    CATALOG_SCHEMA_VERSION,
    LEGACY_CATALOG_SCHEMA_VERSION,
    Catalog,
    default_catalog,
    legacy_catalog_to_schema2_dict,
    save_catalog_bytes,
)
from .errors import ManagerError
from .paths import Paths
from .toml_config import _remove_table_key, _set_table_value
from .transaction import atomic_write


LEGACY_PROVIDER_BEGIN = "# BEGIN CODEX-DEEPSEEK-SUBAGENT PROVIDER"
LEGACY_PROVIDER_END = "# END CODEX-DEEPSEEK-SUBAGENT PROVIDER"
LEGACY_ROLE_BEGIN = "# BEGIN CODEX-DEEPSEEK-SUBAGENT ROLE"
LEGACY_ROLE_END = "# END CODEX-DEEPSEEK-SUBAGENT ROLE"


@dataclass(frozen=True)
class LegacyMigration:
    config_text: str
    files: dict[Path, bytes]
    removals: tuple[Path, ...]


@dataclass(frozen=True)
class CatalogMigrationResult:
    """Validated catalog plus the provenance of the bytes that produced it."""

    catalog: Catalog
    source_schema: int
    source_sha256: str
    changed: bool
    backup_path: Path | None


CatalogWriter = Callable[[Path, bytes, int], None]


def migrate_catalog_1_to_2(value: Mapping[str, Any] | Catalog) -> Catalog:
    """Pure, deterministic schema 1 migration; schema 2 input is idempotent."""

    if isinstance(value, Catalog):
        return Catalog.from_dict(value.to_dict())
    if not isinstance(value, Mapping):
        raise ManagerError("catalog_invalid", "Catalog must be a JSON object.")
    schema = value.get("schema_version")
    if schema == LEGACY_CATALOG_SCHEMA_VERSION and not isinstance(schema, bool):
        return Catalog.from_dict(legacy_catalog_to_schema2_dict(value))
    if schema == CATALOG_SCHEMA_VERSION and not isinstance(schema, bool):
        return Catalog.from_dict(value)
    return Catalog.from_dict(value)


def _read_catalog_candidate(path: Path) -> tuple[bytes, CatalogMigrationResult]:
    try:
        raw = path.read_bytes()
        decoded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagerError(
            "catalog_invalid",
            "Catalog JSON could not be read or decoded; fix the catalog before retrying migration.",
            {"path": str(path)},
        ) from exc
    if not isinstance(decoded, Mapping):
        raise ManagerError(
            "catalog_invalid",
            "Catalog must be a JSON object; fix the catalog before retrying migration.",
            {"path": str(path)},
        )
    schema = decoded.get("schema_version")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise ManagerError(
            "unsupported_catalog_schema",
            "Catalog schema_version must be an integer.",
            {"path": str(path), "schema_version": schema},
        )
    catalog = migrate_catalog_1_to_2(decoded)
    return raw, CatalogMigrationResult(
        catalog=catalog,
        source_schema=schema,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        changed=schema == LEGACY_CATALOG_SCHEMA_VERSION,
        backup_path=None,
    )


def inspect_catalog_migration(path: Path) -> CatalogMigrationResult:
    """Read and validate a catalog without changing it."""

    _, result = _read_catalog_candidate(path)
    return result


def migrate_catalog_file(
    path: Path,
    backup_root: Path,
    *,
    catalog_writer: CatalogWriter = atomic_write,
) -> CatalogMigrationResult:
    """Migrate one standalone catalog; manager callers use a multi-file transaction."""

    source = path.expanduser().resolve()
    backup_directory = backup_root.expanduser().resolve()
    raw, candidate = _read_catalog_candidate(source)
    try:
        source_mode = source.stat().st_mode & 0o777
    except OSError as exc:
        raise ManagerError(
            "catalog_invalid",
            "Catalog metadata could not be read; fix file access before retrying migration.",
            {"path": str(source)},
        ) from exc
    if not candidate.changed:
        return candidate

    catalog_bytes = save_catalog_bytes(candidate.catalog)
    # Validate the exact bytes before any backup or source-file write occurs.
    try:
        Catalog.from_dict(json.loads(catalog_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ManagerError) as exc:
        raise ManagerError(
            "catalog_migration_failed",
            "The migrated catalog did not pass validation; the original catalog was not changed.",
            {"path": str(source)},
        ) from exc

    backup = backup_directory / (
        f"{source.stem}.schema1.{candidate.source_sha256}.json"
    )
    if backup == source:
        raise ManagerError(
            "catalog_backup_failed",
            "The catalog backup path must differ from the source catalog.",
            {"path": str(source)},
        )
    backup_created = False
    try:
        if backup.exists():
            if not backup.is_file() or backup.read_bytes() != raw:
                raise OSError("existing migration backup does not match source")
        else:
            atomic_write(backup, raw, 0o600)
            backup_created = True
        if backup.read_bytes() != raw:
            raise OSError("migration backup verification failed")
    except Exception as exc:
        raise ManagerError(
            "catalog_backup_failed",
            "The original catalog could not be backed up; the source catalog was not changed.",
            {"path": str(source), "backup": str(backup)},
        ) from exc

    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{source.name}.migration.",
        dir=source.parent,
    )
    os.close(descriptor)
    staged = Path(staged_name)
    try:
        catalog_writer(staged, catalog_bytes, source_mode)
        if staged.read_bytes() != catalog_bytes:
            raise OSError("staged catalog verification failed")
        try:
            source_unchanged = source.read_bytes() == raw
        except OSError:
            source_unchanged = False
        if not source_unchanged:
            if backup_created:
                try:
                    backup.unlink()
                except OSError:
                    pass
            raise ManagerError(
                "catalog_changed",
                "The catalog changed while its migration candidate was being prepared; retry the migration.",
                {"path": str(source)},
            )
        os.replace(staged, source)
        os.chmod(source, source_mode)
        if source.read_bytes() != catalog_bytes:
            raise OSError("catalog post-write verification failed")
    except ManagerError as exc:
        if exc.code == "catalog_changed":
            raise
        raise ManagerError(
            "catalog_migration_failed",
            "Catalog migration could not stage its replacement; the original catalog remains unchanged.",
            {"path": str(source), "backup": str(backup)},
        ) from exc
    except Exception as exc:
        try:
            current = source.read_bytes() if source.is_file() else None
            if current == catalog_bytes:
                atomic_write(source, raw, source_mode)
                current = source.read_bytes()
            if current != raw:
                raise OSError("catalog changed outside the staged migration")
        except OSError as rollback_exc:
            raise ManagerError(
                "catalog_migration_rollback_failed",
                "Catalog migration failed and the source no longer matches the migration pre-state; recover from the verified backup without overwriting newer data.",
                {"path": str(source), "backup": str(backup)},
            ) from rollback_exc
        raise ManagerError(
            "catalog_migration_failed",
            "Catalog migration could not be written; the original catalog remains unchanged.",
            {"path": str(source), "backup": str(backup)},
        ) from exc
    finally:
        try:
            staged.unlink()
        except OSError:
            pass

    return CatalogMigrationResult(
        catalog=candidate.catalog,
        source_schema=candidate.source_schema,
        source_sha256=candidate.source_sha256,
        changed=True,
        backup_path=backup,
    )


def catalog_from_schema4(manifest: dict[str, Any]) -> Catalog:
    """Convert the former single-provider manifest into a schema 2 catalog."""

    if manifest.get("schema_version") != 4:
        raise ManagerError("invalid_manifest", "A schema-4 Relay manifest is required.")
    selection = manifest.get("selection")
    if not isinstance(selection, dict):
        raise ManagerError("invalid_manifest", "The legacy model selection is missing.")
    model = selection.get("resolved_model")
    effort = selection.get("reasoning_effort")
    if not isinstance(model, str) or not model.strip():
        raise ManagerError("invalid_manifest", "The legacy model selection is invalid.")
    if effort is not None and not isinstance(effort, str):
        raise ManagerError("invalid_manifest", "The legacy reasoning effort is invalid.")
    concurrency = manifest.get("concurrency", 8)
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ManagerError("invalid_manifest", "The legacy concurrency value is invalid.")
    payload = default_catalog("hybrid").to_dict()
    payload["concurrency"] = concurrency
    deepseek_targets: set[str] = set()
    for target in payload["targets"]:
        if isinstance(target, dict) and target.get("provider_id") == "deepseek":
            target["model"] = model
            if effort is not None:
                efforts = target.get("reasoning_efforts")
                if isinstance(efforts, list) and effort not in efforts:
                    efforts.append(effort)
            target_id = target.get("id")
            if isinstance(target_id, str):
                deepseek_targets.add(target_id)
    deepseek_pools = {
        pool.get("id")
        for pool in payload["pools"]
        if isinstance(pool, dict)
        and isinstance(pool.get("targets"), list)
        and deepseek_targets.intersection(pool["targets"])
    }
    for agent in payload["agents"]:
        if isinstance(agent, dict) and agent.get("pool_id") in deepseek_pools:
            agent["reasoning_effort"] = effort
    return Catalog.from_dict(payload)


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _remove_block(text: str, begin: str, end: str, *, owned: bool) -> str:
    has_begin = begin in text
    has_end = end in text
    if has_begin != has_end:
        raise ManagerError("conflict", "A legacy managed configuration block is incomplete.")
    if not has_begin:
        return text
    if not owned:
        raise ManagerError(
            "conflict",
            "A legacy configuration block exists without ownership evidence.",
        )
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(begin)}\s*\n.*?^\s*{re.escape(end)}\s*(?:\n|$)"
    )
    return pattern.sub("", _normalize(text)).rstrip() + "\n"


def _remove_top_level_key(text: str, key: str) -> str:
    lines = _normalize(text).splitlines()
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    output: list[str] = []
    inside_table = False
    for line in lines:
        if re.match(r"^\s*\[", line):
            inside_table = True
        if not inside_table and pattern.match(line):
            continue
        output.append(line)
    return "\n".join(output).rstrip() + "\n"


def _set_top_level_string(text: str, key: str, value: str) -> str:
    cleaned = _remove_top_level_key(text, key)
    lines = cleaned.splitlines()
    insert_at = next(
        (index for index, line in enumerate(lines) if re.match(r"^\s*\[", line)),
        len(lines),
    )
    lines.insert(insert_at, f"{key} = {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines).rstrip() + "\n"


def _matches_checksum(path: Path, expected: object) -> bool:
    if not path.is_file() or not isinstance(expected, str):
        return False
    data = path.read_bytes()
    raw = hashlib.sha256(data).hexdigest()
    try:
        normalized = hashlib.sha256(
            path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        ).hexdigest()
    except UnicodeDecodeError:
        normalized = raw
    return expected in {raw, normalized}


def _identifies_same_file(value: object, expected: Path) -> bool:
    """Compare a configured path with its managed file across aliases."""

    if not isinstance(value, str) or not value:
        return False
    try:
        candidate = Path(value).expanduser()
        if candidate.exists() and expected.exists():
            return candidate.samefile(expected)
        return candidate.resolve(strict=False) == expected.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False


def plan_legacy_migration(
    paths: Paths,
    config_text: str,
    manifest: dict[str, Any],
    *,
    state_root: Path | None = None,
) -> LegacyMigration:
    """Return candidate restoration/removal actions without changing live files."""

    schema = manifest.get("schema_version", 1)
    if not isinstance(schema, int) or schema >= 4:
        raise ManagerError("invalid_manifest", "The legacy manifest schema is invalid.")
    candidate = _remove_block(
        config_text,
        LEGACY_PROVIDER_BEGIN,
        LEGACY_PROVIDER_END,
        owned=bool(manifest.get("managed_provider_block")),
    )
    candidate = _remove_block(
        candidate,
        LEGACY_ROLE_BEGIN,
        LEGACY_ROLE_END,
        owned=bool(manifest.get("legacy_role_block_removed")),
    )
    try:
        parsed = tomllib.loads(candidate)
    except tomllib.TOMLDecodeError:
        raise ManagerError("invalid_config", "Legacy config.toml is not valid TOML.") from None

    catalog = paths.home / "models-with-deepseek.json"
    if manifest.get("managed_catalog_selection") and _identifies_same_file(
        parsed.get("model_catalog_json"),
        catalog,
    ):
        previous_catalog = manifest.get("previous_model_catalog_json")
        if previous_catalog is None:
            candidate = _remove_top_level_key(candidate, "model_catalog_json")
        elif isinstance(previous_catalog, str):
            candidate = _set_top_level_string(candidate, "model_catalog_json", previous_catalog)
        else:
            raise ManagerError("invalid_manifest", "The previous model catalog value is invalid.")

    parsed = tomllib.loads(candidate)
    features = parsed.get("features")
    current_v2 = features.get("multi_agent_v2") if isinstance(features, dict) else None
    if manifest.get("managed_multi_agent_v2") and current_v2 is False:
        previous_v2 = manifest.get("previous_multi_agent_v2")
        if previous_v2 is None:
            candidate = _remove_table_key(candidate, "features", "multi_agent_v2")
        elif isinstance(previous_v2, bool):
            candidate = _set_table_value(
                candidate,
                "features",
                "multi_agent_v2",
                "true" if previous_v2 else "false",
            )
        else:
            raise ManagerError("invalid_manifest", "The previous multi-agent value is invalid.")

    files: dict[Path, bytes] = {}
    removals: list[Path] = []
    legacy_agent = paths.agents_dir / "DeepSeek.toml"
    if legacy_agent.exists():
        if not manifest.get("managed_agent_file") or not _matches_checksum(
            legacy_agent, manifest.get("agent_sha256")
        ):
            raise ManagerError(
                "conflict",
                "The legacy DeepSeek role changed after installation and was not removed.",
                {"path": str(legacy_agent)},
            )
        removals.append(legacy_agent)

    if catalog.exists():
        if manifest.get("catalog_preexisted"):
            backup_value = manifest.get("catalog_original_backup")
            if not isinstance(backup_value, str):
                raise ManagerError("invalid_manifest", "The original catalog backup is missing.")
            backup = Path(backup_value).expanduser().resolve()
            trusted_state_root = (state_root or paths.state_dir).resolve()
            try:
                backup.relative_to(trusted_state_root)
            except ValueError:
                raise ManagerError("unsafe_backup", "The legacy catalog backup is outside managed state.") from None
            if not backup.is_file():
                raise ManagerError("backup_missing", "The legacy catalog backup is missing.")
            files[catalog] = backup.read_bytes()
        else:
            if not _matches_checksum(catalog, manifest.get("catalog_sha256")):
                raise ManagerError(
                    "conflict",
                    "The legacy model catalog changed after installation and was not removed.",
                    {"path": str(catalog)},
                )
            removals.append(catalog)

    try:
        tomllib.loads(candidate)
    except tomllib.TOMLDecodeError:
        raise ManagerError("invalid_config", "Migrated config.toml is not valid TOML.") from None
    return LegacyMigration(
        config_text=candidate,
        files=files,
        removals=tuple(removals),
    )
