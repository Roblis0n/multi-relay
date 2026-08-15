"""Filesystem layout owned by Codex Multi Relay."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    """User-level Codex paths, including ordered legacy read locations."""

    home: Path
    config: Path
    agents_dir: Path
    instruction_file: Path
    state_dir: Path
    manifest: Path
    catalog: Path
    relay_state_dir: Path
    relay_manifest: Path
    legacy_state_dir: Path
    legacy_manifest: Path


def resolve_paths(codex_home: str | None = None) -> Paths:
    """Resolve the target Codex Home without creating or changing files."""

    raw_home = codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    home = Path(raw_home).expanduser().resolve()
    state_dir = home / "codex-multi-relay"
    relay_state_dir = home / "codex-deepseek-relay"
    legacy_state_dir = home / "codex-deepseek-subagent"
    return Paths(
        home=home,
        config=home / "config.toml",
        agents_dir=home / "agents",
        instruction_file=home / "AGENTS.md",
        state_dir=state_dir,
        manifest=state_dir / "manifest.json",
        catalog=state_dir / "catalog.json",
        relay_state_dir=relay_state_dir,
        relay_manifest=relay_state_dir / "manifest.json",
        legacy_state_dir=legacy_state_dir,
        legacy_manifest=legacy_state_dir / "manifest.json",
    )
