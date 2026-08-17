"""Filesystem layout owned by Multi Relay."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .branding import LEGACY_STATE_DIRECTORY_NAMES, STATE_DIRECTORY_NAME


@dataclass(frozen=True)
class Paths:
    """Host paths, product state, and ordered migration-only read locations."""

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
    user_home: Path | None = None

    @property
    def codex_state_dir(self) -> Path:
        """Former Codex-scoped Multi Relay state; migration reads only."""

        return self.home / LEGACY_STATE_DIRECTORY_NAMES[0]

    @property
    def codex_manifest(self) -> Path:
        return self.codex_state_dir / "manifest.json"

    @property
    def legacy_state_dirs(self) -> tuple[Path, ...]:
        """Legacy state directories in newest-to-oldest precedence order."""

        return (self.codex_state_dir, self.relay_state_dir, self.legacy_state_dir)

    @property
    def legacy_manifests(self) -> tuple[Path, ...]:
        return tuple(path / "manifest.json" for path in self.legacy_state_dirs)

    @property
    def codex_host_manifest(self) -> Path:
        """Ownership snapshot for the Codex host adapter."""

        return self.state_dir / "hosts" / "codex.json"

    @property
    def claude_user_agents_dir(self) -> Path:
        """Default user-scope Claude Code subagent directory."""

        return (self.user_home or self.home.parent) / ".claude" / "agents"

    @property
    def claude_host_manifest(self) -> Path:
        """Ownership snapshot for the Claude Code host adapter."""

        return self.state_dir / "hosts" / "claude-code.json"


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
    elif codex_home is not None and user_home is None:
        # An explicitly injected Codex home is an isolated test/project root.
        # Keep its product state inside that disposable root instead of touching
        # the real user's operating-system state directory.
        product_root = home
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
    product_state_dir = product_root / STATE_DIRECTORY_NAME
    state_dir = product_state_dir
    relay_state_dir = home / LEGACY_STATE_DIRECTORY_NAMES[1]
    legacy_state_dir = home / LEGACY_STATE_DIRECTORY_NAMES[2]
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
        user_home=selected_user_home,
    )
