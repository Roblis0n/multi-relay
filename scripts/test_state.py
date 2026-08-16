#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay.paths import resolve_paths  # noqa: E402
from multi_relay.state import (  # noqa: E402
    PoolRuntimeState,
    RuntimeState,
    RuntimeStateStore,
    TargetRuntimeState,
)


class RuntimeStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "runtime-state.json"
        self.store = RuntimeStateStore(self.path)

    @staticmethod
    def state(
        generation: int,
        *,
        active: str = "target-a",
        reason: str = "quota_exhausted",
    ) -> RuntimeState:
        return RuntimeState(
            schema_version=1,
            catalog_hash="sha256:catalog-a",
            generation=generation,
            pools={
                "general": PoolRuntimeState(
                    active_target_id=active,
                    selected_at="2026-08-16T00:00:00Z",
                    hold_until=None,
                    targets={
                        "target-a": TargetRuntimeState(
                            status="cooldown",
                            reason=reason,
                            retry_at="2026-08-17T00:00:00Z",
                            failure_count=1,
                        )
                    },
                )
            },
        )

    def test_missing_state_loads_empty_and_cas_is_deterministic(self) -> None:
        empty = self.store.load("sha256:catalog-a")
        self.assertEqual(empty.generation, 0)
        self.assertEqual(dict(empty.pools), {})

        committed = self.state(1)
        self.assertTrue(self.store.compare_and_swap(0, committed))
        self.assertEqual(self.store.load("sha256:catalog-a"), committed)
        self.assertFalse(
            self.store.compare_and_swap(0, replace(committed, generation=1))
        )
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["schema_version"],
            1,
        )

    def test_two_concurrent_cas_writers_advance_generation_once(self) -> None:
        barrier = threading.Barrier(2)
        outcomes: list[bool] = []

        def write(active: str) -> None:
            barrier.wait()
            outcomes.append(
                self.store.compare_and_swap(0, self.state(1, active=active))
            )

        threads = [
            threading.Thread(target=write, args=("target-a",)),
            threading.Thread(target=write, args=("target-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(self.store.load("sha256:catalog-a").generation, 1)

    def test_catalog_change_reconciles_only_still_valid_state(self) -> None:
        original = replace(
            self.state(7, active="removed"),
            pools={
                "general": PoolRuntimeState(
                    active_target_id="removed",
                    selected_at="2026-08-16T00:00:00Z",
                    hold_until="2026-08-16T01:00:00Z",
                    targets={
                        "target-a": TargetRuntimeState(
                            status="cooldown",
                            reason="rate_limited",
                            retry_at="2026-08-16T00:01:00Z",
                            failure_count=2,
                        ),
                        "removed": TargetRuntimeState(
                            status="cooldown",
                            reason="auth_invalid",
                            retry_at="2026-08-16T01:00:00Z",
                            failure_count=1,
                        ),
                    },
                ),
                "deleted-pool": PoolRuntimeState.empty(),
            },
        )

        reconciled = original.reconcile(
            "sha256:catalog-b",
            {"general": ("target-a", "target-b")},
        )

        self.assertEqual(reconciled.catalog_hash, "sha256:catalog-b")
        self.assertEqual(reconciled.generation, 8)
        self.assertEqual(tuple(reconciled.pools), ("general",))
        self.assertIsNone(reconciled.pools["general"].active_target_id)
        self.assertIsNone(reconciled.pools["general"].selected_at)
        self.assertIsNone(reconciled.pools["general"].hold_until)
        self.assertEqual(
            tuple(reconciled.pools["general"].targets),
            ("target-a",),
        )

    def test_reset_pool_is_atomic_and_preserves_other_pools(self) -> None:
        initial = replace(
            self.state(1),
            pools={
                "general": self.state(1).pools["general"],
                "review": PoolRuntimeState.empty(),
            },
        )
        self.assertTrue(self.store.compare_and_swap(0, initial))

        reset = self.store.reset_pool("general", "sha256:catalog-a")

        self.assertEqual(reset.generation, 2)
        self.assertNotIn("general", reset.pools)
        self.assertIn("review", reset.pools)

    def test_truncated_and_invalid_json_recover_without_rewriting_source(self) -> None:
        for raw in (b'{"schema_version":1', b"not-json", b"\xff"):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                loaded = self.store.load("sha256:catalog-a")
                self.assertEqual(loaded, RuntimeState.empty("sha256:catalog-a"))
                self.assertEqual(self.path.read_bytes(), raw)

    def test_future_schema_is_rejected_without_overwrite(self) -> None:
        raw = b'{"schema_version":999,"catalog_hash":"sha256:x","generation":1,"pools":{}}\n'
        self.path.write_bytes(raw)

        with self.assertRaises(ManagerError) as raised:
            self.store.load("sha256:catalog-a")

        self.assertEqual(raised.exception.code, "unsupported_runtime_state_schema")
        self.assertEqual(self.path.read_bytes(), raw)

    def test_atomic_replace_failure_leaves_previous_state_unchanged(self) -> None:
        initial = self.state(1)
        self.assertTrue(self.store.compare_and_swap(0, initial))
        before = self.path.read_bytes()

        def fail_write(path: Path, data: bytes, mode: int) -> None:
            del path, data, mode
            raise OSError("simulated replace failure")

        failing = RuntimeStateStore(self.path, writer=fail_write)
        with self.assertRaises(ManagerError) as raised:
            failing.compare_and_swap(1, replace(initial, generation=2))

        self.assertEqual(raised.exception.code, "runtime_state_write_failed")
        self.assertEqual(self.path.read_bytes(), before)

    def test_secret_scanner_blocks_state_before_write(self) -> None:
        unsafe = self.state(1, reason="sk-example-secret-material")

        with self.assertRaises(ManagerError) as raised:
            self.store.compare_and_swap(0, unsafe)

        self.assertEqual(raised.exception.code, "secret_not_allowed")
        self.assertFalse(self.path.exists())

    def test_runtime_lock_path_is_separate_and_resolution_is_read_only(self) -> None:
        selected = resolve_paths(
            str(self.root / "codex"),
            state_home=self.root / "state",
            platform="linux",
            user_home=self.root / "user",
        )

        self.assertEqual(
            selected.runtime_state_lock,
            selected.product_state_dir / "runtime-state.lock",
        )
        self.assertFalse(selected.product_state_dir.exists())


if __name__ == "__main__":
    unittest.main()
