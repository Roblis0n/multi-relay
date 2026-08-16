#!/usr/bin/env python3

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay.cli import build_parser, main  # noqa: E402
from multi_relay.hosts.claude_code import (  # noqa: E402
    build_claude_environment,
    find_claude_code,
    launch_claude_code,
)


class FakeController:
    def __init__(self, token: str = "short-local-token") -> None:
        self.host = "127.0.0.1"
        self.token_store = mock.Mock()
        self.token_store.read.return_value = token
        self.ensure_calls = 0
        self.stop_calls = 0
        self.failure: Exception | None = None

    def ensure(self):
        self.ensure_calls += 1
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(port=43111)

    def stop(self) -> bool:
        self.stop_calls += 1
        return True


class ClaudeCodeLauncherTests(unittest.TestCase):
    def executable(self, root: Path) -> Path:
        path = root / "claude"
        path.write_text("placeholder", encoding="utf-8")
        return path

    def test_child_environment_preserves_unrelated_values_and_overrides_bypass_auth(self) -> None:
        parent = {
            "KEEP": "yes",
            "ANTHROPIC_API_KEY": "upstream-key",
            "ANTHROPIC_AUTH_TOKEN": "upstream-token",
            "CLAUDE_CODE_OAUTH_TOKEN": "subscription-token",
            "CLAUDE_CODE_USE_BEDROCK": "1",
        }

        child = build_claude_environment(
            parent,
            base_url="http://127.0.0.1:43111",
            local_token="short-local-token",
            model_alias="multi-relay-general",
        )

        self.assertEqual(parent["ANTHROPIC_AUTH_TOKEN"], "upstream-token")
        self.assertEqual(child["KEEP"], "yes")
        self.assertEqual(child["ANTHROPIC_BASE_URL"], "http://127.0.0.1:43111")
        self.assertEqual(child["ANTHROPIC_AUTH_TOKEN"], "short-local-token")
        self.assertEqual(child["ANTHROPIC_MODEL"], "multi-relay-general")
        self.assertNotIn("ANTHROPIC_API_KEY", child)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", child)
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", child)

    def test_launcher_uses_argument_array_returns_exit_code_and_stops_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = self.executable(Path(directory))
            controller = FakeController()
            captured: dict[str, object] = {}
            summaries: list[str] = []

            def runner(command, **options):
                captured["command"] = command
                captured.update(options)
                return SimpleNamespace(returncode=23)

            code = launch_claude_code(
                ["--", "--version", "value with spaces"],
                pool="general",
                executable=str(binary),
                environ={"KEEP": "yes"},
                controller=controller,
                runner=runner,
                output=summaries.append,
            )

        self.assertEqual(code, 23)
        self.assertEqual(captured["command"], [str(binary.resolve()), "--version", "value with spaces"])
        self.assertIs(captured["shell"], False)
        self.assertIs(captured["check"], False)
        self.assertEqual(captured["env"]["ANTHROPIC_MODEL"], "multi-relay-general")
        self.assertEqual(controller.stop_calls, 1)
        self.assertNotIn("short-local-token", "".join(summaries))

    def test_gateway_failure_never_starts_claude(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = self.executable(Path(directory))
            controller = FakeController()
            controller.failure = ManagerError("gateway_start_failed", "failed")
            runner = mock.Mock()

            with self.assertRaises(ManagerError):
                launch_claude_code(
                    executable=str(binary),
                    controller=controller,
                    runner=runner,
                    output=None,
                )

        runner.assert_not_called()
        self.assertEqual(controller.stop_calls, 0)

    def test_ctrl_c_is_rethrown_after_dedicated_gateway_is_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = self.executable(Path(directory))
            controller = FakeController()

            with self.assertRaises(KeyboardInterrupt):
                launch_claude_code(
                    executable=str(binary),
                    controller=controller,
                    runner=mock.Mock(side_effect=KeyboardInterrupt),
                    output=None,
                )

        self.assertEqual(controller.stop_calls, 1)

    def test_keep_gateway_policy_skips_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = self.executable(Path(directory))
            controller = FakeController()
            launch_claude_code(
                executable=str(binary),
                controller=controller,
                runner=lambda *args, **kwargs: SimpleNamespace(returncode=0),
                keep_gateway=True,
                output=None,
            )

        self.assertEqual(controller.stop_calls, 0)

    def test_missing_executable_has_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ManagerError) as raised:
            find_claude_code(str(Path(directory) / "missing"))

        self.assertEqual(raised.exception.code, "claude_code_not_found")

    def test_cli_parses_pool_and_forwards_tail_without_constructing_manager(self) -> None:
        calls: list[tuple[object, object]] = []

        code = main(
            ["launch", "claude-code", "--pool", "general", "--", "--version"],
            manager_factory=lambda args: self.fail("manager should not be constructed"),
            claude_launcher=lambda arguments, **options: (
                calls.append((list(arguments), options)) or 17
            ),
        )

        self.assertEqual(code, 17)
        self.assertEqual(calls[0][0], ["--", "--version"])
        self.assertEqual(calls[0][1]["pool"], "general")


if __name__ == "__main__":
    unittest.main()
