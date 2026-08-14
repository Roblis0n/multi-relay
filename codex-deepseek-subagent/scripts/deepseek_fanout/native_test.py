"""Authoritative post-install acceptance for native Codex child fan-out."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import tomllib
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compatibility import CompatibilityReport
from .errors import ManagerError
from .model_capabilities import ModelSelection
from .paths import Paths, resolve_paths


_TOKENS = (
    "DEEPSEEK_SINGLE_OK",
    "DEEPSEEK_DEFAULT_OK",
    "DEEPSEEK_WORKER_OK",
    "DEEPSEEK_EXPLORER_OK",
    "DEEPSEEK_RESUME_OK",
)
_MAX_ROLLOUT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class NativeEvidence:
    session_id: str
    child_thread_ids: tuple[str, ...]
    fanout_child_ids: tuple[str, ...]
    role_names: tuple[str, ...]
    tokens: tuple[str, ...]
    metadata: tuple[dict[str, Any], ...]
    child_messages: tuple[tuple[str, tuple[str, ...]], ...]
    tool_child_ids: tuple[str, ...]
    resumed_child_ids: tuple[str, ...]
    parent_metadata: dict[str, Any]


def _parent_expected(config_path: Path) -> dict[str, Any]:
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ManagerError("invalid_config", "Codex config.toml could not be parsed.") from None
    model = config.get("model")
    if not isinstance(model, str) or not model:
        raise ManagerError("parent_model_unconfigured", "The parent model is not configured.")
    return {
        "model_provider": config.get("model_provider", "openai"),
        "model": model,
        "reasoning_effort": config.get("model_reasoning_effort"),
    }


def _prompt() -> str:
    return (
        "Run this native subagent acceptance exactly. For every spawn, use explicit "
        'agent_type="default", agent_type="worker", or agent_type="explorer" as requested '
        'and fork_turns="none"; never fork_turns=all. Put the complete task in the message. '
        "Before each spawn or follow-up, emit the required commentary block "
        '"[DeepSeek task: <target>]" with the exact complete message and its matching '
        '"[/DeepSeek task: <target>]" closing line. '
        "Spawn one default child, require "
        "DEEPSEEK_SINGLE_OK, and wait. Next spawn three children before any wait: default must "
        "return DEEPSEEK_DEFAULT_OK, worker must return DEEPSEEK_WORKER_OK, and explorer must "
        "use one read tool before returning DEEPSEEK_EXPLORER_OK. Wait for all three. Send a "
        "follow-up task to the first child requiring DEEPSEEK_RESUME_OK and wait for it."
    )


def _parse_events(stdout: str) -> tuple[
    str,
    list[str],
    dict[str, set[str]],
    set[str],
    set[str],
]:
    session_id = ""
    child_ids: list[str] = []
    messages: dict[str, set[str]] = {}
    event_tool_threads: set[str] = set()
    resumed: set[str] = set()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            session_id = event["thread_id"]
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"command_execution", "mcp_tool_call", "tool_call"}:
            thread_id = item.get("thread_id")
            if isinstance(thread_id, str):
                event_tool_threads.add(thread_id)
        if item.get("type") != "collab_tool_call":
            continue
        tool = item.get("tool")
        receivers = tuple(
            receiver
            for receiver in item.get("receiver_thread_ids", [])
            if isinstance(receiver, str)
        )
        if tool == "spawn_agent":
            child_ids.extend(receivers)
        elif tool in {"send_input", "send_message", "followup_task", "resume_agent"}:
            resumed.update(receivers)
        elif tool in {"wait", "wait_agent"}:
            states = item.get("agents_states")
            if not isinstance(states, dict):
                continue
            for child_id, state in states.items():
                if not isinstance(child_id, str) or not isinstance(state, dict):
                    continue
                message = state.get("message")
                if state.get("status") == "completed" and isinstance(message, str):
                    messages.setdefault(child_id, set()).add(message.strip())
    return session_id, child_ids, messages, event_tool_threads, resumed


def _query_metadata(paths: Paths) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for database in sorted(paths.home.glob("state_*.sqlite")):
        try:
            with closing(sqlite3.connect(database, timeout=0.1)) as connection:
                table_info = connection.execute("PRAGMA table_info(threads)").fetchall()
                column_indexes = {row[1]: index for index, row in enumerate(table_info)}
                required = {
                    "id",
                    "model_provider",
                    "model",
                    "reasoning_effort",
                    "agent_role",
                }
                if not required.issubset(column_indexes):
                    continue
                rows = connection.execute("SELECT * FROM threads").fetchall()
        except (OSError, sqlite3.Error):
            continue

        def value(row: tuple[Any, ...], name: str) -> Any:
            index = column_indexes.get(name)
            return row[index] if index is not None else None

        for row in rows:
            thread_id = value(row, "id")
            output[str(thread_id)] = {
                "model_provider": value(row, "model_provider"),
                "model": value(row, "model"),
                "reasoning_effort": value(row, "reasoning_effort"),
                "agent_role": value(row, "agent_role"),
                "rollout_path": value(row, "rollout_path"),
                "source": value(row, "source"),
                "created_at_ms": value(row, "created_at_ms"),
            }
    return output


def _source_parent_id(value: object) -> str | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    subagent = value.get("subagent")
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    parent_id = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    return parent_id if isinstance(parent_id, str) else None


def _linked_children(
    metadata: dict[str, dict[str, Any]],
    parent_thread_id: str,
) -> list[str]:
    children = [
        (thread_id, row)
        for thread_id, row in metadata.items()
        if _source_parent_id(row.get("source")) == parent_thread_id
    ]
    children.sort(
        key=lambda item: (
            item[1].get("created_at_ms")
            if isinstance(item[1].get("created_at_ms"), int)
            else 2**63 - 1,
            item[0],
        )
    )
    return [thread_id for thread_id, _ in children]


def _wait_for_metadata(
    paths: Paths,
    thread_ids: tuple[str, ...],
    *,
    parent_thread_id: str = "",
    expected_children: int = 0,
    timeout_seconds: float = 5.0,
) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        metadata = _query_metadata(paths)
        linked_count = (
            len(_linked_children(metadata, parent_thread_id))
            if parent_thread_id and expected_children
            else 0
        )
        if all(thread_id in metadata for thread_id in thread_ids) and (
            not expected_children or linked_count >= expected_children or bool(thread_ids[1:])
        ):
            return metadata
        if time.monotonic() >= deadline:
            return metadata
        time.sleep(0.1)


def _rollout_payloads(home: Path, value: object) -> list[dict[str, Any]]:
    if not isinstance(value, str) or not value:
        return []
    raw_path = value[4:] if value.startswith("\\\\?\\") else value
    try:
        path = Path(raw_path).resolve()
        path.relative_to(home.resolve())
    except (OSError, ValueError):
        return []
    if not path.is_file() or path.stat().st_size > _MAX_ROLLOUT_BYTES:
        return []
    payloads: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") if isinstance(event, dict) else None
                if isinstance(payload, dict):
                    payloads.append(payload)
    except OSError:
        return []
    return payloads


_COLLABORATION_TOOLS = {
    "spawn_agent",
    "wait",
    "wait_agent",
    "send_input",
    "send_message",
    "followup_task",
    "resume_agent",
    "close_agent",
    "list_agents",
}


def _rollout_has_tool(payloads: list[dict[str, Any]]) -> bool:
    return any(
        payload.get("type") in {"custom_tool_call", "function_call", "tool_call"}
        and isinstance(payload.get("name"), str)
        and payload.get("name") not in _COLLABORATION_TOOLS
        for payload in payloads
    )


def _rollout_messages(payloads: list[dict[str, Any]]) -> set[str]:
    return {
        message
        for payload in payloads
        if payload.get("type") == "agent_message"
        and isinstance((message := payload.get("message")), str)
    }


def _rollout_was_resumed(payloads: list[dict[str, Any]]) -> bool:
    return sum(payload.get("type") == "task_started" for payload in payloads) >= 2


def _parent_has_parallel_fanout(payloads: list[dict[str, Any]]) -> bool:
    calls = [
        str(payload.get("name"))
        for payload in payloads
        if payload.get("type") in {"custom_tool_call", "function_call", "tool_call"}
        and isinstance(payload.get("name"), str)
    ]
    spawn_positions = [index for index, name in enumerate(calls) if name == "spawn_agent"]
    wait_positions = [index for index, name in enumerate(calls) if name in {"wait", "wait_agent"}]
    if len(spawn_positions) != 4 or len(wait_positions) < 2:
        return False
    fanout_positions = spawn_positions[1:]
    return (
        spawn_positions[0] < wait_positions[0] < fanout_positions[0]
        and fanout_positions == list(range(fanout_positions[0], fanout_positions[0] + 3))
        and any(position > fanout_positions[-1] for position in wait_positions)
    )


def run_native_acceptance(
    codex_bin: str,
    paths: Paths,
    selection: ModelSelection,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> NativeEvidence:
    """Run one formal-home native acceptance session and collect authoritative evidence."""

    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(paths.home)
    command = [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--json",
        "-s",
        "read-only",
        "-C",
        str(paths.home),
        _prompt(),
    ]
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=420,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ManagerError(
            "native_test_failed",
            "The native Codex acceptance process could not run.",
        ) from None
    if getattr(completed, "returncode", 1) != 0:
        raise ManagerError(
            "native_test_failed",
            "The native Codex acceptance process rejected the installed configuration.",
            {"returncode": int(getattr(completed, "returncode", 1))},
        )
    session_id, event_child_ids, messages, event_tools, resumed = _parse_events(
        str(getattr(completed, "stdout", ""))
    )
    all_thread_ids = tuple(([session_id] if session_id else []) + event_child_ids)
    metadata_by_id = _wait_for_metadata(
        paths,
        all_thread_ids,
        parent_thread_id=session_id,
        expected_children=4,
    )
    linked_child_ids = _linked_children(metadata_by_id, session_id) if session_id else []
    child_ids = linked_child_ids if linked_child_ids else event_child_ids
    child_metadata = tuple(metadata_by_id.get(child_id, {}) for child_id in child_ids)
    tool_threads = set(event_tools)
    for child_id, metadata in zip(child_ids, child_metadata):
        payloads = _rollout_payloads(paths.home, metadata.get("rollout_path"))
        messages.setdefault(child_id, set()).update(_rollout_messages(payloads))
        if _rollout_has_tool(payloads):
            tool_threads.add(child_id)
        if _rollout_was_resumed(payloads):
            resumed.add(child_id)
    if linked_child_ids:
        ordered_tokens = (
            "DEEPSEEK_SINGLE_OK",
            "DEEPSEEK_DEFAULT_OK",
            "DEEPSEEK_WORKER_OK",
            "DEEPSEEK_EXPLORER_OK",
        )
        ordered_ids: list[str] = []
        for token in ordered_tokens:
            matches = [
                child_id
                for child_id in child_ids
                if token in messages.get(child_id, set())
            ]
            if len(matches) != 1 or matches[0] in ordered_ids:
                ordered_ids = []
                break
            ordered_ids.append(matches[0])
        if len(ordered_ids) == 4:
            child_ids = ordered_ids
            child_metadata = tuple(metadata_by_id.get(child_id, {}) for child_id in child_ids)
    parent_payloads = _rollout_payloads(
        paths.home,
        metadata_by_id.get(session_id, {}).get("rollout_path"),
    )
    authoritative_fanout = bool(parent_payloads and linked_child_ids)
    fanout_child_ids = (
        child_ids[1:]
        if not authoritative_fanout or _parent_has_parallel_fanout(parent_payloads)
        else []
    )
    tokens = tuple(
        token
        for token in _TOKENS
        if any(token in child_messages for child_messages in messages.values())
    )
    return NativeEvidence(
        session_id=session_id,
        child_thread_ids=tuple(child_ids),
        fanout_child_ids=tuple(fanout_child_ids),
        role_names=tuple(str(item.get("agent_role", "")) for item in child_metadata),
        tokens=tokens,
        metadata=child_metadata,
        child_messages=tuple(
            (child_id, tuple(sorted(messages.get(child_id, set())))) for child_id in child_ids
        ),
        tool_child_ids=tuple(child_id for child_id in child_ids if child_id in tool_threads),
        resumed_child_ids=tuple(child_id for child_id in child_ids if child_id in resumed),
        parent_metadata=metadata_by_id.get(session_id, {}),
    )


def verify_native_evidence(
    evidence: NativeEvidence,
    selection: ModelSelection,
    parent: dict[str, Any],
) -> None:
    """Reject incomplete, forged, misrouted, or parent-changing evidence."""

    issues: list[str] = []
    child_ids = evidence.child_thread_ids
    if len(child_ids) != 4 or len(set(child_ids)) != 4:
        issues.append("child_count")
    if evidence.fanout_child_ids != child_ids[1:] or len(evidence.fanout_child_ids) != 3:
        issues.append("fanout_count")
    if len(evidence.metadata) != len(child_ids):
        issues.append("metadata_count")
    messages = dict(evidence.child_messages)
    expected_roles = ("default", "default", "worker", "explorer")
    if evidence.role_names != expected_roles:
        issues.append("roles")
    expected_by_role = {
        "default": "DEEPSEEK_DEFAULT_OK",
        "worker": "DEEPSEEK_WORKER_OK",
        "explorer": "DEEPSEEK_EXPLORER_OK",
    }
    if child_ids:
        if "DEEPSEEK_SINGLE_OK" not in messages.get(child_ids[0], ()):
            issues.append("single_token")
        if "DEEPSEEK_RESUME_OK" not in messages.get(child_ids[0], ()):
            issues.append("resume_token")
        if child_ids[0] not in evidence.resumed_child_ids:
            issues.append("resume_tool")
    for index, (child_id, metadata) in enumerate(zip(child_ids, evidence.metadata)):
        role = metadata.get("agent_role")
        if (
            metadata.get("model_provider") != "deepseek"
            or metadata.get("model") != selection.resolved_model
            or role != expected_roles[index]
            or (
                selection.reasoning_effort is not None
                and metadata.get("reasoning_effort") != selection.reasoning_effort
            )
        ):
            issues.append("child_metadata")
        if index > 0 and expected_by_role.get(str(role)) not in messages.get(child_id, ()):
            issues.append("fanout_token")
    if not any(child_id in evidence.tool_child_ids for child_id in evidence.fanout_child_ids):
        issues.append("child_tool")
    if (
        evidence.parent_metadata.get("model_provider") != parent.get("model_provider")
        or evidence.parent_metadata.get("model") != parent.get("model")
        or evidence.parent_metadata.get("reasoning_effort") != parent.get("reasoning_effort")
    ):
        issues.append("parent_changed")
    if not evidence.session_id:
        issues.append("session")
    if issues:
        raise ManagerError(
            "native_route_mismatch",
            "Native DeepSeek fan-out evidence is incomplete or does not match the installed route.",
            {"failed_checks": sorted(set(issues))},
        )


def native_acceptance_report(
    codex_bin: str,
    home: Path,
    selection: ModelSelection,
) -> CompatibilityReport:
    """Adapter used by the lifecycle manager after formal installation."""

    paths = resolve_paths(str(home))
    parent = _parent_expected(paths.config)
    evidence = run_native_acceptance(codex_bin, paths, selection)
    verify_native_evidence(evidence, selection, parent)
    return CompatibilityReport(
        model=selection.resolved_model,
        effort=selection.reasoning_effort,
        provider_initialized=True,
        single_child_passed=True,
        fanout_passed=True,
        tools_passed=True,
        resume_passed=True,
        child_metadata_passed=True,
        parent_unchanged=True,
    )
