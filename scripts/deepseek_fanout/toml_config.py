"""Safe, ownership-scoped editing of user-level Codex TOML."""

from __future__ import annotations

import json
import re
import tomllib
from typing import Any

from .bridge import BRIDGE_BASE_URL
from .errors import ManagerError


PROVIDER_BEGIN = "# BEGIN CODEX-DEEPSEEK-FANOUT PROVIDER"
PROVIDER_END = "# END CODEX-DEEPSEEK-FANOUT PROVIDER"
_PARENT_KEYS = ("model", "model_provider", "model_reasoning_effort")
_V2_KEYS = (
    "enabled",
    "hide_spawn_agent_metadata",
    "tool_namespace",
    "max_concurrent_threads_per_session",
)
_HEADER_RE = re.compile(r"^\s*\[\[?.+?\]\]?\s*(?:#.*)?$")


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    raise ManagerError("invalid_manifest", "Stored configuration values have an invalid type.")


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _parse(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManagerError("invalid_config", "Codex config.toml is not valid TOML.") from exc


def _table_pattern(table: str) -> re.Pattern[str]:
    escaped = re.escape(table)
    return re.compile(
        rf"^\s*\[\s*(?:{escaped}|\"{escaped}\"|'{escaped}')\s*\]\s*(?:#.*)?$"
    )


def _table_range(lines: list[str], table: str) -> tuple[int, int] | None:
    pattern = _table_pattern(table)
    start = next((index for index, line in enumerate(lines) if pattern.match(line)), None)
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _HEADER_RE.match(lines[index]):
            end = index
            break
    return start, end


def _set_table_value(text: str, table: str, key: str, rendered: str) -> str:
    lines = _normalize(text).splitlines()
    table_range = _table_range(lines, table)
    if table_range is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.extend((f"[{table}]", f"{key} = {rendered}"))
        return "\n".join(lines) + "\n"

    start, end = table_range
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(start + 1, end):
        if key_pattern.match(lines[index]):
            lines[index] = f"{key} = {rendered}"
            return "\n".join(lines) + "\n"

    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def _remove_table_key(text: str, table: str, key: str) -> str:
    lines = _normalize(text).splitlines()
    table_range = _table_range(lines, table)
    if table_range is None:
        return _normalize(text)
    start, end = table_range
    key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    lines = [
        line
        for index, line in enumerate(lines)
        if not (start < index < end and key_pattern.match(line))
    ]
    return "\n".join(lines).rstrip() + "\n"


def _remove_empty_table(text: str, table: str) -> str:
    lines = _normalize(text).splitlines()
    table_range = _table_range(lines, table)
    if table_range is None:
        return _normalize(text)
    start, end = table_range
    meaningful = [
        line for line in lines[start + 1 : end] if line.strip() and not line.lstrip().startswith("#")
    ]
    if meaningful:
        return _normalize(text)
    del lines[start:end]
    while len(lines) >= 2 and not lines[-1].strip() and not lines[-2].strip():
        lines.pop()
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def _remove_provider_block(text: str) -> str:
    normalized = _normalize(text)
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(PROVIDER_BEGIN)}\s*\n.*?^\s*{re.escape(PROVIDER_END)}\s*(?:\n|$)"
    )
    return pattern.sub("", normalized).rstrip() + ("\n" if normalized.strip() else "")


def build_provider_block(auth_command: list[str]) -> str:
    """Render the command-authenticated DeepSeek Responses provider."""

    if not auth_command or not all(isinstance(part, str) and part for part in auth_command):
        raise ManagerError("invalid_auth_command", "Provider auth command is incomplete.")
    command, *args = auth_command
    return (
        f"{PROVIDER_BEGIN}\n"
        "[model_providers.deepseek]\n"
        'name = "DeepSeek"\n'
        f"base_url = {_toml_string(BRIDGE_BASE_URL)}\n"
        'wire_api = "responses"\n\n'
        "[model_providers.deepseek.auth]\n"
        f"command = {_toml_string(command)}\n"
        f"args = {_toml_array(args)}\n"
        "timeout_ms = 5000\n"
        "refresh_interval_ms = 0\n"
        f"{PROVIDER_END}\n"
    )


def validate_parent_unchanged(before: str, after: str) -> None:
    """Reject a candidate that alters the parent model selection."""

    before_data = _parse(_normalize(before))
    after_data = _parse(_normalize(after))
    missing = object()
    changed = [
        key
        for key in _PARENT_KEYS
        if before_data.get(key, missing) != after_data.get(key, missing)
    ]
    if changed:
        raise ManagerError(
            "parent_changed",
            "Managed configuration attempted to change the parent model.",
            {"fields": changed},
        )


