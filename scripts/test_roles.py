#!/usr/bin/env python3

from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from deepseek_fanout import ManagerError  # noqa: E402
from deepseek_fanout.model_capabilities import (  # noqa: E402
    ModelSelection,
    resolve_effort,
)
from deepseek_fanout.roles import (  # noqa: E402
    expected_agent_files,
    render_agent,
)


class ModelCapabilityTests(unittest.TestCase):
    def test_effort_resolution_uses_highest_mutually_supported_value(self) -> None:
        cases = [
            ({"ultra", "max"}, {"ultra", "max"}, "max"),
            ({"minimal", "max"}, {"high", "max"}, "max"),
            ({"low", "xhigh"}, {"medium", "xhigh"}, "xhigh"),
            ({"minimal", "high"}, {"low", "high"}, "high"),
            ({"minimal", "medium"}, {"low", "medium"}, "medium"),
            ({"minimal", "low"}, {"minimal", "low"}, "low"),
            ({"minimal"}, {"minimal"}, "minimal"),
        ]

        for codex_values, provider_values, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    resolve_effort(codex_values, provider_values),
                    (expected, "verified_intersection"),
                )

    def test_effort_resolution_omits_unverified_setting(self) -> None:
        self.assertEqual(
            resolve_effort({"max", "high"}, {"medium", "low"}),
            (None, "provider_default"),
        )


class RoleRenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection = ModelSelection(
            requested_model="deepseek-v4-pro",
            resolved_model="deepseek-v4-pro-202608",
            reasoning_effort="max",
            effort_source="verified_intersection",
        )

    def test_all_builtin_roles_route_to_same_verified_deepseek_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            files = expected_agent_files(Path(directory), self.selection)

        self.assertEqual(
            {path.name for path in files},
            {"default.toml", "worker.toml", "explorer.toml"},
        )
        descriptions: set[str] = set()
        for path, content in files.items():
            parsed = tomllib.loads(content.decode("utf-8"))
            self.assertEqual(parsed["name"], path.stem)
            self.assertEqual(parsed["model_provider"], "deepseek")
            self.assertEqual(parsed["model"], "deepseek-v4-pro-202608")
            self.assertEqual(parsed["model_reasoning_effort"], "max")
            self.assertEqual(parsed["model_context_window"], 1_000_000)
            self.assertIn("text-only", parsed["developer_instructions"])
            self.assertIn(path.stem, parsed["developer_instructions"])
            descriptions.add(parsed["description"])
        self.assertEqual(len(descriptions), 3)

    def test_role_omits_reasoning_key_when_codex_and_provider_do_not_overlap(self) -> None:
        selection = ModelSelection(
            requested_model="deepseek-v4-pro",
            resolved_model="deepseek-v4-pro",
            reasoning_effort=None,
            effort_source="provider_default",
        )

        parsed = tomllib.loads(render_agent("worker", selection))

        self.assertNotIn("model_reasoning_effort", parsed)
        self.assertEqual(parsed["model"], "deepseek-v4-pro")

    def test_unknown_role_is_rejected_before_a_file_can_be_written(self) -> None:
        with self.assertRaises(ManagerError) as raised:
            render_agent("reviewer", self.selection)

        self.assertEqual(raised.exception.code, "invalid_role")


if __name__ == "__main__":
    unittest.main()
