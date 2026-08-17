#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from multi_relay import (  # noqa: E402
    CLI_NAME,
    MANAGEMENT_PREFIX,
    OWNERSHIP_MARKER,
    PACKAGE_NAME,
    PRODUCT_NAME,
    REPOSITORY_NAME,
    ManagerError,
    resolve_paths,
)
from multi_relay.cli import build_parser  # noqa: E402
from multi_relay.manager import FanoutManager, RelayManager  # noqa: E402


class RebrandTests(unittest.TestCase):
    def test_canonical_identity_is_centralized(self) -> None:
        self.assertEqual(PRODUCT_NAME, "Multi Relay")
        self.assertEqual(REPOSITORY_NAME, "multi-relay")
        self.assertEqual(PACKAGE_NAME, "multi_relay")
        self.assertEqual(CLI_NAME, "multi-relay")
        self.assertEqual(OWNERSHIP_MARKER, "MULTI-RELAY")
        self.assertEqual(MANAGEMENT_PREFIX, "/_multi-relay")
        self.assertIn("Multi Relay", build_parser().description or "")

    def test_product_state_is_separate_from_host_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = resolve_paths(
                str(root / "codex"),
                state_home=root / "state",
                platform="linux",
                user_home=root / "user",
            )

        self.assertEqual(paths.state_dir, root / "state" / "multi-relay")
        self.assertEqual(paths.codex_state_dir, root / "codex" / "codex-multi-relay")
        self.assertEqual(
            paths.legacy_state_dirs,
            (
                root / "codex" / "codex-multi-relay",
                root / "codex" / "codex-deepseek-relay",
                root / "codex" / "codex-deepseek-subagent",
            ),
        )

    def test_divergent_canonical_and_legacy_state_is_a_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = resolve_paths(
                str(root / "codex"),
                state_home=root / "state",
                platform="linux",
                user_home=root / "user",
            )
            paths.manifest.parent.mkdir(parents=True)
            paths.codex_manifest.parent.mkdir(parents=True)
            paths.manifest.write_text(json.dumps({"schema_version": 5}), encoding="utf-8")
            paths.codex_manifest.write_text(json.dumps({"schema_version": 4}), encoding="utf-8")
            manager = RelayManager(paths, "codex")

            with self.assertRaises(ManagerError) as raised:
                manager._read_manifest()

        self.assertEqual(raised.exception.code, "state_conflict")

    def test_former_manager_import_is_a_compatibility_alias(self) -> None:
        self.assertIs(FanoutManager, RelayManager)

    def test_public_metadata_uses_only_the_product_identity(self) -> None:
        texts = {
            path: path.read_text(encoding="utf-8")
            for path in (
                ROOT / "README.md",
                ROOT / "README_EN.md",
                ROOT / "SKILL.md",
                ROOT / "agents" / "openai.yaml",
            )
        }
        for path, text in texts.items():
            with self.subTest(path=path):
                self.assertNotIn("Codex Multi Relay", text)
                self.assertNotIn("Roblis0n/codex-multi-relay", text)
        self.assertIn("name: multi-relay", texts[ROOT / "SKILL.md"])
        self.assertIn("$multi-relay", texts[ROOT / "agents" / "openai.yaml"])


if __name__ == "__main__":
    unittest.main()
