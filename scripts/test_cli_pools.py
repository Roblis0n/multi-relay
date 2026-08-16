#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay.catalog import ExecutionTarget, ProviderSpec, TargetPool  # noqa: E402
from multi_relay.cli import MAX_DURATION_SECONDS, build_parser, parse_duration  # noqa: E402
from test_cli_targets import installed_manager  # noqa: E402


class PoolCliAndManagerTests(unittest.TestCase):
    def test_duration_units_and_hard_boundaries(self) -> None:
        self.assertEqual(parse_duration("1s"), 1)
        self.assertEqual(parse_duration("2m"), 120)
        self.assertEqual(parse_duration("3h"), 10800)
        self.assertEqual(parse_duration("4d"), 345600)
        self.assertEqual(parse_duration("365d"), MAX_DURATION_SECONDS)
        for value in ("0s", "-1h", "1", "366d"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                parse_duration(value)

    def test_parser_exposes_every_pool_action(self) -> None:
        parser = build_parser()
        samples = {
            "list": [],
            "add": [
                "--id", "p", "--target", "t", "--capability", "text", "--host", "codex",
            ],
            "edit": ["p", "--max-rate-limit-wait", "5"],
            "order": ["p", "t"],
            "strategy": ["p", "timed", "--duration", "2h"],
            "rotate": ["p"],
            "reset": ["p"],
            "status": ["p"],
            "remove": ["p"],
        }
        for action, tail in samples.items():
            with self.subTest(action=action):
                parsed = parser.parse_args(["pool", action, *tail])
                self.assertEqual(parsed.pool_command, action)

    def test_pool_order_rejects_duplicates_missing_and_disabled_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = installed_manager(Path(directory))
            pool_id = str(manager.list_pools()[0]["id"])
            target_id = str(manager.list_targets()[0]["id"])

            with self.assertRaises(ManagerError) as duplicate:
                manager.set_pool_order(pool_id, [target_id, target_id])
            self.assertEqual(duplicate.exception.code, "duplicate_target")

            with self.assertRaises(ManagerError) as missing:
                manager.set_pool_order(pool_id, ["missing-target"])
            self.assertEqual(missing.exception.code, "unknown_target")

            manager.set_target_enabled(target_id, False)
            with self.assertRaises(ManagerError) as disabled:
                manager.set_pool_order(pool_id, [target_id])
            self.assertEqual(disabled.exception.code, "target_disabled")

    def test_timed_strategy_requires_duration_and_sticky_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = installed_manager(Path(directory))
            pool_id = str(manager.list_pools()[0]["id"])

            with self.assertRaises(ManagerError):
                manager.set_pool_strategy(pool_id, "timed")
            manager.set_pool_strategy(pool_id, "timed", duration_seconds=7200)
            self.assertEqual(manager.list_pools()[0]["duration_seconds"], 7200)
            manager.set_pool_strategy(pool_id, "sticky")
            self.assertIsNone(manager.list_pools()[0]["duration_seconds"])


if __name__ == "__main__":
    unittest.main()
