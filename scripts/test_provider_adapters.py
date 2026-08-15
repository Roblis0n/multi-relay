#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay.bridge import (  # noqa: E402
    BridgeError,
    ChatStreamTranslator,
    _BridgeServer,
    _chat_completions_url,
    _installed_catalog_path,
    _open_reasoning,
    _seal_legacy_reasoning,
    _seal_reasoning,
    build_chat_request,
    main as bridge_main,
)
from multi_relay.catalog import Catalog, ProviderSpec, default_catalog  # noqa: E402


def provider(
    provider_id: str,
    protocol: str,
    base_url: str,
    *,
    auth: str = "vault",
) -> ProviderSpec:
    return ProviderSpec.from_dict(
        {
            "id": provider_id,
            "name": provider_id.title(),
            "protocol": protocol,
            "base_url": base_url,
            "auth": auth,
            "capabilities": ["text", "tools"],
            "context_window": 128000,
            "enabled": True,
        }
    )


def body(model: str) -> dict[str, object]:
    return {
        "model": model,
        "input": "Reply READY",
        "tools": [],
        "reasoning": {"effort": "max"},
    }


class AdapterRequestTests(unittest.TestCase):
    def test_full_chat_endpoint_is_not_appended_twice(self) -> None:
        generic = provider(
            "vendor",
            "chat-completions-compatible",
            "https://chat.example.test/v1/chat/completions",
        )

        self.assertEqual(
            _chat_completions_url(generic),
            "https://chat.example.test/v1/chat/completions",
        )

    def test_installed_catalog_path_honors_explicit_home_and_legacy_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            legacy = home / "codex-deepseek-relay" / "catalog.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("{}", encoding="utf-8")

            selected = _installed_catalog_path(home)

        self.assertEqual(selected, legacy)

    def test_invalid_explicit_catalog_exits_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.json"
            catalog_path.write_text("not json", encoding="utf-8")

            code = bridge_main(["--serve", "--catalog", str(catalog_path)])

        self.assertEqual(code, 2)

    def test_generic_chat_omits_deepseek_only_request_fields(self) -> None:
        generic = provider(
            "vendor",
            "chat-completions-compatible",
            "https://chat.example.test/v1",
        )

        translated = build_chat_request(
            body("vendor-model"),
            provider=generic,
            allowed_models={"vendor-model"},
            reasoning_secret="provider-token",
        )

        self.assertEqual(translated.payload["model"], "vendor-model")
        self.assertNotIn("thinking", translated.payload)
        self.assertNotIn("reasoning_effort", translated.payload)

    def test_deepseek_chat_keeps_thinking_and_reasoning_effort(self) -> None:
        deepseek = provider(
            "deepseek",
            "deepseek-chat",
            "https://api.deepseek.com/v1",
        )

        translated = build_chat_request(
            body("deepseek-v4-pro"),
            provider=deepseek,
            allowed_models={"deepseek-v4-pro"},
            reasoning_secret="sk-test",
        )

        self.assertEqual(translated.payload["thinking"], {"type": "enabled"})
        self.assertEqual(translated.payload["reasoning_effort"], "max")

    def test_model_must_belong_to_the_selected_provider_route(self) -> None:
        generic = provider(
            "vendor",
            "chat-completions-compatible",
            "https://chat.example.test/v1",
        )

        with self.assertRaises(BridgeError) as raised:
            build_chat_request(
                body("other-model"),
                provider=generic,
                allowed_models={"vendor-model"},
            )

        self.assertEqual(raised.exception.code, "unsupported_model")

    def test_reasoning_seals_are_provider_domain_separated(self) -> None:
        sealed = _seal_reasoning("private reasoning", "same-token", "provider-a")

        self.assertEqual(
            _open_reasoning(sealed, "same-token", "provider-a"),
            "private reasoning",
        )
        with self.assertRaises(BridgeError) as raised:
            _open_reasoning(sealed, "same-token", "provider-b")
        self.assertEqual(raised.exception.code, "invalid_reasoning")

    def test_legacy_deepseek_reasoning_remains_readable_only_by_deepseek(self) -> None:
        sealed = _seal_legacy_reasoning("legacy reasoning", "sk-test")

        self.assertEqual(_open_reasoning(sealed, "sk-test", "deepseek"), "legacy reasoning")
        with self.assertRaises(BridgeError):
            _open_reasoning(sealed, "sk-test", "vendor")
        self.assertEqual(
            _open_reasoning(
                sealed,
                "sk-test",
                "renamed-deepseek",
                allow_legacy=True,
            ),
            "legacy reasoning",
        )

    def test_generic_stream_never_emits_a_reasoning_item(self) -> None:
        translator = ChatStreamTranslator(
            {},
            reasoning_secret="provider-token",
            provider_id="vendor",
            preserve_reasoning=False,
        )
        translator.start("resp_test")
        translator.feed(
            {
                "choices": [
                    {"delta": {"reasoning_content": "must not leak", "content": "ok"}}
                ]
            }
        )

        events = translator.finish()

        self.assertFalse(
            any(event.get("item", {}).get("type") == "reasoning" for event in events)
        )

    def test_growing_prefix_tool_names_are_not_duplicated(self) -> None:
        generic = provider(
            "vendor",
            "chat-completions-compatible",
            "https://chat.example.test/v1",
        )
        request_body = body("vendor-model")
        request_body["tools"] = [
            {
                "type": "function",
                "name": "get_weather",
                "description": "Get weather.",
                "parameters": {"type": "object"},
            }
        ]
        translated = build_chat_request(
            request_body,
            provider=generic,
            allowed_models={"vendor-model"},
        )
        translator = ChatStreamTranslator(
            translated.tools,
            provider_id="vendor",
            preserve_reasoning=False,
        )
        translator.start("resp_test")
        for name in ("get_", "get_weather", "get_weather"):
            translator.feed(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": name, "arguments": ""},
                                    }
                                ]
                            }
                        }
                    ]
                }
            )

        events = translator.finish()
        completed = [
            event["item"]
            for event in events
            if event.get("type") == "response.output_item.done"
            and event.get("item", {}).get("type") == "function_call"
        ]
        self.assertEqual(completed[0]["name"], "get_weather")


