"""Command-line entrypoint for Codex Multi Relay."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .catalog import AgentSpec, Catalog, ExecutionTarget, ProviderSpec, TargetPool
from .credentials import prompt_and_store
from .errors import ManagerError
from .hosts.claude_code import launch_claude_code
from .manager import RelayManager
from .paths import resolve_paths


MAX_DURATION_SECONDS = 365 * 24 * 60 * 60


def parse_duration(value: str) -> int:
    """Parse a positive s/m/h/d duration with a one-year hard limit."""

    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", value.strip())
    if match is None:
        raise argparse.ArgumentTypeError("duration must be a positive integer followed by s, m, h, or d")
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    seconds = int(match.group(1)) * multiplier
    if seconds > MAX_DURATION_SECONDS:
        raise argparse.ArgumentTypeError("duration exceeds the one-year maximum")
    return seconds


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Emit structured JSON.")
    common.add_argument("--codex-home", help="Target Codex Home; defaults to CODEX_HOME or ~/.codex.")
    common.add_argument("--codex-bin", help="Path to the Codex desktop runtime.")
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multi-relay",
        description="Capability-aware child-agent routing across multiple model providers.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    common = _common_parser()
    global_commands: dict[str, argparse.ArgumentParser] = {}
    for name, help_text in (
        ("status", "Inspect configuration without changing it."),
        ("catalog", "Show the active provider and agent catalog."),
        ("repair", "Re-apply and validate the installed catalog."),
        ("apply", "Validate and atomically apply catalog.json."),
        ("test", "Run available native acceptance checks."),
        ("disable", "Disable generated agents while retaining the catalog."),
        ("enable", "Re-enable the installed catalog."),
    ):
        global_commands[name] = commands.add_parser(name, parents=[common], help=help_text)
    for name in ("test", "disable", "enable"):
        global_commands[name].add_argument(
            "--host",
            choices=("codex", "claude-code", "all"),
        )
    for name in ("disable", "enable"):
        global_commands[name].add_argument("--project", type=Path)
    setup = commands.add_parser(
        "setup",
        parents=[common],
        help="Install a complete hybrid or native-only catalog.",
    )
    setup.add_argument(
        "--preset",
        choices=("hybrid", "native"),
        default="hybrid",
        help="hybrid uses DeepSeek workers plus a native reviewer; native never asks for a provider key.",
    )
    setup.add_argument("--host", choices=("codex", "claude-code", "all"))
    setup.add_argument("--project", type=Path)

    provider = commands.add_parser("provider", help="Manage model providers.")
    provider_commands = provider.add_subparsers(dest="provider_command", required=True)
    provider_commands.add_parser("list", parents=[common], help="List catalog providers.")
    provider_add = provider_commands.add_parser("add", parents=[common], help="Add a provider.")
    provider_add.add_argument("--id", required=True)
    provider_add.add_argument("--name", required=True)
    provider_add.add_argument(
        "--protocol",
        required=True,
        choices=(
            "codex-native",
            "responses-compatible",
            "chat-completions-compatible",
            "deepseek-chat",
            "anthropic-messages",
        ),
    )
    provider_add.add_argument("--base-url")
    provider_add.add_argument(
        "--auth",
        "--auth-mode",
        dest="auth",
        choices=("codex", "host-native", "vault", "none"),
    )
    provider_add.add_argument("--capability", action="append", required=True)
    provider_add.add_argument("--context-window", type=int)
    provider_add.add_argument("--disabled", action="store_true")
    provider_remove = provider_commands.add_parser(
        "remove",
        parents=[common],
        help="Remove an unused provider.",
    )
    provider_remove.add_argument("provider_id")
    provider_remove.add_argument("--remove-credential", action="store_true")
    provider_edit = provider_commands.add_parser("edit", parents=[common], help="Edit a provider.")
    provider_edit.add_argument("provider_id")
    provider_edit.add_argument("--name")
    provider_edit.add_argument("--base-url")
    provider_edit.add_argument("--models-endpoint")
    provider_edit.add_argument("--capability", action="append")
    provider_discover = provider_commands.add_parser(
        "discover-models", parents=[common], help="Discover a provider model."
    )
    provider_discover.add_argument("provider_id")
    provider_discover.add_argument("--model", required=True)
    for action in ("test", "enable", "disable"):
        selected = provider_commands.add_parser(action, parents=[common])
        selected.add_argument("provider_id")

    credential = commands.add_parser("credential", help="Manage vault credential references.")
    credential_commands = credential.add_subparsers(dest="credential_command", required=True)
    credential_commands.add_parser("list", parents=[common])
    for action in ("add", "replace"):
        selected = credential_commands.add_parser(action, parents=[common])
        selected.add_argument("--provider", required=True)
        selected.add_argument("--id", required=True)
        if action == "add":
            selected.add_argument("--label")
    for action in ("enable", "disable", "test", "remove"):
        selected = credential_commands.add_parser(action, parents=[common])
        selected.add_argument("--provider", required=True)
        selected.add_argument("--id", required=True)

    target = commands.add_parser("target", help="Manage execution targets.")
    target_commands = target.add_subparsers(dest="target_command", required=True)
    target_commands.add_parser("list", parents=[common])
    target_add = target_commands.add_parser("add", parents=[common])
    target_add.add_argument("--id", required=True)
    target_add.add_argument("--provider", required=True)
    target_add.add_argument("--model")
    target_add.add_argument("--credential")
    target_add.add_argument("--protocol")
    target_add.add_argument("--capability", action="append", required=True)
    target_add.add_argument("--context-window", type=int)
    target_add.add_argument("--max-output-tokens", type=int)
    target_add.add_argument("--reasoning-effort", action="append", default=[])
    target_add.add_argument("--trust", choices=("standard", "high"), default="standard")
    target_add.add_argument("--host", action="append", choices=("codex", "claude-code"), required=True)
    target_add.add_argument("--disabled", action="store_true")
    target_edit = target_commands.add_parser("edit", parents=[common])
    target_edit.add_argument("target_id")
    target_edit.add_argument("--model")
    target_edit.add_argument("--credential")
    target_edit.add_argument("--context-window", type=int)
    target_edit.add_argument("--max-output-tokens", type=int)
    for action in ("test", "enable", "disable", "remove"):
        selected = target_commands.add_parser(action, parents=[common])
        selected.add_argument("target_id")

    pool = commands.add_parser("pool", help="Manage ordered target pools.")
    pool_commands = pool.add_subparsers(dest="pool_command", required=True)
    pool_commands.add_parser("list", parents=[common])
    pool_add = pool_commands.add_parser("add", parents=[common])
    pool_add.add_argument("--id", required=True)
    pool_add.add_argument("--target", action="append", required=True)
    pool_add.add_argument("--strategy", choices=("sticky", "timed"), default="sticky")
    pool_add.add_argument("--duration", type=parse_duration)
    pool_add.add_argument("--max-rate-limit-wait", type=int, default=30)
    pool_add.add_argument("--capability", action="append", required=True)
    pool_add.add_argument("--host", action="append", choices=("codex", "claude-code"), required=True)
    pool_add.add_argument("--disabled", action="store_true")
    pool_edit = pool_commands.add_parser("edit", parents=[common])
    pool_edit.add_argument("pool_id")
    pool_edit.add_argument("--max-rate-limit-wait", type=int)
    pool_order = pool_commands.add_parser("order", parents=[common])
    pool_order.add_argument("pool_id")
    pool_order.add_argument("targets", nargs="+")
    pool_strategy = pool_commands.add_parser("strategy", parents=[common])
    pool_strategy.add_argument("pool_id")
    pool_strategy.add_argument("strategy", choices=("sticky", "timed"))
    pool_strategy.add_argument("--duration", type=parse_duration)
    for action in ("rotate", "reset", "status", "remove"):
        selected = pool_commands.add_parser(action, parents=[common])
        selected.add_argument("pool_id")

    agent = commands.add_parser("agent", help="Manage custom child agents.")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_commands.add_parser("list", parents=[common], help="List catalog agents.")
    agent_set = agent_commands.add_parser("set", parents=[common], help="Add or replace an agent.")
    agent_set.add_argument("--name", required=True)
    agent_set.add_argument("--description", required=True)
    agent_set.add_argument("--provider")
    agent_set.add_argument("--pool")
    agent_set.add_argument("--fallback-pool")
    agent_set.add_argument("--model")
    agent_set.add_argument("--reasoning-effort")
    agent_set.add_argument("--context-window", type=int)
    agent_set.add_argument("--capability", action="append", required=True)
    agent_set.add_argument("--trust", choices=("standard", "high"), default="standard")
    agent_set.add_argument("--priority", type=int, default=100)
    agent_set.add_argument(
        "--sandbox-mode",
        choices=("read-only", "workspace-write", "danger-full-access"),
        default="read-only",
    )
    agent_set.add_argument(
        "--mcp-json",
        default="{}",
        help="JSON object containing MCP server definitions.",
    )
    agent_set.add_argument("--skill", action="append", default=[])
    agent_set.add_argument("--tool", action="append", default=[])
    agent_set.add_argument(
        "--host",
        action="append",
        choices=("codex", "claude-code"),
        default=[],
    )
    agent_set.add_argument("--instructions", required=True)
    agent_remove = agent_commands.add_parser("remove", parents=[common], help="Remove an agent.")
    agent_remove.add_argument("name")

    host = commands.add_parser("host", help="Manage host integrations.")
    host_commands = host.add_subparsers(dest="host_command", required=True)
    host_commands.add_parser("list", parents=[common])
    for action in ("apply", "status"):
        selected = host_commands.add_parser(action, parents=[common])
        selected.add_argument("host_name", choices=("codex", "claude-code"))
        selected.add_argument("--project", type=Path)

    gateway = commands.add_parser("gateway", help="Manage the local relay gateway.")
    gateway_commands = gateway.add_subparsers(dest="gateway_command", required=True)
    for action in ("start", "status", "stop"):
        gateway_commands.add_parser(action, parents=[common])

    route = commands.add_parser("route", parents=[common], help="Resolve a capability request.")
    route.add_argument("--capability", action="append", required=True)
    route.add_argument("--high-risk", action="store_true")
    launch = commands.add_parser("launch", help="Launch a supported host through Multi Relay.")
    launch_commands = launch.add_subparsers(dest="launch_host", required=True)
    claude = launch_commands.add_parser(
        "claude-code",
        parents=[common],
        help="Launch Claude Code through the local Anthropic gateway.",
    )
    claude.add_argument("--claude-bin", help="Explicit Claude Code executable path.")
    claude.add_argument("--pool", help="Target pool id; defaults to the Claude Code host pool.")
    claude.add_argument("--project", type=Path)
    claude.add_argument(
        "--keep-gateway",
        action="store_true",
        help="Keep a gateway started by this launcher running after Claude exits.",
    )
    claude.add_argument("claude_args", nargs=argparse.REMAINDER)
    uninstall = commands.add_parser(
        "uninstall",
        parents=[common],
        help="Remove managed configuration.",
    )
    uninstall.add_argument(
        "--remove-credential",
        "--remove-credentials",
        dest="remove_credential",
        action="store_true",
        help="Also remove the API Key from the operating-system credential vault.",
    )
    uninstall.add_argument("--host", choices=("codex", "claude-code", "all"))
    uninstall.add_argument("--project", type=Path)
    return parser


def _codex_candidates(codex_home: Path | None = None) -> list[Path]:
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates: list[Path] = []
    selected_home = codex_home or (
        Path(os.environ["CODEX_HOME"]).expanduser()
        if os.environ.get("CODEX_HOME")
        else None
    )
    if selected_home is not None:
        candidates.extend(
            (
                selected_home / ".sandbox-bin" / "codex.exe",
                selected_home / ".sandbox-bin" / "codex",
            )
        )
    if local_app_data:
        root = Path(local_app_data)
        candidates.extend(
            (
                root / "Programs" / "Codex" / "resources" / "codex.exe",
                root / "Programs" / "OpenAI" / "Codex" / "resources" / "codex.exe",
            )
        )
    candidates.extend(
        (
            Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
            Path("/Applications/Codex.app/Contents/Resources/codex"),
        )
    )
    return candidates


def find_codex(
    explicit: str | None,
    *,
    required: bool,
    codex_home: Path | None = None,
) -> str:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ManagerError("codex_not_found", f"Codex runtime was not found at {path}.")
        return str(path)
    environment_path = os.environ.get("CODEX_DESKTOP_BIN")
    if environment_path and Path(environment_path).is_file():
        return str(Path(environment_path).resolve())
    for candidate in _codex_candidates(codex_home):
        if candidate.is_file():
            return str(candidate.resolve())
    discovered = shutil.which("codex") or shutil.which("codex.exe")
    if discovered:
        return discovered
    if required:
        raise ManagerError(
            "codex_not_found",
            "The Codex desktop runtime could not be found. Set CODEX_DESKTOP_BIN.",
        )
    return "codex"


def _default_manager(args: argparse.Namespace) -> RelayManager:
    selected_host = getattr(args, "host", None)
    live_command = (
        args.command == "test" and selected_host in {None, "codex", "all"}
    ) or (
        args.command == "setup" and getattr(args, "preset", "hybrid") == "hybrid"
        and selected_host != "claude-code"
    )
    paths = resolve_paths(args.codex_home)
    return RelayManager(
        paths,
        find_codex(args.codex_bin, required=live_command, codex_home=paths.home),
    )


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if not {"changed", "warnings", "details", "next_actions"}.issubset(payload):
        payload = {
            "status": payload.get("status", "unknown"),
            "changed": bool(payload.get("changed", False)),
            "warnings": list(payload.get("warnings", [])),
            "details": {
                key: value
                for key, value in payload.items()
                if key not in {"status", "changed", "warnings", "next_actions"}
            },
            "next_actions": list(payload.get("next_actions", [])),
        }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(payload.get("status", "unknown"))
    for key, value in payload.items():
        if key != "status":
            print(f"{key}: {value}")


def _provider_from_args(args: argparse.Namespace) -> ProviderSpec:
    auth = args.auth or ("codex" if args.protocol == "codex-native" else "vault")
    return ProviderSpec.from_dict(
        {
            "id": args.id,
            "name": args.name,
            "protocol": args.protocol,
            "base_url": args.base_url,
            "auth": "codex" if auth == "host-native" else auth,
            "capabilities": args.capability,
            "context_window": args.context_window,
            "enabled": not args.disabled,
        }
    )


def _agent_from_args(args: argparse.Namespace) -> AgentSpec:
    try:
        mcp_servers = json.loads(args.mcp_json)
    except json.JSONDecodeError:
        raise ManagerError("catalog_invalid", "--mcp-json must be a valid JSON object.") from None
    if args.pool:
        return AgentSpec.from_dict(
            {
                "name": args.name,
                "description": args.description,
                "developer_instructions": args.instructions,
                "pool_id": args.pool,
                "required_capabilities": args.capability,
                "fallback_pool_id": args.fallback_pool,
                "reasoning_effort": args.reasoning_effort,
                "context_window": args.context_window,
                "trust": args.trust,
                "priority": args.priority,
                "sandbox_mode": args.sandbox_mode,
                "tools": args.tool,
                "mcp_servers": mcp_servers,
                "skills": args.skill,
                "hosts": args.host or ["codex"],
            }
        )
    if not args.provider:
        raise ManagerError("catalog_invalid", "agent set requires --pool or --provider.")
    return AgentSpec.from_dict(
        {
            "name": args.name,
            "description": args.description,
            "provider": args.provider,
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "context_window": args.context_window,
            "capabilities": args.capability,
            "trust": args.trust,
            "priority": args.priority,
            "sandbox_mode": args.sandbox_mode,
            "mcp_servers": mcp_servers,
            "skills": args.skill,
            "developer_instructions": args.instructions,
        }
    )


def _target_from_args(args: argparse.Namespace) -> ExecutionTarget:
    return ExecutionTarget.from_dict(
        {
            "id": args.id,
            "provider_id": args.provider,
            "protocol": args.protocol,
            "model": args.model,
            "credential_id": args.credential,
            "capabilities": args.capability,
            "context_window": args.context_window,
            "max_output_tokens": args.max_output_tokens,
            "reasoning_efforts": args.reasoning_effort,
            "trust": args.trust,
            "host_compatibility": args.host,
            "enabled": not args.disabled,
            "metadata": {},
        }
    )


def _pool_from_args(args: argparse.Namespace) -> TargetPool:
    if args.strategy == "timed" and args.duration is None:
        raise ManagerError("catalog_invalid", "Timed pools require --duration.")
    if args.strategy == "sticky" and args.duration is not None:
        raise ManagerError("catalog_invalid", "Sticky pools cannot use --duration.")
    return TargetPool.from_dict(
        {
            "id": args.id,
            "targets": args.target,
            "strategy": args.strategy,
            "duration_seconds": args.duration,
            "max_rate_limit_wait_seconds": args.max_rate_limit_wait,
            "cooldown": {
                "quota_seconds": 86400,
                "rate_limit_seconds": 60,
                "auth_seconds": 3600,
                "provider_seconds": 30,
            },
            "required_capabilities": args.capability,
            "host_compatibility": args.host,
            "enabled": not args.disabled,
        }
    )


def _prompt_catalog_credentials(
    manager: RelayManager,
    catalog_value: dict[str, object],
    prompt_fn: Callable[[str], str],
) -> None:
    catalog = Catalog.from_dict(catalog_value)
    for provider in catalog.providers:
        if provider.enabled and provider.auth == "vault":
            store = manager.credential_for_provider(provider, catalog=catalog)
            if not store.exists():
                prompt_and_store(store, prompt_fn=prompt_fn)


def main(
    argv: Sequence[str] | None = None,
    *,
    manager_factory: Callable[[argparse.Namespace], RelayManager] | None = None,
    prompt_fn: Callable[[str], str] = getpass.getpass,
    claude_launcher: Callable[..., int] = launch_claude_code,
) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    factory = manager_factory or _default_manager
    try:
        if args.command == "launch" and args.launch_host == "claude-code":
            paths = resolve_paths(args.codex_home)
            return claude_launcher(
                args.claude_args,
                pool=args.pool,
                executable=args.claude_bin,
                codex_home=paths.home,
                keep_gateway=args.keep_gateway,
                project_path=args.project,
            )
        manager = factory(args)
        if args.command == "setup":
            if args.preset == "hybrid":
                installation_status = manager.status().get("status")
                if installation_status == "not_configured":
                    if not manager.credentials.exists():
                        prompt_and_store(manager.credentials, prompt_fn=prompt_fn)
                elif installation_status != "legacy":
                    _prompt_catalog_credentials(manager, manager.catalog(), prompt_fn)
            payload = (
                manager.setup(preset=args.preset)
                if args.host is None
                else manager.setup(
                    preset=args.preset,
                    host=args.host,
                    project_path=args.project,
                )
            )
        elif args.command == "repair":
            if manager.status().get("status") != "legacy":
                _prompt_catalog_credentials(manager, manager.catalog(), prompt_fn)
            payload = manager.repair()
        elif args.command == "status":
            payload = manager.status()
        elif args.command == "catalog":
            payload = {"status": "ready", "catalog": manager.catalog()}
        elif args.command == "apply":
            _prompt_catalog_credentials(manager, manager.catalog(), prompt_fn)
            payload = manager.apply()
        elif args.command == "provider":
            if args.provider_command == "list":
                payload = {"status": "ready", "providers": manager.list_providers()}
            elif args.provider_command == "add":
                provider = _provider_from_args(args)
                if provider.auth == "vault":
                    store = manager.credential_for_provider(provider)
                    if not store.exists():
                        prompt_and_store(store, prompt_fn=prompt_fn)
                payload = manager.add_provider(provider)
            elif args.provider_command == "remove":
                payload = manager.remove_provider(
                    args.provider_id,
                    remove_credential=args.remove_credential,
                )
            elif args.provider_command == "edit":
                changes = {
                    key: value
                    for key, value in {
                        "name": args.name,
                        "base_url": args.base_url,
                        "models_endpoint": args.models_endpoint,
                        "capabilities": args.capability,
                    }.items()
                    if value is not None
                }
                payload = manager.edit_provider(args.provider_id, changes)
            elif args.provider_command == "discover-models":
                payload = manager.discover_provider_model(args.provider_id, args.model)
            elif args.provider_command == "test":
                payload = manager.test_provider(args.provider_id)
            elif args.provider_command in {"enable", "disable"}:
                payload = manager.set_provider_enabled(
                    args.provider_id,
                    args.provider_command == "enable",
                )
            else:
                raise ManagerError("invalid_command", "Unknown provider command.")
        elif args.command == "credential":
            if args.credential_command == "list":
                payload = {"status": "ready", "credentials": manager.list_credentials()}
            elif args.credential_command == "add":
                secret = prompt_fn("Credential: ")
                payload = manager.add_credential(
                    args.provider,
                    args.id,
                    label=args.label,
                    secret=secret,
                )
            elif args.credential_command == "replace":
                payload = manager.replace_credential(
                    args.provider,
                    args.id,
                    prompt_fn("Credential: "),
                )
            elif args.credential_command in {"enable", "disable"}:
                payload = manager.set_credential_enabled(
                    args.provider,
                    args.id,
                    args.credential_command == "enable",
                )
            elif args.credential_command == "test":
                payload = manager.test_credential(args.provider, args.id)
            elif args.credential_command == "remove":
                payload = manager.remove_credential(args.provider, args.id)
            else:
                raise ManagerError("invalid_command", "Unknown credential command.")
        elif args.command == "target":
            if args.target_command == "list":
                payload = {"status": "ready", "targets": manager.list_targets()}
            elif args.target_command == "add":
                payload = manager.add_target(_target_from_args(args))
            elif args.target_command == "edit":
                changes = {
                    key: value
                    for key, value in {
                        "model": args.model,
                        "credential_id": args.credential,
                        "context_window": args.context_window,
                        "max_output_tokens": args.max_output_tokens,
                    }.items()
                    if value is not None
                }
                payload = manager.edit_target(args.target_id, changes)
            elif args.target_command == "test":
                payload = manager.test_target(args.target_id)
            elif args.target_command in {"enable", "disable"}:
                payload = manager.set_target_enabled(
                    args.target_id,
                    args.target_command == "enable",
                )
            elif args.target_command == "remove":
                payload = manager.remove_target(args.target_id)
            else:
                raise ManagerError("invalid_command", "Unknown target command.")
        elif args.command == "pool":
            if args.pool_command == "list":
                payload = {"status": "ready", "pools": manager.list_pools()}
            elif args.pool_command == "add":
                payload = manager.add_pool(_pool_from_args(args))
            elif args.pool_command == "edit":
                changes = (
                    {"max_rate_limit_wait_seconds": args.max_rate_limit_wait}
                    if args.max_rate_limit_wait is not None
                    else {}
                )
                payload = manager.edit_pool(args.pool_id, changes)
            elif args.pool_command == "order":
                payload = manager.set_pool_order(args.pool_id, args.targets)
            elif args.pool_command == "strategy":
                if args.strategy == "timed" and args.duration is None:
                    raise ManagerError("catalog_invalid", "Timed pools require --duration.")
                payload = manager.set_pool_strategy(
                    args.pool_id,
                    args.strategy,
                    duration_seconds=args.duration,
                )
            elif args.pool_command == "rotate":
                payload = manager.rotate_pool(args.pool_id)
            elif args.pool_command == "reset":
                payload = manager.reset_pool(args.pool_id)
            elif args.pool_command == "status":
                payload = manager.pool_status(args.pool_id)
            elif args.pool_command == "remove":
                payload = manager.remove_pool(args.pool_id)
            else:
                raise ManagerError("invalid_command", "Unknown pool command.")
        elif args.command == "agent":
            if args.agent_command == "list":
                payload = {"status": "ready", "agents": manager.list_agents()}
            elif args.agent_command == "set":
                payload = manager.set_agent(_agent_from_args(args))
            elif args.agent_command == "remove":
                payload = manager.remove_agent(args.name)
            else:
                raise ManagerError("invalid_command", "Unknown agent command.")
        elif args.command == "host":
            if args.host_command == "list":
                payload = {"status": "ready", "hosts": manager.list_hosts()}
            elif args.host_command == "apply":
                payload = manager.apply_host(args.host_name, project_path=args.project)
            elif args.host_command == "status":
                payload = manager.host_status(args.host_name, project_path=args.project)
            else:
                raise ManagerError("invalid_command", "Unknown host command.")
        elif args.command == "gateway":
            if args.gateway_command == "start":
                payload = manager.gateway_start()
            elif args.gateway_command == "status":
                payload = manager.gateway_status()
            elif args.gateway_command == "stop":
                payload = manager.gateway_stop()
            else:
                raise ManagerError("invalid_command", "Unknown gateway command.")
        elif args.command == "route":
            payload = manager.route(set(args.capability), high_risk=args.high_risk)
        elif args.command == "test":
            payload = manager.test() if args.host is None else manager.test(args.host)
        elif args.command == "disable":
            if args.host is None:
                payload = manager.disable()
            elif args.host == "all":
                payload = {
                    "status": "disabled",
                    "hosts": [
                        manager.disable_host("claude-code", project_path=args.project),
                        manager.disable_host("codex"),
                    ],
                }
            else:
                payload = manager.disable_host(args.host, project_path=args.project)
        elif args.command == "enable":
            if args.host is None:
                payload = manager.enable()
            elif args.host == "all":
                payload = {
                    "status": "ready",
                    "hosts": [
                        manager.enable_host("codex"),
                        manager.enable_host("claude-code", project_path=args.project),
                    ],
                }
            else:
                payload = manager.enable_host(args.host, project_path=args.project)
        elif args.command == "uninstall":
            payload = (
                manager.uninstall(remove_credential=args.remove_credential)
                if args.host is None
                else manager.uninstall_host(
                    args.host,
                    project_path=args.project,
                    remove_credentials=args.remove_credential,
                )
            )
        else:
            raise ManagerError("invalid_command", "Unknown manager command.")
        _emit(payload, args.json)
        return 0
    except ManagerError as exc:
        payload = {"status": exc.code, "message": str(exc), **exc.details}
        _emit(payload, args.json)
        return 2
    except KeyboardInterrupt:
        _emit(
            {"status": "cancelled", "message": "The local operation was cancelled."},
            args.json,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
