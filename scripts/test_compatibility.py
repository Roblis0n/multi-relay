#!/usr/bin/env python3

from __future__ import annotations

import io
import json
import sys
import tempfile
import tomllib
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from deepseek_fanout import ManagerError  # noqa: E402
from deepseek_fanout.compatibility import (  # noqa: E402
    probe_efforts,
    run_isolated_gate,
)
from deepseek_fanout.model_capabilities import ModelSelection  # noqa: E402
from deepseek_fanout.native_test import _prompt as _native_prompt  # noqa: E402
from deepseek_fanout.provider_api import discover_model  # noqa: E402


class FakeHttpResponse:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]


class ProviderModelTests(unittest.TestCase):
    def test_discover_model_returns_exact_provider_model(self) -> None:
        payload = {
            "object": "list",
            "data": [
                {"id": "deepseek-chat", "object": "model"},
                {"id": "deepseek-v4-pro", "object": "model"},
            ],
        }

        resolved = discover_model(
            "sk-test",
            opener=lambda *args, **kwargs: FakeHttpResponse(payload),
        )

        self.assertEqual(resolved, "deepseek-v4-pro")

    def test_discover_model_rejects_casefold_ambiguity(self) -> None:
        payload = {
            "object": "list",
            "data": [
                {"id": "DeepSeek-V4-Pro", "object": "model"},
                {"id": "DEEPSEEK-V4-PRO", "object": "model"},
            ],
        }

        with self.assertRaises(ManagerError) as raised:
            discover_model(
                "sk-test",
                opener=lambda *args, **kwargs: FakeHttpResponse(payload),
            )

        self.assertEqual(raised.exception.code, "model_ambiguous")

    def test_discover_model_reports_authentication_failure_without_key(self) -> None:
        def opener(*args: object, **kwargs: object) -> object:
            raise urllib.error.HTTPError(
                "https://api.deepseek.com/models",
                401,
                "Unauthorized sk-test",
                None,
                io.BytesIO(b"sk-test"),
            )

        with self.assertRaises(ManagerError) as raised:
            discover_model("sk-test", opener=opener)

        self.assertEqual(raised.exception.code, "authentication_failed")
        self.assertNotIn("sk-test", str(raised.exception))
        self.assertNotIn("sk-test", repr(raised.exception.details))

    def test_discover_model_rejects_unavailable_or_malformed_catalog(self) -> None:
        unavailable = {"object": "list", "data": [{"id": "deepseek-chat"}]}
        malformed = {"object": "list", "data": {"id": "deepseek-v4-pro"}}

        with self.assertRaises(ManagerError) as missing:
            discover_model(
                "sk-test",
                opener=lambda *args, **kwargs: FakeHttpResponse(unavailable),
            )
        with self.assertRaises(ManagerError) as invalid:
            discover_model(
                "sk-test",
                opener=lambda *args, **kwargs: FakeHttpResponse(malformed),
            )

        self.assertEqual(missing.exception.code, "model_unavailable")
        self.assertEqual(invalid.exception.code, "malformed_provider_response")

    def test_discover_model_redacts_timeout_details(self) -> None:
        def opener(*args: object, **kwargs: object) -> object:
            raise TimeoutError("request with sk-test timed out")

        with self.assertRaises(ManagerError) as raised:
            discover_model("sk-test", opener=opener)

        self.assertEqual(raised.exception.code, "provider_unavailable")
        self.assertNotIn("sk-test", str(raised.exception))


