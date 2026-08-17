#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay.catalog import Catalog, default_catalog, save_catalog_bytes  # noqa: E402
from multi_relay.hosts.claude_code import (  # noqa: E402
    CLAUDE_OWNERSHIP_MARKER,
    ClaudeCodeHostAdapter,
    render_claude_agent,
)
from multi_relay.paths import resolve_paths  # noqa: E402


def claude_catalog(*, scope: str = "user") -> Catalog:
    payload = default_catalog().to_dict()
    payload["hosts"]["claude-code"].update({"enabled": True, "scope": scope})
    return Catalog.from_dict(payload)


class ClaudeCodeHostAdapterTests(unittest.TestCase):
    def make_paths(self, root: Path):
        return resolve_paths(
            str(root / "codex"),
            state_home=root / "state",
            user_home=root / "user",
        )

    def test_agent_markdown_has_safe_frontmatter_alias_and_instructions(self) -> None:
        payload = claude_catalog().to_dict()
        payload["agents"][0].update(
            {
                "description": "Worker: #1\nsecond line",
                "developer_instructions": "Do the bounded task.\nKeep evidence.",
                "tools": ["Read", "Bash"],
            }
        )
        catalog = Catalog.from_dict(payload)
        digest = hashlib.sha256(save_catalog_bytes(catalog)).hexdigest()

        rendered = render_claude_agent(catalog.agents[0], digest)

        self.assertTrue(rendered.startswith("---\n# MULTI-RELAY-OWNED"))
        self.assertIn(f"catalog-sha256={digest}", rendered)
        self.assertIn('description: "Worker: #1\\nsecond line"', rendered)
        self.assertIn('model: "multi-relay-agent-default"', rendered)
        self.assertIn('tools: ["Read", "Bash"]', rendered)
        self.assertTrue(rendered.endswith("Do the bounded task.\nKeep evidence.\n"))
        self.assertNotIn("ANTHROPIC_API_KEY", rendered)
        self.assertNotIn("x-api-key", rendered)
        self.assertNotIn("Authorization", rendered)

    def test_user_and_project_scopes_choose_exact_agent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = self.make_paths(root)
            user_plan = ClaudeCodeHostAdapter(paths).plan(claude_catalog(scope="user"))
            project = root / "project"
            project.mkdir()
            project_plan = ClaudeCodeHostAdapter(
                paths,
                project_path=project,
            ).plan(claude_catalog(scope="project"))

        self.assertTrue(
            all(path.parent == root / "user" / ".claude" / "agents" for path in user_plan.files)
        )
        self.assertTrue(
            all(path.parent == project / ".claude" / "agents" for path in project_plan.files)
        )

    def test_project_scope_requires_an_explicit_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self.make_paths(Path(directory))
            for project in (None, Path(directory) / "missing"):
                with self.subTest(project=project), self.assertRaises(ManagerError) as raised:
                    ClaudeCodeHostAdapter(paths, project_path=project).plan(
                        claude_catalog(scope="project")
                    )
                self.assertIn(raised.exception.code, {"project_required", "project_not_found"})

    def test_unmanaged_same_name_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.make_paths(root)
            existing = paths.claude_user_agents_dir / "default.md"
            existing.parent.mkdir(parents=True)
            existing.write_text("user-owned\n", encoding="utf-8")

            with self.assertRaises(ManagerError) as raised:
                ClaudeCodeHostAdapter(paths).plan(claude_catalog())

            self.assertEqual(raised.exception.code, "conflict")
            self.assertEqual(existing.read_text(encoding="utf-8"), "user-owned\n")

    def test_modified_managed_file_is_retained_during_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.make_paths(root)
            adapter = ClaudeCodeHostAdapter(paths)
            adapter.apply(claude_catalog())
            worker = paths.claude_user_agents_dir / "worker.md"
            worker.write_text(worker.read_text(encoding="utf-8") + "user change\n", encoding="utf-8")

            result = adapter.uninstall()

            self.assertEqual(result["status"], "uninstalled")
            self.assertTrue(worker.is_file())
            self.assertTrue(any("worker.md" in warning for warning in result["warnings"]))
            self.assertFalse(paths.claude_host_manifest.exists())

    def test_apply_status_disable_enable_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self.make_paths(Path(directory))
            adapter = ClaudeCodeHostAdapter(paths)
            catalog = claude_catalog()

            adapter.apply(catalog)
            before = {
                path: path.read_bytes() for path in paths.claude_user_agents_dir.glob("*.md")
            }
            adapter.apply(catalog)
            self.assertEqual(
                before,
                {path: path.read_bytes() for path in paths.claude_user_agents_dir.glob("*.md")},
            )
            self.assertEqual(adapter.status()["status"], "enabled")
            adapter.disable()
            self.assertEqual(list(paths.claude_user_agents_dir.glob("*.md")), [])
            self.assertEqual(adapter.status()["status"], "disabled")
            adapter.enable(catalog)
            self.assertEqual(adapter.status()["status"], "enabled")


if __name__ == "__main__":
    unittest.main()
