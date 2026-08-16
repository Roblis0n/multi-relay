#!/usr/bin/env python3

from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay.catalog import Catalog, default_catalog  # noqa: E402
from multi_relay.gateway import GATEWAY_BASE_URL  # noqa: E402
from multi_relay.toml_config import (  # noqa: E402
    apply_codex_config,
    build_provider_blocks,
    capture_managed_values,
    remove_codex_config,
    validate_parent_unchanged,
)
from multi_relay import ManagerError  # noqa: E402


class TomlConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth_command = [
            r"C:\Program Files\Python\python.exe",
            r"F:\Skill\Codex\credential_helper.py",
        ]

    @staticmethod
    def multi_catalog() -> Catalog:
        return Catalog.from_dict(
            {
                "schema_version": 1,
                "concurrency": 9,
                "providers": [
                    {
                        "id": "codex",
                        "name": "Native Codex",
                        "protocol": "codex-native",
                        "base_url": None,
                        "auth": "codex",
                        "capabilities": ["text", "vision", "audio", "tools"],
                        "context_window": None,
                        "enabled": True,
                    },
                    {
                        "id": "responses",
                        "name": "Responses Direct",
                        "protocol": "responses-compatible",
                        "base_url": "https://responses.example.test/v1",
                        "auth": "none",
                        "capabilities": ["text", "tools"],
                        "context_window": 200000,
                        "enabled": True,
                    },
                    {
                        "id": "chat",
                        "name": "Generic Chat",
                        "protocol": "chat-completions-compatible",
                        "base_url": "https://chat.example.test/v1",
                        "auth": "vault",
                        "capabilities": ["text", "tools"],
                        "context_window": 128000,
                        "enabled": True,
                    },
                    {
                        "id": "deepseek",
                        "name": "DeepSeek",
                        "protocol": "deepseek-chat",
                        "base_url": "https://api.deepseek.com/v1",
                        "auth": "vault",
                        "capabilities": ["text", "tools"],
                        "context_window": 1000000,
                        "enabled": True,
                    },
                ],
                "agents": [
                    {
                        "name": "reviewer",
                        "description": "review",
                        "provider": "codex",
                        "model": None,
                        "reasoning_effort": None,
                        "context_window": None,
                        "capabilities": ["text", "tools"],
                        "trust": "high",
                        "priority": 1,
                        "sandbox_mode": "read-only",
                        "mcp_servers": {},
                        "skills": [],
                        "developer_instructions": "Review.",
                    }
                ],
            }
        )

    def test_catalog_provider_blocks_expose_one_stable_gateway_route(self) -> None:
        seen: list[tuple[str, bool]] = []

        def auth_factory(provider_id: str, start_bridge: bool) -> list[str]:
            seen.append((provider_id, start_bridge))
            return ["python", "helper.py", "--provider", provider_id]

        rendered = build_provider_blocks(default_catalog(), auth_factory)
        parsed = tomllib.loads(rendered)

        self.assertEqual(set(parsed["model_providers"]), {"multi-relay"})
        self.assertEqual(
            parsed["model_providers"]["multi-relay"]["base_url"],
            GATEWAY_BASE_URL,
        )
        self.assertEqual(
            parsed["model_providers"]["multi-relay"]["wire_api"],
            "responses",
        )
        self.assertEqual(
            parsed["model_providers"]["multi-relay"]["auth"]["args"],
            ["helper.py", "--provider", "local-gateway"],
        )
        self.assertEqual(seen, [("local-gateway", True)])
        self.assertEqual(rendered.count("# BEGIN CODEX-MULTI-RELAY PROVIDERS"), 1)

    def test_responses_vault_auth_uses_helper_without_starting_bridge(self) -> None:
        payload = self.multi_catalog().to_dict()
        responses = {
            **payload["providers"][1],
            "auth_mode": "vault",
        }
        payload["providers"] = [
            responses
        ]
        payload["credentials"] = [
            {
                "id": "responses-default",
                "provider_id": "responses",
                "vault_target": "multi-relay/responses/default",
                "enabled": True,
                "created_at": "2026-08-16T00:00:00Z",
                "label": "Primary",
            }
        ]
        payload["targets"] = [
            {
                "id": "responses-primary",
                "provider_id": "responses",
                "protocol": None,
                "model": "responses-model",
                "credential_id": "responses-default",
                "capabilities": ["text", "tool_calling"],
                "context_window": 200000,
                "max_output_tokens": None,
                "reasoning_efforts": [],
                "trust": "high",
                "host_compatibility": ["codex"],
                "enabled": True,
                "metadata": {},
            }
        ]
        payload["pools"] = [
            {
                "id": "responses-pool",
                "targets": ["responses-primary"],
                "strategy": "sticky",
                "duration_seconds": None,
                "max_rate_limit_wait_seconds": 30,
                "cooldown": {
                    "quota_seconds": 86400,
                    "rate_limit_seconds": 60,
                    "auth_seconds": 3600,
                    "provider_seconds": 30,
                },
                "required_capabilities": ["text", "tool_calling"],
                "host_compatibility": ["codex"],
                "enabled": True,
            }
        ]
        payload["agents"] = [
            {
                **payload["agents"][0],
                "pool_id": "responses-pool",
                "required_capabilities": ["text", "tool_calling"],
            }
        ]
        payload["hosts"]["codex"]["default_pool"] = "responses-pool"
        catalog = Catalog.from_dict(payload)
        calls: list[tuple[str, bool]] = []

        build_provider_blocks(
            catalog,
            lambda provider_id, start_bridge: (
                calls.append((provider_id, start_bridge)) or ["helper", provider_id]
            ),
        )

        self.assertEqual(calls, [("local-gateway", True)])

    def test_native_only_catalog_emits_no_provider_marker(self) -> None:
        payload = self.multi_catalog().to_dict()
        payload["providers"] = [payload["providers"][0]]
        payload["credentials"] = []
        payload["agents"] = [payload["agents"][0]]
        catalog = Catalog.from_dict(payload)

        candidate = apply_codex_config("", catalog)

        self.assertNotIn("CODEX-MULTI-RELAY PROVIDERS", candidate)
        self.assertNotIn("model_providers", tomllib.loads(candidate))

    def test_catalog_apply_is_idempotent_and_preserves_parent(self) -> None:
        original = 'model = "gpt-parent"\nmodel_provider = "openai"\n'
        factory = lambda provider_id, start_bridge: ["helper", provider_id]

        first = apply_codex_config(
            original,
            self.multi_catalog(),
            auth_command_factory=factory,
        )
        second = apply_codex_config(
            first,
            self.multi_catalog(),
            auth_command_factory=factory,
        )

        self.assertEqual(first, second)
        self.assertEqual(tomllib.loads(first)["model"], "gpt-parent")
        self.assertEqual(
            tomllib.loads(first)["agents"]["max_concurrent_threads_per_session"],
            9,
        )

    def test_catalog_apply_removes_legacy_owned_provider_block(self) -> None:
        legacy = (
            "# BEGIN CODEX-DEEPSEEK-FANOUT PROVIDER\n"
            "[model_providers.deepseek]\n"
            'name = "old"\n'
            "# END CODEX-DEEPSEEK-FANOUT PROVIDER\n"
        )

        candidate = apply_codex_config(
            legacy,
            default_catalog(),
            auth_command_factory=lambda provider_id, start_bridge: ["helper", provider_id],
        )

        self.assertNotIn("CODEX-DEEPSEEK-FANOUT", candidate)
        self.assertEqual(candidate.count("[model_providers.multi-relay]\n"), 1)

    def test_apply_preserves_parent_and_enables_eight_native_children(self) -> None:
        original = (
            'model = "gpt-5.6-sol"\n'
            'model_provider = "openai"\n'
            'model_reasoning_effort = "max"\n\n'
            '[features]\n'
            'multi_agent = false\n'
        )

        candidate = apply_codex_config(original, self.auth_command)
        parsed = tomllib.loads(candidate)

        self.assertEqual(parsed["model"], "gpt-5.6-sol")
        self.assertEqual(parsed["model_provider"], "openai")
        self.assertEqual(parsed["model_reasoning_effort"], "max")
        self.assertIs(parsed["features"]["multi_agent"], True)
        self.assertIs(parsed["features"]["multi_agent_v2"]["enabled"], True)
        self.assertIs(
            parsed["features"]["multi_agent_v2"]["hide_spawn_agent_metadata"],
            False,
        )
        self.assertEqual(
            parsed["features"]["multi_agent_v2"]["tool_namespace"],
            "agents",
        )
        self.assertEqual(
            parsed["features"]["multi_agent_v2"][
                "max_concurrent_threads_per_session"
            ],
            8,
        )
        self.assertIs(parsed["agents"]["enabled"], True)
        self.assertEqual(parsed["agents"]["max_concurrent_threads_per_session"], 8)
        self.assertEqual(parsed["model_providers"]["deepseek"]["wire_api"], "responses")
        provider = parsed["model_providers"]["deepseek"]
        self.assertEqual(provider["base_url"], "http://127.0.0.1:42137/v1")
        self.assertEqual(provider["auth"]["command"], self.auth_command[0])
        self.assertEqual(provider["auth"]["args"], self.auth_command[1:])
        self.assertNotIn("experimental_bearer_token", provider)
        validate_parent_unchanged(original, candidate)

    def test_apply_preserves_user_concurrency_above_default(self) -> None:
        original = (
            '[features.multi_agent_v2]\n'
            'enabled = false\n'
            'max_concurrent_threads_per_session = 13\n\n'
            '[agents]\n'
            'enabled = false\n'
            'max_concurrent_threads_per_session = 12\n'
        )

        candidate = apply_codex_config(original, self.auth_command)
        parsed = tomllib.loads(candidate)

        self.assertIs(parsed["agents"]["enabled"], True)
        self.assertEqual(parsed["agents"]["max_concurrent_threads_per_session"], 12)
        self.assertEqual(
            parsed["features"]["multi_agent_v2"][
                "max_concurrent_threads_per_session"
            ],
            13,
        )

    def test_apply_is_idempotent_with_crlf_input(self) -> None:
        original = 'model = "gpt-5.6-sol"\r\n\r\n[features]\r\nmulti_agent = true\r\n'

        first = apply_codex_config(original, self.auth_command)
        second = apply_codex_config(first, self.auth_command)

        self.assertEqual(second, first)
        self.assertEqual(first.count("# BEGIN CODEX-DEEPSEEK-FANOUT PROVIDER"), 1)

    def test_apply_updates_quoted_tables_without_duplicate_tables(self) -> None:
        original = (
            '["features"]\n'
            'multi_agent = false\n\n'
            '["agents"]\n'
            'enabled = false\n'
            'max_concurrent_threads_per_session = 2\n'
        )

        candidate = apply_codex_config(original, self.auth_command)
        parsed = tomllib.loads(candidate)

        self.assertIs(parsed["features"]["multi_agent"], True)
        self.assertIs(parsed["agents"]["enabled"], True)
        self.assertEqual(parsed["agents"]["max_concurrent_threads_per_session"], 8)
        self.assertEqual(candidate.count('["features"]'), 1)
        self.assertEqual(candidate.count('["agents"]'), 1)

    def test_apply_never_emits_legacy_downgrade_or_live_catalog(self) -> None:
        candidate = apply_codex_config("", self.auth_command)

        self.assertNotIn("multi_agent_version", candidate)
        self.assertNotIn("multi_agent_v2 = false", candidate)
        self.assertNotIn("model_catalog_json", candidate)

    def test_apply_converts_boolean_v2_flag_and_uninstall_restores_it(self) -> None:
        original = (
            '[features]\n'
            'multi_agent = false\n'
            'multi_agent_v2 = false\n'
        )
        values = capture_managed_values(original)

        candidate = apply_codex_config(original, self.auth_command)
        parsed = tomllib.loads(candidate)

        self.assertIsInstance(parsed["features"]["multi_agent_v2"], dict)
        self.assertIs(parsed["features"]["multi_agent_v2"]["enabled"], True)
        restored = remove_codex_config(candidate, values)
        restored_parsed = tomllib.loads(restored)
        self.assertIs(restored_parsed["features"]["multi_agent_v2"], False)

    def test_remove_restores_values_owned_before_setup(self) -> None:
        original = (
            '[features]\n'
            'multi_agent = false\n\n'
            '[agents]\n'
            'enabled = false\n'
            'max_concurrent_threads_per_session = 3\n'
        )
        values = capture_managed_values(original)
        candidate = apply_codex_config(original, self.auth_command)

        restored = remove_codex_config(candidate, values)
        parsed = tomllib.loads(restored)

        self.assertIs(parsed["features"]["multi_agent"], False)
        self.assertIs(parsed["agents"]["enabled"], False)
        self.assertEqual(parsed["agents"]["max_concurrent_threads_per_session"], 3)
        self.assertNotIn("model_providers", parsed)

    def test_remove_drops_tables_created_from_a_minimal_config(self) -> None:
        original = 'model = "gpt-5.6-sol"\n'
        values = capture_managed_values(original)
        candidate = apply_codex_config(original, self.auth_command)

        restored = tomllib.loads(remove_codex_config(candidate, values))

        self.assertEqual(restored, {"model": "gpt-5.6-sol"})

    def test_remove_restores_existing_v2_table_and_keeps_unowned_values(self) -> None:
        original = (
            '[features.multi_agent_v2]\n'
            'enabled = false\n'
            'hide_spawn_agent_metadata = true\n'
            'tool_namespace = "old_agents"\n'
            'max_concurrent_threads_per_session = 3\n'
            'usage_hint_enabled = false\n'
        )
        values = capture_managed_values(original)
        candidate = apply_codex_config(original, self.auth_command)

        restored = tomllib.loads(remove_codex_config(candidate, values))

        self.assertEqual(restored["features"]["multi_agent_v2"], {
            "enabled": False,
            "hide_spawn_agent_metadata": True,
            "tool_namespace": "old_agents",
            "max_concurrent_threads_per_session": 3,
            "usage_hint_enabled": False,
        })

    def test_remove_refuses_to_destroy_new_user_v2_values_when_restoring_scalar(self) -> None:
        original = '[features]\nmulti_agent_v2 = false\n'
        values = capture_managed_values(original)
        candidate = apply_codex_config(original, self.auth_command)
        candidate = candidate.replace(
            '[agents]\n',
            'usage_hint_enabled = false\n\n[agents]\n',
            1,
        )

        with self.assertRaises(ManagerError) as raised:
            remove_codex_config(candidate, values)

        self.assertEqual(raised.exception.code, "conflict")


if __name__ == "__main__":
    unittest.main()
