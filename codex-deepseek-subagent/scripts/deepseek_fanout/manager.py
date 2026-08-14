"""Lifecycle orchestration for native DeepSeek child fan-out."""

from __future__ import annotations

import hashlib
import json
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge import BRIDGE_BASE_URL, stop_bridge
from .compatibility import CompatibilityReport, probe_efforts, run_isolated_gate
from .credentials import CredentialStore, credential_store, provider_auth_command
from .errors import ManagerError
from .instructions import (
    INSTRUCTIONS_BEGIN,
    INSTRUCTIONS_END,
    apply_fanout_instructions,
    remove_fanout_instructions,
)
from .model_capabilities import ModelSelection
from .migration import LegacyMigration, plan_legacy_migration
from .native_test import native_acceptance_report
from .paths import Paths
from .provider_api import discover_model
from .roles import ROLE_NAMES, expected_agent_files
from .toml_config import (
    apply_codex_config,
    capture_managed_values,
    remove_codex_config,
)
from .transaction import (
    InstallPlan,
    atomic_write,
    execute_install_plan,
    operation_lock,
    rollback_transaction,
)


SCHEMA_VERSION = 4
DEFAULT_CONCURRENCY = 8
_FULL_ACCEPTANCE_CHECKS = {
    "provider_initialized",
    "single_child_passed",
    "fanout_passed",
    "tools_passed",
    "resume_passed",
    "child_metadata_passed",
    "parent_unchanged",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ManagerError("invalid_manifest", "The DeepSeek fan-out manifest is invalid.") from None
    if not isinstance(payload, dict):
        raise ManagerError("invalid_manifest", "The DeepSeek fan-out manifest is invalid.")
    return payload


def _selection_from_manifest(manifest: dict[str, Any]) -> ModelSelection:
    selection = manifest.get("selection")
    if not isinstance(selection, dict):
        raise ManagerError("invalid_manifest", "The manifest has no validated model selection.")
    required = ("requested_model", "resolved_model", "effort_source")
    if not all(isinstance(selection.get(key), str) for key in required):
        raise ManagerError("invalid_manifest", "The validated model selection is incomplete.")
    effort = selection.get("reasoning_effort")
    if effort is not None and not isinstance(effort, str):
        raise ManagerError("invalid_manifest", "The validated reasoning effort is invalid.")
    return ModelSelection(
        requested_model=selection["requested_model"],
        resolved_model=selection["resolved_model"],
        reasoning_effort=effort,
        effort_source=selection["effort_source"],
    )


class FanoutManager:
    def __init__(
        self,
        paths: Paths,
        codex_bin: str,
        *,
        credentials: CredentialStore | None = None,
        model_discoverer: Callable[[str], str] = discover_model,
        selection_resolver: Callable[[str], ModelSelection] | None = None,
        compatibility_gate: Callable[
            [str, Path, ModelSelection], CompatibilityReport
        ] = run_isolated_gate,
        live_acceptance: Callable[
            [str, Path, ModelSelection], CompatibilityReport
        ] = native_acceptance_report,
        bridge_stopper: Callable[[], bool] = stop_bridge,
    ) -> None:
        self.paths = paths
        self.codex_bin = codex_bin
        self.credentials = credentials or credential_store()
        self._model_discoverer = model_discoverer
        self._selection_resolver = selection_resolver
        self._compatibility_gate = compatibility_gate
        self._live_acceptance = live_acceptance
        self._bridge_stopper = bridge_stopper

    @property
    def _lock_path(self) -> Path:
        return self.paths.state_dir / "manager.lock"

    def _backup_dir(self, operation: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-%fZ")
        return self.paths.state_dir / "backups" / f"{stamp}-{operation}"

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.paths.home).as_posix()
        except ValueError:
            raise ManagerError("unsafe_target", "A managed path escaped Codex Home.") from None

    def _default_selection(self, model: str) -> ModelSelection:
        with tempfile.TemporaryDirectory(prefix="codex-deepseek-effort-") as directory:
            home = Path(directory).resolve()
            (home / "config.toml").write_text(
                apply_codex_config("", provider_auth_command()),
                encoding="utf-8",
                newline="\n",
            )
            return probe_efforts(self.codex_bin, home, model)

    def _resolve_selection(self, model: str) -> ModelSelection:
        resolver = self._selection_resolver or self._default_selection
        selection = resolver(model)
        if selection.resolved_model != model:
            raise ManagerError(
                "model_selection_mismatch",
                "The reasoning probe returned a different DeepSeek model.",
            )
        return selection

    @staticmethod
    def _require_report(
        report: CompatibilityReport,
        stage: str,
        *,
        require_full: bool = False,
    ) -> None:
        checks = report.as_checks()
        failed = [name for name, passed in checks.items() if not passed]
        if require_full:
            failed.extend(sorted(_FULL_ACCEPTANCE_CHECKS.difference(checks)))
        if failed:
            raise ManagerError(
                "compatibility_failed",
                f"DeepSeek fan-out failed the {stage} compatibility checks.",
                {"failed_checks": failed},
            )

    def _assert_instruction_markers(self, text: str) -> None:
        if (INSTRUCTIONS_BEGIN in text) != (INSTRUCTIONS_END in text):
            raise ManagerError(
                "conflict",
                "AGENTS.md contains an incomplete DeepSeek fan-out managed block.",
                {"path": str(self.paths.instruction_file)},
            )

    def _assert_role_ownership(
        self,
        desired: dict[Path, bytes],
        manifest: dict[str, Any],
    ) -> None:
        managed = manifest.get("managed_files")
        managed_hashes = managed if isinstance(managed, dict) else {}
        conflicts: list[str] = []
        for path, content in desired.items():
            if not path.exists():
                continue
            if not path.is_file():
                conflicts.append(str(path))
                continue
            current = path.read_bytes()
            if current == content:
                continue
            previous_hash = managed_hashes.get(self._relative(path))
            if not isinstance(previous_hash, str) or _sha256(current) != previous_hash:
                conflicts.append(str(path))
        if conflicts:
            raise ManagerError(
                "conflict",
                "Existing user-owned Codex role files differ from the DeepSeek fan-out roles.",
                {"paths": conflicts},
            )

    def _assert_removal_ownership(self, manifest: dict[str, Any]) -> tuple[Path, ...]:
        managed = manifest.get("managed_files")
        if not isinstance(managed, dict):
            raise ManagerError("invalid_manifest", "Managed role ownership is missing.")
        removals: list[Path] = []
        conflicts: list[str] = []
        for role in ROLE_NAMES:
            path = self.paths.agents_dir / f"{role}.toml"
            if not path.exists():
                continue
            expected_hash = managed.get(self._relative(path))
            if (
                not path.is_file()
                or not isinstance(expected_hash, str)
                or _sha256(path.read_bytes()) != expected_hash
            ):
                conflicts.append(str(path))
            else:
                removals.append(path)
        if conflicts:
            raise ManagerError(
                "conflict",
                "A managed role was changed after setup; it was not removed.",
                {"paths": conflicts},
            )
        return tuple(removals)

    def _manifest(
        self,
        selection: ModelSelection,
        original_values: dict[str, Any],
        desired_roles: dict[Path, bytes],
        gate: CompatibilityReport,
        *,
        instruction_file_preexisted: bool,
        config_preexisted: bool,
        status: str = "enabled",
        legacy_migrated: bool = False,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "selection": asdict(selection),
            "concurrency": DEFAULT_CONCURRENCY,
            "original_values": original_values,
            "instruction_file_preexisted": instruction_file_preexisted,
            "config_preexisted": config_preexisted,
            "managed_files": {
                self._relative(path): _sha256(content)
                for path, content in desired_roles.items()
            },
            "preinstall_compatibility": gate.as_checks(),
            "compatibility": gate.as_checks(),
            "legacy_migrated": legacy_migrated,
        }

    def setup(self) -> dict[str, Any]:
        with operation_lock(self._lock_path):
            secret = self.credentials.read()
            if not secret:
                raise ManagerError(
                    "credential_missing",
                    "No DeepSeek API Key is stored in the operating-system credential vault.",
                )
            if not self.paths.config.is_file():
                raise ManagerError("config_missing", "Codex config.toml was not found.")
            original_config = self.paths.config.read_text(encoding="utf-8")
            previous_manifest = _read_json(self.paths.manifest)
            migration: LegacyMigration | None = None
            if previous_manifest and previous_manifest.get("schema_version") != SCHEMA_VERSION:
                migration = plan_legacy_migration(
                    self.paths,
                    original_config,
                    previous_manifest,
                )
            working_config = migration.config_text if migration else original_config

            model = self._model_discoverer(secret)
            selection = self._resolve_selection(model)
            desired_roles = expected_agent_files(self.paths.agents_dir, selection)
            self._assert_role_ownership(desired_roles, previous_manifest)
            original_instructions = (
                self.paths.instruction_file.read_text(encoding="utf-8")
                if self.paths.instruction_file.is_file()
                else ""
            )
            self._assert_instruction_markers(original_instructions)
            candidate_instructions = apply_fanout_instructions(
                original_instructions,
                DEFAULT_CONCURRENCY,
            )
            try:
                candidate_config = apply_codex_config(
                    working_config,
                    provider_auth_command(),
                    DEFAULT_CONCURRENCY,
                )
            except ManagerError as exc:
                if exc.code == "invalid_config" and "model_providers" in original_config:
                    raise ManagerError(
                        "conflict",
                        "An unmanaged DeepSeek provider conflicts with the managed provider.",
                    ) from None
                raise

            gate = self._compatibility_gate(self.codex_bin, self.paths.home, selection)
            self._require_report(gate, "isolated pre-install")

            stored_original_values = previous_manifest.get("original_values")
            original_values: dict[str, Any]
            if isinstance(stored_original_values, dict):
                original_values = stored_original_values
            else:
                original_values = capture_managed_values(working_config)
            instruction_preexisted = bool(
                previous_manifest.get(
                    "instruction_file_preexisted",
                    self.paths.instruction_file.is_file(),
                )
            )
            config_preexisted = bool(
                previous_manifest.get("config_preexisted", self.paths.config.is_file())
            )
            manifest = self._manifest(
                selection,
                original_values,
                desired_roles,
                gate,
                instruction_file_preexisted=instruction_preexisted,
                config_preexisted=config_preexisted,
                legacy_migrated=bool(
                    migration or previous_manifest.get("legacy_migrated", False)
                ),
            )
            files = {
                self.paths.config: candidate_config.encode("utf-8"),
                self.paths.instruction_file: candidate_instructions.encode("utf-8"),
                **(migration.files if migration else {}),
                **desired_roles,
            }
            transaction = execute_install_plan(
                InstallPlan(
                    files=files,
                    removals=migration.removals if migration else (),
                    manifest=manifest,
                    backup_dir=self._backup_dir("setup"),
                ),
                self.paths.manifest,
            )
            try:
                live = self._live_acceptance(self.codex_bin, self.paths.home, selection)
                self._require_report(live, "post-install native", require_full=True)
                installed_manifest = _read_json(self.paths.manifest)
                installed_manifest["preinstall_compatibility"] = gate.as_checks()
                installed_manifest["compatibility"] = live.as_checks()
                atomic_write(
                    self.paths.manifest,
                    (
                        json.dumps(installed_manifest, ensure_ascii=False, indent=2)
                        + "\n"
                    ).encode("utf-8"),
                    0o600,
                )
            except Exception:
                rollback_transaction(transaction)
                raise
            return {
                "status": "ready",
                "model": selection.resolved_model,
                "reasoning_effort": selection.reasoning_effort,
                "roles": list(ROLE_NAMES),
                "max_concurrent_children": DEFAULT_CONCURRENCY,
                "backup": str(transaction.backup_dir),
            }

    def status(self) -> dict[str, Any]:
        manifest = _read_json(self.paths.manifest)
        credential_present = False
        try:
            credential_present = self.credentials.exists()
        except ManagerError:
            pass
        if not manifest:
            return {
                "status": "not_configured",
                "credential_present": credential_present,
            }
        if manifest.get("schema_version") != SCHEMA_VERSION:
            return {"status": "legacy", "credential_present": credential_present}
        if manifest.get("status") == "disabled":
            return {"status": "disabled", "credential_present": credential_present}
        checks: dict[str, bool] = {"credential_present": credential_present}
        try:
            selection = _selection_from_manifest(manifest)
            config = tomllib.loads(self.paths.config.read_text(encoding="utf-8"))
            provider = (config.get("model_providers") or {}).get("deepseek")
            agents = config.get("agents") or {}
            checks["provider"] = (
                isinstance(provider, dict)
                and provider.get("wire_api") == "responses"
                and provider.get("base_url") == BRIDGE_BASE_URL
            )
            features = config.get("features") or {}
            v2 = features.get("multi_agent_v2") if isinstance(features, dict) else None
            checks["v2_routing"] = (
                isinstance(v2, dict)
                and v2.get("enabled") is True
                and v2.get("hide_spawn_agent_metadata") is False
                and v2.get("tool_namespace") == "agents"
            )
            v2_limit = (
                v2.get("max_concurrent_threads_per_session")
                if isinstance(v2, dict)
                else None
            )
            checks["v2_concurrency"] = (
                isinstance(v2_limit, int)
                and not isinstance(v2_limit, bool)
                and v2_limit >= 8
            )
            checks["agents_enabled"] = agents.get("enabled") is True
            limit = agents.get("max_concurrent_threads_per_session")
            checks["concurrency"] = isinstance(limit, int) and not isinstance(limit, bool) and limit >= 8
            for role in ROLE_NAMES:
                path = self.paths.agents_dir / f"{role}.toml"
                parsed = tomllib.loads(path.read_text(encoding="utf-8"))
                checks[f"role_{role}"] = (
                    parsed.get("model_provider") == "deepseek"
                    and parsed.get("model") == selection.resolved_model
                    and (
                        selection.reasoning_effort is None
                        or parsed.get("model_reasoning_effort") == selection.reasoning_effort
                    )
                )
            instructions = self.paths.instruction_file.read_text(encoding="utf-8")
            checks["fanout_instructions"] = (
                INSTRUCTIONS_BEGIN in instructions and INSTRUCTIONS_END in instructions
            )
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ManagerError):
            checks["readable_configuration"] = False
        return {
            "status": "ready" if checks and all(checks.values()) else "partial",
            "checks": checks,
        }

    def test(self) -> dict[str, Any]:
        manifest = _read_json(self.paths.manifest)
        if not manifest or manifest.get("status") != "enabled":
            raise ManagerError("not_configured", "DeepSeek fan-out is not enabled.")
        selection = _selection_from_manifest(manifest)
        report = self._live_acceptance(self.codex_bin, self.paths.home, selection)
        self._require_report(report, "native")
        return {"status": "ready", "checks": report.as_checks()}

    def disable(self) -> dict[str, Any]:
        with operation_lock(self._lock_path):
            manifest = _read_json(self.paths.manifest)
            if not manifest:
                raise ManagerError("not_configured", "DeepSeek fan-out is not installed.")
            if manifest.get("status") == "disabled":
                return {"status": "disabled"}
            removals = list(self._assert_removal_ownership(manifest))
            instructions = (
                self.paths.instruction_file.read_text(encoding="utf-8")
                if self.paths.instruction_file.is_file()
                else ""
            )
            self._assert_instruction_markers(instructions)
            unmanaged = remove_fanout_instructions(instructions)
            files: dict[Path, bytes] = {}
            if unmanaged or manifest.get("instruction_file_preexisted"):
                files[self.paths.instruction_file] = unmanaged.encode("utf-8")
            elif self.paths.instruction_file.exists():
                removals.append(self.paths.instruction_file)
            updated_manifest = dict(manifest)
            updated_manifest.pop("backup", None)
            updated_manifest.pop("transaction_targets", None)
            updated_manifest["status"] = "disabled"
            execute_install_plan(
                InstallPlan(
                    files=files,
                    removals=tuple(removals),
                    manifest=updated_manifest,
                    backup_dir=self._backup_dir("disable"),
                ),
                self.paths.manifest,
            )
            return {"status": "disabled"}

    def enable(self) -> dict[str, Any]:
        with operation_lock(self._lock_path):
            manifest = _read_json(self.paths.manifest)
            if not manifest:
                raise ManagerError("not_configured", "DeepSeek fan-out is not installed.")
            if manifest.get("status") == "enabled":
                return self.status()
            selection = _selection_from_manifest(manifest)
            desired_roles = expected_agent_files(self.paths.agents_dir, selection)
            self._assert_role_ownership(desired_roles, manifest)
            instructions = (
                self.paths.instruction_file.read_text(encoding="utf-8")
                if self.paths.instruction_file.is_file()
                else ""
            )
            self._assert_instruction_markers(instructions)
            files = {
                **desired_roles,
                self.paths.instruction_file: apply_fanout_instructions(
                    instructions, DEFAULT_CONCURRENCY
                ).encode("utf-8"),
            }
            updated_manifest = dict(manifest)
            updated_manifest.pop("backup", None)
            updated_manifest.pop("transaction_targets", None)
            updated_manifest["status"] = "enabled"
            execute_install_plan(
                InstallPlan(
                    files=files,
                    removals=(),
                    manifest=updated_manifest,
                    backup_dir=self._backup_dir("enable"),
                ),
                self.paths.manifest,
            )
            return self.status()

    def uninstall(self, remove_credential: bool = False) -> dict[str, Any]:
        with operation_lock(self._lock_path):
            manifest = _read_json(self.paths.manifest)
            if not manifest:
                if remove_credential:
                    self.credentials.remove()
                self._bridge_stopper()
                return {"status": "uninstalled"}
            removals = list(self._assert_removal_ownership(manifest))
            config = self.paths.config.read_text(encoding="utf-8")
            original_values = manifest.get("original_values")
            if not isinstance(original_values, dict):
                raise ManagerError("invalid_manifest", "Original configuration values are missing.")
            unmanaged_config = remove_codex_config(config, original_values)
            instructions = (
                self.paths.instruction_file.read_text(encoding="utf-8")
                if self.paths.instruction_file.is_file()
                else ""
            )
            self._assert_instruction_markers(instructions)
            unmanaged_instructions = remove_fanout_instructions(instructions)
            files: dict[Path, bytes] = {}
            if unmanaged_config or manifest.get("config_preexisted"):
                files[self.paths.config] = unmanaged_config.encode("utf-8")
            elif self.paths.config.exists():
                removals.append(self.paths.config)
            if unmanaged_instructions or manifest.get("instruction_file_preexisted"):
                files[self.paths.instruction_file] = unmanaged_instructions.encode("utf-8")
            elif self.paths.instruction_file.exists():
                removals.append(self.paths.instruction_file)
            transaction = execute_install_plan(
                InstallPlan(
                    files=files,
                    removals=tuple(removals),
                    manifest=None,
                    backup_dir=self._backup_dir("uninstall"),
                ),
                self.paths.manifest,
            )
            if remove_credential:
                self.credentials.remove()
            self._bridge_stopper()
            return {"status": "uninstalled", "backup": str(transaction.backup_dir)}