def capture_managed_values(text: str) -> dict[str, Any]:
    """Capture values that setup is allowed to overwrite."""

    parsed = _parse(_normalize(text))
    snapshot: dict[str, Any] = {}
    for table, keys in {
        "features": ("multi_agent",),
        "agents": ("enabled", "max_concurrent_threads_per_session"),
    }.items():
        table_value = parsed.get(table)
        snapshot[table] = {
            "present": isinstance(table_value, dict),
            "keys": {
                key: {
                    "present": isinstance(table_value, dict) and key in table_value,
                    "value": table_value.get(key) if isinstance(table_value, dict) else None,
                }
                for key in keys
            },
        }
    features = parsed.get("features")
    v2 = features.get("multi_agent_v2") if isinstance(features, dict) else None
    if not isinstance(features, dict) or "multi_agent_v2" not in features:
        snapshot["features.multi_agent_v2"] = {"kind": "missing", "keys": {}}
    elif isinstance(v2, bool):
        snapshot["features.multi_agent_v2"] = {
            "kind": "scalar",
            "value": v2,
            "keys": {},
        }
    elif isinstance(v2, dict):
        snapshot["features.multi_agent_v2"] = {
            "kind": "table",
            "keys": {
                key: {
                    "present": key in v2,
                    "value": v2.get(key),
                }
                for key in _V2_KEYS
            },
        }
    else:
        raise ManagerError(
            "invalid_config",
            "features.multi_agent_v2 must be a boolean or table.",
        )
    return snapshot


def apply_codex_config(
    original: str,
    auth_command: list[str],
    concurrency: int = 8,
) -> str:
    """Build a valid candidate config while preserving the parent selection."""

    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise ManagerError("invalid_concurrency", "Concurrency must be a positive integer.")
    normalized = _normalize(original)
    parsed = _parse(normalized)
    existing_agents = parsed.get("agents")
    features = parsed.get("features")
    existing_v2 = (
        features.get("multi_agent_v2") if isinstance(features, dict) else None
    )
    existing_limit = (
        existing_agents.get("max_concurrent_threads_per_session")
        if isinstance(existing_agents, dict)
        else None
    )
    effective_limit = (
        max(concurrency, existing_limit)
        if isinstance(existing_limit, int) and not isinstance(existing_limit, bool)
        else concurrency
    )
    existing_v2_limit = (
        existing_v2.get("max_concurrent_threads_per_session")
        if isinstance(existing_v2, dict)
        else None
    )
    effective_v2_limit = (
        max(concurrency, existing_v2_limit)
        if isinstance(existing_v2_limit, int)
        and not isinstance(existing_v2_limit, bool)
        else concurrency
    )

    candidate = _remove_provider_block(normalized)
    candidate = _remove_table_key(candidate, "features", "multi_agent_v2")
    candidate = _set_table_value(candidate, "features", "multi_agent", "true")
    candidate = _set_table_value(
        candidate,
        "features.multi_agent_v2",
        "enabled",
        "true",
    )
    candidate = _set_table_value(
        candidate,
        "features.multi_agent_v2",
        "hide_spawn_agent_metadata",
        "false",
    )
    candidate = _set_table_value(
        candidate,
        "features.multi_agent_v2",
        "tool_namespace",
        _toml_string("agents"),
    )
    candidate = _set_table_value(
        candidate,
        "features.multi_agent_v2",
        "max_concurrent_threads_per_session",
        str(effective_v2_limit),
    )
    candidate = _set_table_value(candidate, "agents", "enabled", "true")
    candidate = _set_table_value(
        candidate,
        "agents",
        "max_concurrent_threads_per_session",
        str(effective_limit),
    )
    candidate = candidate.rstrip() + "\n\n" + build_provider_block(auth_command)
    _parse(candidate)
    validate_parent_unchanged(normalized, candidate)
    return candidate


def remove_codex_config(text: str, original_values: dict[str, Any]) -> str:
    """Remove the provider block and restore values captured before setup."""

    candidate = _remove_provider_block(text)
    for table in ("features", "agents"):
        table_snapshot = original_values.get(table, {})
        for key, key_snapshot in table_snapshot.get("keys", {}).items():
            if key_snapshot.get("present"):
                value = key_snapshot.get("value")
                rendered = _render_scalar(value)
                candidate = _set_table_value(candidate, table, key, rendered)
            else:
                candidate = _remove_table_key(candidate, table, key)
        if not table_snapshot.get("present"):
            candidate = _remove_empty_table(candidate, table)

    v2_snapshot = original_values.get("features.multi_agent_v2", {})
    kind = v2_snapshot.get("kind")
    if kind == "table":
        for key, key_snapshot in v2_snapshot.get("keys", {}).items():
            if key_snapshot.get("present"):
                candidate = _set_table_value(
                    candidate,
                    "features.multi_agent_v2",
                    key,
                    _render_scalar(key_snapshot.get("value")),
                )
            else:
                candidate = _remove_table_key(
                    candidate,
                    "features.multi_agent_v2",
                    key,
                )
    elif kind in {"missing", "scalar"}:
        for key in _V2_KEYS:
            candidate = _remove_table_key(candidate, "features.multi_agent_v2", key)
        candidate = _remove_empty_table(candidate, "features.multi_agent_v2")
        if _table_range(_normalize(candidate).splitlines(), "features.multi_agent_v2"):
            raise ManagerError(
                "conflict",
                "features.multi_agent_v2 gained user-owned values and cannot be restored safely.",
            )
        if kind == "scalar":
            candidate = _set_table_value(
                candidate,
                "features",
                "multi_agent_v2",
                _render_scalar(v2_snapshot.get("value")),
            )
    else:
        raise ManagerError("invalid_manifest", "Stored multi-agent V2 values are missing.")
    _parse(candidate)
    return candidate
