"""Managed Codex instructions for safe automatic child fan-out."""

from __future__ import annotations

import re

from .errors import ManagerError


INSTRUCTIONS_BEGIN = "<!-- BEGIN CODEX-DEEPSEEK-FANOUT -->"
INSTRUCTIONS_END = "<!-- END CODEX-DEEPSEEK-FANOUT -->"


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def render_fanout_block(max_children: int = 8) -> str:
    """Render the complete bounded fan-out policy."""

    if isinstance(max_children, bool) or not isinstance(max_children, int) or max_children < 1:
        raise ManagerError("invalid_concurrency", "Child limit must be a positive integer.")
    return f"""{INSTRUCTIONS_BEGIN}
## DeepSeek child fan-out

When a request has two or more independent, bounded work items, fan out automatically with one child per work item and at most {max_children} children. Use `explorer` for read-heavy research, `worker` for isolated writes, and `default` for other bounded independent work.

For every spawn, set an explicit `agent_type` to `default`, `worker`, or `explorer`, and set `fork_turns="none"` (or a positive partial-turn count when the child truly needs recent context). Never use full-history inheritance or omit `agent_type`: either can inherit the Sol parent instead of selecting the managed DeepSeek role. Put all context required by a `fork_turns="none"` child in its task message.

Codex protects native agent messages before a custom Provider receives them. Therefore, before the matching `spawn_agent`, `followup_task`, or `send_message` call, emit a commentary handoff block containing the exact complete child message. Use the short `task_name` as `<target>` for a spawn and the canonical agent path for a follow-up. Do not wrap the block in a code fence, abbreviate its message, or reuse one block for multiple calls:

[DeepSeek task: <target>]
<exact complete child message>
[/DeepSeek task: <target>]

For a parallel batch, emit one complete block per child in the same commentary update, then make the matching calls. The local bridge matches each block to its target and fails closed if the handoff is absent.

Never fan out work that has overlapping file writes, shared mutable state, or a required execution order. Keep trivial or sequential work in the parent. Wait for every child before synthesis; the parent verifies evidence, resolves conflicts, and owns the final answer. A more specific instruction for the repository or task overrides this block.
{INSTRUCTIONS_END}
"""


def remove_fanout_instructions(text: str) -> str:
    """Remove only the block owned by this manager."""

    normalized = _normalize(text)
    pattern = re.compile(
        rf"(?ms)^\s*{re.escape(INSTRUCTIONS_BEGIN)}\s*\n.*?^\s*{re.escape(INSTRUCTIONS_END)}\s*(?:\n|$)"
    )
    removed = pattern.sub("", normalized).rstrip()
    return removed + ("\n" if removed else "")


def apply_fanout_instructions(text: str, max_children: int = 8) -> str:
    """Replace the managed block without touching unrelated instructions."""

    unmanaged = remove_fanout_instructions(text).rstrip()
    block = render_fanout_block(max_children)
    if not unmanaged:
        return block
    return unmanaged + "\n\n" + block
