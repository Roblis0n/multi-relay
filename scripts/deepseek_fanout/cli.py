"""Command-line entrypoint for secure DeepSeek fan-out lifecycle management."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .credentials import credential_store, prompt_and_store
from .errors import ManagerError
from .manager import FanoutManager
from .paths import resolve_paths


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Emit structured JSON.")
    common.add_argument("--codex-home", help="Target Codex Home; defaults to CODEX_HOME or ~/.codex.")
    common.add_argument("--codex-bin", help="Path to the Codex desktop runtime.")
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-deepseek-relay",
        description="Local Relay for secure native Codex-to-DeepSeek child fan-out.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    common = _common_parser()
    for name, help_text in (
        ("status", "Inspect configuration without changing it."),
        ("setup", "Validate, install, and test DeepSeek fan-out."),
        ("repair", "Re-run validated setup for an existing installation."),
        ("test", "Run native fan-out acceptance checks."),
        ("disable", "Disable managed roles while retaining provider and credential."),
        ("enable", "Re-enable the last validated role selection."),
    ):
        commands.add_parser(name, parents=[common], help=help_text)
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


def _default_manager(args: argparse.Namespace) -> FanoutManager:
    live_command = args.command in {"setup", "repair", "test"}
    paths = resolve_paths(args.codex_home)
    return FanoutManager(
        paths,
        find_codex(args.codex_bin, required=live_command, codex_home=paths.home),
        credentials=credential_store(),
    )


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(payload.get("status", "unknown"))
    for key, value in payload.items():
        if key != "status":
            print(f"{key}: {value}")


def main(
    argv: Sequence[str] | None = None,
    *,
    manager_factory: Callable[[argparse.Namespace], FanoutManager] | None = None,
    prompt_fn: Callable[[str], str] = getpass.getpass,
) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    factory = manager_factory or _default_manager
    try:
        manager = factory(args)
        if args.command in {"setup", "repair"}:
            if not manager.credentials.exists():
                prompt_and_store(manager.credentials, prompt_fn=prompt_fn)
            payload = manager.setup()
        elif args.command == "status":
            payload = manager.status()
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
