#!/usr/bin/env python3

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay.catalog import default_catalog  # noqa: E402
from multi_relay.instructions import (  # noqa: E402
    apply_fanout_instructions,
    remove_fanout_instructions,
)


class FanoutInstructionTests(unittest.TestCase):
    def test_apply_is_idempotent_and_preserves_unrelated_instructions(self) -> None:
        original = "# My rules\n\nAlways run repository tests.\n"

        first = apply_fanout_instructions(original)
        second = apply_fanout_instructions(first)

        self.assertEqual(second, first)
        self.assertTrue(first.startswith(original.rstrip()))
        self.assertEqual(first.count("<!-- BEGIN MULTI-RELAY -->"), 1)
        self.assertEqual(first.count("<!-- END MULTI-RELAY -->"), 1)

    def test_remove_deletes_only_the_managed_block(self) -> None:
        original = "# My rules\n\nKeep this text.\n"
        managed = apply_fanout_instructions(original)

        removed = remove_fanout_instructions(managed)

        self.assertEqual(removed, original)

    def test_policy_encodes_safe_eight_child_fanout_contract(self) -> None:
        policy = apply_fanout_instructions("")

        required_meanings = (
            "two or more independent, bounded work items",
            "one child per work item",
            "at most 8 children",
            "`explorer` for read-heavy",
            "`worker` for isolated writes",
            "explicit `agent_type`",
            "`fork_turns=\"none\"`",
            "Never use full-history inheritance",
            "[Relay task: <target>]",
            "exact complete child message",
            "before the matching `spawn_agent`",
            "followup_task",
            "send_message",
            "overlapping file writes",
            "shared mutable state",
            "Wait for every child",
            "parent verifies",
            "trivial or sequential",
            "more specific instruction",
            "vision, audio",
            "real MCP server",
            "high-trust",
            "final verification",
            "no qualifying child",
            "keep the task in the parent",
        )
        for meaning in required_meanings:
            with self.subTest(meaning=meaning):
                self.assertIn(meaning, policy)

    def test_non_positive_child_limit_is_rejected(self) -> None:
        with self.assertRaises(ManagerError) as raised:
            apply_fanout_instructions("", max_children=0)

        self.assertEqual(raised.exception.code, "invalid_concurrency")

    def test_policy_cannot_claim_more_children_than_catalog_allows(self) -> None:
        with self.assertRaises(ManagerError) as raised:
            apply_fanout_instructions("", max_children=9, catalog=default_catalog())

        self.assertEqual(raised.exception.code, "invalid_concurrency")

    def test_policy_embeds_catalog_agents_and_capabilities(self) -> None:
        policy = apply_fanout_instructions("", catalog=default_catalog())

        for value in ("default", "worker", "explorer", "reviewer"):
            self.assertIn(f"`{value}`", policy)
        self.assertIn("text, tools", policy)
        self.assertIn("text, vision, audio, tools", policy)
        self.assertIn("trust=high", policy)
        self.assertIn("pool=general", policy)
        self.assertNotIn("provider=deepseek", policy)

    def test_apply_replaces_the_legacy_deepseek_managed_block(self) -> None:
        legacy = """# Keep\n\n<!-- BEGIN CODEX-DEEPSEEK-FANOUT -->
legacy managed text
<!-- END CODEX-DEEPSEEK-FANOUT -->
"""

        applied = apply_fanout_instructions(legacy, catalog=default_catalog())

        self.assertIn("# Keep", applied)
        self.assertNotIn("legacy managed text", applied)
        self.assertNotIn("BEGIN CODEX-DEEPSEEK-FANOUT", applied)
        self.assertEqual(applied.count("BEGIN MULTI-RELAY"), 1)


if __name__ == "__main__":
    unittest.main()
