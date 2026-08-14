#!/usr/bin/env python3

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "codex-deepseek-subagent"
    / "scripts"
)
sys.path.insert(0, str(PACKAGE_ROOT))

from deepseek_fanout import ManagerError  # noqa: E402
from deepseek_fanout import credential_helper  # noqa: E402
from deepseek_fanout.credentials import (  # noqa: E402
    CREDENTIAL_TARGET,
    MacOSCredentialStore,
    WindowsCredentialStore,
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
