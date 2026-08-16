#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
import multi_relay.transaction as transaction_module  # noqa: E402
from multi_relay.transaction import (  # noqa: E402
    InstallPlan,
    atomic_write,
    execute_install_plan,
    operation_lock,
    rollback_transaction,
)


class TransactionTests(unittest.TestCase):
    def test_precondition_change_after_backup_aborts_before_any_install_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            role = root / "agents" / "default.toml"
            manifest = root / "state" / "manifest.json"
            backup = root / "state" / "backups" / "precondition-race"
            original = b"original-owner-bytes\n"
            concurrent = b"concurrent-owner-bytes\n"
            config.write_bytes(original)
            real_write_backup = transaction_module._write_backup

            def backup_then_change(path: Path, snapshots: object) -> None:
                real_write_backup(path, snapshots)  # type: ignore[arg-type]
                config.write_bytes(concurrent)

            with patch.object(
                transaction_module,
                "_write_backup",
                side_effect=backup_then_change,
            ):
                with self.assertRaises(ManagerError) as raised:
                    execute_install_plan(
                        InstallPlan(
                            files={
                                config: b"migrated-bytes\n",
                                role: b"role-bytes\n",
                            },
                            removals=(),
                            manifest={"schema_version": 5},
                            backup_dir=backup,
                            preconditions={
                                config: hashlib.sha256(original).hexdigest(),
                            },
                        ),
                        manifest,
                    )

            self.assertEqual(raised.exception.code, "transaction_precondition_failed")
            self.assertEqual(config.read_bytes(), concurrent)
            self.assertFalse(role.exists())
            self.assertFalse(manifest.exists())
            self.assertTrue((backup / "snapshot.json").is_file())

    def test_success_writes_manifest_last_and_keeps_recoverable_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            role = root / "agents" / "default.toml"
            legacy = root / "agents" / "DeepSeek.toml"
            manifest = root / "state" / "manifest.json"
            backup = root / "state" / "backups" / "test-run"
            config.write_bytes(b'original = true\n')
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"legacy-agent\n")
            writes: list[Path] = []

            def recording_writer(path: Path, data: bytes, mode: int = 0o600) -> None:
                writes.append(path)
                atomic_write(path, data, mode)

            result = execute_install_plan(
                InstallPlan(
                    files={config: b'updated = true\n', role: b'model = "deepseek"\n'},
                    removals=(legacy,),
                    manifest={"schema_version": 4, "status": "enabled"},
                    backup_dir=backup,
                ),
                manifest,
                writer=recording_writer,
            )

            self.assertEqual(config.read_bytes(), b'updated = true\n')
            self.assertEqual(role.read_bytes(), b'model = "deepseek"\n')
            self.assertFalse(legacy.exists())
            self.assertEqual(writes[-1], manifest)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 4)
            self.assertEqual(payload["status"], "enabled")
            self.assertEqual(Path(payload["backup"]), backup)
            self.assertTrue((backup / "snapshot.json").is_file())

            rollback_transaction(result)

            self.assertEqual(config.read_bytes(), b'original = true\n')
            self.assertFalse(role.exists())
            self.assertEqual(legacy.read_bytes(), b"legacy-agent\n")
            self.assertFalse(manifest.exists())

    def test_every_injected_target_write_failure_restores_exact_pre_state(self) -> None:
        for fail_on_call in (1, 2, 3):
            with self.subTest(fail_on_call=fail_on_call), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "config.toml"
                role = root / "agents" / "default.toml"
                manifest = root / "state" / "manifest.json"
                config.write_bytes(b'original = true\n')
                calls = 0

                def failing_writer(path: Path, data: bytes, mode: int = 0o600) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == fail_on_call:
                        raise OSError("injected write failure")
                    atomic_write(path, data, mode)

                with self.assertRaises(ManagerError) as raised:
                    execute_install_plan(
                        InstallPlan(
                            files={
                                config: b'updated = true\n',
                                role: b'model = "deepseek"\n',
                            },
                            removals=(),
                            manifest={"schema_version": 4},
                            backup_dir=root / "state" / "backups" / "failed-run",
                        ),
                        manifest,
                        writer=failing_writer,
                    )

                self.assertEqual(raised.exception.code, "transaction_failed")
                self.assertEqual(config.read_bytes(), b'original = true\n')
                self.assertFalse(role.exists())
                self.assertFalse(manifest.exists())

    def test_operation_lock_rejects_concurrent_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "manager.lock"
            with operation_lock(lock_path):
                with self.assertRaises(ManagerError) as raised:
                    with operation_lock(lock_path, timeout_seconds=0.05):
                        self.fail("a second operation acquired the same lock")

        self.assertEqual(raised.exception.code, "operation_in_progress")


if __name__ == "__main__":
    unittest.main()
