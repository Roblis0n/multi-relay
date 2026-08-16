#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay.catalog import route_agent, save_catalog_bytes  # noqa: E402
from multi_relay.migration import (  # noqa: E402
    migrate_catalog_1_to_2,
    migrate_catalog_file,
)
from multi_relay.transaction import atomic_write as real_atomic_write  # noqa: E402


def legacy_provider(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "deepseek",
        "name": "DeepSeek",
        "protocol": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "auth": "vault",
        "capabilities": ["text", "tools"],
        "context_window": 131072,
        "enabled": True,
    }
    value.update(overrides)
    return value


def legacy_agent(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": "default",
        "description": "General delegated worker.",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "reasoning_effort": "high",
        "context_window": 65536,
        "capabilities": ["text", "tools"],
        "trust": "standard",
        "priority": 10,
        "sandbox_mode": "workspace-write",
        "mcp_servers": {
            "local_docs": {
                "command": "docs-server",
                "args": ["--read-only"],
            }
        },
        "skills": [{"path": "skills/default/SKILL.md", "enabled": True}],
        "developer_instructions": "Complete the bounded implementation task.",
    }
    value.update(overrides)
    return value


def legacy_catalog() -> dict[str, object]:
    return {
        "schema_version": 1,
        "concurrency": 8,
        "providers": [
            legacy_provider(),
            legacy_provider(
                id="codex",
                name="Native Codex",
                protocol="codex-native",
                base_url=None,
                auth="codex",
                capabilities=["text", "vision", "audio", "tools", "web"],
                context_window=None,
            ),
        ],
        "agents": [
            legacy_agent(),
            legacy_agent(
                name="worker",
                description="Implementation specialist.",
                priority=20,
                sandbox_mode="read-only",
                skills=["skills/worker/SKILL.md"],
                developer_instructions="Implement only the assigned change.",
            ),
            legacy_agent(
                name="reviewer",
                description="Native high-trust reviewer.",
                provider="codex",
                model=None,
                reasoning_effort=None,
                context_window=None,
                capabilities=["text", "vision", "tools"],
                trust="high",
                priority=30,
                sandbox_mode="read-only",
                mcp_servers={},
                skills=[],
                developer_instructions="Review the evidence and report defects.",
            ),
        ],
    }


def legacy_default_catalog_fixture() -> dict[str, object]:
    """Frozen schema 1 hybrid default from before the schema 2 model change."""

    deepseek_agents = []
    role_details = {
        "default": (
            "General-purpose DeepSeek child for independent bounded tasks.",
            "Complete only the independent bounded task assigned to the default role.",
            10,
        ),
        "worker": (
            "DeepSeek implementation child for isolated file ownership.",
            "Edit only files explicitly assigned to the worker role; never overlap another child's write set.",
            20,
        ),
        "explorer": (
            "DeepSeek research child for read-heavy repository exploration.",
            "Treat the explorer role as read-heavy: inspect and report, and do not edit files unless the parent explicitly grants an isolated write set.",
            30,
        ),
    }
    for name, (description, rule, priority) in role_details.items():
        deepseek_agents.append(
            legacy_agent(
                name=name,
                description=description,
                reasoning_effort=None,
                context_window=1_000_000,
                priority=priority,
                sandbox_mode=(
                    "workspace-write" if name == "worker" else "read-only"
                ),
                mcp_servers={},
                skills=[],
                developer_instructions=(
                    f"You are Codex's {name} child agent. {rule} "
                    "This is a text-only model: never claim to have inspected images, video, screenshots, "
                    "audio, or other non-text inputs. Follow the parent's task boundary, report concrete "
                    "evidence, and return control when the bounded task is complete."
                ),
            )
        )
    return {
        "schema_version": 1,
        "concurrency": 8,
        "providers": [
            legacy_provider(context_window=1_000_000),
            legacy_provider(
                id="codex",
                name="Native Codex",
                protocol="codex-native",
                base_url=None,
                auth="codex",
                capabilities=["text", "vision", "audio", "tools", "web"],
                context_window=None,
            ),
        ],
        "agents": deepseek_agents
        + [
            legacy_agent(
                name="reviewer",
                description="High-trust native reviewer for risky or media-dependent work.",
                provider="codex",
                model=None,
                reasoning_effort=None,
                context_window=None,
                capabilities=["text", "vision", "audio", "tools"],
                trust="high",
                priority=100,
                sandbox_mode="read-only",
                mcp_servers={},
                skills=[],
                developer_instructions=(
                    "Review high-risk, media-dependent, or provider-boundary work without modifying files. "
                    "Return evidence and recommendations to the parent; the parent retains final verification."
                ),
            )
        ],
    }


