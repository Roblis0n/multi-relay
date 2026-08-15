"""Render Codex built-in child roles backed by DeepSeek."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from .errors import ManagerError
from .model_capabilities import ModelSelection


ROLE_NAMES = ("default", "worker", "explorer")
DEEPSEEK_CONTEXT_WINDOW = 1_000_000
_DESCRIPTIONS = {
    "default": "General-purpose DeepSeek child for independent bounded tasks.",
    "worker": "DeepSeek implementation child for isolated file ownership.",
    "explorer": "DeepSeek research child for read-heavy repository exploration.",
}
_ROLE_RULES = {
    "default": "Complete only the independent bounded task assigned to the default role.",
    "worker": (
        "Edit only files explicitly assigned to the worker role; never overlap another "
        "child's write set."
    ),
    "explorer": (
        "Treat the explorer role as read-heavy: inspect and report, and do not edit files "
        "unless the parent explicitly grants an isolated write set."
    ),
}


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_agent(role: str, selection: ModelSelection) -> str:
    """Render and parse-check one built-in agent override."""

    if role not in ROLE_NAMES:
        raise ManagerError("invalid_role", f"Unsupported Codex child role: {role}")
    if not selection.resolved_model.strip():
        raise ManagerError("invalid_model", "A verified DeepSeek model is required.")

    instructions = (
        f"You are Codex's {role} child agent. {_ROLE_RULES[role]} "
        "This is a text-only model: never claim to have inspected images, video, "
        "screenshots, audio, or other non-text inputs. Follow the parent's task boundary, "
        "report concrete evidence, and return control when the bounded task is complete."
    )
    lines = [
        f"name = {_toml_string(role)}",
        f"description = {_toml_string(_DESCRIPTIONS[role])}",
        f"model = {_toml_string(selection.resolved_model)}",
        'model_provider = "deepseek"',
        f"model_context_window = {DEEPSEEK_CONTEXT_WINDOW}",
    ]
    if selection.reasoning_effort is not None:
        lines.append(f"model_reasoning_effort = {_toml_string(selection.reasoning_effort)}")
    lines.append(f"developer_instructions = {_toml_string(instructions)}")
    rendered = "\n".join(lines) + "\n"
    try:
        tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise ManagerError("invalid_agent", f"Generated {role} agent TOML is invalid.") from exc
    return rendered


def expected_agent_files(
    agents_dir: Path,
    selection: ModelSelection,
) -> dict[Path, bytes]:
    """Return the complete deterministic built-in role file set."""

    return {
        agents_dir / f"{role}.toml": render_agent(role, selection).encode("utf-8")
        for role in ROLE_NAMES
    }
