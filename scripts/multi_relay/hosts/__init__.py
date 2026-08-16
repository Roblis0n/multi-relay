"""Host adapters and their host-neutral change plans."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class HostPlan:
    """A fully rendered, secret-free set of host filesystem changes."""

    host: str
    action: str
    files: Mapping[Path, bytes]
    removals: tuple[Path, ...] = ()
    manifest: Mapping[str, Any] | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return bool(self.files or self.removals)


from .codex import CodexHostAdapter  # noqa: E402
from .claude_code import ClaudeCodeHostAdapter  # noqa: E402

__all__ = ["ClaudeCodeHostAdapter", "CodexHostAdapter", "HostPlan"]
