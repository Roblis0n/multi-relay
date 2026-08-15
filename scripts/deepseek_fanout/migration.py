"""Ownership-checked migration from the former single-agent installation."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ManagerError
from .paths import Paths
from .toml_config import _remove_table_key, _set_table_value


LEGACY_PROVIDER_BEGIN = "# BEGIN CODEX-DEEPSEEK-SUBAGENT PROVIDER"
LEGACY_PROVIDER_END = "# END CODEX-DEEPSEEK-SUBAGENT PROVIDER"
LEGACY_ROLE_BEGIN = "# BEGIN CODEX-DEEPSEEK-SUBAGENT ROLE"
LEGACY_ROLE_END = "# END CODEX-DEEPSEEK-SUBAGENT ROLE"


@dataclass(frozen=True)
class LegacyMigration:
    config_text: str
    files: dict[Path, bytes]
    removals: tuple[Path, ...]


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
    if manifest.get("managed_catalog_selection") and parsed.get("model_catalog_json") == str(catalog):
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
