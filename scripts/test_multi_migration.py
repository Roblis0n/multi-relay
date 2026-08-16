#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError, resolve_paths  # noqa: E402
from multi_relay.catalog import (  # noqa: E402
    Catalog,
    default_catalog,
    load_catalog,
    save_catalog_bytes,
)
from multi_relay.compatibility import CompatibilityReport  # noqa: E402
from multi_relay.manager import RelayManager, SCHEMA_VERSION  # noqa: E402
from multi_relay.migration import (  # noqa: E402
    catalog_from_schema4,
    inspect_catalog_migration,
)
from multi_relay.model_capabilities import ModelSelection  # noqa: E402
from multi_relay.roles import expected_agent_files  # noqa: E402
from test_catalog_migration2 import legacy_catalog  # noqa: E402


class FakeStore:
    def __init__(self, secret: str | None = "sk-test") -> None:
        self.secret = secret
        self.remove_calls = 0

    def exists(self) -> bool:
        return self.secret is not None

    def read(self) -> str | None:
        return self.secret

    def store(self, secret: str) -> None:
        self.secret = secret

    def remove(self) -> bool:
        self.remove_calls += 1
        existed = self.secret is not None
        self.secret = None
        return existed


def report(selection: ModelSelection) -> CompatibilityReport:
    return CompatibilityReport(
        model=selection.resolved_model,
        effort=selection.reasoning_effort,
        provider_initialized=True,
        single_child_passed=True,
        fanout_passed=True,
        tools_passed=True,
        resume_passed=True,
        child_metadata_passed=True,
        parent_unchanged=True,
    )


class MultiRelayMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection = ModelSelection(
            requested_model="deepseek-v4-pro",
            resolved_model="deepseek-v4-pro",
            reasoning_effort="xhigh",
            effort_source="test",
        )

    def manager(
        self,
        home: Path,
        *,
        store: FakeStore | None = None,
        events: list[str] | None = None,
    ) -> RelayManager:
        timeline = events if events is not None else []
        return RelayManager(
            resolve_paths(str(home)),
            "codex.exe",
            credentials=store or FakeStore(),
            model_discoverer=lambda secret: timeline.append("discover") or "deepseek-v4-pro",
            selection_resolver=lambda model: timeline.append("effort") or self.selection,
            compatibility_gate=lambda binary, path, selection: timeline.append("gate")
            or CompatibilityReport(
                model=selection.resolved_model,
                effort=selection.reasoning_effort,
                provider_initialized=True,
                single_child_passed=None,
                fanout_passed=None,
                tools_passed=None,
                resume_passed=None,
                child_metadata_passed=None,
                parent_unchanged=None,
            ),
            live_acceptance=lambda binary, path, selection: timeline.append("live")
            or report(selection),
            bridge_stopper=lambda: True,
        )

    def test_schema4_catalog_migration_preserves_model_and_effort(self) -> None:
        catalog = catalog_from_schema4(
            {
                "schema_version": 4,
                "selection": {
                    "resolved_model": "deepseek-migrated-model",
                    "reasoning_effort": "low",
                },
                "concurrency": 11,
            }
        )

        deepseek_targets = [
            item for item in catalog.targets if item.provider_id == "deepseek"
        ]
        deepseek_agents = [
            item for item in catalog.agents if item.provider == "deepseek"
        ]
        self.assertEqual(
            {item.model for item in deepseek_targets},
            {"deepseek-migrated-model"},
        )
        self.assertEqual(
            {item.reasoning_effort for item in deepseek_agents},
            {"low"},
        )
        self.assertEqual(catalog.concurrency, 11)

    def test_manager_transaction_migrates_schema1_and_records_backup_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = resolve_paths(str(home))
            (home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\n',
                encoding="utf-8",
            )
            paths.state_dir.mkdir(parents=True)
            old_bytes = json.dumps(
                legacy_catalog(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            old_hash = hashlib.sha256(old_bytes).hexdigest()
            paths.catalog.write_bytes(old_bytes)
            paths.manifest.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "catalog_schema_version": 1,
                        "status": "disabled",
                        "catalog_sha256": old_hash,
                        "managed_files": {
                            paths.catalog.relative_to(home).as_posix(): old_hash,
                        },
                        "original_values": {},
                        "instruction_file_preexisted": False,
                        "config_preexisted": True,
                    }
                ),
                encoding="utf-8",
            )
            manager = self.manager(home)

            manager.setup()

            migrated = load_catalog(paths.catalog)
            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
            migration_backup = Path(manifest["catalog_migration_backup"])
            self.assertEqual(migrated.schema_version, 2)
            self.assertEqual(manifest["catalog_source_schema"], 1)
            self.assertEqual(manifest["catalog_source_sha256"], old_hash)
            self.assertTrue(migration_backup.is_file())
            self.assertEqual(migration_backup.read_bytes(), old_bytes)

            manager.setup()
            repeated = json.loads(paths.manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                repeated["catalog_migration_backup"],
                str(migration_backup),
            )
            self.assertEqual(repeated["catalog_source_sha256"], old_hash)

    def test_apply_uses_one_schema1_read_for_catalog_and_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = resolve_paths(str(home))
            (home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\n',
                encoding="utf-8",
            )
            paths.state_dir.mkdir(parents=True)
            old_bytes = json.dumps(
                legacy_catalog(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            old_hash = hashlib.sha256(old_bytes).hexdigest()
            paths.catalog.write_bytes(old_bytes)
            paths.manifest.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "catalog_schema_version": 1,
                        "status": "disabled",
                        "catalog_sha256": old_hash,
                        "managed_files": {
                            paths.catalog.relative_to(home).as_posix(): old_hash,
                        },
                        "original_values": {},
                        "instruction_file_preexisted": False,
                        "config_preexisted": True,
                    }
                ),
                encoding="utf-8",
            )
            manager = self.manager(home)

            with patch(
                "multi_relay.relay_manager.inspect_catalog_migration",
                wraps=inspect_catalog_migration,
            ) as inspected:
                manager.apply()

            self.assertEqual(inspected.call_count, 1)
            self.assertEqual(load_catalog(paths.catalog).schema_version, 2)

    def test_schema1_catalog_in_prior_state_directory_is_adopted_transactionally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = resolve_paths(str(home))
            (home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\n',
                encoding="utf-8",
            )
            paths.relay_state_dir.mkdir(parents=True)
            prior_catalog = paths.relay_state_dir / "catalog.json"
            old_bytes = json.dumps(
                legacy_catalog(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            old_hash = hashlib.sha256(old_bytes).hexdigest()
            prior_catalog.write_bytes(old_bytes)
            paths.relay_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "catalog_schema_version": 1,
                        "status": "disabled",
                        "catalog_sha256": old_hash,
                        "managed_files": {
                            prior_catalog.relative_to(home).as_posix(): old_hash,
                        },
                        "original_values": {},
                        "instruction_file_preexisted": False,
                        "config_preexisted": True,
                    }
                ),
                encoding="utf-8",
            )
            manager = self.manager(home)

            manager.setup()

            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
            migration_backup = Path(manifest["catalog_migration_backup"])
            self.assertEqual(load_catalog(paths.catalog).schema_version, 2)
            self.assertEqual(migration_backup.read_bytes(), old_bytes)
            self.assertFalse(prior_catalog.exists())
            self.assertFalse(paths.relay_manifest.exists())

    def test_enable_persists_schema1_migration_before_enabling_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = resolve_paths(str(home))
            (home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\n',
                encoding="utf-8",
            )
            paths.state_dir.mkdir(parents=True)
            old_bytes = json.dumps(
                legacy_catalog(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            old_hash = hashlib.sha256(old_bytes).hexdigest()
            paths.catalog.write_bytes(old_bytes)
            paths.manifest.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "catalog_schema_version": 1,
                        "status": "disabled",
                        "catalog_sha256": old_hash,
                        "managed_files": {
                            paths.catalog.relative_to(home).as_posix(): old_hash,
                        },
                        "original_values": {},
                        "instruction_file_preexisted": False,
                        "config_preexisted": True,
                    }
                ),
                encoding="utf-8",
            )
            manager = self.manager(home)

            manager.enable()

            manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
            self.assertEqual(load_catalog(paths.catalog).schema_version, 2)
            self.assertEqual(manifest["catalog_schema_version"], 2)
            self.assertEqual(manifest["catalog_source_schema"], 1)
            self.assertEqual(
                Path(manifest["catalog_migration_backup"]).read_bytes(),
                old_bytes,
            )

    def test_paths_use_new_state_and_keep_both_legacy_locations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = resolve_paths(directory)

        self.assertEqual(paths.state_dir, root / "codex-multi-relay")
        self.assertEqual(paths.catalog, paths.state_dir / "catalog.json")
        self.assertEqual(paths.relay_state_dir, root / "codex-deepseek-relay")
        self.assertEqual(paths.relay_manifest, paths.relay_state_dir / "manifest.json")
        self.assertEqual(paths.legacy_state_dir, root / "codex-deepseek-subagent")

    def test_native_setup_never_reads_a_deepseek_credential_or_runs_network_gates(self) -> None:
        events: list[str] = []
        store = FakeStore(None)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
            manager = self.manager(home, store=store, events=events)

            result = manager.setup(preset="native")
            catalog = load_catalog(manager.paths.catalog)
            manifest = json.loads(manager.paths.manifest.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "ready")
        self.assertEqual([item.name for item in catalog.agents], ["reviewer"])
        self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
        self.assertEqual(events, [])

    def test_schema4_state_is_adopted_in_precedence_order_as_catalog_schema1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = resolve_paths(str(home))
            (home / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
            old_roles = expected_agent_files(home / "agents", self.selection)
            for path, content in old_roles.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            paths.relay_state_dir.mkdir(parents=True)
            paths.relay_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 4,
                        "status": "enabled",
                        "selection": {
                            "requested_model": self.selection.requested_model,
                            "resolved_model": self.selection.resolved_model,
                            "reasoning_effort": self.selection.reasoning_effort,
                            "effort_source": self.selection.effort_source,
                        },
                        "concurrency": 8,
                        "original_values": {},
                        "instruction_file_preexisted": False,
                        "config_preexisted": True,
                        "managed_files": {
                            path.relative_to(home).as_posix(): hashlib.sha256(content).hexdigest()
                            for path, content in old_roles.items()
                        },
                    }
                ),
                encoding="utf-8",
            )
            manager = self.manager(home)

            manager.setup()
            catalog = load_catalog(paths.catalog)

            self.assertEqual({item.name for item in catalog.agents}, {"default", "worker", "explorer", "reviewer"})
            self.assertFalse(paths.relay_manifest.exists())
            self.assertEqual(json.loads(paths.manifest.read_text(encoding="utf-8"))["schema_version"], 5)

    def test_future_manifest_schema_is_refused_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = resolve_paths(str(home))
            config = home / "config.toml"
            config.write_bytes(b'model = "gpt-5.6-sol"\n')
            paths.state_dir.mkdir(parents=True)
            paths.manifest.write_text('{"schema_version":999}\n', encoding="utf-8")
            before = config.read_bytes()
            manager = self.manager(home)

            with self.assertRaises(ManagerError) as raised:
                manager.setup()

            self.assertEqual(raised.exception.code, "unsupported_manifest_schema")
            self.assertEqual(config.read_bytes(), before)

    def test_unowned_legacy_managed_blocks_are_never_adopted(self) -> None:
        cases = {
            "provider": (
                'model = "gpt-parent"\n\n'
                '# BEGIN CODEX-DEEPSEEK-FANOUT PROVIDER\n'
                '[model_providers.deepseek]\n'
                'name = "User-owned lookalike"\n'
                'base_url = "https://example.test/v1"\n'
                'wire_api = "responses"\n'
                '# END CODEX-DEEPSEEK-FANOUT PROVIDER\n',
                "",
            ),
            "instructions": (
                'model = "gpt-parent"\n',
                '<!-- BEGIN CODEX-DEEPSEEK-FANOUT -->\n'
                'User-owned lookalike instructions.\n'
                '<!-- END CODEX-DEEPSEEK-FANOUT -->\n',
            ),
        }
        for name, (config_text, instruction_text) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                config = home / "config.toml"
                instructions = home / "AGENTS.md"
                config.write_text(config_text, encoding="utf-8")
                if instruction_text:
                    instructions.write_text(instruction_text, encoding="utf-8")
                before_config = config.read_bytes()
                before_instructions = (
                    instructions.read_bytes() if instructions.exists() else None
                )
                manager = self.manager(home)

                with self.assertRaises(ManagerError) as raised:
                    manager.setup(preset="native")

                self.assertEqual(raised.exception.code, "conflict")
                self.assertEqual(config.read_bytes(), before_config)
                self.assertEqual(
                    instructions.read_bytes() if instructions.exists() else None,
                    before_instructions,
                )

    def test_provider_in_use_cannot_be_removed_and_routing_has_no_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
            manager = self.manager(home)
            manager.setup(preset="native")

            with self.assertRaises(ManagerError) as raised:
                manager.remove_provider("codex")
            routed = manager.route({"vision"}, high_risk=True)
            parent = manager.route({"web"})

        self.assertEqual(raised.exception.code, "provider_in_use")
        self.assertEqual(routed["agent"], "reviewer")
        self.assertEqual(parent["status"], "parent_required")

    def test_default_catalog_is_written_in_the_same_transaction_as_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
            reviewer = home / "agents" / "reviewer.toml"
            reviewer.parent.mkdir(parents=True)
            reviewer.write_bytes(b"user-owned = true\n")
            manager = self.manager(home)

            with self.assertRaises(ManagerError):
                manager.setup()

            self.assertFalse(manager.paths.catalog.exists())
            self.assertFalse(manager.paths.manifest.exists())
            self.assertEqual(reviewer.read_bytes(), b"user-owned = true\n")

    def test_custom_provider_and_agent_mutations_regenerate_config_and_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(
                'model = "gpt-parent"\nmodel_provider = "openai"\n',
                encoding="utf-8",
            )
            manager = self.manager(home)
            manager.setup(preset="native")
            manager.add_provider(
                {
                    "id": "vendor",
                    "name": "Vendor",
                    "protocol": "responses-compatible",
                    "base_url": "https://api.vendor.test/v1",
                    "auth": "none",
                    "capabilities": ["text", "tools"],
                    "context_window": 64000,
                    "enabled": True,
                }
            )
            manager.set_agent(
                {
                    "name": "vendor-worker",
                    "description": "Vendor implementation worker.",
                    "provider": "vendor",
                    "model": "vendor-model",
                    "reasoning_effort": "medium",
                    "context_window": 64000,
                    "capabilities": ["text", "tools"],
                    "trust": "standard",
                    "priority": 5,
                    "sandbox_mode": "workspace-write",
                    "mcp_servers": {},
                    "skills": [],
                    "developer_instructions": "Implement only the assigned bounded task.",
                }
            )

            parsed = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
            catalog = load_catalog(manager.paths.catalog)
            agent_file = home / "agents" / "vendor-worker.toml"

            self.assertEqual(parsed["model"], "gpt-parent")
            self.assertEqual(parsed["model_provider"], "openai")
            self.assertEqual(
                parsed["model_providers"]["vendor"]["base_url"],
                "https://api.vendor.test/v1",
            )
            self.assertIsNone(parsed["model_providers"]["vendor"].get("auth"))
            self.assertEqual(catalog.agent("vendor-worker").model, "vendor-model")
            self.assertTrue(agent_file.is_file())

            manager.remove_agent("vendor-worker")
            manager.remove_provider("vendor")

            self.assertFalse(agent_file.exists())
            self.assertNotIn(
                "vendor",
                tomllib.loads((home / "config.toml").read_text(encoding="utf-8")).get(
                    "model_providers",
                    {},
                ),
            )

    def test_remove_agent_preserves_unrelated_unpooled_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(
                'model = "gpt-parent"\n',
                encoding="utf-8",
            )
            manager = self.manager(home)
            manager.setup(preset="native")
            manager.add_provider(
                {
                    "id": "vendor",
                    "name": "Vendor",
                    "protocol": "responses-compatible",
                    "base_url": "https://api.vendor.test/v1",
                    "auth": "none",
                    "capabilities": ["text", "tools"],
                    "context_window": 64000,
                    "enabled": True,
                }
            )
            manager.set_agent(
                {
                    "name": "vendor-worker",
                    "description": "Vendor worker.",
                    "provider": "vendor",
                    "model": "vendor-model",
                    "reasoning_effort": None,
                    "context_window": 64000,
                    "capabilities": ["text", "tools"],
                    "trust": "standard",
                    "priority": 5,
                    "sandbox_mode": "workspace-write",
                    "mcp_servers": {},
                    "skills": [],
                    "developer_instructions": "Implement the bounded task.",
                }
            )
            payload = load_catalog(manager.paths.catalog).to_dict()
            reserved = {
                **next(
                    item
                    for item in payload["targets"]
                    if item["provider_id"] == "vendor"
                ),
                "id": "reserved-target",
                "metadata": {"purpose": "reserved"},
            }
            payload["targets"].append(reserved)
            manager.paths.catalog.write_bytes(
                save_catalog_bytes(Catalog.from_dict(payload))
            )

            manager.remove_agent("vendor-worker")
            remaining = load_catalog(manager.paths.catalog)

        self.assertIn(
            "reserved-target",
            {item.id for item in remaining.targets},
        )

    def test_legacy_agent_set_refuses_to_overwrite_shared_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(
                'model = "gpt-parent"\n',
                encoding="utf-8",
            )
            manager = self.manager(home)
            manager.setup(preset="native")
            manager.add_provider(
                {
                    "id": "vendor",
                    "name": "Vendor",
                    "protocol": "responses-compatible",
                    "base_url": "https://api.vendor.test/v1",
                    "auth": "none",
                    "capabilities": ["text", "tools"],
                    "context_window": 64000,
                    "enabled": True,
                }
            )
            legacy_agent = {
                "name": "vendor-worker",
                "description": "Vendor worker.",
                "provider": "vendor",
                "model": "vendor-model",
                "reasoning_effort": None,
                "context_window": 64000,
                "capabilities": ["text", "tools"],
                "trust": "standard",
                "priority": 5,
                "sandbox_mode": "workspace-write",
                "mcp_servers": {},
                "skills": [],
                "developer_instructions": "Implement the bounded task.",
            }
            manager.set_agent(legacy_agent)
            payload = load_catalog(manager.paths.catalog).to_dict()
            shared = {
                **next(
                    item
                    for item in payload["agents"]
                    if item["name"] == "vendor-worker"
                ),
                "name": "observer",
                "description": "Shared-pool observer.",
            }
            payload["agents"].append(shared)
            manager.paths.catalog.write_bytes(
                save_catalog_bytes(Catalog.from_dict(payload))
            )
            replacement = {**legacy_agent, "model": "replacement-model"}

            with self.assertRaises(ManagerError) as raised:
                manager.set_agent(replacement)

        self.assertEqual(raised.exception.code, "routing_in_use")

    def test_setup_is_idempotent_and_preserves_a_custom_schema5_catalog(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text('model = "gpt-parent"\n', encoding="utf-8")
            manager = self.manager(home, events=events)
            manager.setup(preset="native")
            manager.add_provider(
                {
                    "id": "vendor",
                    "name": "Vendor",
                    "protocol": "responses-compatible",
                    "base_url": "https://api.vendor.test/v1",
                    "auth": "none",
                    "capabilities": ["text", "tools"],
                    "context_window": 64000,
                    "enabled": True,
                }
            )
            manager.set_agent(
                {
                    "name": "vendor-worker",
                    "description": "Vendor implementation worker.",
                    "provider": "vendor",
                    "model": "vendor-model",
                    "reasoning_effort": "medium",
                    "context_window": 64000,
                    "capabilities": ["text", "tools"],
                    "trust": "standard",
                    "priority": 5,
                    "sandbox_mode": "workspace-write",
                    "mcp_servers": {},
                    "skills": [],
                    "developer_instructions": "Implement only the assigned bounded task.",
                }
            )
            before = manager.paths.catalog.read_bytes()
            events.clear()

            result = manager.setup(preset="hybrid")

            self.assertEqual(manager.paths.catalog.read_bytes(), before)
            self.assertEqual(
                {item.name for item in load_catalog(manager.paths.catalog).agents},
                {"reviewer", "vendor-worker"},
            )
            self.assertEqual(result["status"], "ready")
            self.assertEqual(events, [])

    def test_catalog_mutation_preserves_disabled_install_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text('model = "gpt-parent"\n', encoding="utf-8")
            manager = self.manager(home)
            manager.setup(preset="native")
            manager.disable()
            reviewer = default_catalog("native").agent("reviewer").to_dict()
            reviewer["description"] = "Updated while disabled."

            result = manager.set_agent(reviewer)
            manifest = json.loads(manager.paths.manifest.read_text(encoding="utf-8"))
            catalog = load_catalog(manager.paths.catalog)
            instructions = (
                manager.paths.instruction_file.read_text(encoding="utf-8")
                if manager.paths.instruction_file.exists()
                else ""
            )

            self.assertEqual(result["status"], "disabled")
            self.assertEqual(manifest["status"], "disabled")
            self.assertEqual(catalog.agent("reviewer").description, "Updated while disabled.")
            self.assertFalse((home / "agents" / "reviewer.toml").exists())
            self.assertNotIn("BEGIN CODEX-MULTI-RELAY", instructions)


if __name__ == "__main__":
    unittest.main()
