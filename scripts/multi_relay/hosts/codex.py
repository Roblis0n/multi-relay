"""Ownership-safe Codex host adapter for Multi Relay target pools."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..catalog import Catalog
from ..credentials import gateway_auth_command
from ..errors import ManagerError
from ..instructions import (
    INSTRUCTIONS_BEGIN,
    LEGACY_INSTRUCTION_MARKERS,
    apply_fanout_instructions,
    remove_fanout_instructions,
)
from ..paths import Paths
from ..roles import expected_agent_files
from ..toml_config import (
    LEGACY_PROVIDER_MARKERS,
    PROVIDER_BEGIN,
    apply_codex_config,
    capture_managed_values,
    remove_codex_config,
)
from ..transaction import InstallPlan, execute_install_plan
from . import HostPlan


HOST_MANIFEST_SCHEMA = 1


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes() if path.is_file() else b""
    except OSError:
        raise ManagerError("host_read_failed", f"Could not read Codex host file: {path}") from None


def _decode(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ManagerError("invalid_config", f"Codex host file is not UTF-8: {path}") from None


class CodexHostAdapter:
    """Render and manage only the files owned by the Codex integration."""

    name = "codex"

    def __init__(
        self,
        paths: Paths,
        *,
        auth_command_factory: Callable[[str, bool], list[str]] | None = None,
    ) -> None:
        self.paths = paths
        self.manifest_path = paths.codex_host_manifest
        self._auth_command_factory = auth_command_factory or (
            lambda provider_id, start_gateway: gateway_auth_command(paths.home)
        )

    def _read_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.is_file():
            return None
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ManagerError("invalid_manifest", "The Codex host manifest is invalid.") from None
        if not isinstance(value, dict) or value.get("schema_version") != HOST_MANIFEST_SCHEMA:
            raise ManagerError("invalid_manifest", "The Codex host manifest is invalid.")
        return value

    @staticmethod
    def _agent_records(manifest: Mapping[str, Any] | None) -> dict[str, str]:
        raw = manifest.get("agent_files", {}) if manifest else {}
        if not isinstance(raw, dict) or not all(
            isinstance(path, str) and isinstance(digest, str)
            for path, digest in raw.items()
        ):
            raise ManagerError("invalid_manifest", "Codex agent ownership records are invalid.")
        return dict(raw)

    def _assert_agent_write(
        self,
        path: Path,
        previous: Mapping[str, str],
    ) -> None:
        if not path.exists():
            return
        key = str(path)
        expected = previous.get(key)
        if expected is None or _sha256(_read_bytes(path)) != expected:
            raise ManagerError(
                "conflict",
                "A Codex agent file is not safely owned by Multi Relay.",
                {"path": key},
            )

    @staticmethod
    def _assert_block_ownership(
        text: str,
        manifest: Mapping[str, Any] | None,
    ) -> None:
        if manifest is not None:
            return
        if PROVIDER_BEGIN in text or INSTRUCTIONS_BEGIN in text:
            raise ManagerError(
                "conflict",
                "A Multi Relay marker exists without a matching Codex host manifest.",
            )

    def plan(self, catalog: Catalog) -> HostPlan:
        """Pre-render a complete Codex change without writing to disk."""

        host = catalog.hosts.get("codex")
        if host is None or not host.enabled:
            return HostPlan(host=self.name, action="disable", files={})

        manifest = self._read_manifest()
        config_bytes = _read_bytes(self.paths.config)
        instruction_bytes = _read_bytes(self.paths.instruction_file)
        config = _decode(config_bytes, self.paths.config)
        instructions = _decode(instruction_bytes, self.paths.instruction_file)
        self._assert_block_ownership(config, manifest)
        self._assert_block_ownership(instructions, manifest)

        previous_agents = self._agent_records(manifest)
        desired_agents = expected_agent_files(self.paths.agents_dir, catalog)
        for path in desired_agents:
            self._assert_agent_write(path, previous_agents)

        removals: list[Path] = []
        for raw_path, expected in previous_agents.items():
            path = Path(raw_path)
            if path in desired_agents or not path.exists():
                continue
            if _sha256(_read_bytes(path)) != expected:
                raise ManagerError(
                    "conflict",
                    "A stale managed Codex agent was modified and cannot be replaced.",
                    {"path": str(path)},
                )
            removals.append(path)

        rendered_config = apply_codex_config(
            config,
            catalog,
            auth_command_factory=self._auth_command_factory,
        ).encode("utf-8")
        rendered_instructions = apply_fanout_instructions(
            instructions,
            max_children=catalog.concurrency,
            catalog=catalog,
        ).encode("utf-8")
        files: dict[Path, bytes] = {
            self.paths.config: rendered_config,
            self.paths.instruction_file: rendered_instructions,
            **desired_agents,
        }
        snapshot = dict(manifest or {})
        snapshot.update(
            {
                "schema_version": HOST_MANIFEST_SCHEMA,
                "host": self.name,
                "status": "enabled",
                "config_preexisted": (
                    bool(manifest.get("config_preexisted"))
                    if manifest
                    else self.paths.config.is_file()
                ),
                "instruction_file_preexisted": (
                    bool(manifest.get("instruction_file_preexisted"))
                    if manifest
                    else self.paths.instruction_file.is_file()
                ),
                "original_values": (
                    manifest.get("original_values")
                    if manifest
                    else capture_managed_values(config)
                ),
                "agent_files": {
                    str(path): _sha256(data)
                    for path, data in sorted(desired_agents.items(), key=lambda item: str(item[0]))
                },
                "installed": {
                    str(path): _sha256(data)
                    for path, data in sorted(files.items(), key=lambda item: str(item[0]))
                },
            }
        )
        snapshot.pop("backup", None)
        snapshot.pop("transaction_targets", None)
        return HostPlan(
            host=self.name,
            action="apply",
            files=files,
            removals=tuple(removals),
            manifest=snapshot,
        )

    def _backup_dir(self, action: str) -> Path:
        return self.paths.state_dir / "backups" / f"codex-{action}-{time.time_ns()}"

    def _execute(self, plan: HostPlan) -> dict[str, Any]:
        result = execute_install_plan(
            InstallPlan(
                files=dict(plan.files),
                removals=plan.removals,
                manifest=(dict(plan.manifest) if plan.manifest is not None else None),
                backup_dir=self._backup_dir(plan.action),
            ),
            self.manifest_path,
        )
        return {
            "status": "uninstalled" if plan.manifest is None else plan.manifest.get("status"),
            "changed": plan.changed,
            "warnings": list(plan.warnings),
            "backup": str(result.backup_dir),
        }

    def apply(self, catalog: Catalog) -> dict[str, Any]:
        host = catalog.hosts.get("codex")
        if host is None or not host.enabled:
            return self.disable()
        return self._execute(self.plan(catalog))

    def status(self) -> dict[str, Any]:
        manifest = self._read_manifest()
        if manifest is None:
            return {"status": "not_configured", "changed": False, "warnings": []}
        drift = [
            path
            for path, expected in manifest.get("installed", {}).items()
            if not Path(path).is_file() or _sha256(_read_bytes(Path(path))) != expected
        ]
        status = str(manifest.get("status", "partial"))
        if status == "enabled" and drift:
            status = "partial"
        return {
            "status": status,
            "changed": False,
            "warnings": (["Codex managed files have drifted."] if drift else []),
            "details": {"drift": drift},
        }

    def _removal_plan(self, *, uninstall: bool) -> HostPlan:
        manifest = self._read_manifest()
        if manifest is None:
            return HostPlan(
                host=self.name,
                action="uninstall" if uninstall else "disable",
                files={},
                manifest=None,
            )
        files: dict[Path, bytes] = {}
        removals: list[Path] = []
        warnings: list[str] = []

        if self.paths.config.is_file():
            config = _decode(_read_bytes(self.paths.config), self.paths.config)
            restored = remove_codex_config(config, manifest.get("original_values", {}))
            if restored or manifest.get("config_preexisted"):
                files[self.paths.config] = restored.encode("utf-8")
            else:
                removals.append(self.paths.config)
        if self.paths.instruction_file.is_file():
            instructions = _decode(
                _read_bytes(self.paths.instruction_file), self.paths.instruction_file
            )
            restored = remove_fanout_instructions(instructions)
            if restored or manifest.get("instruction_file_preexisted"):
                files[self.paths.instruction_file] = restored.encode("utf-8")
            else:
                removals.append(self.paths.instruction_file)

        for raw_path, expected in self._agent_records(manifest).items():
            path = Path(raw_path)
            if not path.exists():
                continue
            if _sha256(_read_bytes(path)) == expected:
                removals.append(path)
            else:
                warnings.append(f"Retained modified Codex agent: {path}")

        updated: dict[str, Any] | None
        if uninstall:
            updated = None
        else:
            updated = dict(manifest)
            updated["status"] = "disabled"
            updated["installed"] = {}
            updated.pop("backup", None)
            updated.pop("transaction_targets", None)
        return HostPlan(
            host=self.name,
            action="uninstall" if uninstall else "disable",
            files=files,
            removals=tuple(dict.fromkeys(removals)),
            manifest=updated,
            warnings=tuple(warnings),
        )

    def disable(self) -> dict[str, Any]:
        manifest = self._read_manifest()
        if manifest is None:
            return {"status": "disabled", "changed": False, "warnings": []}
        if manifest.get("status") == "disabled":
            return {"status": "disabled", "changed": False, "warnings": []}
        return self._execute(self._removal_plan(uninstall=False))

    def enable(self, catalog: Catalog) -> dict[str, Any]:
        return self.apply(catalog)

    def uninstall(self) -> dict[str, Any]:
        if self._read_manifest() is None:
            return {"status": "uninstalled", "changed": False, "warnings": []}
        return self._execute(self._removal_plan(uninstall=True))


__all__ = ["CodexHostAdapter", "HOST_MANIFEST_SCHEMA"]
