#!/usr/bin/env python3

from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT


class SkillContractTests(unittest.TestCase):
    def test_skill_frontmatter_triggers_only_on_management_and_fanout_problems(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"^---\nname: ([^\n]+)\ndescription: ([^\n]+)\n---\n", text)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), "codex-multi-relay")
        self.assertTrue(match.group(2).startswith("Use when "))
        self.assertIn("Codex", match.group(2))
        self.assertIn("multi-provider", match.group(2))
        self.assertLess(len(match.group(2)), 500)

    def test_skill_routes_every_lifecycle_action_through_the_manager(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        for command in (
            "status --json",
            "setup --preset hybrid --json",
            "setup --preset native --json",
            "test --json",
            "repair --json",
            "disable --json",
            "enable --json",
            "uninstall --json",
            "uninstall --remove-credential --json",
            "catalog --json",
            "apply --json",
            "provider list --json",
            "provider add",
            "provider remove",
            "agent list --json",
            "agent set",
            "agent remove",
            "route --capability",
        ):
            with self.subTest(command=command):
                self.assertIn(command, text)
        self.assertIn("default.toml", text)
        self.assertIn("worker.toml", text)
        self.assertIn("explorer.toml", text)
        self.assertIn("8", text)
        self.assertIn("Windows Credential Manager", text)
        self.assertIn("codex-deepseek-api-key", text)

    def test_skill_and_readme_do_not_teach_removed_legacy_runtime_behavior(self) -> None:
        combined = "\n".join(
            (
                (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"),
                (SKILL_DIR / "references" / "compatibility.md").read_text(encoding="utf-8"),
                (ROOT / "README.md").read_text(encoding="utf-8"),
            )
        )

        for forbidden in (
            "--api-key-stdin",
            "deepseek-v4-flash",
            'multi_agent_version = "v1"',
            "multi_agent_v2 = false",
            "models-with-deepseek.json",
            "agents/DeepSeek.toml",
            'agent_type="DeepSeek"',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, combined)
        self.assertNotRegex(combined, r"[閰鍑瀛绱楠锛銆]{3,}")

    def test_docs_explain_capability_routing_and_supported_protocols(self) -> None:
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        compatibility = (
            SKILL_DIR / "references" / "compatibility.md"
        ).read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        combined = "\n".join((skill, compatibility, readme))

        self.assertIn("multi_agent_v2", combined)
        self.assertIn("agent_type", combined)
        self.assertIn('fork_turns="none"', combined)
        self.assertIn("127.0.0.1:42137", combined)
        self.assertIn("Responses", combined)
        self.assertIn("Chat Completions", combined)
        self.assertIn("codex-native", combined)
        self.assertIn("responses-compatible", combined)
        self.assertIn("chat-completions-compatible", combined)
        self.assertIn("deepseek-chat", combined)
        for capability in ("vision", "audio", "web", "high-risk"):
            self.assertIn(capability, combined)
        self.assertNotIn("DeepSeek 官方模型目录与 Responses 兼容接口", combined)

    def test_evals_cover_setup_fanout_sequential_lifecycle_and_redaction(self) -> None:
        payload = json.loads(
            (SKILL_DIR / "evals" / "evals.json").read_text(encoding="utf-8")
        )
        evals = payload["evals"]
        ids = {item["id"] for item in evals}
        joined = json.dumps(evals, ensure_ascii=False)

        self.assertEqual(ids, set(range(1, 10)))
        for concept in (
            "setup",
            "credential_missing",
            "model_unavailable",
            "fan-out",
            "顺序",
            "disable",
            "enable",
            "uninstall",
            "密钥",
        ):
            with self.subTest(concept=concept):
                self.assertIn(concept, joined)
        self.assertNotRegex(joined, r"sk-[A-Za-z0-9_-]{8,}")

    def test_ci_discovers_all_tests_compiles_package_and_validates_json(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")

        self.assertIn("unittest discover", workflow)
        self.assertIn("compileall", workflow)
        self.assertIn("json.tool", workflow)
        self.assertIn("legacy configuration writes", workflow)

    def test_runtime_contract_scanner_accepts_current_production_modules(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_runtime_contract.py")],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_public_repository_uses_relay_identity(self) -> None:
        readmes = tuple(
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("README.md", "README_EN.md")
        )

        self.assertIn("Codex Multi Relay", readmes[0])
        legacy_public_repo = "Roblis0n/" + "codex-deepseek-" + "subagent"
        for text in readmes:
            self.assertIn("Roblis0n/codex-multi-relay", text)
            self.assertNotIn("Roblis0n/codex-deepseek-relay", text)
            self.assertNotIn(legacy_public_repo, text)
        self.assertFalse((ROOT / "GITHUB_UPLOAD.md").exists())

    def test_bilingual_readmes_are_complete_and_cross_linked(self) -> None:
        chinese = (ROOT / "README.md").read_text(encoding="utf-8")
        english_path = ROOT / "README_EN.md"
        self.assertTrue(english_path.is_file(), "English README is missing")
        english = english_path.read_text(encoding="utf-8")

        self.assertIn("[English](./README_EN.md)", chinese)
        self.assertIn("[简体中文](./README.md) | English", english)
        for required in (
            "Codex Multi Relay",
            "deepseek-v4-pro",
            "multi_agent_v2",
            'fork_turns="none"',
            "127.0.0.1:42137",
            "Windows Credential Manager",
            "macOS Keychain",
            "uninstall --remove-credential --json",
            "MIT",
        ):
            with self.subTest(required=required):
                self.assertIn(required, english)

    def test_readme_visuals_use_generated_hero_and_describe_fanout(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        builder = (ROOT / "scripts" / "build_readme_assets.py").read_text(
            encoding="utf-8"
        )
        workflow = (ROOT / "assets" / "readme" / "workflow.svg").read_text(
            encoding="utf-8"
        )
        hero = ROOT / "assets" / "readme" / "hero.png"

        self.assertIn('./assets/readme/hero.png', readme)
        self.assertNotIn('./assets/readme/hero.svg', readme)
        self.assertIn(
            'alt="Codex 父任务按能力路由到多模型子代理，联网、视觉、音频和高风险任务保留在主代理"',
            readme,
        )
        self.assertNotIn('alt="ChatGPT', readme)
        self.assertTrue(hero.is_file())
        header = hero.read_bytes()[:24]
        self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", header[16:24])
        self.assertGreaterEqual(width, 1200)
        self.assertGreaterEqual(width / height, 2.5)

        self.assertIn('hero.png', builder)
        self.assertNotIn('hero.svg', builder)
        visual_text = "\n".join((readme, workflow, builder))
        self.assertIn("deepseek-v4-pro", visual_text)
        for role in ("default", "worker", "explorer", "reviewer"):
            self.assertIn(role, visual_text)
        self.assertIn("capability routing", visual_text.lower())
        self.assertNotIn("v4-flash", visual_text)
        self.assertNotRegex(visual_text, r"[閰鍑瀛绱楠锛銆]{3,}")


    def test_readme_local_images_resolve(self) -> None:
        for readme_name in ("README.md", "README_EN.md"):
            with self.subTest(readme=readme_name):
                readme = (ROOT / readme_name).read_text(encoding="utf-8")
                references = re.findall(r'src="(\./[^"]+)"', readme)
                self.assertTrue(
                    references, f"{readme_name} must reference local images"
                )

                resolved = {
                    (ROOT / reference).name: (ROOT / reference).resolve()
                    for reference in references
                }
                self.assertIn("architecture.svg", resolved)
                self.assertIn("workflow.svg", resolved)
                for name, target in resolved.items():
                    with self.subTest(readme=readme_name, image=name):
                        self.assertTrue(
                            target.is_file(), f"README image missing on disk: {target}"
                        )


if __name__ == "__main__":
    unittest.main()
