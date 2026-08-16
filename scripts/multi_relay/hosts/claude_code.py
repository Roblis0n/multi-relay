"""Claude Code subagent rendering, ownership, and safe launcher support."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..catalog import AgentProfile, Catalog, save_catalog_bytes
from ..errors import ManagerError
from ..gateway import GatewayController
from ..paths import Paths, resolve_paths
from ..transaction import InstallPlan, execute_install_plan
from . import HostPlan


CLAUDE_OWNERSHIP_MARKER = "MULTI-RELAY-OWNED"
CLAUDE_HOST_MANIFEST_SCHEMA = 1
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_BYPASS_ENVIRONMENT = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    }
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _yaml_string(value: str) -> str:
    """JSON strings are a safe YAML scalar subset and avoid implicit types."""

    return json.dumps(value, ensure_ascii=False)


def render_claude_agent(agent: AgentProfile, catalog_hash: str) -> str:
    """Render one managed Claude Code subagent Markdown file."""

    if not re.fullmatch(r"[0-9a-f]{64}", catalog_hash):
        raise ManagerError("catalog_invalid", "Claude agent catalog hash is invalid.")
    tools = "[" + ", ".join(_yaml_string(tool) for tool in agent.tools) + "]"
    body = agent.developer_instructions.replace("\r\n", "\n").replace("\r", "\n").rstrip()
    return (
        "---\n"
        f"# {CLAUDE_OWNERSHIP_MARKER} catalog-sha256={catalog_hash}\n"
        f"name: {_yaml_string(agent.name)}\n"
        f"description: {_yaml_string(agent.description)}\n"
        f"model: {_yaml_string(f'multi-relay-agent-{agent.name}')}\n"
        f"tools: {tools}\n"
        "---\n\n"
        f"{body}\n"
    )


def find_claude_code(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """Find Claude Code without invoking a shell."""

    selected_environment = os.environ if environ is None else environ
    candidate = explicit or selected_environment.get("CLAUDE_CODE_BIN")
    if candidate:
        path = Path(candidate).expanduser().resolve()
        if path.is_file():
            return str(path)
        raise ManagerError(
            "claude_code_not_found",
            f"Claude Code executable was not found at {path}.",
        )
    discovered = which("claude") or which("claude.exe")
    if not discovered:
        raise ManagerError(
            "claude_code_not_found",
            "Claude Code executable was not found. Install it or pass --claude-bin.",
        )
    return str(Path(discovered).resolve())


def build_claude_environment(
    parent: Mapping[str, str],
    *,
    base_url: str,
    local_token: str,
    model_alias: str,
) -> dict[str, str]:
    """Build a child-only environment that cannot bypass the local gateway."""

    if not local_token or not model_alias or not base_url.startswith("http://127.0.0.1:"):
        raise ManagerError("launcher_invalid", "Claude Code gateway settings are incomplete.")
    child = dict(parent)
    for key in _BYPASS_ENVIRONMENT:
        child.pop(key, None)
    child.update(
        {
            "ANTHROPIC_BASE_URL": base_url.rstrip("/"),
            "ANTHROPIC_AUTH_TOKEN": local_token,
            "ANTHROPIC_MODEL": model_alias,
        }
    )
    return child


def launch_claude_code(
    arguments: Sequence[str] = (),
    *,
    pool: str | None = None,
    executable: str | None = None,
    codex_home: Path | None = None,
    catalog_path: Path | None = None,
    keep_gateway: bool = False,
    environ: Mapping[str, str] | None = None,
    controller: Any | None = None,
    runner: Callable[..., Any] = subprocess.run,
    output: Callable[[str], None] | None = print,
) -> int:
    """Start Claude Code through the Anthropic gateway and return its exit code."""

    if pool is not None and not _IDENTIFIER.fullmatch(pool):
        raise ManagerError("unknown_pool", "Claude Code pool id is invalid.")
    binary = find_claude_code(executable, environ=environ)
    gateway = controller or GatewayController(
        codex_home=codex_home,
        catalog_path=catalog_path,
    )
    ready = False
    try:
        state = gateway.ensure()
        ready = True
        token = gateway.token_store.read()
        if not token:
            raise ManagerError(
                "gateway_token_missing",
                "The local gateway token is unavailable.",
            )
        alias = f"multi-relay-{pool}" if pool else "multi-relay-default"
        child_environment = build_claude_environment(
            os.environ if environ is None else environ,
            base_url=f"http://{gateway.host}:{state.port}",
            local_token=token,
            model_alias=alias,
        )
        if output is not None:
            output(f"Launching Claude Code through Multi Relay (model: {alias}).")
        tail = list(arguments)
        if tail[:1] == ["--"]:
            tail = tail[1:]
        completed = runner(
            [binary, *tail],
            env=child_environment,
            shell=False,
            check=False,
        )
        return int(completed.returncode)
    finally:
        if ready and not keep_gateway:
            gateway.stop()


class ClaudeCodeHostAdapter:
    """Manage Claude Code subagent Markdown files for user or project scope."""

    name = "claude-code"

    def __init__(self, paths: Paths, *, project_path: Path | None = None) -> None:
        self.paths = paths
        self.project_path = project_path
        self.manifest_path = paths.claude_host_manifest

    def _read_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.is_file():
            return None
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ManagerError("invalid_manifest", "Claude Code host manifest is invalid.") from None
        if not isinstance(value, dict) or value.get("schema_version") != CLAUDE_HOST_MANIFEST_SCHEMA:
            raise ManagerError("invalid_manifest", "Claude Code host manifest is invalid.")
        return value

    def _agents_dir(self, catalog: Catalog) -> tuple[str, Path]:
        host = catalog.hosts.get(self.name)
        scope = host.scope if host and host.scope else "user"
        if scope == "user":
            return scope, self.paths.claude_user_agents_dir
        if scope != "project":
            raise ManagerError("catalog_invalid", f"Unsupported Claude Code scope: {scope}.")
        if self.project_path is None:
            raise ManagerError(
                "project_required",
                "Project-scope Claude Code agents require an explicit project path.",
            )
        project = self.project_path.expanduser().resolve()
        if not project.is_dir():
            raise ManagerError("project_not_found", f"Claude Code project does not exist: {project}")
        return scope, project / ".claude" / "agents"

    @staticmethod
    def _records(manifest: Mapping[str, Any] | None) -> dict[str, str]:
        raw = manifest.get("files", {}) if manifest else {}
        if not isinstance(raw, dict) or not all(
            isinstance(path, str) and isinstance(digest, str)
            for path, digest in raw.items()
        ):
            raise ManagerError("invalid_manifest", "Claude Code ownership records are invalid.")
        return dict(raw)

    def plan(self, catalog: Catalog) -> HostPlan:
        host = catalog.hosts.get(self.name)
        if host is None or not host.enabled:
            return HostPlan(host=self.name, action="disable", files={})
        scope, agents_dir = self._agents_dir(catalog)
        digest = _sha256(save_catalog_bytes(catalog))
        desired = {
            agents_dir / f"{agent.name}.md": render_claude_agent(agent, digest).encode("utf-8")
            for agent in catalog.agents
            if self.name in agent.hosts
        }
        manifest = self._read_manifest()
        previous = self._records(manifest)
        for path in desired:
            if not path.exists():
                continue
            expected = previous.get(str(path))
            if expected is None or _sha256(path.read_bytes()) != expected:
                raise ManagerError(
                    "conflict",
                    "A Claude Code agent file is not safely owned by Multi Relay.",
                    {"path": str(path)},
                )
        removals: list[Path] = []
        for raw_path, expected in previous.items():
            path = Path(raw_path)
            if path in desired or not path.exists():
                continue
            if _sha256(path.read_bytes()) != expected:
                raise ManagerError(
                    "conflict",
                    "A stale Claude Code agent was modified and cannot be replaced.",
                    {"path": str(path)},
                )
            removals.append(path)
        snapshot = {
            "schema_version": CLAUDE_HOST_MANIFEST_SCHEMA,
            "host": self.name,
            "status": "enabled",
            "scope": scope,
            "agents_dir": str(agents_dir),
            "catalog_sha256": digest,
            "files": {
                str(path): _sha256(data)
                for path, data in sorted(desired.items(), key=lambda item: str(item[0]))
            },
        }
        return HostPlan(
            host=self.name,
            action="apply",
            files=desired,
            removals=tuple(removals),
            manifest=snapshot,
        )

    def _execute(self, plan: HostPlan) -> dict[str, Any]:
        result = execute_install_plan(
            InstallPlan(
                files=dict(plan.files),
                removals=plan.removals,
                manifest=dict(plan.manifest) if plan.manifest is not None else None,
                backup_dir=self.paths.state_dir
                / "backups"
                / f"claude-code-{plan.action}-{time.time_ns()}",
            ),
            self.manifest_path,
        )
        return {
            "status": "uninstalled" if plan.manifest is None else plan.manifest["status"],
            "changed": plan.changed,
            "warnings": list(plan.warnings),
            "backup": str(result.backup_dir),
        }

    def apply(self, catalog: Catalog) -> dict[str, Any]:
        host = catalog.hosts.get(self.name)
        if host is None or not host.enabled:
            return self.disable()
        return self._execute(self.plan(catalog))

    def status(self) -> dict[str, Any]:
        manifest = self._read_manifest()
        if manifest is None:
            return {"status": "not_configured", "changed": False, "warnings": []}
        drift = [
            path
            for path, expected in self._records(manifest).items()
            if not Path(path).is_file() or _sha256(Path(path).read_bytes()) != expected
        ]
        status = str(manifest.get("status", "partial"))
        if status == "enabled" and drift:
            status = "partial"
        return {
            "status": status,
            "changed": False,
            "warnings": (["Claude Code managed files have drifted."] if drift else []),
            "details": {"drift": drift, "scope": manifest.get("scope")},
        }

    def _removal_plan(self, *, uninstall: bool) -> HostPlan:
        manifest = self._read_manifest()
        if manifest is None:
            return HostPlan(host=self.name, action="uninstall", files={}, manifest=None)
        removals: list[Path] = []
        warnings: list[str] = []
        for raw_path, expected in self._records(manifest).items():
            path = Path(raw_path)
            if not path.exists():
                continue
            if _sha256(path.read_bytes()) == expected:
                removals.append(path)
            else:
                warnings.append(f"Retained modified Claude Code agent: {path}")
        updated = None if uninstall else {**manifest, "status": "disabled"}
        return HostPlan(
            host=self.name,
            action="uninstall" if uninstall else "disable",
            files={},
            removals=tuple(removals),
            manifest=updated,
            warnings=tuple(warnings),
        )

    def disable(self) -> dict[str, Any]:
        manifest = self._read_manifest()
        if manifest is None or manifest.get("status") == "disabled":
            return {"status": "disabled", "changed": False, "warnings": []}
        return self._execute(self._removal_plan(uninstall=False))

    def enable(self, catalog: Catalog) -> dict[str, Any]:
        return self.apply(catalog)

    def uninstall(self) -> dict[str, Any]:
        if self._read_manifest() is None:
            return {"status": "uninstalled", "changed": False, "warnings": []}
        return self._execute(self._removal_plan(uninstall=True))


__all__ = [
    "CLAUDE_OWNERSHIP_MARKER",
    "ClaudeCodeHostAdapter",
    "build_claude_environment",
    "find_claude_code",
    "launch_claude_code",
    "render_claude_agent",
]