class ProviderAddressedHttpTests(unittest.TestCase):
    def test_provider_redirect_is_blocked_without_forwarding_authorization(self) -> None:
        sink_headers: list[str | None] = []

        class SinkHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                sink_headers.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            def do_POST(self) -> None:
                sink_headers.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

        sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()

        class RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{sink.server_address[1]}/stolen",
                )
                # A malformed/truncated redirect body must not prevent the
                # bridge from returning its managed 502 response.
                self.send_header("Content-Length", "1")
                self.end_headers()

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        catalog = Catalog.from_dict(
            {
                "schema_version": 1,
                "concurrency": 1,
                "providers": [
                    {
                        "id": "vendor",
                        "name": "Vendor",
                        "protocol": "chat-completions-compatible",
                        "base_url": f"http://127.0.0.1:{redirect.server_address[1]}/v1",
                        "auth": "vault",
                        "capabilities": ["text"],
                        "context_window": 32000,
                        "enabled": True,
                    }
                ],
                "agents": [
                    {
                        "name": "vendor-agent",
                        "description": "Vendor agent",
                        "provider": "vendor",
                        "model": "vendor-model",
                        "reasoning_effort": None,
                        "context_window": 32000,
                        "capabilities": ["text"],
                        "trust": "standard",
                        "priority": 1,
                        "sandbox_mode": "read-only",
                        "mcp_servers": {},
                        "skills": [],
                        "developer_instructions": "Return a bounded result.",
                    }
                ],
            }
        )
        bridge = _BridgeServer(("127.0.0.1", 0), catalog=catalog)
        bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        bridge_thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{bridge.server_address[1]}/v1/providers/vendor/responses",
                data=json.dumps(body("vendor-model")).encode("utf-8"),
                headers={
                    "Authorization": "Bearer provider-token",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
        finally:
            bridge.shutdown()
            redirect.shutdown()
            sink.shutdown()
            bridge.server_close()
            redirect.server_close()
            sink.server_close()
            bridge_thread.join(timeout=2)
            redirect_thread.join(timeout=2)
            sink_thread.join(timeout=2)

        self.assertEqual(raised.exception.code, 502)
        self.assertEqual(sink_headers, [])

    def test_unreadable_redirect_body_still_returns_a_managed_502(self) -> None:
        class UnreadableBody:
            def __init__(self) -> None:
                self.closed = False

            def read(self, amount: int = -1) -> bytes:
                raise OSError("truncated redirect body")

            def close(self) -> None:
                self.closed = True

        error_body = UnreadableBody()
        redirect_error = urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions",
            302,
            "redirect",
            {},
            error_body,
        )
        bridge = _BridgeServer(("127.0.0.1", 0), catalog=default_catalog("hybrid"))
        bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        bridge_thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{bridge.server_address[1]}/v1/providers/deepseek/responses",
                data=json.dumps(body("deepseek-v4-pro")).encode("utf-8"),
                headers={
                    "Authorization": "Bearer sk-test",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with mock.patch(
                "multi_relay.bridge._open_upstream",
                side_effect=redirect_error,
            ):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=5)
        finally:
            bridge.shutdown()
            bridge.server_close()
            bridge_thread.join(timeout=2)

        self.assertEqual(raised.exception.code, 502)
        self.assertTrue(error_body.closed)

    def test_provider_addressed_route_uses_its_upstream_and_generic_payload(self) -> None:
        received: dict[str, object] = {}

        class UpstreamHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                received["path"] = self.path
                received["authorization"] = self.headers.get("Authorization")
                received["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
                payload = (
                    'data: {"id":"chatcmpl-test","choices":[{"delta":{"content":"READY"}}]}\n\n'
                    "data: [DONE]\n\n"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        base_url = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
        catalog = Catalog.from_dict(
            {
                "schema_version": 1,
                "concurrency": 8,
                "providers": [
                    {
                        "id": "vendor",
                        "name": "Vendor",
                        "protocol": "chat-completions-compatible",
                        "base_url": base_url,
                        "auth": "vault",
                        "capabilities": ["text", "tools"],
                        "context_window": 128000,
                        "enabled": True,
                    }
                ],
                "agents": [
                    {
                        "name": "vendor-worker",
                        "description": "Vendor worker",
                        "provider": "vendor",
                        "model": "vendor-model",
                        "reasoning_effort": None,
                        "context_window": 128000,
                        "capabilities": ["text", "tools"],
                        "trust": "standard",
                        "priority": 1,
                        "sandbox_mode": "workspace-write",
                        "mcp_servers": {},
                        "skills": [],
                        "developer_instructions": "Work on the bounded task.",
                    }
                ],
            }
        )
        bridge = _BridgeServer(("127.0.0.1", 0), catalog=catalog)
        bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        bridge_thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{bridge.server_address[1]}/v1/providers/vendor/responses",
                data=json.dumps(body("vendor-model")).encode("utf-8"),
                headers={
                    "Authorization": "Bearer provider-token",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                response_body = response.read().decode("utf-8")
        finally:
            bridge.shutdown()
            upstream.shutdown()
            bridge.server_close()
            upstream.server_close()

        self.assertEqual(received["path"], "/v1/chat/completions")
        self.assertEqual(received["authorization"], "Bearer provider-token")
        self.assertNotIn("thinking", received["body"])
        self.assertIn("READY", response_body)


if __name__ == "__main__":
    unittest.main()
