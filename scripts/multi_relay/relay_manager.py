"""Transactional lifecycle management for Multi Relay."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bridge import stop_bridge
from .branding import CLI_NAME, PRODUCT_VERSION, REPOSITORY_NAME
from .catalog import (
    AgentSpec,
    Catalog,
    CredentialRef,
    ExecutionTarget,
    ProviderSpec,
    TargetPool,
    default_catalog,
    load_catalog,
    route_agent,
    save_catalog_bytes,
)
from .compatibility import CompatibilityReport, probe_efforts, run_isolated_gate
from .credentials import (
    CredentialStore,
    credential_store,
    credential_target,
    gateway_auth_command,
    legacy_credential_target,
    provider_auth_command,
)
from .errors import ManagerError
from .gateway import GATEWAY_BASE_URL, GatewayController, load_gateway_state
from .instructions import (
    INSTRUCTIONS_BEGIN,
    INSTRUCTIONS_END,
    LEGACY_INSTRUCTION_MARKERS,
    apply_fanout_instructions,
    remove_fanout_instructions,
)
from .migration import (
    CatalogMigrationResult,
    LegacyMigration,
    catalog_from_schema4,
    inspect_catalog_migration,
    plan_legacy_migration,
)
from .model_capabilities import ModelSelection
from .native_test import native_acceptance_report
from .paths import Paths
from .provider_api import discover_model
from .rotation import RotationController, catalog_fingerprint
from .roles import expected_agent_files
from .selection import SelectionRequirements
from .state import RuntimeStateStore
from .toml_config import (
    LEGACY_PROVIDER_MARKERS,
    PROVIDER_BEGIN,
    PROVIDER_END,
    apply_codex_config,
    capture_managed_values,
    remove_codex_config,
)
from .transaction import (
    InstallPlan,
    TransactionResult,
    atomic_write,
    execute_install_plan,
    operation_lock,
    rollback_transaction,
    transaction_target_paths,
)


SCHEMA_VERSION = 5
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


def _outcome(
    status: str,
    *,
    changed: bool,
    details: dict[str, Any] | None = None,
    warnings: Sequence[str] = (),
    next_actions: Sequence[str] = (),
) -> dict[str, Any]:
    """Return the stable secret-free manager result envelope."""

    return {
        "status": status,
        "changed": changed,
        "warnings": list(warnings),
        "details": details or {},
        "next_actions": list(next_actions),
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ManagerError("invalid_manifest", "The Multi Relay manifest is invalid.") from None
    if not isinstance(payload, dict):
        raise ManagerError("invalid_manifest", "The Multi Relay manifest is invalid.")
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


def _has_original_values(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("features"), dict)
        and isinstance(value.get("agents"), dict)
        and isinstance(value.get("features.multi_agent_v2"), dict)
    )


class RelayManager:
    """Own the installed catalog, generated agents, routing policy, and lifecycle."""

    def __init__(
        self,
        paths: Paths,
        codex_bin: str,
        *,
        credentials: CredentialStore | None = None,
        credential_factory: Callable[[ProviderSpec], CredentialStore] | None = None,
        credential_reference_factory: Callable[
            [ProviderSpec, CredentialRef], CredentialStore
        ]
        | None = None,
        model_discoverer: Callable[..., str] = discover_model,
        selection_resolver: Callable[[str], ModelSelection] | None = None,
        compatibility_gate: Callable[
            [str, Path, ModelSelection], CompatibilityReport
        ]
        | None = None,
        live_acceptance: Callable[
            [str, Path, ModelSelection], CompatibilityReport
        ] = native_acceptance_report,
        bridge_stopper: Callable[[], bool] = stop_bridge,
    ) -> None:
        self.paths = paths
        self.codex_bin = codex_bin
        self.credentials = credentials or credential_store()
        self._credential_factory = credential_factory or (
            lambda provider: credential_store(
                provider_id=provider.id,
                protocol=provider.protocol,
            )
        )
        self._credential_reference_factory = credential_reference_factory
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

    def _read_manifest(self) -> tuple[dict[str, Any], Path]:
        candidates = (
            self.paths.manifest,
            self.paths.codex_manifest,
            self.paths.relay_manifest,
            self.paths.legacy_manifest,
        )
        existing = [path for path in candidates if path.exists()]
        if len(existing) > 1:
            hashes = {_sha256(path.read_bytes()) for path in existing if path.is_file()}
            if any(not path.is_file() for path in existing) or len(hashes) != 1:
                raise ManagerError(
                    "state_conflict",
                    "Canonical and legacy Multi Relay state both exist with different manifests.",
                    {"paths": [str(path) for path in existing]},
                )
        if existing:
            path = existing[0]
            return _read_json(path), path
        return {}, self.paths.manifest

    def _catalog_source(self, manifest_source: Path) -> Path:
        if manifest_source == self.paths.manifest:
            return self.paths.catalog
        return manifest_source.parent / "catalog.json"

    def _adoption_removals(self, manifest_source: Path) -> tuple[Path, ...]:
        if manifest_source == self.paths.manifest:
            return ()
        removals = [manifest_source]
        old_catalog = self._catalog_source(manifest_source)
        if old_catalog.exists() and old_catalog != self.paths.catalog:
            removals.append(old_catalog)
        return tuple(removals)

    @staticmethod
    def _require_current_schema(manifest: dict[str, Any]) -> None:
        schema = manifest.get("schema_version")
        if schema == SCHEMA_VERSION:
            return
        if isinstance(schema, int) and not isinstance(schema, bool) and schema > SCHEMA_VERSION:
            raise ManagerError(
                "unsupported_manifest_schema",
                "This installation was created by a newer Multi Relay version.",
                {"schema_version": schema},
            )
        raise ManagerError(
            "legacy_requires_setup",
            "Run setup or repair to migrate this earlier Relay installation first.",
        )

    @staticmethod
    def _reject_future_schema(manifest: dict[str, Any]) -> None:
        schema = manifest.get("schema_version")
        if isinstance(schema, int) and not isinstance(schema, bool) and schema > SCHEMA_VERSION:
            raise ManagerError(
                "unsupported_manifest_schema",
                "This installation was created by a newer Multi Relay version.",
                {"schema_version": schema},
            )

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.paths.home).as_posix()
        except ValueError:
            try:
                relative = path.resolve().relative_to(self.paths.state_dir)
            except ValueError:
                raise ManagerError(
                    "unsafe_target",
                    "A managed path escaped the host and product state roots.",
                ) from None
            return f"@state/{relative.as_posix()}"

    def _default_selection(
        self,
        model: str,
        *,
        auth_command: list[str] | None = None,
    ) -> ModelSelection:
        with tempfile.TemporaryDirectory(prefix=f"{CLI_NAME}-effort-") as directory:
            home = Path(directory).resolve()
            (home / "config.toml").write_text(
                apply_codex_config(
                    "",
                    provider_auth_command() if auth_command is None else auth_command,
                ),
                encoding="utf-8",
                newline="\n",
            )
            return probe_efforts(self.codex_bin, home, model)

    def _resolve_selection(
        self,
        model: str,
        *,
        auth_command: list[str] | None = None,
    ) -> ModelSelection:
        selection = (
            self._selection_resolver(model)
            if self._selection_resolver is not None
            else self._default_selection(model, auth_command=auth_command)
        )
        if selection.resolved_model != model:
            raise ManagerError(
                "model_selection_mismatch",
                "The reasoning probe returned a different provider model.",
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
                f"Multi Relay failed the {stage} compatibility checks.",
                {"failed_checks": sorted(set(failed))},
            )

    def _assert_instruction_markers(self, text: str) -> None:
        for begin, end in ((INSTRUCTIONS_BEGIN, INSTRUCTIONS_END), *LEGACY_INSTRUCTION_MARKERS):
            if (begin in text) != (end in text):
                raise ManagerError(
                    "conflict",
                    "AGENTS.md contains an incomplete managed Relay block.",
                    {"path": str(self.paths.instruction_file)},
                )

    def _assert_no_unowned_managed_blocks(
        self,
        config: str,
        manifest: dict[str, Any],
    ) -> None:
        """Refuse lookalike managed blocks when no manifest proves ownership."""

        if manifest:
            return
        instructions = (
            self.paths.instruction_file.read_text(encoding="utf-8")
            if self.paths.instruction_file.is_file()
            else ""
        )
        provider_markers = ((PROVIDER_BEGIN, PROVIDER_END), *LEGACY_PROVIDER_MARKERS)
        instruction_markers = (
            (INSTRUCTIONS_BEGIN, INSTRUCTIONS_END),
            *LEGACY_INSTRUCTION_MARKERS,
        )
        if any(marker in config for pair in provider_markers for marker in pair) or any(
            marker in instructions for pair in instruction_markers for marker in pair
        ):
            raise ManagerError(
                "conflict",
                "Managed Relay markers exist without a manifest proving ownership.",
            )

    def _managed_agent_entries(self, manifest: dict[str, Any]) -> dict[Path, str]:
        managed = manifest.get("managed_files")
        if not isinstance(managed, dict):
            return {}
        result: dict[Path, str] = {}
        agents_root = self.paths.agents_dir.resolve()
        for relative, digest in managed.items():
            if not isinstance(relative, str) or not isinstance(digest, str):
                raise ManagerError("invalid_manifest", "Managed file ownership is invalid.")
            path = (self.paths.home / relative).resolve()
            try:
                path.relative_to(self.paths.home)
                inside = path.parent == agents_root and path.suffix.casefold() == ".toml"
            except ValueError:
                inside = False
            if inside:
                result[path] = digest
        return result

    def _assert_role_ownership(
        self,
        desired: dict[Path, bytes],
        manifest: dict[str, Any],
    ) -> None:
        managed = self._managed_agent_entries(manifest)
        conflicts: list[str] = []
        for raw_path, content in desired.items():
            path = raw_path.resolve()
            if not path.exists():
                continue
            if not path.is_file():
                conflicts.append(str(path))
                continue
            current = path.read_bytes()
            if current == content:
                continue
            previous_hash = managed.get(path)
            if previous_hash is None or _sha256(current) != previous_hash:
                conflicts.append(str(path))
        if conflicts:
            raise ManagerError(
                "conflict",
                "Existing user-owned Codex agent files differ from the managed catalog.",
                {"paths": conflicts},
            )

    def _agent_removals(
        self,
        manifest: dict[str, Any],
        *,
        keep: set[Path] | None = None,
    ) -> tuple[Path, ...]:
        keep_resolved = {path.resolve() for path in (keep or set())}
        removals: list[Path] = []
        conflicts: list[str] = []
        for path, digest in self._managed_agent_entries(manifest).items():
            if path in keep_resolved or not path.exists():
                continue
            if not path.is_file() or _sha256(path.read_bytes()) != digest:
                conflicts.append(str(path))
            else:
                removals.append(path)
        if conflicts:
            raise ManagerError(
                "conflict",
                "A managed agent changed after setup and was not removed.",
                {"paths": conflicts},
            )
        return tuple(removals)

    def _require_catalog_owned(self, manifest: dict[str, Any], catalog_path: Path) -> None:
        expected = manifest.get("catalog_sha256")
        if not isinstance(expected, str):
            managed = manifest.get("managed_files")
            if isinstance(managed, dict):
                expected = managed.get(self._relative(catalog_path))
        if (
            not catalog_path.is_file()
            or not isinstance(expected, str)
            or _sha256(catalog_path.read_bytes()) != expected
        ):
            raise ManagerError(
                "conflict",
                "The managed catalog changed outside an explicit apply operation.",
                {"path": str(catalog_path)},
            )

    def _credential_for(self, provider: ProviderSpec) -> CredentialStore:
        if provider.id == "deepseek":
            return self.credentials
        return self._credential_factory(provider)

    @staticmethod
    def _credential_reference(catalog: Catalog, provider: ProviderSpec) -> CredentialRef:
        enabled = [
            item
            for item in catalog.credentials
            if item.provider_id == provider.id and item.enabled
        ]
        primary = [item for item in enabled if item.id == "primary"]
        if len(primary) == 1:
            return primary[0]
        if len(enabled) == 1:
            return enabled[0]
        if not enabled:
            raise ManagerError(
                "credential_required",
                f"Provider {provider.id} has no enabled credential reference.",
                {"provider": provider.id},
            )
        raise ManagerError(
            "ambiguous_credential",
            f"Provider {provider.id} has multiple enabled credentials and no primary reference.",
            {"provider": provider.id},
        )

    def _credential_for_reference(
        self,
        provider: ProviderSpec,
        reference: CredentialRef,
    ) -> CredentialStore:
        if self._credential_reference_factory is not None:
            return self._credential_reference_factory(provider, reference)
        return credential_store(
            provider_id=provider.id,
            credential_id=reference.id,
            protocol=provider.protocol,
            vault_target=reference.vault_target,
            label=reference.label,
        )

    def _provider_auth_command(
        self,
        catalog: Catalog,
        provider: ProviderSpec,
        start_bridge: bool,
    ) -> list[str]:
        reference = self._credential_reference(catalog, provider)
        return provider_auth_command(
            provider.id,
            self.paths.home,
            start_bridge,
            credential_id=reference.id,
            protocol=provider.protocol,
            vault_target=reference.vault_target,
        )

    def credential_for_provider(
        self,
        provider: ProviderSpec,
        *,
        catalog: Catalog | None = None,
    ) -> CredentialStore:
        """Expose the scoped vault selected for an already validated provider."""

        if catalog is not None and provider.auth_mode == "vault":
            reference = self._credential_reference(catalog, provider)
            return self._credential_for_reference(provider, reference)
        return self._credential_for(provider)

    def _auth_factory(self, catalog: Catalog) -> Callable[[str, bool], list[str]]:
        def command(provider_id: str, start_bridge: bool) -> list[str]:
            if provider_id == "local-gateway":
                return gateway_auth_command(self.paths.home)
            provider = catalog.provider(provider_id)
            return self._provider_auth_command(catalog, provider, start_bridge)

        return command

    def _discover_builtin_selection(
        self,
        catalog: Catalog,
    ) -> tuple[Catalog, ModelSelection | None, CompatibilityReport | None]:
        providers = [
            item
            for item in catalog.providers
            if item.enabled and item.id == "deepseek" and item.protocol == "deepseek-chat"
        ]
        if not providers:
            return catalog, None, None
        provider = providers[0]
        reference = self._credential_reference(catalog, provider)
        secret = self._credential_for_reference(provider, reference).read()
        if not secret:
            raise ManagerError(
                "credential_missing",
                "No DeepSeek API Key is stored in the operating-system credential vault.",
                {"provider": provider.id},
            )
        requested_models = {
            item.model for item in catalog.agents if item.provider == provider.id and item.model
        }
        if len(requested_models) != 1:
            raise ManagerError(
                "invalid_model",
                "The built-in DeepSeek preset must use one validated model.",
            )
        requested = next(iter(requested_models))
        if self._model_discoverer is discover_model:
            model = discover_model(secret, requested, provider=provider)
        else:
            model = self._model_discoverer(secret)
        auth_command = self._provider_auth_command(catalog, provider, True)
        selection = self._resolve_selection(model, auth_command=auth_command)
        payload = catalog.to_dict()
        selected_target_ids: set[str] = set()
        for target in payload["targets"]:
            if (
                isinstance(target, dict)
                and target.get("provider_id") == provider.id
                and target.get("model") == requested
            ):
                target["model"] = selection.resolved_model
                if selection.reasoning_effort is not None:
                    efforts = target.get("reasoning_efforts")
                    if (
                        isinstance(efforts, list)
                        and selection.reasoning_effort not in efforts
                    ):
                        efforts.append(selection.reasoning_effort)
                target_id = target.get("id")
                if isinstance(target_id, str):
                    selected_target_ids.add(target_id)
        selected_pool_ids = {
            pool.get("id")
            for pool in payload["pools"]
            if isinstance(pool, dict)
            and isinstance(pool.get("targets"), list)
            and selected_target_ids.intersection(pool["targets"])
        }
        for agent in payload["agents"]:
            if (
                isinstance(agent, dict)
                and agent.get("pool_id") in selected_pool_ids
            ):
                agent["reasoning_effort"] = selection.reasoning_effort
        selected_catalog = Catalog.from_dict(payload)
        gate = (
            run_isolated_gate(
                self.codex_bin,
                self.paths.home,
                selection,
                auth_command=auth_command,
            )
            if self._compatibility_gate is None
            else self._compatibility_gate(self.codex_bin, self.paths.home, selection)
        )
        self._require_report(gate, "isolated pre-install")
        return selected_catalog, selection, gate

    def _manifest_payload(
        self,
        catalog: Catalog,
        catalog_bytes: bytes,
        desired_roles: dict[Path, bytes],
        original_values: dict[str, Any],
        compatibility: dict[str, bool],
        *,
        previous: dict[str, Any],
        selection: ModelSelection | None,
        instruction_file_preexisted: bool,
        config_preexisted: bool,
        status: str = "enabled",
        legacy_migrated: bool = False,
    ) -> dict[str, Any]:
        managed = {
            self._relative(path): _sha256(content)
            for path, content in desired_roles.items()
        }
        managed[self._relative(self.paths.catalog)] = _sha256(catalog_bytes)
        payload: dict[str, Any] = {
            "product": REPOSITORY_NAME,
            "product_version": PRODUCT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "catalog_schema_version": catalog.schema_version,
            "status": status,
            "installed_at": previous.get("installed_at")
            if isinstance(previous.get("installed_at"), str)
            else datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "concurrency": catalog.concurrency,
            "providers": [item.id for item in catalog.providers],
            "agents": [item.name for item in catalog.agents],
            "catalog_sha256": _sha256(catalog_bytes),
            "original_values": original_values,
            "instruction_file_preexisted": instruction_file_preexisted,
            "config_preexisted": config_preexisted,
            "managed_files": managed,
            "preinstall_compatibility": compatibility,
            "compatibility": compatibility,
            "legacy_migrated": legacy_migrated,
        }
        if selection is not None:
            payload["selection"] = asdict(selection)
        for key in (
            "catalog_source_schema",
            "catalog_source_sha256",
            "catalog_migration_backup",
        ):
            if key in previous:
                payload[key] = previous[key]
        return payload

    def _record_catalog_migration(
        self,
        manifest: dict[str, Any],
        files: dict[Path, bytes],
        removals: list[Path] | tuple[Path, ...],
        backup_dir: Path,
        migration: CatalogMigrationResult | None,
        source: Path | None,
    ) -> Path | None:
        if migration is None or not migration.changed:
            return None
        migration_source = source or self.paths.catalog
        transaction_targets = transaction_target_paths(
            files,
            removals,
            self.paths.manifest,
        )
        try:
            source_index = transaction_targets.index(migration_source)
        except ValueError:
            raise ManagerError(
                "catalog_migration_failed",
                "The schema 1 catalog was not included in the migration transaction.",
                {"path": str(migration_source)},
            ) from None
        migration_backup = backup_dir / f"{source_index:04d}.bin"
        manifest["catalog_source_schema"] = migration.source_schema
        manifest["catalog_source_sha256"] = migration.source_sha256
        manifest["catalog_migration_backup"] = str(migration_backup)
        return migration_backup

    @staticmethod
    def _verify_catalog_migration_backup(
        transaction: TransactionResult,
        migration: CatalogMigrationResult | None,
        backup: Path | None,
    ) -> None:
        if migration is None or not migration.changed or backup is None:
            return
        try:
            backup_hash = _sha256(backup.read_bytes())
        except OSError:
            rollback_transaction(transaction)
            raise ManagerError(
                "catalog_backup_failed",
                "The schema 1 catalog backup could not be verified; the previous state was restored.",
                {"backup": str(backup)},
            ) from None
        if backup_hash != migration.source_sha256:
            rollback_transaction(transaction)
            raise ManagerError(
                "catalog_backup_failed",
                "The schema 1 catalog backup did not match its source; the previous state was restored.",
                {"backup": str(backup)},
            )

    def _install_catalog_locked(
        self,
        catalog: Catalog,
        *,
        operation: str,
        original_config: str,
        working_config: str,
        previous_manifest: dict[str, Any],
        manifest_source: Path,
        migration: LegacyMigration | None = None,
        selection: ModelSelection | None = None,
        gate: CompatibilityReport | None = None,
        run_live: bool = False,
        catalog_migration: CatalogMigrationResult | None = None,
        catalog_migration_source: Path | None = None,
    ) -> dict[str, Any]:
        installation_status = (
            "disabled" if previous_manifest.get("status") == "disabled" else "enabled"
        )
        enabled = installation_status == "enabled"
        desired_roles = expected_agent_files(self.paths.agents_dir, catalog)
        self._assert_role_ownership(desired_roles, previous_manifest)
        original_instructions = (
            self.paths.instruction_file.read_text(encoding="utf-8")
            if self.paths.instruction_file.is_file()
            else ""
        )
        self._assert_instruction_markers(original_instructions)
        candidate_instructions = (
            apply_fanout_instructions(
                original_instructions,
                catalog.concurrency,
                catalog=catalog,
            )
            if enabled
            else remove_fanout_instructions(original_instructions)
        )
        try:
            candidate_config = apply_codex_config(
                working_config,
                catalog,
                auth_command_factory=self._auth_factory(catalog),
            )
        except ManagerError as exc:
            if exc.code == "invalid_config" and "model_providers" in original_config:
                raise ManagerError(
                    "conflict",
                    "An unmanaged provider conflicts with a managed provider identifier.",
                ) from None
            raise

        stored_values = previous_manifest.get("original_values")
        original_values = (
            stored_values
            if _has_original_values(stored_values)
            else capture_managed_values(working_config)
        )
        instruction_preexisted = bool(
            previous_manifest.get(
                "instruction_file_preexisted",
                self.paths.instruction_file.is_file(),
            )
        )
        config_preexisted = bool(
            previous_manifest.get("config_preexisted", self.paths.config.is_file())
        )
        compatibility = gate.as_checks() if gate is not None else {"catalog_valid": True}
        catalog_bytes = save_catalog_bytes(catalog)
        manifest = self._manifest_payload(
            catalog,
            catalog_bytes,
            desired_roles,
            original_values,
            compatibility,
            previous=previous_manifest,
            selection=selection,
            instruction_file_preexisted=instruction_preexisted,
            config_preexisted=config_preexisted,
            status=installation_status,
            legacy_migrated=bool(
                migration or previous_manifest.get("legacy_migrated", False)
            ),
        )
        files = {
            self.paths.config: candidate_config.encode("utf-8"),
            self.paths.catalog: catalog_bytes,
            **(migration.files if migration else {}),
        }
        removals = list(migration.removals if migration else ())
        if enabled:
            files[self.paths.instruction_file] = candidate_instructions.encode("utf-8")
            files.update(desired_roles)
            removals.extend(
                self._agent_removals(previous_manifest, keep=set(desired_roles))
            )
        else:
            removals.extend(self._agent_removals(previous_manifest))
            if candidate_instructions or instruction_preexisted:
                files[self.paths.instruction_file] = candidate_instructions.encode("utf-8")
            elif self.paths.instruction_file.exists():
                removals.append(self.paths.instruction_file)
        removals.extend(self._adoption_removals(manifest_source))
        backup_dir = self._backup_dir(operation)
        migration_backup = self._record_catalog_migration(
            manifest,
            files,
            removals,
            backup_dir,
            catalog_migration,
            catalog_migration_source,
        )
        transaction = execute_install_plan(
            InstallPlan(
                files=files,
                removals=tuple(dict.fromkeys(removals)),
                manifest=manifest,
                backup_dir=backup_dir,
                preconditions=(
                    {
                        (
                            catalog_migration_source or self.paths.catalog
                        ): catalog_migration.source_sha256,
                    }
                    if catalog_migration is not None and catalog_migration.changed
                    else {}
                ),
            ),
            self.paths.manifest,
        )
        self._verify_catalog_migration_backup(
            transaction,
            catalog_migration,
            migration_backup,
        )
        if enabled and run_live and selection is not None:
            try:
                live = self._live_acceptance(self.codex_bin, self.paths.home, selection)
                self._require_report(live, "post-install native", require_full=True)
                installed = _read_json(self.paths.manifest)
                installed["preinstall_compatibility"] = compatibility
                installed["compatibility"] = live.as_checks()
                atomic_write(
                    self.paths.manifest,
                    (json.dumps(installed, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                    0o600,
                )
            except Exception:
                rollback_transaction(transaction)
                raise
        return {
            "status": "ready" if enabled else "disabled",
            "providers": [item.id for item in catalog.providers],
            "agents": [item.name for item in catalog.agents],
            "max_concurrent_children": catalog.concurrency,
            "backup": str(transaction.backup_dir),
            **(
                {
                    "model": selection.resolved_model,
                    "reasoning_effort": selection.reasoning_effort,
                }
                if selection is not None
                else {}
            ),
        }

    def setup(
        self,
        preset: str = "hybrid",
        *,
        host: str | None = None,
        project_path: Path | None = None,
    ) -> dict[str, Any]:
        with operation_lock(self._lock_path):
            if host not in {None, "codex", "claude-code", "all"}:
                raise ManagerError("unknown_host", f"Unsupported host: {host}.")
            if not self.paths.config.is_file():
                raise ManagerError("config_missing", "Codex config.toml was not found.")
            original_config = self.paths.config.read_text(encoding="utf-8")
            previous, source = self._read_manifest()
            self._reject_future_schema(previous)
            self._assert_no_unowned_managed_blocks(original_config, previous)
            migration: LegacyMigration | None = None
            catalog_migration: CatalogMigrationResult | None = None
            catalog_migration_source: Path | None = None
            working_config = original_config
            schema = previous.get("schema_version")
            if schema == SCHEMA_VERSION:
                catalog_migration, catalog_migration_source = self._active_catalog_result(
                    previous,
                    source,
                    require_owned=True,
                )
                catalog = catalog_migration.catalog
            elif schema == 4:
                catalog = catalog_from_schema4(previous)
            else:
                if previous and schema not in {SCHEMA_VERSION, 4}:
                    migration = plan_legacy_migration(
                        self.paths,
                        original_config,
                        previous,
                        state_root=source.parent,
                    )
                    working_config = migration.config_text
                catalog = default_catalog(preset)
                if migration is not None:
                    payload = catalog.to_dict()
                    for reference in payload["credentials"]:
                        if (
                            isinstance(reference, dict)
                            and reference.get("provider_id") == "deepseek"
                        ):
                            reference["vault_target"] = legacy_credential_target(
                                "deepseek"
                            )
                    catalog = Catalog.from_dict(payload)
            if host is not None:
                payload = catalog.to_dict()
                hosts = payload["hosts"]
                assert isinstance(hosts, dict)
                for host_name in ("codex", "claude-code"):
                    current = hosts.get(host_name)
                    if isinstance(current, dict):
                        current["enabled"] = host == "all" or host == host_name
                catalog = Catalog.from_dict(payload)
            # Ownership conflicts are local and deterministic; reject them before
            # any provider discovery or compatibility process is started.
            self._assert_role_ownership(
                expected_agent_files(self.paths.agents_dir, catalog),
                previous,
            )
            if previous.get("status") == "disabled":
                selection = self._preserved_selection(previous, catalog)
                gate = None
            elif not catalog.hosts.get("codex") or not catalog.hosts["codex"].enabled:
                selection = None
                gate = None
            else:
                catalog, selection, gate = self._discover_builtin_selection(catalog)
            claude = None
            claude_was_configured = False
            claude_config = catalog.hosts.get("claude-code")
            if claude_config is not None and claude_config.enabled:
                from .hosts.claude_code import ClaudeCodeHostAdapter

                claude = ClaudeCodeHostAdapter(self.paths, project_path=project_path)
                claude_was_configured = claude.status()["status"] != "not_configured"
                claude.plan(catalog)
                claude.apply(catalog)
            try:
                result = self._install_catalog_locked(
                    catalog,
                    operation="setup",
                    original_config=original_config,
                    working_config=working_config,
                    previous_manifest=previous,
                    manifest_source=source,
                    migration=migration,
                    selection=selection,
                    gate=gate,
                    run_live=selection is not None,
                    catalog_migration=catalog_migration,
                    catalog_migration_source=catalog_migration_source,
                )
            except Exception:
                if claude is not None and not claude_was_configured:
                    claude.uninstall()
                raise
            if host is not None:
                result["hosts"] = [
                    name for name, config in catalog.hosts.items() if config.enabled
                ]
            return result

    def _active_catalog_result(
        self,
        manifest: dict[str, Any],
        manifest_source: Path,
        *,
        require_owned: bool,
    ) -> tuple[CatalogMigrationResult, Path]:
        self._require_current_schema(manifest)
        source = self._catalog_source(manifest_source)
        if not source.is_file() and manifest_source != self.paths.manifest:
            source = self.paths.catalog
        if require_owned:
            self._require_catalog_owned(manifest, source)
        return inspect_catalog_migration(source), source

    def _active_catalog(
        self,
        manifest: dict[str, Any],
        manifest_source: Path,
        *,
        require_owned: bool,
    ) -> Catalog:
        result, _ = self._active_catalog_result(
            manifest,
            manifest_source,
            require_owned=require_owned,
        )
        return result.catalog

    def _preserved_selection(
        self,
        manifest: dict[str, Any],
        catalog: Catalog,
    ) -> ModelSelection | None:
        try:
            selection = _selection_from_manifest(manifest)
        except ManagerError:
            return None
        if any(
            item.provider == "deepseek" and item.model == selection.resolved_model
            for item in catalog.agents
        ):
            return selection
        return None

    def _apply_catalog_locked(
        self,
        catalog: Catalog,
        manifest: dict[str, Any],
        source: Path,
        *,
        operation: str,
        catalog_migration: CatalogMigrationResult | None = None,
        catalog_migration_source: Path | None = None,
    ) -> dict[str, Any]:
        if not self.paths.config.is_file():
            raise ManagerError("config_missing", "Codex config.toml was not found.")
        config = self.paths.config.read_text(encoding="utf-8")
        return self._install_catalog_locked(
            catalog,
            operation=operation,
            original_config=config,
            working_config=config,
            previous_manifest=manifest,
            manifest_source=source,
            selection=self._preserved_selection(manifest, catalog),
            catalog_migration=catalog_migration,
            catalog_migration_source=catalog_migration_source,
        )

    def repair(self) -> dict[str, Any]:
        manifest, _ = self._read_manifest()
        if not manifest or manifest.get("schema_version") != SCHEMA_VERSION:
            return self.setup()
        return self.apply()

    def apply(self, catalog: Catalog | None = None) -> dict[str, Any]:
        with operation_lock(self._lock_path):
            manifest, source = self._read_manifest()
            if not manifest:
                raise ManagerError("not_configured", "Multi Relay is not installed.")
            catalog_migration, catalog_source = self._active_catalog_result(
                manifest,
                source,
                require_owned=False,
            )
            selected = catalog or catalog_migration.catalog
            return self._apply_catalog_locked(
                selected,
                manifest,
                source,
                operation="apply",
                catalog_migration=catalog_migration,
                catalog_migration_source=catalog_source,
            )

    def catalog(self) -> dict[str, object]:
        manifest, source = self._read_manifest()
        if not manifest:
            raise ManagerError("not_configured", "Multi Relay is not installed.")
        return self._active_catalog(manifest, source, require_owned=False).to_dict()

    def list_providers(self) -> list[dict[str, object]]:
        return list(self.catalog()["providers"])  # type: ignore[arg-type]

    def list_agents(self) -> list[dict[str, object]]:
        return list(self.catalog()["agents"])  # type: ignore[arg-type]

    def list_credentials(self) -> list[dict[str, object]]:
        """Return credential presence metadata without key material or fingerprints."""

        catalog = Catalog.from_dict(self.catalog())
        items: list[dict[str, object]] = []
        for reference in catalog.credentials:
            provider = catalog.provider(reference.provider_id)
            try:
                present = self._credential_for_reference(provider, reference).exists()
            except ManagerError:
                present = False
            items.append(
                {
                    "provider": provider.id,
                    "credential": reference.id,
                    "label": reference.label,
                    "enabled": reference.enabled,
                    "present": present,
                }
            )
        return items

    def list_targets(self) -> list[dict[str, object]]:
        return list(self.catalog()["targets"])  # type: ignore[arg-type]

    def list_pools(self) -> list[dict[str, object]]:
        return list(self.catalog()["pools"])  # type: ignore[arg-type]

    def list_hosts(self) -> list[dict[str, object]]:
        catalog = Catalog.from_dict(self.catalog())
        return [
            {"host": name, **config.to_dict()}
            for name, config in catalog.hosts.items()
        ]

    @staticmethod
    def _mutation_result(
        result: dict[str, Any],
        *,
        operation: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        details = {**details, "backup": result.get("backup")}
        return _outcome(
            str(result.get("status", "ready")),
            changed=True,
            details={"operation": operation, **details},
        )

    def edit_provider(self, provider_id: str, changes: Mapping[str, object]) -> dict[str, Any]:
        def mutate(catalog: Catalog) -> Catalog:
            provider = catalog.provider(provider_id)
            replacement = ProviderSpec.from_dict({**provider.to_dict(), **dict(changes)})
            if replacement.id != provider.id:
                raise ManagerError("provider_id_immutable", "Provider ids cannot be changed.")
            payload = catalog.to_dict()
            payload["providers"] = [
                replacement.to_dict() if item.id == provider.id else item.to_dict()
                for item in catalog.providers
            ]
            return Catalog.from_dict(payload)

        result = self._mutate_catalog("provider-edit", mutate)
        return self._mutation_result(
            result,
            operation="provider-edit",
            details={"provider": provider_id},
        )

    def set_provider_enabled(self, provider_id: str, enabled: bool) -> dict[str, Any]:
        return self.edit_provider(provider_id, {"enabled": enabled})

    def discover_provider_model(self, provider_id: str, requested: str) -> dict[str, Any]:
        catalog = Catalog.from_dict(self.catalog())
        provider = catalog.provider(provider_id)
        secret = ""
        if provider.auth_mode == "vault":
            reference = self._credential_reference(catalog, provider)
            secret = self._credential_for_reference(provider, reference).read() or ""
        model = self._model_discoverer(secret, requested=requested, provider=provider)
        return _outcome(
            "ready",
            changed=False,
            details={"provider": provider.id, "models": [model]},
        )

    def test_provider(self, provider_id: str) -> dict[str, Any]:
        catalog = Catalog.from_dict(self.catalog())
        provider = catalog.provider(provider_id)
        present: bool | None = None
        if provider.auth_mode == "vault":
            references = [
                item for item in catalog.credentials if item.provider_id == provider.id and item.enabled
            ]
            present = bool(references) and any(
                self._credential_for_reference(provider, item).exists()
                for item in references
            )
        return _outcome(
            "ready",
            changed=False,
            details={
                "provider": provider.id,
                "enabled": provider.enabled,
                "credential_present": present,
                "protocol_handshake": "unknown",
            },
        )

    def add_credential(
        self,
        provider_id: str,
        credential_id: str,
        *,
        label: str | None = None,
        secret: str | None = None,
    ) -> dict[str, Any]:
        added: list[CredentialRef] = []

        def mutate(catalog: Catalog) -> Catalog:
            provider = catalog.provider(provider_id)
            if provider.auth_mode != "vault":
                raise ManagerError(
                    "credential_not_allowed",
                    f"Provider {provider.id} does not use vault credentials.",
                )
            if any(
                item.provider_id == provider.id and item.id.casefold() == credential_id.casefold()
                for item in catalog.credentials
            ):
                raise ManagerError(
                    "duplicate_credential",
                    f"Credential {credential_id} already exists for {provider.id}.",
                )
            reference = CredentialRef.from_dict(
                {
                    "id": credential_id,
                    "provider_id": provider.id,
                    "vault_target": credential_target(
                        provider.id,
                        credential_id,
                        protocol=provider.protocol,
                    ),
                    "enabled": True,
                    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "label": label or credential_id,
                }
            )
            added.append(reference)
            payload = catalog.to_dict()
            credentials = payload["credentials"]
            assert isinstance(credentials, list)
            credentials.append(reference.to_dict())
            return Catalog.from_dict(payload)

        result = self._mutate_catalog("credential-add", mutate)
        if secret is not None and added:
            provider = Catalog.from_dict(self.catalog()).provider(provider_id)
            self._credential_for_reference(provider, added[0]).store(secret)
        return self._mutation_result(
            result,
            operation="credential-add",
            details={"provider": provider_id, "credential": credential_id},
        )

    def replace_credential(
        self,
        provider_id: str,
        credential_id: str,
        secret: str,
    ) -> dict[str, Any]:
        catalog = Catalog.from_dict(self.catalog())
        provider = catalog.provider(provider_id)
        reference = catalog.credential(credential_id, provider.id)
        self._credential_for_reference(provider, reference).store(secret)
        return _outcome(
            "ready",
            changed=True,
            details={"provider": provider.id, "credential": reference.id, "present": True},
        )

    def set_credential_enabled(
        self,
        provider_id: str,
        credential_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        def mutate(catalog: Catalog) -> Catalog:
            selected = catalog.credential(credential_id, provider_id)
            payload = catalog.to_dict()
            payload["credentials"] = [
                {**item.to_dict(), "enabled": enabled}
                if item.provider_id == selected.provider_id and item.id == selected.id
                else item.to_dict()
                for item in catalog.credentials
            ]
            return Catalog.from_dict(payload)

        result = self._mutate_catalog("credential-enable" if enabled else "credential-disable", mutate)
        return self._mutation_result(
            result,
            operation="credential-enable" if enabled else "credential-disable",
            details={"provider": provider_id, "credential": credential_id},
        )

    def test_credential(self, provider_id: str, credential_id: str) -> dict[str, Any]:
        catalog = Catalog.from_dict(self.catalog())
        provider = catalog.provider(provider_id)
        reference = catalog.credential(credential_id, provider.id)
        present = self._credential_for_reference(provider, reference).exists()
        return _outcome(
            "ready" if present else "credential_missing",
            changed=False,
            details={
                "provider": provider.id,
                "credential": reference.id,
                "enabled": reference.enabled,
                "present": present,
            },
        )

    def remove_credential(self, provider_id: str, credential_id: str) -> dict[str, Any]:
        removed: list[tuple[ProviderSpec, CredentialRef]] = []

        def mutate(catalog: Catalog) -> Catalog:
            provider = catalog.provider(provider_id)
            reference = catalog.credential(credential_id, provider.id)
            users = [
                target.id
                for target in catalog.targets
                if target.provider_id == provider.id and target.credential_id == reference.id
            ]
            if users:
                raise ManagerError(
                    "credential_in_use",
                    f"Credential {reference.id} is still used by execution targets.",
                    {"provider": provider.id, "credential": reference.id, "targets": users},
                )
            removed.append((provider, reference))
            payload = catalog.to_dict()
            payload["credentials"] = [
                item.to_dict()
                for item in catalog.credentials
                if not (item.provider_id == provider.id and item.id == reference.id)
            ]
            return Catalog.from_dict(payload)

        result = self._mutate_catalog("credential-remove", mutate)
        if removed:
            self._credential_for_reference(*removed[0]).remove()
        return self._mutation_result(
            result,
            operation="credential-remove",
            details={"provider": provider_id, "credential": credential_id},
        )

    def add_target(self, target: ExecutionTarget | Mapping[str, object]) -> dict[str, Any]:
        selected = target if isinstance(target, ExecutionTarget) else ExecutionTarget.from_dict(target)

        def mutate(catalog: Catalog) -> Catalog:
            if any(item.id.casefold() == selected.id.casefold() for item in catalog.targets):
                raise ManagerError("duplicate_target", f"Target {selected.id} already exists.")
            payload = catalog.to_dict()
            targets = payload["targets"]
            assert isinstance(targets, list)
            targets.append(selected.to_dict())
            return Catalog.from_dict(payload)

        result = self._mutate_catalog("target-add", mutate)
        return self._mutation_result(result, operation="target-add", details={"target": selected.id})

    def edit_target(self, target_id: str, changes: Mapping[str, object]) -> dict[str, Any]:
        def mutate(catalog: Catalog) -> Catalog:
            target = catalog.target(target_id)
            replacement = ExecutionTarget.from_dict({**target.to_dict(), **dict(changes)})
            if replacement.id != target.id:
                raise ManagerError("target_id_immutable", "Target ids cannot be changed.")
            payload = catalog.to_dict()
            payload["targets"] = [
                replacement.to_dict() if item.id == target.id else item.to_dict()
                for item in catalog.targets
            ]
            return Catalog.from_dict(payload)

        result = self._mutate_catalog("target-edit", mutate)
        return self._mutation_result(result, operation="target-edit", details={"target": target_id})

    def set_target_enabled(self, target_id: str, enabled: bool) -> dict[str, Any]:
        return self.edit_target(target_id, {"enabled": enabled})

    def test_target(self, target_id: str) -> dict[str, Any]:
        catalog = Catalog.from_dict(self.catalog())
        target = catalog.target(target_id)
        provider = catalog.provider(target.provider_id)
        credential_present: bool | None = None
        if target.credential_id is not None:
            reference = catalog.credential(target.credential_id, provider.id)
            credential_present = self._credential_for_reference(provider, reference).exists()
        return _outcome(
            "ready",
            changed=False,
            details={
                "target": target.id,
                "authentication": "present" if credential_present else (
                    "missing" if credential_present is False else "not_required"
                ),
                "model_available": "unknown",
                "protocol_handshake": "unknown",
                "capabilities": {name: "unknown" for name in sorted(target.capabilities)},
            },
        )

    def remove_target(self, target_id: str) -> dict[str, Any]:
        def mutate(catalog: Catalog) -> Catalog:
            target = catalog.target(target_id)
            users = [pool.id for pool in catalog.pools if target.id in pool.targets]
            if users:
                raise ManagerError(
                    "target_in_use",
                    f"Target {target.id} is still used by target pools.",
                    {"target": target.id, "pools": users},
                )
            payload = catalog.to_dict()
            payload["targets"] = [item.to_dict() for item in catalog.targets if item.id != target.id]
            return Catalog.from_dict(payload)

        result = self._mutate_catalog("target-remove", mutate)
        return self._mutation_result(result, operation="target-remove", details={"target": target_id})

    def add_pool(self, pool: TargetPool | Mapping[str, object]) -> dict[str, Any]:
        selected = pool if isinstance(pool, TargetPool) else TargetPool.from_dict(pool)

        def mutate(catalog: Catalog) -> Catalog:
            if any(item.id.casefold() == selected.id.casefold() for item in catalog.pools):
                raise ManagerError("duplicate_pool", f"Pool {selected.id} already exists.")
            disabled = [target_id for target_id in selected.targets if not catalog.target(target_id).enabled]
            if disabled:
                raise ManagerError(
                    "target_disabled",
                    "Pools cannot include disabled execution targets.",
                    {"targets": disabled},
                )
            payload = catalog.to_dict()
            pools = payload["pools"]
            assert isinstance(pools, list)
            pools.append(selected.to_dict())
            return Catalog.from_dict(payload)

        result = self._mutate_catalog("pool-add", mutate)
        return self._mutation_result(result, operation="pool-add", details={"pool": selected.id})

    def edit_pool(self, pool_id: str, changes: Mapping[str, object]) -> dict[str, Any]:
        def mutate(catalog: Catalog) -> Catalog:
            pool = catalog.pool(pool_id)
            replacement = TargetPool.from_dict({**pool.to_dict(), **dict(changes)})
            if replacement.id != pool.id:
                raise ManagerError("pool_id_immutable", "Pool ids cannot be changed.")
            disabled = [target_id for target_id in replacement.targets if not catalog.target(target_id).enabled]
            if disabled:
                raise ManagerError(
                    "target_disabled",
                    "Pools cannot include disabled execution targets.",
                    {"targets": disabled},
                )
            payload = catalog.to_dict()
            payload["pools"] = [
                replacement.to_dict() if item.id == pool.id else item.to_dict()
                for item in catalog.pools
            ]
            return Catalog.from_dict(payload)

        result = self._mutate_catalog("pool-edit", mutate)
        return self._mutation_result(result, operation="pool-edit", details={"pool": pool_id})

    def set_pool_order(self, pool_id: str, target_ids: Sequence[str]) -> dict[str, Any]:
        if len(target_ids) != len(set(target_ids)):
            raise ManagerError("duplicate_target", "Pool order contains duplicate targets.")
        if not target_ids:
            raise ManagerError("unknown_target", "Pool order requires at least one target.")
        return self.edit_pool(pool_id, {"targets": list(target_ids)})

    def set_pool_strategy(
        self,
        pool_id: str,
        strategy: str,
        *,
        duration_seconds: int | None = None,
    ) -> dict[str, Any]:
        return self.edit_pool(
            pool_id,
            {
                "strategy": strategy,
                "duration_seconds": duration_seconds if strategy == "timed" else None,
            },
        )

    def _rotation(self, catalog: Catalog) -> RotationController:
        store = RuntimeStateStore(
            self.paths.runtime_state,
            lock_path=self.paths.runtime_state_lock,
        )
        return RotationController(
            catalog,
            store,
            credential_available=lambda reference: self._credential_for_reference(
                catalog.provider(reference.provider_id),
                reference,
            ).exists(),
        )

    def rotate_pool(self, pool_id: str) -> dict[str, Any]:
        catalog = Catalog.from_dict(self.catalog())
        pool = catalog.pool(pool_id)
        controller = self._rotation(catalog)
        state = controller.store.load(controller.catalog_hash)
        result = controller.rotate_pool(
            pool.id,
            expected_generation=state.generation,
            requirements=SelectionRequirements(
                host=pool.host_compatibility[0],
                required_capabilities=pool.required_capabilities,
            ),
        )
        return _outcome(
            "ready" if result.selected_target_id else "no_eligible_target",
            changed=result.changed,
            details=asdict(result),
        )

    def reset_pool(self, pool_id: str) -> dict[str, Any]:
        catalog = Catalog.from_dict(self.catalog())
        state = self._rotation(catalog).reset_pool(pool_id)
        return _outcome(
            "ready",
            changed=True,
            details={"pool": pool_id, "generation": state.generation},
        )

    def pool_status(self, pool_id: str) -> dict[str, Any]:
        catalog = Catalog.from_dict(self.catalog())
        catalog.pool(pool_id)
        store = RuntimeStateStore(self.paths.runtime_state, lock_path=self.paths.runtime_state_lock)
        state = store.load(catalog_fingerprint(catalog))
        pool_state = state.pools.get(pool_id)
        return _outcome(
            "ready",
            changed=False,
            details={
                "pool": pool_id,
                "generation": state.generation,
                "runtime": pool_state.to_dict() if pool_state is not None else None,
            },
        )

    def remove_pool(self, pool_id: str) -> dict[str, Any]:
        def mutate(catalog: Catalog) -> Catalog:
            pool = catalog.pool(pool_id)
            agents = [
                agent.name
                for agent in catalog.agents
                if agent.pool_id == pool.id or agent.fallback_pool_id == pool.id
            ]
            hosts = [host.host for host in catalog.hosts.values() if host.default_pool == pool.id]
            if agents or hosts:
                raise ManagerError(
                    "pool_in_use",
                    f"Pool {pool.id} is still referenced.",
                    {"pool": pool.id, "agents": agents, "hosts": hosts},
                )
            payload = catalog.to_dict()
            payload["pools"] = [item.to_dict() for item in catalog.pools if item.id != pool.id]
            return Catalog.from_dict(payload)

        result = self._mutate_catalog("pool-remove", mutate)
        return self._mutation_result(result, operation="pool-remove", details={"pool": pool_id})

    def _mutate_catalog(
        self,
        operation: str,
        mutation: Callable[[Catalog], Catalog],
    ) -> dict[str, Any]:
        with operation_lock(self._lock_path):
            manifest, source = self._read_manifest()
            if not manifest:
                raise ManagerError("not_configured", "Multi Relay is not installed.")
            catalog_migration, catalog_source = self._active_catalog_result(
                manifest,
                source,
                require_owned=False,
            )
            return self._apply_catalog_locked(
                mutation(catalog_migration.catalog),
                manifest,
                source,
                operation=operation,
                catalog_migration=catalog_migration,
                catalog_migration_source=catalog_source,
            )

    def add_provider(self, provider: ProviderSpec | dict[str, object]) -> dict[str, Any]:
        selected = provider if isinstance(provider, ProviderSpec) else ProviderSpec.from_dict(provider)

        def mutate(catalog: Catalog) -> Catalog:
            if any(item.id.casefold() == selected.id.casefold() for item in catalog.providers):
                raise ManagerError(
                    "duplicate_provider",
                    f"Provider {selected.id} already exists.",
                    {"provider": selected.id},
                )
            payload = catalog.to_dict()
            providers = payload["providers"]
            assert isinstance(providers, list)
            providers.append(selected.to_dict())
            if selected.auth_mode == "vault":
                credentials = payload["credentials"]
                assert isinstance(credentials, list)
                credentials.append(
                    {
                        "id": "primary",
                        "provider_id": selected.id,
                        "vault_target": credential_target(
                            selected.id,
                            "primary",
                            protocol=selected.protocol,
                        ),
                        "enabled": True,
                        "created_at": datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "label": "Primary",
                    }
                )
            return Catalog.from_dict(payload)

        return self._mutate_catalog("provider-add", mutate)

    def remove_provider(
        self,
        provider_id: str,
        *,
        remove_credential: bool = False,
    ) -> dict[str, Any]:
        removed: list[ProviderSpec] = []
        removed_credentials: list[CredentialRef] = []

        def mutate(catalog: Catalog) -> Catalog:
            provider = catalog.provider(provider_id)
            users = [item.name for item in catalog.agents if item.provider == provider.id]
            target_users = [
                item.id
                for item in catalog.targets
                if item.provider_id == provider.id
            ]
            if users or target_users:
                raise ManagerError(
                    "provider_in_use",
                    f"Provider {provider.id} is still used by catalog routing.",
                    {
                        "provider": provider.id,
                        "agents": users,
                        "targets": target_users,
                    },
                )
            removed.append(provider)
            removed_credentials.extend(
                item for item in catalog.credentials if item.provider_id == provider.id
            )
            payload = catalog.to_dict()
            providers = payload["providers"]
            assert isinstance(providers, list)
            payload["providers"] = [
                item
                for item in providers
                if isinstance(item, dict) and item.get("id") != provider.id
            ]
            credentials = payload["credentials"]
            assert isinstance(credentials, list)
            payload["credentials"] = [
                item
                for item in credentials
                if isinstance(item, dict)
                and item.get("provider_id") != provider.id
            ]
            return Catalog.from_dict(payload)

        result = self._mutate_catalog("provider-remove", mutate)
        if remove_credential and removed and removed[0].auth == "vault":
            for reference in removed_credentials:
                self._credential_for_reference(removed[0], reference).remove()
        return result

    def set_agent(self, agent: AgentSpec | dict[str, object]) -> dict[str, Any]:
        selected = agent if isinstance(agent, AgentSpec) else AgentSpec.from_dict(agent)

        def mutate(catalog: Catalog) -> Catalog:
            payload = catalog.to_dict()
            selected_payload = selected.to_dict()
            if selected.provider:
                provider = catalog.provider(selected.provider)
                enabled_credentials = sorted(
                    (
                        item
                        for item in catalog.credentials
                        if item.provider_id == provider.id and item.enabled
                    ),
                    key=lambda item: item.id,
                )
                credential_id = (
                    enabled_credentials[0].id
                    if len(enabled_credentials) == 1
                    else None
                )
                if provider.auth_mode == "vault":
                    if not enabled_credentials:
                        raise ManagerError(
                            "credential_required",
                            f"Provider {provider.id} has no enabled credential reference.",
                            {"provider": provider.id},
                        )
                    if len(enabled_credentials) > 1:
                        raise ManagerError(
                            "ambiguous_credential",
                            f"Provider {provider.id} has multiple enabled credentials; select an execution target explicitly.",
                            {"provider": provider.id},
                        )
                target_id = f"{selected.name}-target"
                pool_id = f"{selected.name}-pool"
                targets = payload["targets"]
                pools = payload["pools"]
                assert isinstance(targets, list)
                assert isinstance(pools, list)
                existing_agent = next(
                    (
                        item
                        for item in catalog.agents
                        if item.name.casefold() == selected.name.casefold()
                    ),
                    None,
                )
                existing_pool = next(
                    (item for item in catalog.pools if item.id == pool_id),
                    None,
                )
                if existing_pool is not None:
                    other_agent_users = [
                        item.name
                        for item in catalog.agents
                        if item.name.casefold() != selected.name.casefold()
                        and (
                            item.pool_id == pool_id
                            or item.fallback_pool_id == pool_id
                        )
                    ]
                    host_users = [
                        item.host
                        for item in catalog.hosts.values()
                        if item.default_pool == pool_id
                    ]
                    if (
                        existing_agent is None
                        or existing_agent.pool_id != pool_id
                        or existing_pool.targets != (target_id,)
                        or other_agent_users
                        or host_users
                    ):
                        raise ManagerError(
                            "routing_in_use",
                            f"Pool {pool_id} is shared and cannot be replaced by the legacy agent command.",
                            {
                                "pool": pool_id,
                                "agents": other_agent_users,
                                "hosts": host_users,
                            },
                        )
                existing_target = next(
                    (
                        item
                        for item in catalog.targets
                        if item.id == target_id
                    ),
                    None,
                )
                if existing_target is not None:
                    other_pool_users = [
                        item.id
                        for item in catalog.pools
                        if item.id != pool_id and target_id in item.targets
                    ]
                    if existing_pool is None or other_pool_users:
                        raise ManagerError(
                            "routing_in_use",
                            f"Target {target_id} is shared and cannot be replaced by the legacy agent command.",
                            {
                                "target": target_id,
                                "pools": other_pool_users,
                            },
                        )
                payload["targets"] = [
                    item
                    for item in targets
                    if not (
                        isinstance(item, dict)
                        and item.get("id") == target_id
                    )
                ] + [
                    {
                        "id": target_id,
                        "provider_id": provider.id,
                        "protocol": None,
                        "model": selected.model,
                        "credential_id": credential_id,
                        "capabilities": sorted(
                            selected.required_capabilities
                        ),
                        "context_window": (
                            selected.context_window
                            or provider.context_window
                        ),
                        "max_output_tokens": None,
                        "reasoning_efforts": (
                            [selected.reasoning_effort]
                            if selected.reasoning_effort is not None
                            else []
                        ),
                        "trust": selected.trust,
                        "host_compatibility": list(selected.hosts),
                        "enabled": True,
                        "metadata": {"managed_for_agent": selected.name},
                    }
                ]
                payload["pools"] = [
                    item
                    for item in pools
                    if not (
                        isinstance(item, dict)
                        and item.get("id") == pool_id
                    )
                ] + [
                    {
                        "id": pool_id,
                        "targets": [target_id],
                        "strategy": "sticky",
                        "duration_seconds": None,
                        "max_rate_limit_wait_seconds": 30,
                        "cooldown": {
                            "quota_seconds": 86400,
                            "rate_limit_seconds": 60,
                            "auth_seconds": 3600,
                            "provider_seconds": 30,
                        },
                        "required_capabilities": sorted(
                            selected.required_capabilities
                        ),
                        "host_compatibility": list(selected.hosts),
                        "enabled": True,
                    }
                ]
                selected_payload["pool_id"] = pool_id
            current_agents = payload["agents"]
            assert isinstance(current_agents, list)
            agents = [
                item
                for item in current_agents
                if isinstance(item, dict)
                and str(item.get("name", "")).casefold() != selected.name.casefold()
            ]
            agents.append(selected_payload)
            payload["agents"] = agents
            return Catalog.from_dict(payload)

        return self._mutate_catalog("agent-set", mutate)

    def remove_agent(self, name: str) -> dict[str, Any]:
        def mutate(catalog: Catalog) -> Catalog:
            selected = catalog.agent(name)
            payload = catalog.to_dict()
            current_agents = payload["agents"]
            assert isinstance(current_agents, list)
            payload["agents"] = [
                item
                for item in current_agents
                if isinstance(item, dict) and item.get("name") != selected.name
            ]
            remaining_agents = payload["agents"]
            assert isinstance(remaining_agents, list)
            pool_still_used = any(
                isinstance(item, dict)
                and (
                    item.get("pool_id") == selected.pool_id
                    or item.get("fallback_pool_id") == selected.pool_id
                )
                for item in remaining_agents
            )
            host_uses_pool = any(
                host.default_pool == selected.pool_id
                for host in catalog.hosts.values()
            )
            if not pool_still_used and not host_uses_pool:
                removed_pool = catalog.pool(selected.pool_id)
                candidate_targets = set(removed_pool.targets)
                pools = payload["pools"]
                assert isinstance(pools, list)
                payload["pools"] = [
                    item
                    for item in pools
                    if not (
                        isinstance(item, dict)
                        and item.get("id") == selected.pool_id
                    )
                ]
                referenced_targets = {
                    target_id
                    for item in payload["pools"]
                    if isinstance(item, dict)
                    for target_id in item.get("targets", [])
                    if isinstance(target_id, str)
                }
                targets = payload["targets"]
                assert isinstance(targets, list)
                payload["targets"] = [
                    item
                    for item in targets
                    if not isinstance(item, dict)
                    or item.get("id") not in candidate_targets
                    or item.get("id") in referenced_targets
                ]
            return Catalog.from_dict(payload)

        return self._mutate_catalog("agent-remove", mutate)

    def configure_host(
        self,
        host_name: str,
        *,
        enabled: bool | None = None,
        scope: str | None = None,
        default_pool: str | None = None,
    ) -> dict[str, Any]:
        if host_name not in {"codex", "claude-code"}:
            raise ManagerError("unknown_host", f"Unsupported host: {host_name}.")

        def mutate(catalog: Catalog) -> Catalog:
            current = catalog.hosts.get(host_name)
            if current is None:
                raise ManagerError("unknown_host", f"Host {host_name} is not configured.")
            replacement = current.to_dict()
            if enabled is not None:
                replacement["enabled"] = enabled
            if scope is not None:
                replacement["scope"] = scope
            if default_pool is not None:
                replacement["default_pool"] = default_pool
            payload = catalog.to_dict()
            hosts = payload["hosts"]
            assert isinstance(hosts, dict)
            hosts[host_name] = replacement
            return Catalog.from_dict(payload)

        result = self._mutate_catalog("host-configure", mutate)
        return self._mutation_result(
            result,
            operation="host-configure",
            details={"host": host_name},
        )

    def apply_host(self, host_name: str, *, project_path: Path | None = None) -> dict[str, Any]:
        if host_name == "codex":
            result = self.apply()
            return _outcome(
                str(result.get("status", "ready")),
                changed=True,
                details={"host": host_name, "backup": result.get("backup")},
            )
        if host_name == "claude-code":
            from .hosts.claude_code import ClaudeCodeHostAdapter

            catalog = Catalog.from_dict(self.catalog())
            return ClaudeCodeHostAdapter(self.paths, project_path=project_path).apply(catalog)
        raise ManagerError("unknown_host", f"Unsupported host: {host_name}.")

    def host_status(self, host_name: str, *, project_path: Path | None = None) -> dict[str, Any]:
        if host_name == "codex":
            result = self.status()
            return _outcome(
                str(result.get("status", "partial")),
                changed=False,
                details={"host": host_name, "checks": result.get("checks", {})},
            )
        if host_name == "claude-code":
            from .hosts.claude_code import ClaudeCodeHostAdapter

            return ClaudeCodeHostAdapter(self.paths, project_path=project_path).status()
        raise ManagerError("unknown_host", f"Unsupported host: {host_name}.")

    def enable_host(self, host_name: str, *, project_path: Path | None = None) -> dict[str, Any]:
        self.configure_host(host_name, enabled=True)
        return self.apply_host(host_name, project_path=project_path)

    def disable_host(self, host_name: str, *, project_path: Path | None = None) -> dict[str, Any]:
        if host_name == "claude-code":
            from .hosts.claude_code import ClaudeCodeHostAdapter

            adapter = ClaudeCodeHostAdapter(self.paths, project_path=project_path)
            result = adapter.disable()
            self.configure_host(host_name, enabled=False)
            return result
        if host_name == "codex":
            self.configure_host(host_name, enabled=False)
            return self.apply_host(host_name)
        raise ManagerError("unknown_host", f"Unsupported host: {host_name}.")

    def uninstall_host(
        self,
        host_name: str,
        *,
        project_path: Path | None = None,
        remove_credentials: bool = False,
    ) -> dict[str, Any]:
        if host_name == "codex":
            return self.uninstall(remove_credential=remove_credentials)
        if host_name == "claude-code":
            from .hosts.claude_code import ClaudeCodeHostAdapter

            return ClaudeCodeHostAdapter(self.paths, project_path=project_path).uninstall()
        if host_name == "all":
            from .hosts.claude_code import ClaudeCodeHostAdapter

            claude = ClaudeCodeHostAdapter(self.paths, project_path=project_path).uninstall()
            codex = self.uninstall(remove_credential=remove_credentials)
            return _outcome(
                "uninstalled",
                changed=True,
                details={"codex": codex, "claude-code": claude},
            )
        raise ManagerError("unknown_host", f"Unsupported host: {host_name}.")

    def gateway_start(self, *, controller: GatewayController | None = None) -> dict[str, Any]:
        gateway = controller or GatewayController(codex_home=self.paths.home)
        state = gateway.ensure()
        return _outcome(
            "running",
            changed=True,
            details={"pid": state.pid, "port": state.port, "generation": state.generation},
        )

    def gateway_status(self) -> dict[str, Any]:
        state = load_gateway_state(self.paths.gateway_state)
        if state is None:
            return _outcome("stopped", changed=False)
        alive = True
        try:
            os.kill(state.pid, 0)
        except (OSError, ValueError):
            alive = False
        return _outcome(
            "running" if alive else "stale",
            changed=False,
            details={"pid": state.pid, "port": state.port, "generation": state.generation},
            next_actions=(["gateway start"] if not alive else []),
        )

    def gateway_stop(self, *, controller: GatewayController | None = None) -> dict[str, Any]:
        gateway = controller or GatewayController(codex_home=self.paths.home)
        stopped = gateway.stop()
        return _outcome(
            "stopped" if stopped else "not_stopped",
            changed=stopped,
            next_actions=([] if stopped else ["Stop the launcher-owned gateway process."]),
        )

    def route(
        self,
        capabilities: set[str] | frozenset[str],
        *,
        high_risk: bool = False,
    ) -> dict[str, Any]:
        manifest, source = self._read_manifest()
        if not manifest:
            raise ManagerError("not_configured", "Multi Relay is not installed.")
        catalog = self._active_catalog(manifest, source, require_owned=False)
        selected = route_agent(catalog, capabilities, high_risk)
        if selected is None:
            return {
                "status": "parent_required",
                "required_capabilities": sorted(capabilities),
                "high_risk": high_risk,
            }
        return {
            "status": "routed",
            "agent": selected.name,
            "provider": selected.provider,
            "model": selected.model,
            "required_capabilities": sorted(capabilities),
            "high_risk": high_risk,
        }

    def status(self) -> dict[str, Any]:
        manifest, source = self._read_manifest()
        if not manifest:
            present = False
            try:
                present = self.credentials.exists()
            except ManagerError:
                pass
            return {"status": "not_configured", "credential_present": present}
        schema = manifest.get("schema_version")
        if schema != SCHEMA_VERSION:
            return {
                "status": "future" if isinstance(schema, int) and schema > SCHEMA_VERSION else "legacy",
                "schema_version": schema,
            }
        try:
            catalog = self._active_catalog(manifest, source, require_owned=True)
        except ManagerError:
            return {"status": "partial", "checks": {"catalog": False}}
        credential_presence: dict[str, bool] = {}
        for provider in catalog.providers:
            if provider.enabled and provider.auth == "vault":
                try:
                    reference = self._credential_reference(catalog, provider)
                    credential_presence[provider.id] = self._credential_for_reference(
                        provider,
                        reference,
                    ).exists()
                except ManagerError:
                    credential_presence[provider.id] = False
        if manifest.get("status") == "disabled":
            return {
                "status": "disabled",
                "providers": [item.id for item in catalog.providers],
                "agents": [item.name for item in catalog.agents],
                "credentials": credential_presence,
            }
        checks: dict[str, bool] = {"catalog": True}
        checks.update({f"credential_{key}": value for key, value in credential_presence.items()})
        try:
            config = tomllib.loads(self.paths.config.read_text(encoding="utf-8"))
            providers = config.get("model_providers") or {}
            has_http_target = any(
                target.enabled
                and "codex" in target.host_compatibility
                and catalog.provider(target.provider_id).protocol != "codex-native"
                for target in catalog.targets
            )
            entry = providers.get("multi-relay") if isinstance(providers, dict) else None
            checks["provider_multi-relay"] = (
                (not has_http_target and entry is None)
                or (
                    isinstance(entry, dict)
                    and entry.get("wire_api") == "responses"
                    and entry.get("base_url") == GATEWAY_BASE_URL
                )
            )
            agents_table = config.get("agents") or {}
            checks["agents_enabled"] = agents_table.get("enabled") is True
            limit = agents_table.get("max_concurrent_threads_per_session")
            checks["concurrency"] = (
                isinstance(limit, int)
                and not isinstance(limit, bool)
                and limit >= catalog.concurrency
            )
            features = config.get("features") or {}
            v2 = features.get("multi_agent_v2") if isinstance(features, dict) else None
            checks["v2_routing"] = (
                isinstance(v2, dict)
                and v2.get("enabled") is True
                and v2.get("hide_spawn_agent_metadata") is False
                and v2.get("tool_namespace") == "agents"
            )
            v2_limit = v2.get("max_concurrent_threads_per_session") if isinstance(v2, dict) else None
            checks["v2_concurrency"] = (
                isinstance(v2_limit, int)
                and not isinstance(v2_limit, bool)
                and v2_limit >= catalog.concurrency
            )
            expected = expected_agent_files(self.paths.agents_dir, catalog)
            for path, content in expected.items():
                checks[f"agent_{path.stem}"] = path.is_file() and path.read_bytes() == content
            instructions = self.paths.instruction_file.read_text(encoding="utf-8")
            checks["routing_instructions"] = (
                INSTRUCTIONS_BEGIN in instructions and INSTRUCTIONS_END in instructions
            )
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ManagerError):
            checks["readable_configuration"] = False
        return {
            "status": "ready" if checks and all(checks.values()) else "partial",
            "credential_present": all(credential_presence.values()),
            "providers": [item.to_dict() for item in catalog.providers],
            "agents": [item.to_dict() for item in catalog.agents],
            "checks": checks,
        }

    def test(self, host: str = "codex") -> dict[str, Any]:
        if host == "all":
            checks: dict[str, Any] = {}
            for name in ("codex", "claude-code"):
                try:
                    checks[name] = self.test(name)
                except ManagerError as exc:
                    checks[name] = {"status": exc.code, "message": str(exc)}
            ready = all(item.get("status") == "ready" for item in checks.values())
            return _outcome(
                "ready" if ready else "partial",
                changed=False,
                details={"hosts": checks},
            )
        if host == "claude-code":
            result = self.host_status(host)
            ready = result.get("status") == "enabled"
            return _outcome(
                "ready" if ready else str(result.get("status", "partial")),
                changed=False,
                details={"host": host, "host_status": result},
            )
        if host != "codex":
            raise ManagerError("unknown_host", f"Unsupported host: {host}.")
        manifest, source = self._read_manifest()
        if not manifest:
            raise ManagerError("not_configured", "Multi Relay is not enabled.")
        self._require_current_schema(manifest)
        if manifest.get("status") != "enabled":
            raise ManagerError("not_configured", "Multi Relay is not enabled.")
        catalog = self._active_catalog(manifest, source, require_owned=True)
        selection = self._preserved_selection(manifest, catalog)
        if selection is None:
            current = self.status()
            if current.get("status") != "ready":
                raise ManagerError("compatibility_failed", "Multi Relay configuration is incomplete.")
            return {"status": "ready", "checks": current.get("checks", {})}
        report = self._live_acceptance(self.codex_bin, self.paths.home, selection)
        self._require_report(report, "native", require_full=True)
        return {"status": "ready", "checks": report.as_checks()}

    def disable(self) -> dict[str, Any]:
        with operation_lock(self._lock_path):
            manifest, source = self._read_manifest()
            if not manifest:
                raise ManagerError("not_configured", "Multi Relay is not installed.")
            self._require_current_schema(manifest)
            if manifest.get("status") == "disabled":
                return {"status": "disabled"}
            removals = list(self._agent_removals(manifest))
            removals.extend(self._adoption_removals(source))
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
            updated = dict(manifest)
            updated.pop("backup", None)
            updated.pop("transaction_targets", None)
            updated["status"] = "disabled"
            execute_install_plan(
                InstallPlan(
                    files=files,
                    removals=tuple(dict.fromkeys(removals)),
                    manifest=updated,
                    backup_dir=self._backup_dir("disable"),
                ),
                self.paths.manifest,
            )
            return {"status": "disabled"}

    def enable(self) -> dict[str, Any]:
        with operation_lock(self._lock_path):
            manifest, source = self._read_manifest()
            if not manifest:
                raise ManagerError("not_configured", "Multi Relay is not installed.")
            self._require_current_schema(manifest)
            if manifest.get("status") == "enabled":
                return self.status()
            catalog_migration, catalog_source = self._active_catalog_result(
                manifest,
                source,
                require_owned=True,
            )
            catalog = catalog_migration.catalog
            desired = expected_agent_files(self.paths.agents_dir, catalog)
            self._assert_role_ownership(desired, manifest)
            instructions = (
                self.paths.instruction_file.read_text(encoding="utf-8")
                if self.paths.instruction_file.is_file()
                else ""
            )
            self._assert_instruction_markers(instructions)
            files = {
                **desired,
                self.paths.instruction_file: apply_fanout_instructions(
                    instructions,
                    catalog.concurrency,
                    catalog=catalog,
                ).encode("utf-8"),
            }
            updated = dict(manifest)
            updated.pop("backup", None)
            updated.pop("transaction_targets", None)
            updated["status"] = "enabled"
            removals = list(self._adoption_removals(source))
            if catalog_migration.changed:
                catalog_bytes = save_catalog_bytes(catalog)
                files[self.paths.catalog] = catalog_bytes
                if catalog_source != self.paths.catalog and catalog_source not in removals:
                    removals.append(catalog_source)
                updated["catalog_schema_version"] = catalog.schema_version
                updated["catalog_sha256"] = _sha256(catalog_bytes)
                updated["concurrency"] = catalog.concurrency
                updated["providers"] = [item.id for item in catalog.providers]
                updated["agents"] = [item.name for item in catalog.agents]
                managed = (
                    dict(updated.get("managed_files"))
                    if isinstance(updated.get("managed_files"), dict)
                    else {}
                )
                old_catalog_key = self._relative(catalog_source)
                managed.pop(old_catalog_key, None)
                if catalog_source != self.paths.catalog:
                    old_state_prefix = (
                        self._relative(catalog_source.parent).rstrip("/") + "/"
                    )
                    managed = {
                        key: value
                        for key, value in managed.items()
                        if not (
                            isinstance(key, str)
                            and key.startswith(old_state_prefix)
                        )
                    }
                managed[self._relative(self.paths.catalog)] = _sha256(catalog_bytes)
                for path, content in desired.items():
                    managed[self._relative(path)] = _sha256(content)
                updated["managed_files"] = managed
            backup_dir = self._backup_dir("enable")
            migration_backup = self._record_catalog_migration(
                updated,
                files,
                removals,
                backup_dir,
                catalog_migration,
                catalog_source,
            )
            transaction = execute_install_plan(
                InstallPlan(
                    files=files,
                    removals=tuple(dict.fromkeys(removals)),
                    manifest=updated,
                    backup_dir=backup_dir,
                    preconditions=(
                        {catalog_source: catalog_migration.source_sha256}
                        if catalog_migration.changed
                        else {}
                    ),
                ),
                self.paths.manifest,
            )
            self._verify_catalog_migration_backup(
                transaction,
                catalog_migration,
                migration_backup,
            )
            return self.status()

    def uninstall(self, remove_credential: bool = False) -> dict[str, Any]:
        with operation_lock(self._lock_path):
            manifest, source = self._read_manifest()
            if not manifest:
                if remove_credential:
                    self.credentials.remove()
                self._bridge_stopper()
                return {"status": "uninstalled"}
            self._require_current_schema(manifest)
            catalog_path = self._catalog_source(source)
            if not catalog_path.is_file() and source != self.paths.manifest:
                catalog_path = self.paths.catalog
            self._require_catalog_owned(manifest, catalog_path)
            catalog = load_catalog(catalog_path)
            removals = list(self._agent_removals(manifest))
            removals.append(catalog_path)
            removals.extend(self._adoption_removals(source))
            config = self.paths.config.read_text(encoding="utf-8")
            original_values = manifest.get("original_values")
            if not _has_original_values(original_values):
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
                    removals=tuple(dict.fromkeys(removals)),
                    manifest=None,
                    backup_dir=self._backup_dir("uninstall"),
                ),
                self.paths.manifest,
            )
            if remove_credential:
                for provider in catalog.providers:
                    if provider.auth == "vault":
                        for reference in catalog.credentials:
                            if reference.provider_id == provider.id:
                                self._credential_for_reference(provider, reference).remove()
            self._bridge_stopper()
            return {"status": "uninstalled", "backup": str(transaction.backup_dir)}

    def launch_claude_code(
        self,
        arguments: Sequence[str] = (),
        *,
        pool: str | None = None,
        executable: str | None = None,
        keep_gateway: bool = False,
        **launcher_options: Any,
    ) -> int:
        """Launch Claude Code against this installation's local gateway."""

        from .hosts.claude_code import launch_claude_code

        return launch_claude_code(
            arguments,
            pool=pool,
            executable=executable,
            codex_home=self.paths.home,
            keep_gateway=keep_gateway,
            **launcher_options,
        )
