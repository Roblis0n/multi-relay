#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay.catalog import Catalog, default_catalog  # noqa: E402
from multi_relay.failure import (  # noqa: E402
    FailureClass,
    NormalizedFailure,
    RetryDirective,
)
from multi_relay.rotation import RotationController  # noqa: E402
from multi_relay.selection import (  # noqa: E402
    SelectionRequirements,
    TargetSelector,
)
from multi_relay.state import RuntimeState, RuntimeStateStore  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 8, 16, tzinfo=UTC)
        self.tick = 10_000.0

    def now_utc(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.tick

    def advance(
        self,
        seconds: float = 0,
        *,
        wall_seconds: float | None = None,
        monotonic_seconds: float | None = None,
    ) -> None:
        self.wall += timedelta(
            seconds=seconds if wall_seconds is None else wall_seconds
        )
        self.tick += seconds if monotonic_seconds is None else monotonic_seconds


class FixedRandom:
    def random(self) -> float:
        return 0.5


def rotation_catalog(
    *,
    strategy: str = "sticky",
    duration_seconds: int | None = None,
    target_count: int = 2,
    separate_credentials: bool = False,
) -> Catalog:
    payload = default_catalog().to_dict()
    original = next(
        item for item in payload["targets"] if item["id"] == "deepseek-primary"
    )
    assert isinstance(original, dict)
    targets: list[dict[str, object]] = []
    for index in range(target_count):
        item = dict(original)
        item["id"] = f"target-{chr(ord('a') + index)}"
        item["model"] = f"model-{chr(ord('a') + index)}"
        item["metadata"] = {}
        if separate_credentials and index:
            item["credential_id"] = "backup"
        targets.append(item)
    native = next(item for item in payload["targets"] if item["id"] == "codex-native")
    payload["targets"] = targets + [native]
    if separate_credentials:
        payload["credentials"].append(
            {
                "id": "backup",
                "provider_id": "deepseek",
                "vault_target": "multi-relay/deepseek-chat/deepseek/backup",
                "enabled": True,
                "created_at": "2026-08-16T00:00:00Z",
                "label": "Backup",
            }
        )
    general = next(item for item in payload["pools"] if item["id"] == "general")
    general["targets"] = [item["id"] for item in targets]
    general["strategy"] = strategy
    general["duration_seconds"] = duration_seconds
    general["cooldown"] = {
        "quota_seconds": 100,
        "rate_limit_seconds": 20,
        "auth_seconds": 300,
        "provider_seconds": 40,
    }
    return Catalog.from_dict(payload)


def normalized_failure(
    failure_class: FailureClass,
    *,
    retry_after_seconds: float | None = None,
) -> NormalizedFailure:
    return NormalizedFailure(
        failure_class=failure_class,
        code=failure_class.value,
        message="Safe normalized failure.",
        retry=RetryDirective(
            retry_same_target=False,
            failover_allowed=failure_class
            not in {
                FailureClass.REQUEST_INVALID,
                FailureClass.CONTEXT_EXCEEDED,
                FailureClass.POLICY_BLOCKED,
                FailureClass.CANCELLED,
            },
            disable_credential=failure_class is FailureClass.AUTH_INVALID,
            retry_after_seconds=retry_after_seconds,
        ),
        provider_id="deepseek",
    )


class RotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = FakeClock()

    def controller(
        self,
        catalog: Catalog,
        *,
        credential_available=None,
        filename: str = "runtime-state.json",
    ) -> RotationController:
        return RotationController(
            catalog,
            RuntimeStateStore(self.root / filename),
            clock=self.clock,
            random_source=FixedRandom(),
            credential_available=credential_available,
        )

    @staticmethod
    def requirements(**changes) -> SelectionRequirements:
        values = {
            "host": "codex",
            "required_capabilities": frozenset({"text"}),
            "context_tokens": 1,
            "required_trust": "standard",
        }
        values.update(changes)
        return SelectionRequirements(**values)

    def test_sticky_prefers_first_fails_over_stays_and_resets(self) -> None:
        controller = self.controller(rotation_catalog())
        requirements = self.requirements()

        initial = controller.select("general", requirements)
        self.assertEqual(initial.selected_target_id, "target-a")
        switched = controller.record_failure(
            "general",
            "target-a",
            normalized_failure(FailureClass.QUOTA_EXHAUSTED),
            expected_generation=initial.generation,
            requirements=requirements,
        )
        self.assertTrue(switched.changed)
        self.assertEqual(switched.selected_target_id, "target-b")
        self.assertEqual(
            controller.select("general", requirements).selected_target_id,
            "target-b",
        )

        reset = controller.reset_pool("general")
        self.assertGreater(reset.generation, switched.generation)
        self.assertEqual(
            controller.select("general", requirements).selected_target_id,
            "target-a",
        )

    def test_timed_holds_backup_then_retries_primary_after_expiry(self) -> None:
        catalog = rotation_catalog(strategy="timed", duration_seconds=20)
        controller = self.controller(catalog)
        requirements = self.requirements()
        initial = controller.select("general", requirements)
        switched = controller.record_failure(
            "general",
            "target-a",
            normalized_failure(FailureClass.PROVIDER_UNAVAILABLE),
            expected_generation=initial.generation,
            requirements=requirements,
        )
        self.assertEqual(switched.selected_target_id, "target-b")

        self.clock.advance(10)
        self.assertEqual(
            controller.select("general", requirements).selected_target_id,
            "target-b",
        )
        self.clock.advance(31)
        self.assertEqual(
            controller.select("general", requirements).selected_target_id,
            "target-a",
        )

    def test_timed_expiry_keeps_current_when_primary_is_cooling(self) -> None:
        controller = self.controller(
            rotation_catalog(strategy="timed", duration_seconds=10)
        )
        requirements = self.requirements()
        initial = controller.select("general", requirements)
        switched = controller.record_failure(
            "general",
            "target-a",
            normalized_failure(FailureClass.QUOTA_EXHAUSTED),
            expected_generation=initial.generation,
            requirements=requirements,
        )
        self.assertEqual(switched.selected_target_id, "target-b")

        self.clock.advance(11)
        selected = controller.select("general", requirements)
        self.assertEqual(selected.selected_target_id, "target-b")

    def test_selector_filters_capability_context_host_trust_and_credential(self) -> None:
        catalog = rotation_catalog(target_count=1)
        selector = TargetSelector()
        state = RuntimeState.empty("sha256:test")

        cases = (
            (
                self.requirements(required_capabilities=frozenset({"vision"})),
                lambda credential: True,
                "capability_missing",
                catalog,
            ),
            (
                self.requirements(context_tokens=2_000_000),
                lambda credential: True,
                "context_exceeded",
                catalog,
            ),
            (
                self.requirements(host="claude-code"),
                lambda credential: True,
                "host_incompatible",
                replace(
                    catalog,
                    targets=tuple(
                        replace(item, host_compatibility=("codex",))
                        if item.id == "target-a"
                        else item
                        for item in catalog.targets
                    ),
                ),
            ),
            (
                self.requirements(required_trust="high"),
                lambda credential: True,
                "trust_too_low",
                catalog,
            ),
            (
                self.requirements(),
                lambda credential: False,
                "credential_unavailable",
                catalog,
            ),
        )
        for requirements, available, expected, selected_catalog in cases:
            with self.subTest(reason=expected):
                result = selector.select(
                    selected_catalog,
                    "general",
                    state,
                    requirements,
                    now=self.clock.now_utc(),
                    credential_available=available,
                )
                self.assertIsNone(result.selected_target_id)
                self.assertIn(expected, result.rejections[0].reasons)

        disabled = replace(
            catalog,
            credentials=tuple(replace(item, enabled=False) for item in catalog.credentials),
        )
        result = selector.select(
            disabled,
            "general",
            state,
            self.requirements(),
            now=self.clock.now_utc(),
            credential_available=lambda credential: True,
        )
        self.assertIn("credential_disabled", result.rejections[0].reasons)

    def test_no_target_result_lists_every_target_with_secret_free_reasons(self) -> None:
        catalog = rotation_catalog(target_count=3)
        result = TargetSelector().select(
            catalog,
            "general",
            RuntimeState.empty("sha256:test"),
            self.requirements(),
            now=self.clock.now_utc(),
            credential_available=lambda credential: False,
        )

        self.assertIsNone(result.selected_target_id)
        self.assertEqual(
            [item.target_id for item in result.rejections],
            ["target-a", "target-b", "target-c"],
        )
        encoded = json.dumps(result.to_dict(), sort_keys=True)
        self.assertNotIn("vault_target", encoded)
        self.assertNotIn("credential_id", encoded)
        self.assertTrue(
            all(item.reasons == ("credential_unavailable",) for item in result.rejections)
        )

    def test_failure_classes_use_distinct_pool_cooldowns(self) -> None:
        cases = {
            FailureClass.QUOTA_EXHAUSTED: 100,
            FailureClass.RATE_LIMITED: 75,
            FailureClass.AUTH_INVALID: 300,
            FailureClass.PROVIDER_UNAVAILABLE: 40,
        }
        for index, (failure_class, expected_seconds) in enumerate(cases.items()):
            with self.subTest(failure_class=failure_class.value):
                controller = self.controller(
                    rotation_catalog(separate_credentials=True),
                    filename=f"runtime-{index}.json",
                )
                requirements = self.requirements()
                initial = controller.select("general", requirements)
                failure = normalized_failure(
                    failure_class,
                    retry_after_seconds=75
                    if failure_class is FailureClass.RATE_LIMITED
                    else None,
                )
                switched = controller.record_failure(
                    "general",
                    "target-a",
                    failure,
                    expected_generation=initial.generation,
                    requirements=requirements,
                )
                state = controller.store.load(controller.catalog_hash)
                target_state = state.pools["general"].targets["target-a"]
                retry_at = datetime.fromisoformat(
                    target_state.retry_at.replace("Z", "+00:00")
                )
                self.assertEqual(
                    retry_at - self.clock.now_utc(),
                    timedelta(seconds=expected_seconds),
                )
                self.assertEqual(switched.selected_target_id, "target-b")

    def test_same_credential_auth_failure_cools_every_bound_target(self) -> None:
        controller = self.controller(rotation_catalog(target_count=3))
        requirements = self.requirements()
        initial = controller.select("general", requirements)

        result = controller.record_failure(
            "general",
            "target-a",
            normalized_failure(FailureClass.AUTH_INVALID),
            expected_generation=initial.generation,
            requirements=requirements,
        )

        self.assertIsNone(result.selected_target_id)
        state = controller.store.load(controller.catalog_hash)
        self.assertEqual(
            set(state.pools["general"].targets),
            {"target-a", "target-b", "target-c"},
        )
        self.assertTrue(
            all(
                item.reason == FailureClass.AUTH_INVALID.value
                for item in state.pools["general"].targets.values()
            )
        )

    def test_auth_cooldown_uses_each_affected_pool_policy(self) -> None:
        payload = rotation_catalog(separate_credentials=True).to_dict()
        general = next(item for item in payload["pools"] if item["id"] == "general")
        secondary = dict(general)
        secondary["id"] = "secondary"
        secondary["targets"] = ["target-a"]
        secondary["cooldown"] = dict(general["cooldown"])
        secondary["cooldown"]["auth_seconds"] = 9
        payload["pools"].append(secondary)
        controller = self.controller(Catalog.from_dict(payload))
        requirements = self.requirements()
        initial = controller.select("general", requirements)

        controller.record_failure(
            "general",
            "target-a",
            normalized_failure(FailureClass.AUTH_INVALID),
            expected_generation=initial.generation,
            requirements=requirements,
        )

        state = controller.store.load(controller.catalog_hash)
        secondary_retry = datetime.fromisoformat(
            state.pools["secondary"]
            .targets["target-a"]
            .retry_at.replace("Z", "+00:00")
        )
        self.assertEqual(
            secondary_retry - self.clock.now_utc(),
            timedelta(seconds=9),
        )

    def test_two_failures_with_same_generation_rotate_only_once(self) -> None:
        controller = self.controller(rotation_catalog(target_count=3))
        requirements = self.requirements()
        initial = controller.select("general", requirements)
        barrier = threading.Barrier(2)
        results = []

        def fail() -> None:
            barrier.wait()
            results.append(
                controller.record_failure(
                    "general",
                    "target-a",
                    normalized_failure(FailureClass.PROVIDER_UNAVAILABLE),
                    expected_generation=initial.generation,
                    requirements=requirements,
                )
            )

        threads = [threading.Thread(target=fail), threading.Thread(target=fail)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(result.changed for result in results), 1)
        state = controller.store.load(controller.catalog_hash)
        self.assertEqual(state.generation, initial.generation + 1)
        self.assertEqual(state.pools["general"].active_target_id, "target-b")

    def test_monotonic_hold_prevents_wall_clock_jumps_from_early_reset(self) -> None:
        controller = self.controller(
            rotation_catalog(strategy="timed", duration_seconds=100)
        )
        requirements = self.requirements()
        initial = controller.select("general", requirements)
        switched = controller.record_failure(
            "general",
            "target-a",
            normalized_failure(FailureClass.PROVIDER_UNAVAILABLE),
            expected_generation=initial.generation,
            requirements=requirements,
        )
        self.assertEqual(switched.selected_target_id, "target-b")

        self.clock.advance(wall_seconds=10_000, monotonic_seconds=10)
        self.assertEqual(
            controller.select("general", requirements).selected_target_id,
            "target-b",
        )
        self.clock.advance(wall_seconds=-20_000, monotonic_seconds=80)
        self.assertEqual(
            controller.select("general", requirements).selected_target_id,
            "target-b",
        )
        self.clock.advance(wall_seconds=20_000, monotonic_seconds=11)
        self.assertEqual(
            controller.select("general", requirements).selected_target_id,
            "target-a",
        )

    def test_manual_rotate_moves_to_next_healthy_target(self) -> None:
        controller = self.controller(rotation_catalog(target_count=3))
        requirements = self.requirements()
        initial = controller.select("general", requirements)

        rotated = controller.rotate_pool(
            "general",
            expected_generation=initial.generation,
            requirements=requirements,
        )

        self.assertTrue(rotated.changed)
        self.assertEqual(rotated.selected_target_id, "target-b")


if __name__ == "__main__":
    unittest.main()
