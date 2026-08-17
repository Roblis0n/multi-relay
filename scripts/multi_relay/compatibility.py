"""Disposable-home compatibility probes for native Codex fan-out."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .credentials import provider_auth_command
from .branding import CLI_NAME
from .errors import ManagerError
from .model_capabilities import EFFORT_PREFERENCE, ModelSelection
from .paths import resolve_paths
from .toml_config import apply_codex_config


@dataclass(frozen=True)
class CompatibilityReport:
    model: str
    effort: str | None
    provider_initialized: bool
    single_child_passed: bool | None
    fanout_passed: bool | None
    tools_passed: bool | None
    resume_passed: bool | None
    child_metadata_passed: bool | None
    parent_unchanged: bool | None
    legacy_assets: tuple[str, ...] = ()
    migration_actions: tuple[str, ...] = ()

    def as_checks(self) -> dict[str, bool]:
        values = asdict(self)
        return {
            key: bool(value)
            for key, value in values.items()
            if key not in {"model", "effort", "legacy_assets", "migration_actions"}
            and value is not None
        }


def _run_process(
    command: list[str],
    home: Path,
    runner: Callable[..., Any],
    timeout: int,
) -> Any:
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(home)
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ManagerError(
            "codex_probe_failed",
            "The isolated Codex compatibility process could not run.",
        ) from None
    if getattr(completed, "returncode", 1) != 0:
        stderr = str(getattr(completed, "stderr", "")).casefold()
        if "model_catalog_json" in stderr or "model catalog" in stderr:
            raise ManagerError(
                "unsupported_live_catalog",
                "This Codex runtime requires a live model catalog override for the custom provider.",
            )
        raise ManagerError(
            "codex_probe_failed",
            "The isolated Codex compatibility process rejected the configuration.",
            {"returncode": int(getattr(completed, "returncode", 1))},
        )
    return completed


def _direct_command(
    codex_bin: str,
    home: Path,
    model: str,
    effort: str | None,
    token: str,
) -> list[str]:
    command = [
        codex_bin,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "-s",
        "read-only",
        "-C",
        str(home),
        "-m",
        model,
        "-c",
        'model_provider="deepseek"',
    ]
    if effort is not None:
        command.extend(("-c", f'model_reasoning_effort="{effort}"'))
    command.append(f"Reply exactly {token} and nothing else.")
    return command


def probe_efforts(
    codex_bin: str,
    isolated_home: Path,
    model: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> ModelSelection:
    """Empirically choose the highest effort accepted by Codex and DeepSeek."""

    success_marker = "DEEPSEEK_EFFORT_OK"
    for effort in (*EFFORT_PREFERENCE, None):
        command = _direct_command(codex_bin, isolated_home, model, effort, success_marker)
        environment = dict(os.environ)
        environment["CODEX_HOME"] = str(isolated_home)
        try:
            completed = runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if getattr(completed, "returncode", 1) == 0 and success_marker in str(
            getattr(completed, "stdout", "")
        ):
            return ModelSelection(
                requested_model=model,
                resolved_model=model,
                reasoning_effort=effort,
                effort_source=(
                    "empirical_codex_provider_probe" if effort is not None else "provider_default"
                ),
            )
    raise ManagerError(
        "reasoning_probe_failed",
        "No DeepSeek reasoning setting completed the isolated Codex probe.",
    )


def _isolated_parent_config(selection: ModelSelection) -> tuple[str, dict[str, Any]]:
    """Build a disposable DeepSeek parent that needs no copied OpenAI credential."""

    parent = {
        "model": selection.resolved_model,
        "model_provider": "deepseek",
        "reasoning_effort": selection.reasoning_effort,
    }
    lines = [
        f"model = {json.dumps(parent['model'])}",
        f"model_provider = {json.dumps(parent['model_provider'])}",
    ]
    if parent["reasoning_effort"] is not None:
        lines.append(
            f"model_reasoning_effort = {json.dumps(parent['reasoning_effort'])}"
        )
    return "\n".join(lines) + "\n", parent


def run_isolated_gate(
    codex_bin: str,
    original_home: Path,
    selection: ModelSelection,
    *,
    auth_command: list[str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> CompatibilityReport:
    """Probe the provider in a disposable home before the transactional install.

    Native child, fan-out, tool, resume, and metadata checks run once against the
    installed candidate. That authoritative post-install check uses the user's
    real Codex parent and rolls the transaction back on any failure.
    """

    config_path = original_home / "config.toml"
    if not config_path.is_file():
        raise ManagerError("config_missing", "Codex config.toml was not found.")
    minimal_parent, parent = _isolated_parent_config(selection)
    with tempfile.TemporaryDirectory(prefix=f"{CLI_NAME}-gate-") as directory:
        home = Path(directory).resolve()
        paths = resolve_paths(str(home))
        candidate = apply_codex_config(
            minimal_parent,
            provider_auth_command() if auth_command is None else auth_command,
        )
        paths.config.parent.mkdir(parents=True, exist_ok=True)
        paths.config.write_text(candidate, encoding="utf-8", newline="\n")
        direct = _run_process(
            _direct_command(
                codex_bin,
                home,
                selection.resolved_model,
                selection.reasoning_effort,
                "DEEPSEEK_GATE_DIRECT_OK",
            ),
            home,
            runner,
            180,
        )
        provider_initialized = "DEEPSEEK_GATE_DIRECT_OK" in str(
            getattr(direct, "stdout", "")
        )
        return CompatibilityReport(
            model=selection.resolved_model,
            effort=selection.reasoning_effort,
            provider_initialized=provider_initialized,
            single_child_passed=None,
            fanout_passed=None,
            tools_passed=None,
            resume_passed=None,
            child_metadata_passed=None,
            parent_unchanged=None,
        )
