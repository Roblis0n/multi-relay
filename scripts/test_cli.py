#!/usr/bin/env python3

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay.cli import build_parser, find_codex, main  # noqa: E402


class FakeStore:
    def __init__(self, secret: str | None = None) -> None:
        self.secret = secret

    def exists(self) -> bool:
        return self.secret is not None

    def read(self) -> str | None:
        return self.secret

    def store(self, secret: str) -> None:
        self.secret = secret

    def remove(self) -> bool:
        self.secret = None
        return True


class FakeManager:
    def __init__(self, store: FakeStore) -> None:
        self.credentials = store
        self.calls: list[tuple[str, object]] = []

    def status(self) -> dict[str, object]:
        self.calls.append(("status", None))
        return {"status": "not_configured"}

    def setup(self, preset: str = "hybrid") -> dict[str, object]:
        self.calls.append(("setup", preset))
        return {"status": "ready", "model": "deepseek-v4-pro"}

    def repair(self) -> dict[str, object]:
        self.calls.append(("repair", None))
        return {"status": "ready"}

    def catalog(self) -> dict[str, object]:
        self.calls.append(("catalog", None))
        return {"schema_version": 1, "concurrency": 8, "providers": [], "agents": []}

    def list_providers(self) -> list[dict[str, object]]:
        self.calls.append(("provider-list", None))
        return []

    def add_provider(self, provider: object) -> dict[str, object]:
        self.calls.append(("provider-add", provider))
        return {"status": "ready"}

    def remove_provider(
        self,
        provider_id: str,
        *,
        remove_credential: bool = False,
    ) -> dict[str, object]:
        self.calls.append(("provider-remove", (provider_id, remove_credential)))
        return {"status": "ready"}

    def credential_for_provider(
        self,
        provider: object,
        *,
        catalog: object | None = None,
    ) -> FakeStore:
        del catalog
        self.calls.append(("credential-for", provider))
        return self.credentials

    def list_agents(self) -> list[dict[str, object]]:
        self.calls.append(("agent-list", None))
        return []

    def set_agent(self, agent: object) -> dict[str, object]:
        self.calls.append(("agent-set", agent))
        return {"status": "ready"}

    def remove_agent(self, name: str) -> dict[str, object]:
        self.calls.append(("agent-remove", name))
        return {"status": "ready"}

    def route(self, capabilities: set[str], *, high_risk: bool = False) -> dict[str, object]:
        self.calls.append(("route", (capabilities, high_risk)))
        return {"status": "parent_required"}

    def apply(self) -> dict[str, object]:
        self.calls.append(("apply", None))
        return {"status": "ready"}

    def test(self) -> dict[str, object]:
        self.calls.append(("test", None))
        return {"status": "ready"}

    def disable(self) -> dict[str, object]:
        self.calls.append(("disable", None))
        return {"status": "disabled"}

    def enable(self) -> dict[str, object]:
        self.calls.append(("enable", None))
        return {"status": "ready"}

    def uninstall(self, remove_credential: bool = False) -> dict[str, object]:
        self.calls.append(("uninstall", remove_credential))
        return {"status": "uninstalled"}


