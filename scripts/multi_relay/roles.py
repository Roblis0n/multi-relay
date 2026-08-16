"""Render catalog-driven Codex child agents and legacy DeepSeek roles."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, Mapping

from .catalog import AgentSpec, Catalog, ExecutionTarget
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


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return "{ " + ", ".join(
            f"{_toml_string(str(key))} = {_toml_value(item)}"
            for key, item in sorted(value.items())
        ) + " }"
    raise ManagerError("invalid_agent", "Generated agent configuration contains an unsupported TOML value.")


def _agent_targets(agent: AgentSpec, catalog: Catalog) -> tuple[ExecutionTarget, ...]:
    """Return the enabled Codex targets visible to an agent's primary pool."""

    pool = catalog.pool(agent.pool_id)
    return tuple(
        target
        for target_id in pool.targets
        for target in (catalog.target(target_id),)
        if target.enabled
        and "codex" in target.host_compatibility
        and catalog.provider(target.provider_id).enabled
    )


def _target_protocol(target: ExecutionTarget, catalog: Catalog) -> str:
    return target.protocol or catalog.provider(target.provider_id).protocol


def _catalog_agent_lines(agent: AgentSpec, catalog: Catalog) -> list[str]:
    targets = _agent_targets(agent, catalog)
    native = bool(targets) and all(
        _target_protocol(target, catalog) == "codex-native" for target in targets
    )
    lines = [
        f"name = {_toml_string(agent.name)}",
        f"description = {_toml_string(agent.description)}",
    ]
    if not native:
        lines.extend(
            [
                f"model = {_toml_string(f'multi-relay-agent-{agent.name}')}",
                'model_provider = "multi-relay"',
            ]
        )
    target_windows = [
        target.context_window for target in targets if target.context_window is not None
    ]
    context_window = agent.context_window or (min(target_windows) if target_windows else None)
    if context_window is not None:
        lines.append(f"model_context_window = {context_window}")
    if agent.reasoning_effort is not None:
        lines.append(f"model_reasoning_effort = {_toml_string(agent.reasoning_effort)}")
    lines.extend(
        [
            f"sandbox_mode = {_toml_string(agent.sandbox_mode)}",
            f"developer_instructions = {_toml_string(agent.developer_instructions)}",
        ]
    )
    for server_name, raw_config in sorted(agent.mcp_servers.items()):
        lines.extend(["", f"[mcp_servers.{_toml_string(server_name)}]"])
        config: Mapping[str, Any] = raw_config
        for key, value in sorted(config.items()):
            lines.append(f"{key} = {_toml_value(value)}")
    for skill in agent.skills:
        lines.extend(["", "[[skills.config]]"])
        lines.append(f"path = {_toml_string(str(skill['path']))}")
        lines.append(f"enabled = {_toml_value(bool(skill.get('enabled', True)))}")
    return lines


def render_agent(
    role: str | AgentSpec,
    selection: ModelSelection | None = None,
    *,
    catalog: Catalog | None = None,
) -> str:
    """Render and parse-check a legacy role or catalog agent override."""

    if isinstance(role, AgentSpec):
        if catalog is None:
            raise ManagerError("catalog_invalid", "Catalog rendering requires its provider catalog.")
        lines = _catalog_agent_lines(role, catalog)
        rendered = "\n".join(lines) + "\n"
        try:
            tomllib.loads(rendered)
        except tomllib.TOMLDecodeError as exc:
            raise ManagerError(
                "invalid_agent", f"Generated {role.name} agent TOML is invalid."
            ) from exc
        return rendered

    if role not in ROLE_NAMES:
        raise ManagerError("invalid_role", f"Unsupported Codex child role: {role}")
    if selection is None:
        raise ManagerError("invalid_model", "A verified DeepSeek model is required.")
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
    selection: ModelSelection | Catalog,
) -> dict[Path, bytes]:
    """Return the complete deterministic legacy or catalog agent file set."""

    if isinstance(selection, Catalog):
        return {
            agents_dir / f"{agent.name}.toml": render_agent(
                agent, catalog=selection
            ).encode("utf-8")
            for agent in selection.agents
            if "codex" in agent.hosts
        }

    return {
        agents_dir / f"{role}.toml": render_agent(role, selection).encode("utf-8")
        for role in ROLE_NAMES
    }
