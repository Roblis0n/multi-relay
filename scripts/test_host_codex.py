#!/usr/bin/env python3

from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay.catalog import Catalog, default_catalog  # noqa: E402
from multi_relay.hosts.codex import CodexHostAdapter  # noqa: E402
from multi_relay.paths import resolve_paths  # noqa: E402


class CodexHostAdapterTests(unittest.TestCase):
    def make_adapter(self, root: Path) -> tuple[CodexHostAdapter, object]:
        paths = resolve_paths(
            str(root / ".codex"),
            state_home=root / "state",
            user_home=root,
        )
        adapter = CodexHostAdapter(
            paths,
            auth_command_factory=lambda provider_id, start_gateway: [
                "python",
                "credential_helper.py",
                "--gateway",
            ],
        )
        return adapter, paths

    def test_plan_preserves_parent_and_renders_aliases_and_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter, paths = self.make_adapter(Path(directory))
            paths.config.parent.mkdir(parents=True)
            original = (
                'model = "gpt-parent"\n'
                'model_provider = "openai"\n'
                'model_reasoning_effort = "max"\n\n'
                '[model_providers.custom]\n'
                'name = "Keep Me"\n'
                'base_url = "https://custom.example/v1"\n'
                'wire_api = "responses"\n'
            )
            paths.config.write_text(original, encoding="utf-8")

            plan = adapter.plan(default_catalog())

            parsed = tomllib.loads(plan.files[paths.config].decode("utf-8"))
            self.assertEqual(parsed["model"], "gpt-parent")
            self.assertEqual(parsed["model_provider"], "openai")
            self.assertEqual(parsed["model_reasoning_effort"], "max")
            self.assertIn("custom", parsed["model_providers"])
            self.assertIn("multi-relay", parsed["model_providers"])
            default_agent = tomllib.loads(
                plan.files[paths.agents_dir / "default.toml"].decode("utf-8")
            )
            self.assertEqual(default_agent["model"], "multi-relay-agent-default")
            self.assertEqual(default_agent["model_provider"], "multi-relay")
            reviewer = tomllib.loads(
                plan.files[paths.agents_dir / "reviewer.toml"].decode("utf-8")
            )
            self.assertNotIn("model", reviewer)
            self.assertNotIn("model_provider", reviewer)
            managed_text = "\n".join(value.decode("utf-8") for value in plan.files.values())
            self.assertNotIn("ANTHROPIC_API_KEY", managed_text)
            self.assertNotIn("Authorization =", managed_text)
            self.assertNotIn("x-api-key", managed_text)

    def test_apply_is_idempotent_and_disable_removes_active_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter, paths = self.make_adapter(Path(directory))

            first = adapter.apply(default_catalog())
            before = paths.config.read_bytes()
            second = adapter.apply(default_catalog())

            self.assertEqual(first["status"], "enabled")
            self.assertEqual(second["status"], "enabled")
            self.assertEqual(paths.config.read_bytes(), before)
            adapter.disable()
            self.assertFalse(paths.config.exists())
            self.assertEqual(adapter.status()["status"], "disabled")

    def test_uninstall_keeps_a_managed_agent_modified_by_the_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter, paths = self.make_adapter(Path(directory))
            adapter.apply(default_catalog())
            agent = paths.agents_dir / "worker.toml"
            agent.write_text(agent.read_text(encoding="utf-8") + "# user edit\n", encoding="utf-8")

            result = adapter.uninstall()

            self.assertEqual(result["status"], "uninstalled")
            self.assertTrue(agent.is_file())
            self.assertTrue(any("worker.toml" in item for item in result["warnings"]))
            self.assertFalse(paths.codex_host_manifest.exists())

    def test_disabled_codex_host_produces_no_active_gateway_provider(self) -> None:
        payload = default_catalog().to_dict()
        payload["hosts"]["codex"]["enabled"] = False
        catalog = Catalog.from_dict(payload)
        with tempfile.TemporaryDirectory() as directory:
            adapter, paths = self.make_adapter(Path(directory))

            result = adapter.apply(catalog)

            self.assertEqual(result["status"], "disabled")
            self.assertFalse(paths.config.exists())

    def test_legacy_markers_are_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter, paths = self.make_adapter(Path(directory))
            paths.config.parent.mkdir(parents=True)
            paths.config.write_text(
                "# BEGIN CODEX-DEEPSEEK-FANOUT PROVIDER\n"
                "[model_providers.deepseek]\n"
                'name = "legacy"\n'
                "# END CODEX-DEEPSEEK-FANOUT PROVIDER\n",
                encoding="utf-8",
            )
            paths.instruction_file.write_text(
                "<!-- BEGIN CODEX-DEEPSEEK-FANOUT -->\nlegacy\n"
                "<!-- END CODEX-DEEPSEEK-FANOUT -->\n",
                encoding="utf-8",
            )

            plan = adapter.plan(default_catalog())

            self.assertNotIn("CODEX-DEEPSEEK-FANOUT", plan.files[paths.config].decode())
            self.assertNotIn(
                "CODEX-DEEPSEEK-FANOUT",
                plan.files[paths.instruction_file].decode(),
            )


if __name__ == "__main__":
    unittest.main()
