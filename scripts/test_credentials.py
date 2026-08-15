#!/usr/bin/env python3

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay import credential_helper  # noqa: E402
from multi_relay.credentials import (  # noqa: E402
    CREDENTIAL_TARGET,
    MacOSCredentialStore,
    WindowsCredentialStore,
    credential_store,
    credential_target,
    prompt_and_store,
    provider_auth_command,
)


class FakeWindowsApi:
    def __init__(self) -> None:
        self.value: str | None = None
        self.write_calls: list[tuple[str, str, str]] = []

    def read(self, target: str) -> str | None:
        return self.value

    def write(self, target: str, account: str, secret: str) -> None:
        self.write_calls.append((target, account, secret))
        self.value = secret

    def delete(self, target: str) -> bool:
        existed = self.value is not None
        self.value = None
        return existed


class FakeMacOSApi:
    def __init__(self) -> None:
        self.value: str | None = None
        self.write_calls: list[tuple[str, str, str]] = []

    def read(self, target: str, account: str) -> str | None:
        return self.value

    def write(self, target: str, account: str, secret: str) -> None:
        self.write_calls.append((target, account, secret))
        self.value = secret

    def delete(self, target: str, account: str) -> bool:
        existed = self.value is not None
        self.value = None
        return existed


class CredentialTests(unittest.TestCase):
    def test_credential_targets_are_provider_scoped_with_deepseek_legacy_compatibility(self) -> None:
        self.assertEqual(credential_target("deepseek", "deepseek-chat"), CREDENTIAL_TARGET)
        self.assertEqual(
            credential_target("vendor-one", "chat-completions-compatible"),
            "codex-multi-relay-vendor-one-api-key",
        )

    def test_generic_store_accepts_provider_token_and_uses_scoped_target(self) -> None:
        api = FakeWindowsApi()
        store = WindowsCredentialStore(
            api=api,
            account="local-user",
            target="codex-multi-relay-vendor-api-key",
            protocol="chat-completions-compatible",
        )

        store.store("provider-token")

        self.assertEqual(
            api.write_calls,
            [("codex-multi-relay-vendor-api-key", "local-user", "provider-token")],
        )

    def test_custom_deepseek_provider_uses_protocol_aware_secret_validation(self) -> None:
        api = FakeWindowsApi()
        with mock.patch(
            "multi_relay.credentials._Win32CredentialApi",
            return_value=api,
        ):
            store = credential_store(
                "windows",
                provider_id="ds-custom",
                protocol="deepseek-chat",
            )

        with self.assertRaises(ManagerError) as raised:
            store.store("ordinary-provider-token")

        self.assertEqual(raised.exception.code, "invalid_api_key")
        self.assertEqual(api.write_calls, [])

    def test_windows_store_delegates_to_credential_blob_api_without_subprocess(self) -> None:
        api = FakeWindowsApi()
        store = WindowsCredentialStore(api=api, account="local-user")

        store.store("sk-test")

        self.assertEqual(
            api.write_calls,
            [(CREDENTIAL_TARGET, "local-user", "sk-test")],
        )
        self.assertEqual(store.read(), "sk-test")
        self.assertTrue(store.remove())
        self.assertIsNone(store.read())

    def test_macos_store_uses_native_keychain_api_without_subprocess_argv(self) -> None:
        api = FakeMacOSApi()
        store = MacOSCredentialStore(api=api, account="local-user")

        store.store("sk-test")

        self.assertEqual(
            api.write_calls,
            [(CREDENTIAL_TARGET, "local-user", "sk-test")],
        )
        self.assertEqual(store.read(), "sk-test")
        self.assertTrue(store.remove())

    def test_backend_error_never_repeats_secret(self) -> None:
        class FailingApi:
            def write(self, target: str, account: str, secret: str) -> None:
                raise OSError(f"backend rejected {secret}")

        store = MacOSCredentialStore(api=FailingApi(), account="local-user")

        with self.assertRaises(ManagerError) as raised:
            store.store("sk-test")

        self.assertNotIn("sk-test", str(raised.exception))
        self.assertNotIn("sk-test", repr(raised.exception.details))

    def test_masked_prompt_stores_key_without_printing_it(self) -> None:
        api = FakeWindowsApi()
        store = WindowsCredentialStore(api=api, account="local-user")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            prompt_and_store(store, prompt_fn=lambda _: "sk-test")

        self.assertEqual(store.read(), "sk-test")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_provider_auth_command_uses_stable_helper_and_contains_no_key(self) -> None:
        command = provider_auth_command()

        self.assertEqual(command[0], sys.executable)
        self.assertEqual(Path(command[1]).name, "credential_helper.py")
        self.assertTrue(Path(command[1]).is_absolute())
        self.assertFalse(any(part.startswith("sk-") for part in command))

    def test_provider_auth_command_scopes_provider_and_bridge_start_without_secret(self) -> None:
        command = provider_auth_command(
            "vendor",
            start_bridge=False,
            protocol="responses-compatible",
        )

        self.assertIn("--provider", command)
        self.assertIn("vendor", command)
        self.assertIn("--no-start-bridge", command)
        self.assertEqual(
            command[command.index("--protocol") + 1],
            "responses-compatible",
        )
        self.assertFalse(any("provider-token" in part for part in command))

    def test_provider_auth_command_carries_an_explicit_codex_home_without_secret(self) -> None:
        codex_home = Path("C:/Users/test/.codex")
        command = provider_auth_command(
            "vendor",
            codex_home=codex_home,
            protocol="responses-compatible",
        )

        self.assertEqual(command[-2:], ["--codex-home", str(codex_home)])

    def test_custom_provider_requires_an_explicit_protocol(self) -> None:
        for operation in (
            lambda: provider_auth_command("vendor"),
            lambda: credential_store("windows", provider_id="vendor"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(ManagerError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, "catalog_invalid")

    def test_credential_helper_can_read_direct_provider_without_starting_bridge(self) -> None:
        store = mock.Mock()
        store.read.return_value = "provider-token"
        stdout = io.StringIO()

        with (
            mock.patch.object(credential_helper, "credential_store", return_value=store) as factory,
            mock.patch.object(credential_helper, "ensure_bridge") as ensure,
            redirect_stdout(stdout),
        ):
            code = credential_helper.main(
                [
                    "--provider",
                    "vendor",
                    "--protocol",
                    "responses-compatible",
                    "--no-start-bridge",
                ]
            )

        self.assertEqual(code, 0)
        factory.assert_called_once_with(
            provider_id="vendor",
            protocol="responses-compatible",
        )
        ensure.assert_not_called()
        self.assertEqual(stdout.getvalue(), "provider-token")

    def test_credential_helper_writes_only_raw_secret(self) -> None:
        store = mock.Mock()
        store.read.return_value = "sk-test"
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.object(credential_helper, "credential_store", return_value=store),
            mock.patch.object(credential_helper, "ensure_bridge") as ensure,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = credential_helper.main()

        self.assertEqual(code, 0)
        ensure.assert_called_once_with()
        self.assertEqual(stdout.getvalue(), "sk-test")
        self.assertEqual(stderr.getvalue(), "")

    def test_credential_helper_forwards_explicit_codex_home_to_bridge(self) -> None:
        store = mock.Mock()
        store.read.return_value = "provider-token"
        with tempfile.TemporaryDirectory() as directory:
            selected_home = Path(directory).resolve()
            with (
                mock.patch.object(credential_helper, "credential_store", return_value=store),
                mock.patch.object(credential_helper, "ensure_bridge") as ensure,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                code = credential_helper.main(
                    [
                        "--provider",
                        "vendor",
                        "--protocol",
                        "chat-completions-compatible",
                        "--codex-home",
                        str(selected_home),
                    ]
                )

        self.assertEqual(code, 0)
        ensure.assert_called_once_with(codex_home=selected_home)

    def test_credential_helper_prints_nothing_when_bridge_cannot_start(self) -> None:
        store = mock.Mock()
        store.read.return_value = "sk-test"
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch.object(credential_helper, "credential_store", return_value=store),
            mock.patch.object(credential_helper, "ensure_bridge", side_effect=OSError("no")),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = credential_helper.main()

        self.assertEqual(code, 4)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