class CatalogSchema2MigrationTests(unittest.TestCase):
    def test_frozen_legacy_default_catalog_remains_behaviorally_equivalent(self) -> None:
        source = legacy_default_catalog_fixture()

        catalog = migrate_catalog_1_to_2(source)

        self.assertEqual(
            [item.name for item in catalog.agents],
            ["default", "worker", "explorer", "reviewer"],
        )
        self.assertEqual(len(catalog.pools), 4)
        self.assertEqual(len(catalog.targets), 2)
        self.assertEqual(route_agent(catalog, {"text", "tools"}).name, "default")
        self.assertEqual(
            route_agent(catalog, {"vision"}, high_risk=True).name,
            "reviewer",
        )

    def test_pure_migration_preserves_roles_and_deduplicates_targets(self) -> None:
        source = legacy_catalog()
        original = deepcopy(source)

        catalog = migrate_catalog_1_to_2(source)

        self.assertEqual(source, original)
        self.assertEqual(catalog.schema_version, 2)
        self.assertEqual(catalog.concurrency, source["concurrency"])
        self.assertEqual(
            {item.name for item in catalog.agents},
            {item["name"] for item in source["agents"]},
        )
        self.assertEqual(
            [(item.provider_id, item.id) for item in catalog.credentials],
            [("deepseek", "primary")],
        )
        self.assertEqual(
            catalog.credentials[0].vault_target,
            "codex-deepseek-api-key",
        )

        default = catalog.agent("default")
        worker = catalog.agent("worker")
        reviewer = catalog.agent("reviewer")
        default_target = catalog.pool(default.pool_id).targets[0]
        worker_target = catalog.pool(worker.pool_id).targets[0]
        reviewer_target = catalog.target(catalog.pool(reviewer.pool_id).targets[0])
        self.assertEqual(default_target, worker_target)
        self.assertEqual(reviewer_target.provider_id, "codex")
        self.assertIsNone(reviewer_target.credential_id)
        self.assertEqual(reviewer_target.host_compatibility, ("codex",))
        self.assertEqual(catalog.provider("codex").auth_mode, "host-native")
        self.assertEqual(route_agent(catalog, {"text", "tools"}).name, "default")
        self.assertEqual(
            route_agent(catalog, {"text", "vision"}, high_risk=True).name,
            "reviewer",
        )

        expected = {item["name"]: item for item in source["agents"]}
        for profile in catalog.agents:
            legacy = expected[profile.name]
            self.assertEqual(profile.description, legacy["description"])
            self.assertEqual(
                profile.developer_instructions,
                legacy["developer_instructions"],
            )
            self.assertEqual(profile.sandbox_mode, legacy["sandbox_mode"])
            self.assertEqual(profile.priority, legacy["priority"])
            self.assertEqual(profile.trust, legacy["trust"])
            self.assertEqual(profile.reasoning_effort, legacy["reasoning_effort"])
            self.assertEqual(profile.context_window, legacy["context_window"])
        serialized_profiles = {
            item["name"]: item for item in catalog.to_dict()["agents"]
        }
        self.assertEqual(
            serialized_profiles["default"]["mcp_servers"]["local_docs"],
            source["agents"][0]["mcp_servers"]["local_docs"],
        )
        self.assertEqual(
            [dict(item) for item in worker.skills],
            [{"path": "skills/worker/SKILL.md", "enabled": True}],
        )

    def test_primary_credential_ids_are_scoped_per_provider(self) -> None:
        source = legacy_catalog()
        source["providers"].append(
            legacy_provider(
                id="vendor",
                name="Vendor",
                protocol="chat-completions-compatible",
                base_url="https://api.vendor.test/v1",
                context_window=64000,
            )
        )
        source["agents"].append(
            legacy_agent(
                name="vendor-worker",
                provider="vendor",
                model="vendor-model",
                context_window=32000,
                mcp_servers={},
                skills=[],
            )
        )

        catalog = migrate_catalog_1_to_2(source)

        self.assertEqual(
            {(item.provider_id, item.id) for item in catalog.credentials},
            {("deepseek", "primary"), ("vendor", "primary")},
        )
        self.assertEqual(
            catalog.credential("primary", provider_id="vendor").vault_target,
            "codex-multi-relay-vendor-api-key",
        )
        self.assertEqual(
            catalog.target(
                catalog.pool(catalog.agent("vendor-worker").pool_id).targets[0]
            ).credential_id,
            "primary",
        )

    def test_deduplicated_target_aggregates_context_efforts_and_trust(self) -> None:
        source = legacy_catalog()
        source["agents"] = [
            legacy_agent(
                context_window=32000,
                reasoning_effort="low",
            ),
            legacy_agent(
                name="trusted-worker",
                trust="high",
                context_window=96000,
                reasoning_effort="max",
                mcp_servers={},
                skills=[],
            ),
        ]

        catalog = migrate_catalog_1_to_2(source)
        target_ids = {
            catalog.pool(item.pool_id).targets[0] for item in catalog.agents
        }
        target = catalog.target(next(iter(target_ids)))

        self.assertEqual(len(target_ids), 1)
        self.assertEqual(target.context_window, 131072)
        self.assertEqual(target.reasoning_efforts, ("low", "max"))
        self.assertEqual(target.trust, "high")

    def test_pure_migration_is_deterministic_and_idempotent(self) -> None:
        first = migrate_catalog_1_to_2(legacy_catalog())
        repeated = migrate_catalog_1_to_2(legacy_catalog())
        already_current = migrate_catalog_1_to_2(first.to_dict())

        self.assertEqual(save_catalog_bytes(first), save_catalog_bytes(repeated))
        self.assertEqual(save_catalog_bytes(first), save_catalog_bytes(already_current))

    def test_legacy_deepseek_credential_is_reference_only(self) -> None:
        catalog = migrate_catalog_1_to_2(legacy_catalog())
        serialized = save_catalog_bytes(catalog)

        self.assertIn(b"codex-deepseek-api-key", serialized)
        self.assertNotIn(b"api_key", serialized.lower())
        self.assertNotIn(b"authorization", serialized.lower())

    def test_file_migration_backs_up_exact_bytes_before_atomic_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "catalog.json"
            backup_root = root / "backups"
            old_bytes = json.dumps(
                legacy_catalog(),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            source.write_bytes(old_bytes)
            original_mode = source.stat().st_mode & 0o777

            result = migrate_catalog_file(source, backup_root)

            self.assertTrue(result.changed)
            self.assertEqual(result.source_schema, 1)
            self.assertEqual(result.source_sha256, hashlib.sha256(old_bytes).hexdigest())
            self.assertIsNotNone(result.backup_path)
            self.assertEqual(result.backup_path.read_bytes(), old_bytes)
            self.assertEqual(json.loads(source.read_text(encoding="utf-8"))["schema_version"], 2)
            self.assertEqual(source.stat().st_mode & 0o777, original_mode)

            current_bytes = source.read_bytes()
            repeated = migrate_catalog_file(source, backup_root)
            self.assertFalse(repeated.changed)
            self.assertEqual(repeated.source_schema, 2)
            self.assertIsNone(repeated.backup_path)
            self.assertEqual(source.read_bytes(), current_bytes)

    def test_catalog_write_failure_leaves_schema1_source_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "catalog.json"
            old_bytes = json.dumps(legacy_catalog(), ensure_ascii=False).encode("utf-8")
            source.write_bytes(old_bytes)

            def fail_write(path: Path, data: bytes, mode: int = 0o600) -> None:
                path.write_bytes(b"partially-written")
                raise OSError("injected catalog write failure")

            with self.assertRaises(ManagerError) as raised:
                migrate_catalog_file(
                    source,
                    root / "backups",
                    catalog_writer=fail_write,
                )

            self.assertEqual(raised.exception.code, "catalog_migration_failed")
            self.assertEqual(source.read_bytes(), old_bytes)
            backups = list((root / "backups").glob("*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), old_bytes)

    def test_concurrent_source_change_is_detected_and_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "catalog.json"
            backup_root = root / "backups"
            old_bytes = json.dumps(legacy_catalog(), ensure_ascii=False).encode("utf-8")
            concurrent_bytes = b"concurrent-owner-update"
            source.write_bytes(old_bytes)

            def backup_then_race(path: Path, data: bytes, mode: int = 0o600) -> None:
                real_atomic_write(path, data, mode)
                if path.parent == backup_root:
                    source.write_bytes(concurrent_bytes)

            with patch(
                "multi_relay.migration.atomic_write",
                side_effect=backup_then_race,
            ):
                with self.assertRaises(ManagerError) as raised:
                    migrate_catalog_file(source, backup_root)

            self.assertEqual(raised.exception.code, "catalog_changed")
            self.assertEqual(source.read_bytes(), concurrent_bytes)
            backups = list(backup_root.glob("*.json"))
            self.assertEqual(backups, [])

    def test_invalid_catalog_is_actionable_and_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "catalog.json"
            old_bytes = b'{"schema_version":1,"providers":['
            source.write_bytes(old_bytes)

            with self.assertRaises(ManagerError) as raised:
                migrate_catalog_file(source, root / "backups")

            self.assertEqual(raised.exception.code, "catalog_invalid")
            self.assertIn("catalog", str(raised.exception).casefold())
            self.assertEqual(source.read_bytes(), old_bytes)
            self.assertFalse((root / "backups").exists())


if __name__ == "__main__":
    unittest.main()
