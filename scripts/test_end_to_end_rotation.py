#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay.canonical import (  # noqa: E402
    CanonicalEvent,
    CanonicalEventSequence,
    EventKind,
)
from multi_relay.catalog import Catalog, default_catalog  # noqa: E402
from multi_relay.gateway import (  # noqa: E402
    AttemptResponse,
    CancellationToken,
    GatewayApplication,
    GatewayCancelled,
    GatewayExhausted,
)
from multi_relay.protocols.base import ProviderErrorMetadata  # noqa: E402
from multi_relay.rotation import RotationController  # noqa: E402
from multi_relay.state import RuntimeStateStore  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.wall = datetime(2026, 8, 16, tzinfo=UTC)
        self.tick = 1000.0

    def now_utc(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.tick

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.tick += seconds


class FixedRandom:
    def random(self) -> float:
        return 0.5


def gateway_catalog(
    *,
    strategy: str = "sticky",
    duration_seconds: int | None = None,
) -> Catalog:
    payload = default_catalog().to_dict()
    original = next(
        item for item in payload["targets"] if item["id"] == "deepseek-primary"
    )
    assert isinstance(original, dict)
    target_a = dict(original)
    target_a.update({"id": "target-a", "model": "model-a", "metadata": {}})
    target_b = dict(original)
    target_b.update(
        {
            "id": "target-b",
            "model": "model-b",
            "credential_id": "backup",
            "metadata": {},
        }
    )
    native = next(item for item in payload["targets"] if item["id"] == "codex-native")
    payload["targets"] = [target_a, target_b, native]
    payload["credentials"].append(
        {
            "id": "backup",
            "provider_id": "deepseek",
            "vault_target": "multi-relay/deepseek/backup",
            "enabled": True,
            "created_at": "2026-08-16T00:00:00Z",
            "label": "Backup",
        }
    )
    general = next(item for item in payload["pools"] if item["id"] == "general")
    general.update(
        {
            "targets": ["target-a", "target-b"],
            "strategy": strategy,
            "duration_seconds": duration_seconds,
            "max_rate_limit_wait_seconds": 3,
            "cooldown": {
                "quota_seconds": 30,
                "rate_limit_seconds": 10,
                "auth_seconds": 60,
                "provider_seconds": 5,
            },
        }
    )
    payload["hosts"]["claude-code"]["enabled"] = True
    return Catalog.from_dict(payload)


def success_events(response_id: str, text: str = "ok") -> tuple[CanonicalEvent, ...]:
    sequence = CanonicalEventSequence(response_id)
    return (
        sequence.emit(EventKind.RESPONSE_STARTED),
        sequence.emit(
            EventKind.CONTENT_BLOCK_STARTED,
            block_index=0,
            payload={"kind": "text"},
        ),
        sequence.emit(
            EventKind.TEXT_DELTA,
            block_index=0,
            payload={"delta": text},
        ),
        sequence.emit(EventKind.CONTENT_BLOCK_COMPLETED, block_index=0),
        sequence.emit(EventKind.RESPONSE_COMPLETED, payload={"finish_reason": "stop"}),
    )


def interrupted_events(
    response_id: str,
    *,
    tool: bool = False,
) -> Iterable[CanonicalEvent]:
    sequence = CanonicalEventSequence(response_id)
    yield sequence.emit(EventKind.RESPONSE_STARTED)
    yield sequence.emit(
        EventKind.CONTENT_BLOCK_STARTED,
        block_index=0,
        payload={"kind": "tool_call" if tool else "text"},
    )
    if tool:
        yield sequence.emit(
            EventKind.TOOL_CALL_STARTED,
            block_index=0,
            tool_call_id="call-1",
            payload={"name": "lookup"},
        )
    else:
        yield sequence.emit(
            EventKind.TEXT_DELTA,
            block_index=0,
            payload={"delta": "partial"},
        )
    raise ConnectionError("secret upstream text must not escape")


class ScriptedExecutor:
    def __init__(self, scripts: dict[str, list[object]]) -> None:
        self.scripts = scripts
        self.calls: list[str] = []
        self.secrets: list[str | None] = []

    def __call__(self, target, request, credential, cancellation):
        del request, cancellation
        self.calls.append(target.id)
        self.secrets.append(credential)
        selected = self.scripts[target.id].pop(0)
        if isinstance(selected, BaseException):
            raise selected
        if callable(selected):
            return selected()
        return selected


class GatewayRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = FakeClock()
        self.sleeps: list[float] = []

    def app(
        self,
        scripts: dict[str, list[object]],
        *,
        catalog: Catalog | None = None,
    ) -> tuple[GatewayApplication, ScriptedExecutor]:
        selected_catalog = catalog or gateway_catalog()
        executor = ScriptedExecutor(scripts)
        rotation = RotationController(
            selected_catalog,
            RuntimeStateStore(self.root / "runtime.json"),
            clock=self.clock,
            random_source=FixedRandom(),
            credential_available=lambda reference: reference.id in {"primary", "backup"},
        )
        app = GatewayApplication(
            selected_catalog,
            rotation=rotation,
            credential_reader=lambda reference, protocol: {
                "primary": "sk-primary-secret",
                "backup": "sk-backup-secret",
            }[reference.id],
            attempt_executor=executor,
            request_token="local-session-token",
            shutdown_token="shutdown-only-token",
            sleep=lambda seconds: self.sleeps.append(seconds),
        )
        return app, executor

    @staticmethod
    def request() -> dict[str, object]:
        return {
            "model": "multi-relay-general",
            "input": "hello",
            "stream": True,
        }

    def execute(self, app: GatewayApplication, *, cancellation=None):
        return app.prepare_execution(
            "responses",
            self.request(),
            headers={"authorization": "Bearer local-session-token"},
            request_id="req-test",
            cancellation=cancellation,
        )

    def test_quota_before_commit_fails_over_to_second_target(self) -> None:
        app, executor = self.app(
            {
                "target-a": [
                    AttemptResponse(
                        status=402,
                        headers={"content-type": "application/json"},
                        error_body=b'{"error":{"code":"insufficient_quota"}}',
                        provider_error=ProviderErrorMetadata(code="insufficient_quota"),
                    )
                ],
                "target-b": [AttemptResponse(events=success_events("resp-b"))],
            }
        )

        execution = self.execute(app)
        events = list(execution)

        self.assertEqual(executor.calls, ["target-a", "target-b"])
        self.assertEqual(events[-1].kind, EventKind.RESPONSE_COMPLETED)
        self.assertEqual(execution.lifecycle.status, "completed")

    def test_short_retry_after_waits_then_retries_same_target(self) -> None:
        app, executor = self.app(
            {
                "target-a": [
                    AttemptResponse(
                        status=429,
                        headers={
                            "content-type": "application/json",
                            "retry-after": "2",
                        },
                        error_body=b'{"error":{"type":"rate_limit_error"}}',
                    ),
                    AttemptResponse(events=success_events("resp-a")),
                ],
                "target-b": [],
            }
        )

        list(self.execute(app))

        self.assertEqual(executor.calls, ["target-a", "target-a"])
        self.assertEqual(self.sleeps, [2.0])

    def test_long_retry_after_rotates_without_waiting(self) -> None:
        app, executor = self.app(
            {
                "target-a": [
                    AttemptResponse(
                        status=429,
                        headers={
                            "content-type": "application/json",
                            "retry-after": "30",
                        },
                        error_body=b'{"error":{"type":"rate_limit_error"}}',
                    )
                ],
                "target-b": [AttemptResponse(events=success_events("resp-b"))],
            }
        )

        list(self.execute(app))

        self.assertEqual(executor.calls, ["target-a", "target-b"])
        self.assertEqual(self.sleeps, [])

    def test_text_or_tool_commit_prevents_failover_after_disconnect(self) -> None:
        for tool in (False, True):
            with self.subTest(tool=tool):
                app, executor = self.app(
                    {
                        "target-a": [
                            AttemptResponse(events=interrupted_events("resp-a", tool=tool))
                        ],
                        "target-b": [AttemptResponse(events=success_events("resp-b"))],
                    }
                )

                execution = self.execute(app)
                events = list(execution)

                self.assertEqual(executor.calls, ["target-a"])
                self.assertEqual(events[-1].kind, EventKind.ERROR)
                self.assertEqual(execution.lifecycle.status, "failed")
                self.assertTrue(execution.lifecycle.committed)

    def test_auth_failure_cools_credential_and_sticky_starts_next_request_on_b(self) -> None:
        app, executor = self.app(
            {
                "target-a": [
                    AttemptResponse(
                        status=401,
                        headers={"content-type": "application/json"},
                        error_body=b'{"error":{"type":"authentication_error"}}',
                    )
                ],
                "target-b": [
                    AttemptResponse(events=success_events("resp-b1")),
                    AttemptResponse(events=success_events("resp-b2")),
                ],
            }
        )

        list(self.execute(app))
        list(self.execute(app))

        self.assertEqual(executor.calls, ["target-a", "target-b", "target-b"])

    def test_timed_pool_reprobes_primary_after_hold_expires(self) -> None:
        catalog = gateway_catalog(strategy="timed", duration_seconds=4)
        app, executor = self.app(
            {
                "target-a": [
                    AttemptResponse(
                        status=503,
                        headers={"content-type": "application/json"},
                        error_body=b'{"error":{"type":"overloaded_error"}}',
                    ),
                    AttemptResponse(events=success_events("resp-a2")),
                ],
                "target-b": [
                    AttemptResponse(events=success_events("resp-b1")),
                    AttemptResponse(events=success_events("resp-b2")),
                ],
            },
            catalog=catalog,
        )

        list(self.execute(app))
        self.clock.advance(2)
        list(self.execute(app))
        self.clock.advance(4)
        list(self.execute(app))

        self.assertEqual(
            executor.calls,
            ["target-a", "target-b", "target-b", "target-a"],
        )

    def test_exhausted_summary_is_ordered_and_secret_free(self) -> None:
        app, executor = self.app(
            {
                "target-a": [ConnectionError("sk-primary-secret")],
                "target-b": [ConnectionError("sk-backup-secret")],
            }
        )

        with self.assertRaises(GatewayExhausted) as raised:
            list(self.execute(app))

        self.assertEqual(
            [item["target_id"] for item in raised.exception.attempts],
            ["target-a", "target-b"],
        )
        encoded = json.dumps(raised.exception.attempts)
        self.assertNotIn("sk-primary-secret", encoded)
        self.assertNotIn("sk-backup-secret", encoded)
        self.assertEqual(executor.calls, ["target-a", "target-b"])

    def test_cancellation_closes_upstream_without_poisoning_rotation(self) -> None:
        cancellation = CancellationToken()
        closed: list[bool] = []

        def cancelling_events():
            sequence = CanonicalEventSequence("resp-cancel")
            yield sequence.emit(EventKind.RESPONSE_STARTED)
            cancellation.cancel()
            raise ConnectionError("cancelled socket")

        app, executor = self.app(
            {
                "target-a": [
                    AttemptResponse(
                        events=cancelling_events(),
                        close=lambda: closed.append(True),
                    )
                ],
                "target-b": [],
            }
        )

        execution = self.execute(app, cancellation=cancellation)
        with self.assertRaises(GatewayCancelled):
            list(execution)

        self.assertEqual(executor.calls, ["target-a"])
        self.assertEqual(closed, [True])
        self.assertEqual(execution.lifecycle.status, "cancelled")
        state = app.rotation.store.load(app.rotation.catalog_hash)
        self.assertEqual(state.pools["general"].active_target_id, "target-a")
        self.assertEqual(dict(state.pools["general"].targets), {})


if __name__ == "__main__":
    unittest.main()
