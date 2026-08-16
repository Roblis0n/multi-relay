"""Command-line entrypoint for Codex Multi Relay."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .catalog import AgentSpec, Catalog, ProviderSpec
from .credentials import prompt_and_store
from .errors import ManagerError
from .manager import RelayManager
from .paths import resolve_paths


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Emit structured JSON.")
    common.add_argument("--codex-home", help="Target Codex Home; defaults to CODEX_HOME or ~/.codex.")
    common.add_argument("--codex-bin", help="Path to the Codex desktop runtime.")
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-multi-relay",
        description="Capability-aware child-agent routing across multiple model providers.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    common = _common_parser()
    for name, help_text in (
        ("status", "Inspect configuration without changing it."),
        ("catalog", "Show the active provider and agent catalog."),
        ("repair", "Re-apply and validate the installed catalog."),
        ("apply", "Validate and atomically apply catalog.json."),
        ("test", "Run available native acceptance checks."),
        ("disable", "Disable generated agents while retaining the catalog."),
        ("enable", "Re-enable the installed catalog."),
    ):
        commands.add_parser(name, parents=[common], help=help_text)
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
        ),
    )
    provider_add.add_argument("--base-url")
    provider_add.add_argument("--auth", choices=("codex", "vault", "none"))
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

    agent = commands.add_parser("agent", help="Manage custom child agents.")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_commands.add_parser("list", parents=[common], help="List catalog agents.")
    agent_set = agent_commands.add_parser("set", parents=[common], help="Add or replace an agent.")
    agent_set.add_argument("--name", required=True)
    agent_set.add_argument("--description", required=True)
    agent_set.add_argument("--provider", required=True)
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
    agent_set.add_argument("--instructions", required=True)
    agent_remove = agent_commands.add_parser("remove", parents=[common], help="Remove an agent.")
    agent_remove.add_argument("name")

    route = commands.add_parser("route", parents=[common], help="Resolve a capability request.")
    route.add_argument("--capability", action="append", required=True)
    route.add_argument("--high-risk", action="store_true")
    uninstall = commands.add_parser(
        "uninstall",
        parents=[common],
        help="Remove managed configuration.",
    )
    uninstall.add_argument(
        "--remove-credential",
        action="store_true",
        help="Also remove the API Key from the operating-system credential vault.",
    )
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
    live_command = args.command == "test" or (
        args.command == "setup" and getattr(args, "preset", "hybrid") == "hybrid"
    )
    paths = resolve_paths(args.codex_home)
    return RelayManager(
        paths,
        find_codex(args.codex_bin, required=live_command, codex_home=paths.home),
    )


def _emit(payload: dict[str, Any], as_json: bool) -> None:
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
            "auth": auth,
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
) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    factory = manager_factory or _default_manager
    try:
        manager = factory(args)
        if args.command == "setup":
            if args.preset == "hybrid":
                installation_status = manager.status().get("status")
                if installation_status == "not_configured":
                    if not manager.credentials.exists():
                        prompt_and_store(manager.credentials, prompt_fn=prompt_fn)
                elif installation_status != "legacy":
                    _prompt_catalog_credentials(manager, manager.catalog(), prompt_fn)
            payload = manager.setup(preset=args.preset)
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
            else:
                raise ManagerError("invalid_command", "Unknown provider command.")
        elif args.command == "agent":
            if args.agent_command == "list":
                payload = {"status": "ready", "agents": manager.list_agents()}
            elif args.agent_command == "set":
                payload = manager.set_agent(_agent_from_args(args))
            elif args.agent_command == "remove":
                payload = manager.remove_agent(args.name)
            else:
                raise ManagerError("invalid_command", "Unknown agent command.")
        elif args.command == "route":
            payload = manager.route(set(args.capability), high_risk=args.high_risk)
        elif args.command == "test":
            payload = manager.test()
        elif args.command == "disable":
            payload = manager.disable()
        elif args.command == "enable":
            payload = manager.enable()
        elif args.command == "uninstall":
            payload = manager.uninstall(remove_credential=args.remove_credential)
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
