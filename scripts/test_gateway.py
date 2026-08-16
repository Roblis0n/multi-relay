#!/usr/bin/env python3

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay.gateway import (  # noqa: E402
    MAX_REQUEST_BYTES,
    AttemptResponse,
    GatewayApplication,
    GatewayHTTPServer,
)
from multi_relay.rotation import RotationController  # noqa: E402
from multi_relay.state import RuntimeStateStore  # noqa: E402
from test_end_to_end_rotation import (  # noqa: E402
    FakeClock,
    FixedRandom,
    ScriptedExecutor,
    gateway_catalog,
    success_events,
)


class GatewayHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clock = FakeClock()
        self.catalog = gateway_catalog()
        self.executor = ScriptedExecutor(
            {
                "target-a": [AttemptResponse(events=success_events(f"resp-{index}")) for index in range(20)],
                "target-b": [],
            }
        )
        rotation = RotationController(
            self.catalog,
            RuntimeStateStore(self.root / "runtime.json"),
            clock=self.clock,
            random_source=FixedRandom(),
            credential_available=lambda reference: True,
        )
        self.app = GatewayApplication(
            self.catalog,
            rotation=rotation,
            credential_reader=lambda reference, protocol: "sk-fixture",
            attempt_executor=self.executor,
            request_token="request-token",
            shutdown_token="shutdown-token",
            token_expires_at=2000.0,
            time_source=lambda: self.clock.monotonic(),
        )
        self.server = GatewayHTTPServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close_server)

    def _close_server(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: object | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        selected_headers = dict(headers or {})
        if isinstance(body, bytes):
            raw = body
        elif body is None:
            raw = None
        else:
            raw = json.dumps(body).encode("utf-8")
            selected_headers.setdefault("content-type", "application/json")
        connection.request(method, path, body=raw, headers=selected_headers)
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    @staticmethod
    def auth() -> dict[str, str]:
        return {"authorization": "Bearer request-token"}

    def test_health_models_responses_messages_and_legacy_route(self) -> None:
        status, _, raw = self.request("GET", "/health")
        health = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(health["service"], "multi-relay-gateway")
        self.assertIn("responses", health["protocols"])
        self.assertIn("messages", health["protocols"])

        status, _, raw = self.request("GET", "/v1/models", headers=self.auth())
        aliases = {item["id"] for item in json.loads(raw)["data"]}
        self.assertEqual(status, 200)
        self.assertIn("multi-relay-default", aliases)
        self.assertIn("multi-relay-general", aliases)

        status, headers, raw = self.request(
            "POST",
            "/v1/responses",
            body={"model": "multi-relay-general", "input": "hello", "stream": True},
            headers=self.auth(),
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        self.assertIn(b"response.completed", raw)

        status, headers, raw = self.request(
            "POST",
            "/v1/messages",
            body={
                "model": "multi-relay-general",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
            headers={
                "x-api-key": "request-token",
                "anthropic-version": "2023-06-01",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        self.assertIn(b"message_stop", raw)

        status, _, raw = self.request(
            "POST",
            "/v1/providers/deepseek/responses",
            body={"model": "model-a", "input": "hello", "stream": True},
            headers=self.auth(),
        )
        self.assertEqual(status, 200, raw)

    def test_missing_wrong_and_expired_request_tokens_return_401(self) -> None:
        for headers in ({}, {"authorization": "Bearer wrong-token"}):
            with self.subTest(headers=headers):
                status, _, _ = self.request("GET", "/v1/models", headers=headers)
                self.assertEqual(status, 401)
        self.clock.tick = 2001.0
        status, _, _ = self.request("GET", "/v1/models", headers=self.auth())
        self.assertEqual(status, 401)

    def test_proxy_forms_content_type_and_oversized_body_are_rejected(self) -> None:
        cases = (
            (
                "POST",
                "http://example.test/v1/responses",
                b"{}",
                {**self.auth(), "content-type": "application/json"},
                400,
            ),
            (
                "POST",
                "/v1/responses",
                b"{}",
                {**self.auth(), "content-type": "text/plain"},
                415,
            ),
            (
                "POST",
                "/v1/responses",
                b"{}",
                {
                    **self.auth(),
                    "content-type": "application/json",
                    "content-length": str(MAX_REQUEST_BYTES + 1),
                },
                413,
            ),
        )
        for method, path, body, headers, expected in cases:
            with self.subTest(path=path, expected=expected):
                status, _, _ = self.request(method, path, body=body, headers=headers)
                self.assertEqual(status, expected)

        status, _, _ = self.request(
            "GET",
            "/health",
            headers={"host": "remote.example.test"},
        )
        self.assertEqual(status, 400)

    def test_shutdown_token_is_independent_from_request_token(self) -> None:
        status, _, _ = self.request(
            "POST",
            "/_shutdown",
            body={},
            headers={"x-multi-relay-shutdown-token": "request-token"},
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST",
            "/_shutdown",
            body={},
            headers={"x-multi-relay-shutdown-token": "shutdown-token"},
        )
        self.assertEqual(status, 200)
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def test_slow_request_does_not_block_health(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def delayed():
            entered.set()
            release.wait(2)
            return AttemptResponse(events=success_events("resp-delayed"))

        self.executor.scripts["target-a"].insert(0, delayed)
        result: list[int] = []

        def invoke() -> None:
            status, _, _ = self.request(
                "POST",
                "/v1/responses",
                body={"model": "multi-relay-general", "input": "hello", "stream": True},
                headers=self.auth(),
            )
            result.append(status)

        worker = threading.Thread(target=invoke)
        worker.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        status, _, _ = self.request("GET", "/health")
        elapsed = time.monotonic() - started
        release.set()
        worker.join(timeout=2)

        self.assertEqual(status, 200)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(result, [200])


if __name__ == "__main__":
    unittest.main()
