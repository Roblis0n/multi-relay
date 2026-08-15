#!/usr/bin/env python3

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RelayPublicLayoutTests(unittest.TestCase):
    def test_repository_root_is_the_relay_skill(self) -> None:
        for path in (
            ROOT / "SKILL.md",
            ROOT / "agents" / "openai.yaml",
            ROOT / "evals" / "evals.json",
            ROOT / "references" / "compatibility.md",
            ROOT / "scripts" / "multi_relay.py",
            ROOT / "scripts" / "multi_relay" / "cli.py",
            ROOT / "configure-multi-relay.cmd",
        ):
            with self.subTest(path=path):
                self.assertTrue(path.is_file(), f"missing public Relay file: {path}")

        for path in (
            ROOT / "codex-deepseek-subagent",
            ROOT / "scripts" / "relay.py",
            ROOT / "scripts" / "deepseek_fanout",
            ROOT / "configure-relay.cmd",
            ROOT / "docs" / "superpowers",
            ROOT / "GITHUB_UPLOAD.md",
            ROOT / "configure-deepseek-subagents.cmd",
        ):
            with self.subTest(path=path):
                self.assertFalse(path.exists(), f"internal or superseded path is public: {path}")

    def test_public_metadata_uses_only_the_relay_identity(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"^---\nname: ([^\n]+)\n", skill)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), "codex-multi-relay")

        agent = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("Codex Multi Relay", agent)
        self.assertIn("$codex-multi-relay", agent)
        self.assertNotIn("$codex-deepseek-relay", agent)
        self.assertNotIn("$codex-deepseek-subagent", agent)

        evals = json.loads((ROOT / "evals" / "evals.json").read_text(encoding="utf-8"))
        self.assertEqual(evals["skill_name"], "codex-multi-relay")

        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Roblis0n", license_text)


if __name__ == "__main__":
    unittest.main()
