#!/usr/bin/env python3

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    HttpAttemptExecutor,
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


class ScriptedHTTPServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), ScriptedHTTPHandler)
        self.scripts: dict[str, list[dict[str, object]]] = {}
        self.requests: list[dict[str, object]] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class ScriptedHTTPHandler(BaseHTTPRequestHandler):
    server: ScriptedHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        raw_request = self.rfile.read(length)
        try:
            payload = json.loads(raw_request.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        self.server.requests.append(
            {
                "path": self.path,
                "headers": {key.casefold(): value for key, value in self.headers.items()},
                "payload": payload,
            }
        )
        actions = self.server.scripts.get(self.path, [])
        if not actions:
            action: dict[str, object] = {
                "status": 500,
                "payload": {"error": {"type": "fixture_exhausted"}},
            }
        else:
            action = actions.pop(0)
        delay = action.get("delay", 0)
        if isinstance(delay, (int, float)) and delay > 0:
            time.sleep(float(delay))
        status = action.get("status", 200)
        assert isinstance(status, int)
        headers = action.get("headers", {})
        assert isinstance(headers, dict)
        raw = action.get("raw")
        if raw is None:
            raw = json.dumps(action.get("payload", {}), separators=(",", ":")).encode()
        elif isinstance(raw, str):
            raw = raw.encode()
        assert isinstance(raw, bytes)
        content_type = str(action.get("content_type", "application/json"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in headers.items():
            self.send_header(str(key), str(value))
        if action.get("disconnect"):
            self.end_headers()
            prefix = action.get("prefix", raw)
            if isinstance(prefix, str):
                prefix = prefix.encode()
            if isinstance(prefix, bytes):
                self.wfile.write(prefix)
                self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def http_gateway_catalog(
    base_url: str,
    *,
    order: tuple[str, ...],
    strategy: str = "sticky",
    duration_seconds: int | None = None,
) -> Catalog:
    payload = default_catalog().to_dict()
    native_provider = next(item for item in payload["providers"] if item["id"] == "codex")
    native_target = next(item for item in payload["targets"] if item["id"] == "codex-native")
    payload["providers"] = [
        {
            "id": "chat",
            "name": "Chat fixture",
            "protocol": "deepseek-chat",
            "base_url": f"{base_url}/chat",
            "auth_mode": "vault",
            "capabilities": ["text", "tool_calling"],
            "models_endpoint": "/models",
            "enabled": True,
        },
        {
            "id": "anthropic",
            "name": "Messages fixture",
            "protocol": "anthropic-messages",
            "base_url": f"{base_url}/anthropic",
            "auth_mode": "vault",
            "capabilities": ["text", "tool_calling"],
            "models_endpoint": "/models",
            "enabled": True,
        },
        {
            "id": "responses",
            "name": "Responses fixture",
            "protocol": "responses-compatible",
            "base_url": f"{base_url}/openai",
            "auth_mode": "vault",
            "capabilities": ["text", "vision", "tool_calling"],
            "models_endpoint": "/models",
            "enabled": True,
        },
        native_provider,
    ]
    payload["credentials"] = [
        {
            "id": f"{provider}-fixture",
            "provider_id": provider,
            "vault_target": f"multi-relay/{provider}/fixture",
            "enabled": True,
            "created_at": "2026-08-16T00:00:00Z",
            "label": f"{provider.title()} fixture",
        }
        for provider in ("chat", "anthropic", "responses")
    ]
    capabilities = {
        "chat-a": ["text", "tool_calling"],
        "anthropic-b": ["text", "tool_calling"],
        "responses-c": ["text", "vision", "tool_calling"],
    }
    providers = {
        "chat-a": ("chat", "chat-fixture", "chat-model"),
        "anthropic-b": ("anthropic", "anthropic-fixture", "messages-model"),
        "responses-c": ("responses", "responses-fixture", "responses-model"),
    }
    targets = []
    for target_id, (provider_id, credential_id, model) in providers.items():
        targets.append(
            {
                "id": target_id,
                "provider_id": provider_id,
                "protocol": None,
                "model": model,
                "credential_id": credential_id,
                "capabilities": capabilities[target_id],
                "context_window": 200000,
                "max_output_tokens": 8192,
                "reasoning_efforts": [],
                "trust": "standard",
                "host_compatibility": ["codex", "claude-code"],
                "enabled": True,
                "metadata": {},
            }
        )
    payload["targets"] = [*targets, native_target]
    general = next(item for item in payload["pools"] if item["id"] == "general")
    general.update(
        {
            "targets": list(order),
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


class LocalProtocolUpstreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.server = ScriptedHTTPServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def app(
        self,
        order: tuple[str, ...],
        *,
        strategy: str = "sticky",
        duration_seconds: int | None = None,
    ) -> GatewayApplication:
        catalog = http_gateway_catalog(
            self.server.base_url,
            order=order,
            strategy=strategy,
            duration_seconds=duration_seconds,
        )
        rotation = RotationController(
            catalog,
            RuntimeStateStore(self.root / "runtime.json"),
            clock=FakeClock(),
            random_source=FixedRandom(),
            credential_available=lambda reference: reference.enabled,
        )
        return GatewayApplication(
            catalog,
            rotation=rotation,
            credential_reader=lambda reference, protocol: (
                f"fixture-{reference.id}-{protocol}"
            ),
            attempt_executor=HttpAttemptExecutor(catalog, timeout=2),
            request_token="local-session-token",
            shutdown_token="shutdown-only-token",
            sleep=lambda seconds: None,
        )

    @staticmethod
    def chat_success(text: str = "chat ok") -> dict[str, object]:
        return {
            "payload": {
                "id": "chat-result",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            }
        }

    @staticmethod
    def messages_success(text: str = "messages ok") -> dict[str, object]:
        return {
            "payload": {
                "id": "messages-result",
                "type": "message",
                "role": "assistant",
                "model": "messages-model",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            }
        }

    @staticmethod
    def responses_success(text: str = "responses ok") -> dict[str, object]:
        return {
            "payload": {
                "id": "responses-result",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            }
        }

    @staticmethod
    def responses_request(**extra: object) -> dict[str, object]:
        return {
            "model": "multi-relay-general",
            "input": "hello",
            "max_output_tokens": 64,
            "stream": False,
            **extra,
        }

    @staticmethod
    def messages_request(**extra: object) -> dict[str, object]:
        return {
            "model": "multi-relay-general",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 64,
            "stream": False,
            **extra,
        }

    @staticmethod
    def execute(app: GatewayApplication, surface: str, payload: dict[str, object]):
        headers = {"authorization": "Bearer local-session-token"}
        if surface == "messages":
            headers["anthropic-version"] = "2023-06-01"
        execution = app.prepare_execution(
            surface,
            payload,
            headers=headers,
            request_id=f"fixture-{surface}",
        )
        events = list(execution)
        return execution, events

    def paths(self) -> list[str]:
        return [str(item["path"]) for item in self.server.requests]

    def test_codex_responses_to_chat_completions(self) -> None:
        self.server.scripts["/chat/chat/completions"] = [self.chat_success()]
        app = self.app(("chat-a", "anthropic-b"))

        _, events = self.execute(app, "responses", self.responses_request())

        self.assertEqual(self.paths(), ["/chat/chat/completions"])
        self.assertEqual(events[-1].kind, EventKind.RESPONSE_COMPLETED)
        request = self.server.requests[0]
        self.assertEqual(request["headers"]["authorization"], "Bearer fixture-chat-fixture-deepseek-chat")
        self.assertEqual(request["payload"]["model"], "chat-model")

    def test_codex_quota_rotates_to_anthropic_and_sticks_without_secret_leak(self) -> None:
        self.server.scripts["/chat/chat/completions"] = [
            {"status": 402, "payload": {"error": {"code": "insufficient_quota"}}}
        ]
        self.server.scripts["/anthropic/messages"] = [
            self.messages_success("first"),
            self.messages_success("second"),
        ]
        app = self.app(("chat-a", "anthropic-b"))

        first, _ = self.execute(app, "responses", self.responses_request())
        second, _ = self.execute(app, "responses", self.responses_request())

        self.assertEqual(
            self.paths(),
            ["/chat/chat/completions", "/anthropic/messages", "/anthropic/messages"],
        )
        self.assertEqual(
            self.server.requests[1]["headers"]["x-api-key"],
            "fixture-anthropic-fixture-anthropic-messages",
        )
        serialized = json.dumps(
            {
                "first": first.attempts,
                "second": second.attempts,
                "state": json.loads((self.root / "runtime.json").read_text(encoding="utf-8")),
            }
        )
        self.assertNotIn("fixture-chat-fixture-deepseek-chat", serialized)
        self.assertNotIn("fixture-anthropic-fixture-anthropic-messages", serialized)

    def test_claude_messages_to_anthropic(self) -> None:
        self.server.scripts["/anthropic/messages"] = [self.messages_success()]
        app = self.app(("anthropic-b", "chat-a"))

        _, events = self.execute(app, "messages", self.messages_request())

        self.assertEqual(self.paths(), ["/anthropic/messages"])
        self.assertEqual(events[-1].kind, EventKind.RESPONSE_COMPLETED)

    def test_claude_rate_limit_rotates_to_responses(self) -> None:
        self.server.scripts["/anthropic/messages"] = [
            {
                "status": 429,
                "headers": {"Retry-After": "30"},
                "payload": {"type": "error", "error": {"type": "rate_limit_error"}},
            }
        ]
        self.server.scripts["/openai/responses"] = [self.responses_success()]
        app = self.app(("anthropic-b", "responses-c"))

        _, events = self.execute(app, "messages", self.messages_request())

        self.assertEqual(self.paths(), ["/anthropic/messages", "/openai/responses"])
        self.assertEqual(events[-1].kind, EventKind.RESPONSE_COMPLETED)

    def test_vision_filters_text_only_target(self) -> None:
        self.server.scripts["/openai/responses"] = [self.responses_success()]
        app = self.app(("chat-a", "responses-c"))
        request = self.responses_request(
            input=[
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "inspect"},
                        {
                            "type": "input_image",
                            "image_url": "https://fixture.example/image.png",
                        },
                    ],
                }
            ]
        )

        self.execute(app, "responses", request)

        self.assertEqual(self.paths(), ["/openai/responses"])

    def test_tool_request_is_translated_for_chat_target(self) -> None:
        self.server.scripts["/chat/chat/completions"] = [
            {
                "payload": {
                    "id": "chat-tool-result",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": "{\"q\":\"relay\"}",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                }
            }
        ]
        app = self.app(("chat-a",))
        request = self.responses_request(
            tools=[
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Look up a value.",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                    },
                }
            ]
        )

        _, events = self.execute(app, "responses", request)

        upstream = self.server.requests[0]["payload"]
        self.assertEqual(upstream["tools"][0]["type"], "function")
        self.assertTrue(any(event.kind is EventKind.TOOL_CALL_COMPLETED for event in events))


if __name__ == "__main__":
    unittest.main()
