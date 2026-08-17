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


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def documented_commands(text: str) -> set[str]:
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("python scripts/multi_relay.py")
    }


class SkillContractTests(unittest.TestCase):
    def test_skill_metadata_matches_the_product_and_hosts(self) -> None:
        text = read("SKILL.md")
        match = re.match(r"^---\nname: ([^\n]+)\ndescription: ([^\n]+)\n---\n", text)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), "multi-relay")
        description = match.group(2)
        for phrase in ("Use when ", "multi-provider", "Codex", "Claude Code"):
            self.assertIn(phrase, description)
        self.assertLess(len(description), 500)

    def test_skill_covers_the_actual_cli_surfaces(self) -> None:
        text = read("SKILL.md")
        for command in (
            "status --json",
            "setup --preset hybrid --host all --json",
            "setup --preset native --host codex --json",
            "provider add",
            "credential add",
            "target add",
            "pool add",
            "pool strategy",
            "pool rotate",
            "agent set",
            "host apply codex",
            "host apply claude-code",
            "gateway status",
            "route --capability",
            "launch claude-code",
            "repair --json",
            "disable --host all --json",
            "enable --host all --json",
            "uninstall --host all --json",
            "--remove-credentials",
        ):
            with self.subTest(command=command):
                self.assertIn(command, text)

    def test_bilingual_readmes_have_identical_commands(self) -> None:
        chinese = read("README.md")
        english = read("README_EN.md")
        self.assertEqual(documented_commands(chinese), documented_commands(english))
        self.assertIn("[English](./README_EN.md)", chinese)
        self.assertIn("[简体中文](./README.md)", english)

    def test_readmes_answer_the_rotation_and_host_contract(self) -> None:
        combined = read("README.md") + "\n" + read("README_EN.md")
        for phrase in (
            "ExecutionTarget",
            "sticky",
            "timed",
            "committed",
            "DeepSeek",
            "Anthropic Messages",
            "OpenAI-compatible",
            "launch claude-code",
            "Windows Credential Manager",
            "macOS Keychain",
            "Linux Secret Service",
            "no_eligible_target",
            "state_conflict",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, combined)
        for capability in ("vision", "audio", "tool_calling", "server_web_search"):
            self.assertIn(capability, combined)

    def test_all_reference_documents_exist_and_are_linked(self) -> None:
        chinese = read("README.md")
        for name in (
            "compatibility.md",
            "catalog.md",
            "rotation.md",
            "codex.md",
            "claude-code.md",
            "security.md",
        ):
            path = ROOT / "references" / name
            with self.subTest(name=name):
                self.assertTrue(path.is_file())
                self.assertIn(f"references/{name}", chinese)

    def test_public_docs_use_fake_endpoints_and_no_secret_examples(self) -> None:
        public = "\n".join(
            read(name)
            for name in (
                "README.md",
                "README_EN.md",
                "RELEASE_NOTES.md",
                "SKILL.md",
                "references/catalog.md",
                "references/rotation.md",
                "references/codex.md",
                "references/claude-code.md",
                "references/security.md",
            )
        )
        urls = re.findall(r"https?://[^\s`)]+", public)
        self.assertTrue(urls)
        official_urls = {"https://github.com/Roblis0n/multi-relay"}
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue(
                    url in official_urls
                    or ".example" in url
                    or url.startswith("http://127.0.0.1:"),
                    url,
                )
        self.assertNotRegex(public, r"\bsk-[A-Za-z0-9_-]{4,}\b")
        self.assertNotRegex(public, r"\b(?:api[_-]?key|token|secret)\s*[:=]\s*[^\s]+")

    def test_old_product_identity_is_confined_to_compatibility_text(self) -> None:
        public = "\n".join(
            read(name)
            for name in (
                "README.md",
                "README_EN.md",
                "SKILL.md",
                "agents/openai.yaml",
                "evals/evals.json",
            )
        )
        self.assertNotIn("Codex Multi Relay", public)
        self.assertNotIn("Roblis0n/codex-multi-relay", public)
        self.assertNotIn("$codex-multi-relay", public)
        self.assertIn("Roblis0n/multi-relay", public)

    def test_readme_visuals_and_alt_text_match_the_new_architecture(self) -> None:
        readme = read("README.md")
        for asset in ("hero.png", "architecture.svg", "workflow.svg"):
            self.assertIn(f"assets/readme/{asset}", readme)
        for phrase in ("Codex", "Claude Code", "target pool", "committed"):
            self.assertIn(phrase, readme)
        for name, expected in (
            ("hero.png", (1800, 620)),
            ("social-preview.png", (1280, 640)),
        ):
            header = (ROOT / "assets" / "readme" / name).read_bytes()[:24]
            self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", header[16:24]), expected)

    def test_evals_have_contiguous_ids_and_no_secret_like_values(self) -> None:
        payload = json.loads(read("evals/evals.json"))
        self.assertEqual(payload["skill_name"], "multi-relay")
        ids = {item["id"] for item in payload["evals"]}
        self.assertEqual(ids, set(range(1, len(ids) + 1)))
        self.assertNotRegex(json.dumps(payload, ensure_ascii=False), r"\bsk-[A-Za-z0-9_-]{4,}\b")

    def test_runtime_contract_scanner_accepts_production_modules(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_runtime_contract.py")],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