class CompatibilityTests(unittest.TestCase):
    def test_isolated_prompt_forces_explicit_v2_roles_without_parent_inheritance(self) -> None:
        prompt = _native_prompt()

        self.assertIn('agent_type="default"', prompt)
        self.assertIn('agent_type="worker"', prompt)
        self.assertIn('agent_type="explorer"', prompt)
        self.assertIn('fork_turns="none"', prompt)
        self.assertIn("never fork_turns=all", prompt)

    def test_effort_probe_chooses_first_empirically_working_level(self) -> None:
        attempted: list[str] = []

        def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
            self.assertEqual(kwargs.get("encoding"), "utf-8")
            self.assertEqual(kwargs.get("errors"), "replace")
            effort_arg = next(
                (part for part in command if part.startswith("model_reasoning_effort=")),
                "provider-default",
            )
            attempted.append(effort_arg)
            passed = effort_arg == 'model_reasoning_effort="xhigh"'
            return SimpleNamespace(
                returncode=0 if passed else 1,
                stdout="DEEPSEEK_EFFORT_OK" if passed else "",
                stderr="unsupported effort",
            )

        with tempfile.TemporaryDirectory() as directory:
            selection = probe_efforts(
                "codex.exe",
                Path(directory),
                "deepseek-v4-pro",
                runner=runner,
            )

        self.assertEqual(selection.reasoning_effort, "xhigh")
        self.assertEqual(selection.effort_source, "empirical_codex_provider_probe")
        self.assertEqual(
            attempted,
            [
                'model_reasoning_effort="max"',
                'model_reasoning_effort="xhigh"',
            ],
        )

    def test_isolated_gate_only_probes_provider_without_copying_real_home(self) -> None:
        selection = ModelSelection(
            requested_model="deepseek-v4-pro",
            resolved_model="deepseek-v4-pro",
            reasoning_effort="xhigh",
            effort_source="empirical_codex_provider_probe",
        )
        isolated_homes: list[Path] = []

        def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
            self.assertEqual(kwargs.get("encoding"), "utf-8")
            self.assertEqual(kwargs.get("errors"), "replace")
            environment = kwargs["env"]
            isolated_home = Path(environment["CODEX_HOME"])
            isolated_homes.append(isolated_home)
            self.assertTrue((isolated_home / "config.toml").is_file())
            self.assertFalse((isolated_home / "auth.json").exists())
            self.assertFalse((isolated_home / "state_user.sqlite").exists())
            isolated_config = tomllib.loads(
                (isolated_home / "config.toml").read_text(encoding="utf-8")
            )
            self.assertEqual(isolated_config["model"], "deepseek-v4-pro")
            self.assertEqual(isolated_config["model_provider"], "deepseek")
            self.assertEqual(isolated_config["model_reasoning_effort"], "xhigh")
            prompt = command[-1]
            if "DEEPSEEK_GATE_DIRECT_OK" in prompt:
                return SimpleNamespace(
                    returncode=0,
                    stdout='{"type":"item.completed","item":{"type":"agent_message","text":"DEEPSEEK_GATE_DIRECT_OK"}}\n',
                    stderr="",
                )

            self.fail("The isolated gate must not run the full native fan-out acceptance.")

        with tempfile.TemporaryDirectory() as directory:
            real_home = Path(directory)
            config = (
                'model = "gpt-5.6-sol"\n'
                'model_provider = "openai"\n'
                'model_reasoning_effort = "max"\n'
            )
            (real_home / "config.toml").write_text(config, encoding="utf-8")
            (real_home / "auth.json").write_bytes(b"user-auth-sentinel")
            (real_home / "state_user.sqlite").write_bytes(b"user-state-sentinel")
            before = {
                path.name: path.read_bytes()
                for path in real_home.iterdir()
            }

            report = run_isolated_gate(
                "codex.exe",
                real_home,
                selection,
                runner=runner,
            )

            after = {
                path.name: path.read_bytes()
                for path in real_home.iterdir()
            }
        self.assertEqual(before, after)
        self.assertTrue(all(isolated_homes))
        self.assertTrue(all(not home.exists() for home in isolated_homes))
        self.assertEqual(report.as_checks(), {"provider_initialized": True})
        self.assertIsNone(report.single_child_passed)
        self.assertIsNone(report.fanout_passed)
        self.assertEqual(len(isolated_homes), 1)

    def test_gate_rejects_runtime_that_requires_live_model_catalog(self) -> None:
        selection = ModelSelection(
            requested_model="deepseek-v4-pro",
            resolved_model="deepseek-v4-pro",
            reasoning_effort="high",
            effort_source="empirical_codex_provider_probe",
        )

        def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="custom model requires model_catalog_json",
            )

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\nmodel_provider = "openai"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ManagerError) as raised:
                run_isolated_gate("codex.exe", home, selection, runner=runner)

        self.assertEqual(raised.exception.code, "unsupported_live_catalog")


if __name__ == "__main__":
    unittest.main()