class CliTests(unittest.TestCase):
    def test_parser_covers_complete_management_surface(self) -> None:
        parser = build_parser()
        samples = (
            ["setup", "--host", "all"],
            ["test", "--host", "claude-code"],
            ["enable", "--host", "codex"],
            ["disable", "--host", "all"],
            ["provider", "edit", "vendor", "--name", "Vendor 2"],
            ["provider", "discover-models", "vendor", "--model", "m1"],
            ["provider", "test", "vendor"],
            ["provider", "enable", "vendor"],
            ["provider", "disable", "vendor"],
            ["credential", "list"],
            ["credential", "add", "--provider", "vendor", "--id", "backup"],
            ["credential", "replace", "--provider", "vendor", "--id", "backup"],
            ["credential", "enable", "--provider", "vendor", "--id", "backup"],
            ["credential", "disable", "--provider", "vendor", "--id", "backup"],
            ["credential", "test", "--provider", "vendor", "--id", "backup"],
            ["credential", "remove", "--provider", "vendor", "--id", "backup"],
            ["host", "list"],
            ["host", "apply", "codex"],
            ["host", "status", "claude-code"],
            ["gateway", "start"],
            ["gateway", "status"],
            ["gateway", "stop"],
            ["launch", "claude-code", "--pool", "general", "--", "--version"],
            ["uninstall", "--host", "all", "--remove-credentials"],
        )
        for argv in samples:
            with self.subTest(argv=argv):
                self.assertIsNotNone(parser.parse_args(argv).command)

    def test_credential_add_rejects_plaintext_key_option(self) -> None:
        parser = build_parser()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "credential",
                    "add",
                    "--provider",
                    "vendor",
                    "--id",
                    "backup",
                    "--key",
                    "secret",
                ]
            )

    def test_json_output_uses_stable_envelope_and_never_echoes_prompted_secret(self) -> None:
        class CredentialManager:
            def add_credential(self, provider, credential, **options):
                self.received = options["secret"]
                return {
                    "status": "ready",
                    "changed": True,
                    "warnings": [],
                    "details": {"provider": provider, "credential": credential},
                    "next_actions": [],
                }

        manager = CredentialManager()
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = main(
                [
                    "credential",
                    "add",
                    "--provider",
                    "vendor",
                    "--id",
                    "backup",
                    "--json",
                ],
                manager_factory=lambda args: manager,
                prompt_fn=lambda prompt: "top-secret-value",
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            set(payload),
            {"status", "changed", "warnings", "details", "next_actions"},
        )
        self.assertEqual(manager.received, "top-secret-value")
        self.assertNotIn("top-secret-value", stdout.getvalue())

    def test_find_codex_prefers_desktop_sandbox_runtime_in_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            runtime = home / ".sandbox-bin" / "codex.exe"
            runtime.parent.mkdir()
            runtime.write_bytes(b"codex")
            with (
                mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False),
                mock.patch(
            "multi_relay.cli.shutil.which",
                    return_value=r"C:\Program Files\WindowsApps\Codex\codex.exe",
                ),
            ):
                discovered = find_codex(None, required=True)

        self.assertEqual(discovered, str(runtime.resolve()))

    def test_parser_has_no_plaintext_api_key_option(self) -> None:
        parser = build_parser()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["setup", "--api-key", "sk-test"])

    def test_setup_prompts_locally_and_never_prints_the_key(self) -> None:
        store = FakeStore()
        manager = FakeManager(store)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                ["setup", "--json"],
                manager_factory=lambda args: manager,
                prompt_fn=lambda _: "sk-test",
            )

        self.assertEqual(code, 0)
        self.assertEqual(store.secret, "sk-test")
        self.assertEqual(manager.calls, [("status", None), ("setup", "hybrid")])
        self.assertEqual(json.loads(stdout.getvalue())["status"], "ready")
        self.assertNotIn("sk-test", stdout.getvalue())
        self.assertNotIn("sk-test", stderr.getvalue())

    def test_status_is_read_only_and_does_not_prompt(self) -> None:
        store = FakeStore()
        manager = FakeManager(store)
        prompts = 0

        def prompt(_: str) -> str:
            nonlocal prompts
            prompts += 1
            return "sk-test"

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(
                ["status", "--json"],
                manager_factory=lambda args: manager,
                prompt_fn=prompt,
            )

        self.assertEqual(code, 0)
        self.assertEqual(prompts, 0)
        self.assertIsNone(store.secret)
        self.assertEqual(manager.calls, [("status", None)])

    def test_uninstall_remove_credential_flag_is_forwarded_explicitly(self) -> None:
        manager = FakeManager(FakeStore("sk-test"))

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(
                ["uninstall", "--remove-credential", "--json"],
                manager_factory=lambda args: manager,
            )

        self.assertEqual(code, 0)
        self.assertEqual(manager.calls, [("uninstall", True)])

    def test_native_setup_does_not_prompt_for_deepseek(self) -> None:
        manager = FakeManager(FakeStore())
        prompts = 0

        def prompt(_: str) -> str:
            nonlocal prompts
            prompts += 1
            return "should-not-be-used"

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(
                ["setup", "--preset", "native", "--json"],
                manager_factory=lambda args: manager,
                prompt_fn=prompt,
            )

        self.assertEqual(code, 0)
        self.assertEqual(prompts, 0)
        self.assertEqual(manager.calls, [("setup", "native")])

    def test_legacy_setup_does_not_prompt_or_write_the_canonical_slot(self) -> None:
        manager = FakeManager(FakeStore())
        prompts = 0

        def status() -> dict[str, object]:
            manager.calls.append(("status", None))
            return {"status": "legacy"}

        def prompt(_: str) -> str:
            nonlocal prompts
            prompts += 1
            return "must-not-be-written"

        manager.status = status  # type: ignore[method-assign]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(
                ["setup", "--json"],
                manager_factory=lambda args: manager,
                prompt_fn=prompt,
            )

        self.assertEqual(code, 0)
        self.assertEqual(prompts, 0)
        self.assertIsNone(manager.credentials.secret)
        self.assertEqual(manager.calls, [("status", None), ("setup", "hybrid")])

    def test_provider_add_builds_valid_definition_and_prompts_only_vault(self) -> None:
        manager = FakeManager(FakeStore())
        stdout = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = main(
                [
                    "provider",
                    "add",
                    "--id",
                    "vendor",
                    "--name",
                    "Vendor",
                    "--protocol",
                    "responses-compatible",
                    "--base-url",
                    "https://api.vendor.test/v1",
                    "--auth",
                    "vault",
                    "--capability",
                    "text",
                    "--capability",
                    "tools",
                    "--json",
                ],
                manager_factory=lambda args: manager,
                prompt_fn=lambda _: "provider-token",
            )

        self.assertEqual(code, 0)
        self.assertEqual(manager.credentials.secret, "provider-token")
        self.assertEqual(manager.calls[0][0], "credential-for")
        self.assertEqual(manager.calls[1][0], "provider-add")
        provider = manager.calls[1][1]
        self.assertEqual(provider.id, "vendor")
        self.assertEqual(provider.protocol, "responses-compatible")

    def test_agent_set_and_route_dispatch_structured_boundaries(self) -> None:
        manager = FakeManager(FakeStore("sk-test"))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            set_code = main(
                [
                    "agent",
                    "set",
                    "--name",
                    "vendor-worker",
                    "--description",
                    "Vendor worker",
                    "--provider",
                    "vendor",
                    "--model",
                    "vendor-model",
                    "--capability",
                    "text",
                    "--trust",
                    "standard",
                    "--priority",
                    "5",
                    "--sandbox-mode",
                    "workspace-write",
                    "--instructions",
                    "Complete the bounded task.",
                    "--json",
                ],
                manager_factory=lambda args: manager,
            )
            route_code = main(
                ["route", "--capability", "vision", "--high-risk", "--json"],
                manager_factory=lambda args: manager,
            )

        self.assertEqual(set_code, 0)
        self.assertEqual(route_code, 0)
        self.assertEqual(manager.calls[0][0], "agent-set")
        self.assertEqual(manager.calls[1], ("route", ({"vision"}, True)))


if __name__ == "__main__":
    unittest.main()
