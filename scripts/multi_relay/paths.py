"""Filesystem layout owned by Codex Multi Relay."""

from __future__ import annotations

import os
import sys
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
    product_state_dir: Path
    runtime_state: Path
    runtime_state_lock: Path
    gateway_state: Path

    @property
    def codex_host_manifest(self) -> Path:
        """Ownership snapshot for the Codex host adapter."""

        return self.state_dir / "hosts" / "codex.json"

    @property
    def claude_user_agents_dir(self) -> Path:
        """Default user-scope Claude Code subagent directory."""

        return self.home.parent / ".claude" / "agents"


def resolve_paths(
    codex_home: str | None = None,
    *,
    state_home: str | Path | None = None,
    platform: str | None = None,
    user_home: str | Path | None = None,
) -> Paths:
    """Resolve host and product state paths without creating or changing files."""

    raw_home = codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    home = Path(raw_home).expanduser().resolve()
    selected_platform = platform or ("windows" if os.name == "nt" else sys.platform)
    selected_user_home = Path(user_home or Path.home()).expanduser().resolve()
    user_home_injected = user_home is not None
    if state_home is not None:
        product_root = Path(state_home).expanduser().resolve()
    elif selected_platform in {"windows", "win32"}:
        product_root = Path(
            (None if user_home_injected else os.environ.get("LOCALAPPDATA"))
            or selected_user_home / "AppData" / "Local"
        ).expanduser().resolve()
    elif selected_platform in {"darwin", "macos"}:
        product_root = selected_user_home / "Library" / "Application Support"
    else:
        product_root = Path(
            (None if user_home_injected else os.environ.get("XDG_STATE_HOME"))
            or selected_user_home / ".local" / "state"
        ).expanduser().resolve()
    product_state_dir = product_root / "multi-relay"
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
        product_state_dir=product_state_dir,
        runtime_state=product_state_dir / "runtime-state.json",
        runtime_state_lock=product_state_dir / "runtime-state.lock",
        gateway_state=product_state_dir / "gateway-state.json",
    )
