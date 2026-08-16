#!/usr/bin/env python3

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay.catalog import CredentialRef  # noqa: E402
from multi_relay.credentials import (  # noqa: E402
    CREDENTIAL_TARGET,
    LinuxSecretServiceCredentialStore,
    MacOSCredentialStore,
    VaultLocator,
    WindowsCredentialStore,
    credential_metadata,
    credential_store,
    migrate_legacy_deepseek_credential,
    read_credential_for_execution,
    redact_secret,
)
from multi_relay.paths import resolve_paths  # noqa: E402


class KeyedWindowsApi:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def read(self, target: str) -> str | None:
        return self.values.get(target)

    def write(self, target: str, account: str, secret: str) -> None:
        self.values[target] = secret

    def delete(self, target: str) -> bool:
        return self.values.pop(target, None) is not None


class KeyedMacOSApi:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def read(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def write(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def delete(self, service: str, account: str) -> bool:
        return self.values.pop((service, account), None) is not None


class FakeSecretToolRunner:
    def __init__(self) -> None:
        self.values: dict[tuple[str, ...], str] = {}
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.options: list[dict[str, object]] = []

    def __call__(self, command: list[str], **options: object) -> SimpleNamespace:
        supplied = options.get("input")
        self.calls.append((list(command), supplied if isinstance(supplied, bytes) else None))
        self.options.append(dict(options))
        operation = command[1]
        start = 3 if operation == "store" else 2
        attributes = tuple(command[start:])
        if operation == "store":
            assert isinstance(supplied, bytes)
            self.values[attributes] = supplied.decode("utf-8")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if operation == "lookup":
            value = self.values.get(attributes)
            return SimpleNamespace(
                returncode=0 if value is not None else 1,
                stdout=value.encode("utf-8") if value is not None else b"",
                stderr=b"",
            )
        if operation == "clear":
            existed = self.values.pop(attributes, None) is not None
            return SimpleNamespace(returncode=0 if existed else 1, stdout=b"", stderr=b"")
        raise AssertionError(f"Unexpected secret-tool operation: {operation}")


@dataclass
class MemoryStore:
    value: str | None = None
    delete_error: Exception | None = None
    corrupt_reads: bool = False

    def exists(self) -> bool:
        return self.value is not None

    def store(self, secret: str) -> None:
        self.value = secret

    def read(self) -> str | None:
        if self.corrupt_reads and self.value is not None:
            return "different-value"
        return self.value

    def remove(self) -> bool:
        if self.delete_error is not None:
            raise self.delete_error
        existed = self.value is not None
        self.value = None
        return existed


def credential_ref(
    provider_id: str,
    credential_id: str,
    *,
    enabled: bool = True,
) -> CredentialRef:
    return CredentialRef.from_dict(
        {
            "id": credential_id,
            "provider_id": provider_id,
            "vault_target": VaultLocator(provider_id, credential_id).target,
            "enabled": enabled,
            "created_at": "2026-08-16T00:00:00Z",
            "label": credential_id.title(),
        }
    )


class MultiCredentialTests(unittest.TestCase):
    def test_redaction_guard_removes_all_known_secrets(self) -> None:
        rendered = redact_secret(
            "primary-secret then backup-secret then primary-secret",
            "primary-secret",
            "backup-secret",
        )

        self.assertEqual(rendered, "[REDACTED] then [REDACTED] then [REDACTED]")

    def test_product_state_path_is_injectable_and_resolution_has_no_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = resolve_paths(
                str(root / "codex"),
                state_home=root / "state",
                platform="linux",
                user_home=root / "user",
            )

            self.assertEqual(selected.product_state_dir, (root / "state" / "multi-relay").resolve())
            self.assertEqual(selected.runtime_state, selected.product_state_dir / "runtime-state.json")
            self.assertEqual(selected.gateway_state, selected.product_state_dir / "gateway-state.json")
            self.assertFalse(selected.product_state_dir.exists())

    def test_injected_user_home_is_hermetic_from_host_state_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with mock.patch.dict(
                "os.environ",
                {
                    "LOCALAPPDATA": str(root / "host-local"),
                    "XDG_STATE_HOME": str(root / "host-xdg"),
                },
            ):
                windows = resolve_paths(
                    str(root / "codex-win"),
                    platform="windows",
                    user_home=root / "simulated-win",
                )
                linux = resolve_paths(
                    str(root / "codex-linux"),
                    platform="linux",
                    user_home=root / "simulated-linux",
                )

            self.assertEqual(
                windows.product_state_dir,
                root / "simulated-win" / "AppData" / "Local" / "multi-relay",
            )
            self.assertEqual(
                linux.product_state_dir,
                root / "simulated-linux" / ".local" / "state" / "multi-relay",
            )

    def test_same_provider_primary_and_backup_are_independent(self) -> None:
        api = KeyedWindowsApi()
        primary = WindowsCredentialStore(
            api=api,
            account="local-user",
            locator=VaultLocator("vendor", "primary"),
            protocol="responses-compatible",
        )
        backup = WindowsCredentialStore(
            api=api,
            account="local-user",
            locator=VaultLocator("vendor", "backup"),
            protocol="responses-compatible",
        )

        primary.store("primary-secret")
        backup.store("backup-secret")

        self.assertEqual(primary.read(), "primary-secret")
        self.assertEqual(backup.read(), "backup-secret")
        self.assertEqual(
            set(api.values),
            {"multi-relay/vendor/primary", "multi-relay/vendor/backup"},
        )

    def test_same_credential_id_on_different_providers_does_not_collide(self) -> None:
        api = KeyedWindowsApi()
        first = WindowsCredentialStore(
            api=api,
            locator=VaultLocator("provider-a", "primary"),
            protocol="responses-compatible",
        )
        second = WindowsCredentialStore(
            api=api,
            locator=VaultLocator("provider-b", "primary"),
            protocol="anthropic-messages",
        )

        first.store("first-secret")
        second.store("second-secret")

        self.assertEqual(first.read(), "first-secret")
        self.assertEqual(second.read(), "second-secret")

    def test_credential_metadata_never_opens_a_store(self) -> None:
        references = [credential_ref("provider-a", "primary"), credential_ref("provider-a", "backup")]
        forbidden_factory = mock.Mock(side_effect=AssertionError("vault must not be read"))

        metadata = credential_metadata(references, store_factory=forbidden_factory)

        forbidden_factory.assert_not_called()
        self.assertEqual(
            metadata,
            [
                {
                    "id": "primary",
                    "provider_id": "provider-a",
                    "vault_target": "multi-relay/provider-a/primary",
                    "enabled": True,
                    "created_at": "2026-08-16T00:00:00Z",
                    "label": "Primary",
                },
                {
                    "id": "backup",
                    "provider_id": "provider-a",
                    "vault_target": "multi-relay/provider-a/backup",
                    "enabled": True,
                    "created_at": "2026-08-16T00:00:00Z",
                    "label": "Backup",
                },
            ],
        )

    def test_disabled_credential_is_not_read_by_execution_path(self) -> None:
        reference = credential_ref("vendor", "primary", enabled=False)
        factory = mock.Mock(side_effect=AssertionError("disabled vault must not be opened"))

        with self.assertRaises(ManagerError) as raised:
            read_credential_for_execution(
                reference,
                protocol="responses-compatible",
                store_factory=factory,
            )

        self.assertEqual(raised.exception.code, "credential_disabled")
        factory.assert_not_called()

    def test_enabled_execution_read_uses_the_reference_exactly(self) -> None:
        reference = credential_ref("vendor", "backup")
        store = MemoryStore("provider-secret")
        factory = mock.Mock(return_value=store)

        secret = read_credential_for_execution(
            reference,
            protocol="responses-compatible",
            platform="windows",
            store_factory=factory,
        )

        self.assertEqual(secret, "provider-secret")
        factory.assert_called_once_with(
            "windows",
            provider_id="vendor",
            credential_id="backup",
            protocol="responses-compatible",
            vault_target="multi-relay/vendor/backup",
        )

    def test_remove_deletes_only_the_exact_locator(self) -> None:
        api = KeyedWindowsApi()
        primary = WindowsCredentialStore(
            api=api,
            locator=VaultLocator("vendor", "primary"),
            protocol="responses-compatible",
        )
        backup = WindowsCredentialStore(
            api=api,
            locator=VaultLocator("vendor", "backup"),
            protocol="responses-compatible",
        )
        primary.store("primary-secret")
        backup.store("backup-secret")

        self.assertTrue(backup.remove())

        self.assertEqual(primary.read(), "primary-secret")
        self.assertIsNone(backup.read())

    def test_factory_uses_provider_and_credential_for_fake_native_backends(self) -> None:
        win_api = KeyedWindowsApi()
        mac_api = KeyedMacOSApi()
        with mock.patch("multi_relay.credentials._Win32CredentialApi", return_value=win_api):
            windows = credential_store(
                "windows",
                provider_id="vendor",
                credential_id="backup",
                protocol="responses-compatible",
            )
        with mock.patch("multi_relay.credentials._MacOSKeychainApi", return_value=mac_api):
            macos = credential_store(
                "macos",
                provider_id="vendor",
                credential_id="backup",
                protocol="responses-compatible",
            )

        windows.store("windows-secret")
        macos.store("macos-secret")

        self.assertEqual(win_api.values["multi-relay/vendor/backup"], "windows-secret")
        self.assertEqual(mac_api.values[("multi-relay", "vendor/backup")], "macos-secret")

    def test_linux_secret_service_uses_attributes_and_never_puts_secret_in_argv(self) -> None:
        runner = FakeSecretToolRunner()
        store = LinuxSecretServiceCredentialStore(
            locator=VaultLocator("vendor", "backup"),
            protocol="responses-compatible",
            label="备用凭据 🔐",
            runner=runner,
        )

        store.store("linux-secret")

        self.assertEqual(store.read(), "linux-secret")
        self.assertTrue(store.remove())
        self.assertIsNone(store.read())
        for command, supplied in runner.calls:
            self.assertNotIn("linux-secret", command)
            if command[1] == "store":
                self.assertEqual(supplied, b"linux-secret")
                self.assertIn("--label=备用凭据 🔐", command)
            self.assertEqual(
                command[-6:],
                ["application", "multi-relay", "provider", "vendor", "credential", "backup"],
            )
        for options in runner.options:
            self.assertTrue(options["capture_output"])
            self.assertFalse(options["check"])
            self.assertFalse(options["shell"])
            self.assertNotIn("text", options)

    def test_linux_missing_secret_tool_fails_explicitly_without_plaintext_fallback(self) -> None:
        test_secret = "secret-that-must-not-leak"

        def missing_runner(command: list[str], **options: object) -> object:
            raise FileNotFoundError("secret-tool is missing")

        store = LinuxSecretServiceCredentialStore(
            locator=VaultLocator("vendor", "primary"),
            protocol="responses-compatible",
            runner=missing_runner,
        )

        with self.assertRaises(ManagerError) as raised:
            store.store(test_secret)

        self.assertEqual(raised.exception.code, "credential_backend_unavailable")
        self.assertNotIn(test_secret, str(raised.exception))
        self.assertNotIn(test_secret, repr(raised.exception.details))

    def test_linux_backend_error_is_not_misreported_as_missing_or_echoed(self) -> None:
        test_secret = "secret-from-backend-error"

        def failing_runner(command: list[str], **options: object) -> object:
            return SimpleNamespace(
                returncode=1,
                stdout=b"",
                stderr=f"service failure containing {test_secret}".encode("utf-8"),
            )

        store = LinuxSecretServiceCredentialStore(
            locator=VaultLocator("vendor", "primary"),
            protocol="responses-compatible",
            runner=failing_runner,
        )

        with self.assertRaises(ManagerError) as raised:
            store.read()

        self.assertEqual(raised.exception.code, "credential_read_failed")
        self.assertNotIn(test_secret, str(raised.exception))
        self.assertNotIn(test_secret, repr(raised.exception.details))

    def test_legacy_deepseek_migration_verifies_switches_then_deletes(self) -> None:
        source = MemoryStore("sk-legacy")
        destination = MemoryStore()
        switched: list[str] = []

        result = migrate_legacy_deepseek_credential(
            source_store=source,
            destination_store=destination,
            switch_reference=switched.append,
        )

        self.assertTrue(result.migrated)
        self.assertTrue(result.verified)
        self.assertTrue(result.legacy_removed)
        self.assertFalse(result.cleanup_pending)
        self.assertEqual(switched, ["multi-relay/deepseek/primary"])
        self.assertEqual(destination.read(), "sk-legacy")
        self.assertIsNone(source.read())

    def test_legacy_deepseek_target_is_read_only_outside_migration_cleanup(self) -> None:
        api = KeyedWindowsApi()
        api.values[CREDENTIAL_TARGET] = "sk-legacy"
        with mock.patch("multi_relay.credentials._Win32CredentialApi", return_value=api):
            source = credential_store(
                "windows",
                provider_id="deepseek",
                credential_id="primary",
                protocol="deepseek-chat",
                vault_target=CREDENTIAL_TARGET,
            )

        self.assertEqual(source.read(), "sk-legacy")
        with self.assertRaises(ManagerError) as raised:
            source.store("sk-new-value")
        self.assertEqual(raised.exception.code, "legacy_credential_read_only")
        self.assertEqual(api.values[CREDENTIAL_TARGET], "sk-legacy")

    def test_legacy_migration_rolls_back_new_target_when_switch_fails(self) -> None:
        test_secret = "sk-rollback-secret"
        source = MemoryStore(test_secret)
        destination = MemoryStore()

        def fail_switch(target: str) -> None:
            raise RuntimeError(f"cannot switch {test_secret}")

        with self.assertRaises(ManagerError) as raised:
            migrate_legacy_deepseek_credential(
                source_store=source,
                destination_store=destination,
                switch_reference=fail_switch,
            )

        self.assertEqual(raised.exception.code, "credential_migration_failed")
        self.assertEqual(source.read(), test_secret)
        self.assertIsNone(destination.read())
        self.assertNotIn(test_secret, str(raised.exception))
        self.assertNotIn(test_secret, repr(raised.exception.details))

    def test_legacy_migration_reports_safe_cleanup_pending_after_delete_failure(self) -> None:
        test_secret = "sk-cleanup-secret"
        source = MemoryStore(
            test_secret,
            delete_error=OSError(f"cannot delete {test_secret}"),
        )
        destination = MemoryStore()

        result = migrate_legacy_deepseek_credential(
            source_store=source,
            destination_store=destination,
            switch_reference=lambda target: None,
        )

        self.assertTrue(result.migrated)
        self.assertTrue(result.verified)
        self.assertFalse(result.legacy_removed)
        self.assertTrue(result.cleanup_pending)
        self.assertEqual(result.cleanup_error, "credential_delete_failed")
        self.assertEqual(destination.read(), test_secret)
        self.assertEqual(source.read(), test_secret)
        self.assertNotIn(test_secret, repr(result))

    def test_legacy_migration_is_idempotent_when_source_is_already_absent(self) -> None:
        destination = MemoryStore("sk-already-migrated")
        switch = mock.Mock()

        result = migrate_legacy_deepseek_credential(
            source_store=MemoryStore(),
            destination_store=destination,
            switch_reference=switch,
        )

        self.assertFalse(result.migrated)
        self.assertFalse(result.cleanup_pending)
        self.assertEqual(destination.read(), "sk-already-migrated")
        switch.assert_not_called()

    def test_legacy_migration_is_a_clean_noop_on_linux_where_no_old_backend_existed(self) -> None:
        destination = MemoryStore("sk-canonical")
        switch = mock.Mock()

        result = migrate_legacy_deepseek_credential(
            platform="linux",
            destination_store=destination,
            switch_reference=switch,
        )

        self.assertFalse(result.migrated)
        self.assertEqual(destination.read(), "sk-canonical")
        switch.assert_not_called()

    def test_legacy_migration_refuses_a_different_existing_destination(self) -> None:
        source = MemoryStore("sk-legacy")
        destination = MemoryStore("sk-user-owned")
        switch = mock.Mock()

        with self.assertRaises(ManagerError) as raised:
            migrate_legacy_deepseek_credential(
                source_store=source,
                destination_store=destination,
                switch_reference=switch,
            )

        self.assertEqual(raised.exception.code, "credential_migration_conflict")
        self.assertEqual(source.read(), "sk-legacy")
        self.assertEqual(destination.read(), "sk-user-owned")
        switch.assert_not_called()

    def test_legacy_migration_rolls_back_unverified_destination(self) -> None:
        source = MemoryStore("sk-verification")
        destination = MemoryStore(corrupt_reads=True)

        with self.assertRaises(ManagerError) as raised:
            migrate_legacy_deepseek_credential(
                source_store=source,
                destination_store=destination,
                switch_reference=lambda target: None,
            )

        self.assertEqual(raised.exception.code, "credential_migration_failed")
        self.assertEqual(source.read(), "sk-verification")
        self.assertIsNone(destination.value)


if __name__ == "__main__":
    unittest.main()
