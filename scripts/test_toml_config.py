#!/usr/bin/env python3

from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path


PACKAGE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "codex-deepseek-subagent"
    / "scripts"
)
sys.path.insert(0, str(PACKAGE_ROOT))

from deepseek_fanout.toml_config import (  # noqa: E402
    apply_codex_config,
    capture_managed_values,
    remove_codex_config,
    validate_parent_unchanged,
)
from deepseek_fanout import ManagerError  # noqa: E402


class TomlConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth_command = [
            r"C:\Program Files\Python\python.exe",
            r"F:\Skill\Codex\credential_helper.py",
        ]

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
