"""Managed Codex instructions for safe automatic child fan-out."""

from __future__ import annotations

import re

from .catalog import Catalog, default_catalog
from .errors import ManagerError


INSTRUCTIONS_BEGIN = "<!-- BEGIN CODEX-MULTI-RELAY -->"
INSTRUCTIONS_END = "<!-- END CODEX-MULTI-RELAY -->"
LEGACY_INSTRUCTION_MARKERS = (
    ("<!-- BEGIN CODEX-DEEPSEEK-FANOUT -->", "<!-- END CODEX-DEEPSEEK-FANOUT -->"),
)


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _agent_table(catalog: Catalog) -> str:
    lines = []
    capability_order = ("text", "vision", "audio", "tools", "web")
    for agent in sorted(catalog.agents, key=lambda item: (item.priority, item.name)):
        capabilities = ", ".join(
            item for item in capability_order if item in agent.capabilities
        )
        provider = catalog.provider(agent.provider)
        lines.append(
            f"- `{agent.name}`: provider={provider.id}; protocol={provider.protocol}; "
            f"capabilities={capabilities}; trust={agent.trust}; priority={agent.priority}; "
            f"sandbox={agent.sandbox_mode}"
        )
    return "\n".join(lines)


def render_fanout_block(
    max_children: int | Catalog = 8,
    catalog: Catalog | None = None,
) -> str:
    """Render the complete capability-aware bounded fan-out policy."""

    if isinstance(max_children, Catalog):
        if catalog is not None:
            raise ManagerError("catalog_invalid", "Catalog was supplied twice.")
        catalog = max_children
        max_children = catalog.concurrency

    if isinstance(max_children, bool) or not isinstance(max_children, int) or max_children < 1:
        raise ManagerError("invalid_concurrency", "Child limit must be a positive integer.")
    selected_catalog = catalog or default_catalog()
    if max_children > selected_catalog.concurrency:
        raise ManagerError(
            "invalid_concurrency",
            "Instruction child limit cannot exceed catalog concurrency.",
        )
    agent_names = ", ".join(f"`{item.name}`" for item in selected_catalog.agents)
    table = _agent_table(selected_catalog)
    return f"""{INSTRUCTIONS_BEGIN}
## Multi-model child routing

When a request has two or more independent, bounded work items, fan out automatically with one child per work item and at most {max_children} children. Available managed agent types are {agent_names}. Use `explorer` for read-heavy research, `worker` for isolated writes, and `default` for other bounded independent work when those agents qualify.

Catalog routing table (capabilities are hard boundaries):
{table}

Before delegation, derive the task's required capabilities. Keep vision, audio, hosted-search, and any other unsupported work in the parent unless a catalog agent explicitly declares every required capability. A web-capable child qualifies only when its generated TOML includes a real MCP server. Use a high-trust reviewer (`trust=high`) for high-risk work, and the parent retains final verification. Select qualifying agents by ascending priority and then name. If there is no qualifying child, keep the task in the parent; never silently substitute another provider, model, or agent. Explain the selected agent in one short sentence.

For every spawn, set an explicit `agent_type` to one of the catalog names above, and set `fork_turns="none"` (or a positive partial-turn count when the child truly needs recent context). Never use full-history inheritance or omit `agent_type`. Put all context required by a `fork_turns="none"` child in its task message.

Codex protects native agent messages before a custom Provider receives them. Therefore, before the matching `spawn_agent`, `followup_task`, or `send_message` call, emit a commentary handoff block containing the exact complete child message. Use the short `task_name` as `<target>` for a spawn and the canonical agent path for a follow-up. Do not wrap the block in a code fence, abbreviate its message, or reuse one block for multiple calls:

[Relay task: <target>]
<exact complete child message>
[/Relay task: <target>]

For a parallel batch, emit one complete block per child in the same commentary update, then make the matching calls. The local bridge matches each block to its target and fails closed if the handoff is absent.

Never fan out work that has overlapping file writes, shared mutable state, or a required execution order. Keep trivial or sequential work in the parent. Wait for every child before synthesis; the parent verifies evidence, resolves conflicts, performs final verification, and owns the final answer. A more specific instruction for the repository or task overrides this block.
{INSTRUCTIONS_END}
"""


def remove_fanout_instructions(text: str) -> str:
    """Remove only the block owned by this manager."""

    normalized = _normalize(text)
    removed = normalized
    for begin, end in ((INSTRUCTIONS_BEGIN, INSTRUCTIONS_END), *LEGACY_INSTRUCTION_MARKERS):
        pattern = re.compile(
            rf"(?ms)^\s*{re.escape(begin)}\s*\n.*?^\s*{re.escape(end)}\s*(?:\n|$)"
        )
        removed = pattern.sub("", removed)
    removed = removed.rstrip()
    return removed + ("\n" if removed else "")


def apply_fanout_instructions(
    text: str,
    max_children: int = 8,
    *,
    catalog: Catalog | None = None,
) -> str:
    """Replace the managed block without touching unrelated instructions."""

    unmanaged = remove_fanout_instructions(text).rstrip()
    block = render_fanout_block(max_children, catalog)
    if not unmanaged:
        return block
    return unmanaged + "\n\n" + block
