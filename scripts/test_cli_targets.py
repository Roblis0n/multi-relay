#!/usr/bin/env python3

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError, resolve_paths  # noqa: E402
from multi_relay.catalog import AgentProfile, ExecutionTarget, ProviderSpec, TargetPool  # noqa: E402
from multi_relay.cli import build_parser  # noqa: E402
from multi_relay.relay_manager import RelayManager  # noqa: E402


class FakeStore:
    def exists(self) -> bool:
        return False

    def read(self):
        return None

    def store(self, secret: str) -> None:
        self.secret = secret

    def remove(self) -> bool:
        return False


def installed_manager(root: Path) -> RelayManager:
    home = root / "codex"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-parent"\n', encoding="utf-8")
    store = FakeStore()
    manager = RelayManager(
        resolve_paths(str(home), state_home=root / "state", user_home=root),
        "codex",
        credentials=store,
        credential_reference_factory=lambda provider, reference: store,
        bridge_stopper=lambda: True,
    )
    manager.setup(preset="native")
    return manager


class TargetCliAndManagerTests(unittest.TestCase):
    def test_parser_exposes_every_target_action(self) -> None:
        parser = build_parser()
        for action, tail in {
            "list": [],
            "test": ["target-one"],
            "enable": ["target-one"],
            "disable": ["target-one"],
            "remove": ["target-one"],
            "edit": ["target-one", "--model", "m2"],
            "add": [
                "--id", "target-one", "--provider", "vendor", "--model", "m1",
                "--capability", "text", "--host", "codex",
            ],
        }.items():
            with self.subTest(action=action):
                parsed = parser.parse_args(["target", action, *tail])
                self.assertEqual(parsed.target_command, action)

    def test_provider_target_pool_agent_creation_and_reference_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = installed_manager(Path(directory))
            manager.add_provider(
                ProviderSpec.from_dict(
                    {
                        "id": "vendor",
                        "name": "Vendor",
                        "protocol": "responses-compatible",
                        "base_url": "https://vendor.example/v1",
                        "auth_mode": "vault",
                        "capabilities": ["text"],
                        "models_endpoint": None,
                        "enabled": True,
                    }
                )
            )
            manager.add_credential("vendor", "backup", secret="provider-token")
            manager.add_target(
                ExecutionTarget.from_dict(
                    {
                        "id": "vendor-primary",
                        "provider_id": "vendor",
                        "protocol": None,
                        "model": "vendor-model",
                        "credential_id": "backup",
                        "capabilities": ["text"],
                        "context_window": 64000,
                        "max_output_tokens": None,
                        "reasoning_efforts": [],
                        "trust": "standard",
                        "host_compatibility": ["codex"],
                        "enabled": True,
                        "metadata": {},
                    }
                )
            )
            manager.add_pool(
                TargetPool.from_dict(
                    {
                        "id": "vendor-pool",
                        "targets": ["vendor-primary"],
                        "strategy": "sticky",
                        "duration_seconds": None,
                        "max_rate_limit_wait_seconds": 30,
                        "cooldown": {
                            "quota_seconds": 86400,
                            "rate_limit_seconds": 60,
                            "auth_seconds": 3600,
                            "provider_seconds": 30,
                        },
                        "required_capabilities": ["text"],
                        "host_compatibility": ["codex"],
                        "enabled": True,
                    }
                )
            )
            manager.set_agent(
                AgentProfile.from_dict(
                    {
                        "name": "vendor-worker",
                        "description": "Vendor worker",
                        "developer_instructions": "Work only on the bounded task.",
                        "pool_id": "vendor-pool",
                        "required_capabilities": ["text"],
                        "fallback_pool_id": None,
                        "reasoning_effort": None,
                        "context_window": None,
                        "trust": "standard",
                        "priority": 50,
                        "sandbox_mode": "workspace-write",
                        "tools": [],
                        "mcp_servers": {},
                        "skills": [],
                        "hosts": ["codex"],
                    }
                )
            )

            with self.assertRaises(ManagerError) as raised:
                manager.remove_provider("vendor")

            self.assertEqual(raised.exception.code, "provider_in_use")
            self.assertEqual(raised.exception.details["targets"], ["vendor-primary"])
            self.assertEqual(manager.test_target("vendor-primary")["details"]["model_available"], "unknown")
            with self.assertRaises(ManagerError) as credential_in_use:
                manager.remove_credential("vendor", "backup")
            self.assertEqual(credential_in_use.exception.code, "credential_in_use")
            self.assertEqual(manager.catalog()["agents"][-1]["pool_id"], "vendor-pool")

    def test_target_remove_lists_referencing_pools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = installed_manager(Path(directory))
            native_target = manager.list_targets()[0]["id"]

            with self.assertRaises(ManagerError) as raised:
                manager.remove_target(str(native_target))

            self.assertEqual(raised.exception.code, "target_in_use")
            self.assertTrue(raised.exception.details["pools"])


if __name__ == "__main__":
    unittest.main()
