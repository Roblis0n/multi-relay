"""Filesystem layout owned by the DeepSeek fan-out manager."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """User-level Codex paths; intentionally excludes a live model catalog."""

    home: Path
    config: Path
    agents_dir: Path
    instruction_file: Path
    state_dir: Path
    manifest: Path


def resolve_paths(codex_home: str | None = None) -> Paths:
    """Resolve the target Codex Home without creating or changing files."""

    raw_home = codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    home = Path(raw_home).expanduser().resolve()
    state_dir = home / "codex-deepseek-subagent"
    return Paths(
        home=home,
        config=home / "config.toml",
        agents_dir=home / "agents",
        instruction_file=home / "AGENTS.md",
        state_dir=state_dir,
        manifest=state_dir / "manifest.json",
    )
