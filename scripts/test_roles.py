#!/usr/bin/env python3

from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay.catalog import Catalog, default_catalog  # noqa: E402
from multi_relay.model_capabilities import (  # noqa: E402
    ModelSelection,
    resolve_effort,
)
from multi_relay.roles import (  # noqa: E402
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

    def test_catalog_agents_render_provider_mcp_skill_and_sandbox_overrides(self) -> None:
        payload = default_catalog("native").to_dict()
        payload["agents"][0].update(
            {
                "model": "gpt-example",
                "reasoning_effort": "high",
                "capabilities": ["text", "tools", "web"],
                "mcp_servers": {
                    "docs": {
                        "url": "https://developers.openai.com/mcp",
                        "env": {"MODE": "safe"},
                        "headers": {"X-Client": "codex"},
                    }
                },
                "skills": [{"path": "C:/skills/docs/SKILL.md", "enabled": False}],
            }
        )
        catalog = Catalog.from_dict(payload)

        parsed = tomllib.loads(render_agent(catalog.agents[0], catalog=catalog))

        self.assertEqual(parsed["name"], "reviewer")
        self.assertEqual(parsed["model"], "gpt-example")
        self.assertNotIn("model_provider", parsed)
        self.assertEqual(parsed["model_reasoning_effort"], "high")
        self.assertEqual(parsed["sandbox_mode"], "read-only")
        self.assertEqual(
            parsed["mcp_servers"]["docs"]["url"],
            "https://developers.openai.com/mcp",
        )
        self.assertEqual(parsed["mcp_servers"]["docs"]["env"], {"MODE": "safe"})
        self.assertEqual(
            parsed["mcp_servers"]["docs"]["headers"],
            {"X-Client": "codex"},
        )
        self.assertEqual(
            parsed["skills"]["config"],
            [{"path": "C:/skills/docs/SKILL.md", "enabled": False}],
        )

    def test_expected_catalog_files_include_custom_agent_names(self) -> None:
        catalog = default_catalog()

        with tempfile.TemporaryDirectory() as directory:
            files = expected_agent_files(Path(directory), catalog)

        self.assertEqual(
            {path.name for path in files},
            {"default.toml", "worker.toml", "explorer.toml", "reviewer.toml"},
        )


if __name__ == "__main__":
    unittest.main()
