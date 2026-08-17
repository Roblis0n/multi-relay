#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay.bridge import (  # noqa: E402
    BRIDGE_SERVICE,
    BRIDGE_VERSION,
    LEGACY_BRIDGE_SERVICE,
    BridgeError,
    ChatStreamTranslator,
    _BridgeServer,
    _completion_as_chunk,
    _explicit_agent_handoffs,
    _stop_bridge_health,
    build_chat_request,
)
from multi_relay.catalog import ProviderSpec  # noqa: E402


def _write_rollout(path: Path, payloads: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps({"payload": payload}, ensure_ascii=False) + "\n"
            for payload in payloads
        ),
        encoding="utf-8",
    )


def _append_rollout(path: Path, payloads: list[dict[str, object]]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for payload in payloads:
            handle.write(json.dumps({"payload": payload}, ensure_ascii=False) + "\n")


def _handoff_text(target: str, message: str) -> str:
    return (
        f"[DeepSeek task: {target}]\n"
        f"{message}\n"
        f"[/DeepSeek task: {target}]"
    )


def _add_agent_task(
    connection: sqlite3.Connection,
    home: Path,
    *,
    parent_id: str,
    child_id: str,
    recipient: str,
    encrypted_content: str,
    task_message: str,
    created_at_ms: int,
    handoff_message: str | None = None,
    model_provider: str = "deepseek",
) -> None:
    parent_rollout = home / "sessions" / f"parent-{parent_id}.jsonl"
    child_rollout = home / "sessions" / f"child-{child_id}.jsonl"
    call_id = f"call-{child_id}"
    parent_payloads: list[dict[str, object]] = []
    if handoff_message is not None:
        parent_payloads.append({
            "type": "message",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": _handoff_text(recipient.rsplit("/", 1)[-1], handoff_message),
            }],
        })
    parent_payloads.extend([
        {
            "type": "function_call",
            "name": "spawn_agent",
            "call_id": call_id,
            "arguments": json.dumps({
                "task_name": recipient.rsplit("/", 1)[-1],
                "message": task_message,
                "agent_type": "explorer",
                "fork_turns": "none",
            }),
        },
        {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps({
                "task_name": recipient,
                "nickname": "Fixture",
            }),
        },
    ])
    _write_rollout(parent_rollout, parent_payloads)
    _write_rollout(child_rollout, [{
        "type": "agent_message",
        "author": "/root",
        "recipient": recipient,
        "content": [
            {
                "type": "input_text",
                "text": (
                    "Message Type: NEW_TASK\n"
                    f"Task name: {recipient}\n"
                    "Sender: /root\n"
                    "Payload:\n"
                ),
            },
            {
                "type": "encrypted_content",
                "encrypted_content": encrypted_content,
            },
        ],
    }])
    source = json.dumps({
        "subagent": {
            "thread_spawn": {
                "parent_thread_id": parent_id,
                "depth": 1,
                "agent_path": recipient,
                "agent_nickname": "Fixture",
                "agent_role": "explorer",
            }
        }
    })
    connection.executemany(
        "INSERT INTO threads "
        "(id, rollout_path, source, model_provider, agent_path, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                parent_id,
                str(parent_rollout),
                "vscode",
                "openai",
                None,
                created_at_ms - 1,
            ),
            (
                child_id,
                str(child_rollout),
                source,
                model_provider,
                recipient,
                created_at_ms,
            ),
        ],
    )


class BridgeRequestTests(unittest.TestCase):
    def test_new_and_legacy_services_use_their_matching_shutdown_headers(self) -> None:
        captured: list[urllib.request.Request] = []

        class Response:
            status = 200

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        class Opener:
            def open(self, request: urllib.request.Request, timeout: float) -> Response:
                captured.append(request)
                return Response()

        with mock.patch(
            "multi_relay.bridge.urllib.request.build_opener",
            return_value=Opener(),
        ):
            self.assertTrue(_stop_bridge_health({"service": BRIDGE_SERVICE, "pid": 101}))
            self.assertTrue(
                _stop_bridge_health({"service": LEGACY_BRIDGE_SERVICE, "pid": 202})
            )

        self.assertEqual(
            captured[0].get_header("X-multi-relay-bridge-pid"),
            "101",
        )
        self.assertEqual(
            captured[1].get_header("X-codex-deepseek-bridge-pid"),
            "202",
        )

    def test_protected_message_lookup_is_scoped_to_the_routed_provider(self) -> None:
        vendor = ProviderSpec.from_dict(
            {
                "id": "vendor",
                "name": "Vendor",
                "protocol": "chat-completions-compatible",
                "base_url": "https://chat.example.test/v1",
                "auth": "vault",
                "capabilities": ["text", "tools"],
                "context_window": 128000,
                "enabled": True,
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            database = home / "state_5.sqlite"
            ciphertext = "gAAAAABvendor-opaque-payload=="
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE threads ("
                    "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, "
                    "model_provider TEXT NOT NULL, agent_path TEXT, created_at_ms INTEGER)"
                )
                _add_agent_task(
                    connection,
                    home,
                    parent_id="parent-vendor",
                    child_id="child-vendor",
                    recipient="/root/vendor-worker",
                    encrypted_content=ciphertext,
                    task_message="Inspect vendor module.",
                    created_at_ms=100,
                    model_provider="vendor",
                )
                connection.commit()
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                translated = build_chat_request(
                    {
                        "model": "vendor-model",
                        "input": [
                            {
                                "type": "agent_message",
                                "author": "/root",
                                "recipient": "/root/vendor-worker",
                                "content": [
                                    {
                                        "type": "encrypted_content",
                                        "encrypted_content": ciphertext,
                                    }
                                ],
                            }
                        ],
                    },
                    provider=vendor,
                    allowed_models={"vendor-model"},
                )

        self.assertIn("Inspect vendor module.", translated.payload["messages"][0]["content"])
    def test_bridge_accepts_new_and_legacy_protected_handoff_markers(self) -> None:
        for label in ("Relay", "DeepSeek"):
            with self.subTest(label=label):
                text = (
                    f"[{label} task: worker]\n"
                    "Inspect module A.\n"
                    f"[/{label} task: worker]"
                )
                self.assertEqual(
                    _explicit_agent_handoffs(text),
                    [("worker", "Inspect module A.")],
                )

    def test_handoff_release_uses_a_new_bridge_process_version(self) -> None:
        self.assertGreaterEqual(BRIDGE_VERSION, 3)

    def test_bridge_rejects_non_https_non_loopback_upstream(self) -> None:
        server = None
        try:
            with self.assertRaises(BridgeError) as error:
                server = _BridgeServer(("127.0.0.1", 0), "file:///tmp/credential")
        finally:
            if server is not None:
                server.server_close()
        self.assertEqual(error.exception.code, "invalid_upstream_url")

    def test_responses_request_becomes_deepseek_chat_with_namespaced_tools(self) -> None:
        body = {
            "model": "deepseek-v4-pro",
            "instructions": "System contract.",
            "reasoning": {"effort": "xhigh"},
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Inspect it."}],
                },
                {
                    "type": "function_call",
                    "namespace": "agents",
                    "name": "spawn_agent",
                    "call_id": "call-old",
                    "arguments": '{"task_name":"scan","message":"scan"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-old",
                    "output": "done",
                },
            ],
            "tools": [
                {
                    "type": "namespace",
                    "name": "agents",
                    "description": "Native agents.",
                    "tools": [
                        {
                            "type": "function",
                            "name": "spawn_agent",
                            "description": "Spawn a child.",
                            "parameters": {
                                "type": "object",
                                "properties": {"agent_type": {"type": "string"}},
                            },
                        }
                    ],
                },
                {
                    "type": "custom",
                    "name": "apply_patch",
                    "description": "Apply a patch.",
                    "format": {"type": "grammar", "syntax": "lark"},
                },
            ],
        }

        translated = build_chat_request(body)

        self.assertEqual(translated.payload["model"], "deepseek-v4-pro")
        self.assertEqual(translated.payload["reasoning_effort"], "max")
        self.assertEqual(translated.payload["thinking"], {"type": "enabled"})
        self.assertIs(translated.payload["stream"], True)
        self.assertNotIn("tool_choice", translated.payload)
        self.assertEqual(translated.payload["messages"][0], {
            "role": "system",
            "content": "System contract.",
        })
        self.assertEqual(translated.payload["messages"][1]["role"], "user")
        assistant = translated.payload["messages"][2]
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "agents__spawn_agent")
        self.assertEqual(translated.payload["messages"][3], {
            "role": "tool",
            "tool_call_id": "call-old",
            "content": "done",
        })
        tool_names = [tool["function"]["name"] for tool in translated.payload["tools"]]
        self.assertEqual(tool_names, ["agents__spawn_agent", "apply_patch"])
        self.assertEqual(
            translated.tools["agents__spawn_agent"].namespace,
            "agents",
        )
        self.assertIs(translated.tools["apply_patch"].custom, True)
        custom_schema = translated.payload["tools"][1]["function"]["parameters"]
        self.assertEqual(custom_schema["required"], ["input"])

    def test_bridge_rejects_images_and_unknown_model(self) -> None:
        image_body = {
            "model": "deepseek-v4-pro",
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "image_url": "data:image/png;base64,AA=="}],
            }],
        }
        with self.assertRaises(BridgeError) as image_error:
            build_chat_request(image_body)
        with self.assertRaises(BridgeError) as model_error:
            build_chat_request({"model": "deepseek-v4-flash", "input": "hi"})
        with self.assertRaises(BridgeError) as compaction_error:
            build_chat_request({
                "model": "deepseek-v4-pro",
                "input": [{"type": "compaction", "encrypted_content": "opaque"}],
            })

        self.assertEqual(image_error.exception.code, "unsupported_media")
        self.assertEqual(model_error.exception.code, "unsupported_model")
        self.assertEqual(compaction_error.exception.code, "unsupported_compaction")

    def test_disabled_hosted_web_search_is_ignored_but_enabled_search_fails_closed(self) -> None:
        translated = build_chat_request({
            "model": "deepseek-v4-pro",
            "input": "hello",
            "tools": [{"type": "web_search", "external_web_access": False}],
        })

        self.assertNotIn("tools", translated.payload)
        with self.assertRaises(BridgeError) as enabled_error:
            build_chat_request({
                "model": "deepseek-v4-pro",
                "input": "hello",
                "tools": [{"type": "web_search", "external_web_access": True}],
            })
        self.assertEqual(enabled_error.exception.code, "unsupported_tool")

    def test_v2_agent_message_resolves_exact_parent_task_without_forwarding_ciphertext(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            database = home / "state_5.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE threads ("
                    "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, "
                    "model_provider TEXT NOT NULL, agent_path TEXT, created_at_ms INTEGER)"
                )
                _add_agent_task(
                    connection,
                    home,
                    parent_id="parent-current",
                    child_id="child-current",
                    recipient="/root/worker",
                    encrypted_content="gAAAAABcurrent-opaque-payload==",
                    task_message="Inspect module A.",
                    created_at_ms=100,
                )
                _add_agent_task(
                    connection,
                    home,
                    parent_id="parent-decoy",
                    child_id="child-decoy",
                    recipient="/root/worker",
                    encrypted_content="gAAAAABnewer-decoy-payload==",
                    task_message="This is the wrong newer task.",
                    created_at_ms=200,
                )
                connection.commit()

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                translated = build_chat_request({
                    "model": "deepseek-v4-pro",
                    "input": [{
                        "type": "agent_message",
                        "author": "/root",
                        "recipient": "/root/worker",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Message Type: NEW_TASK\n"
                                    "Task name: /root/worker\n"
                                    "Sender: /root\n"
                                    "Payload:\n"
                                ),
                            },
                            {
                                "type": "encrypted_content",
                                "encrypted_content": "gAAAAABcurrent-opaque-payload==",
                            },
                        ],
                    }],
                })

        content = translated.payload["messages"][0]["content"]
        self.assertIn("[Codex agent message: /root -> /root/worker]", content)
        self.assertIn("Inspect module A.", content)
        self.assertNotIn("gAAAAABcurrent-opaque-payload==", content)
        self.assertNotIn("This is the wrong newer task.", content)

    def test_v2_agent_message_fails_closed_when_parent_task_cannot_be_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"CODEX_HOME": temporary}):
                with self.assertRaises(BridgeError) as error:
                    build_chat_request({
                        "model": "deepseek-v4-pro",
                        "input": [{
                            "type": "agent_message",
                            "author": "/root",
                            "recipient": "/root/worker",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "Message Type: NEW_TASK\nPayload:\n",
                                },
                                {
                                    "type": "encrypted_content",
                                    "encrypted_content": "gAAAAABunmatched-opaque-payload==",
                                },
                            ],
                        }],
                    })

        self.assertEqual(error.exception.code, "unresolved_agent_message")

    def test_v2_agent_message_fails_closed_when_parent_dispatch_is_ciphertext(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            database = home / "state_5.sqlite"
            ciphertext = "gAAAAABparent-and-child-are-both-opaque=="
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE threads ("
                    "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, "
                    "model_provider TEXT NOT NULL, agent_path TEXT, created_at_ms INTEGER)"
                )
                _add_agent_task(
                    connection,
                    home,
                    parent_id="parent-opaque",
                    child_id="child-opaque",
                    recipient="/root/worker",
                    encrypted_content=ciphertext,
                    task_message=ciphertext,
                    created_at_ms=100,
                )
                connection.commit()

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                with self.assertRaises(BridgeError) as error:
                    build_chat_request({
                        "model": "deepseek-v4-pro",
                        "input": [{
                            "type": "agent_message",
                            "author": "/root",
                            "recipient": "/root/worker",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "Message Type: NEW_TASK\nPayload:\n",
                                },
                                {
                                    "type": "encrypted_content",
                                    "encrypted_content": ciphertext,
                                },
                            ],
                        }],
                    })

        self.assertEqual(error.exception.code, "unresolved_agent_message")

    def test_v2_agent_message_uses_explicit_handoff_for_ciphertext_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            database = home / "state_5.sqlite"
            ciphertext = "gAAAAABparent-and-child-are-both-opaque=="
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE threads ("
                    "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, "
                    "model_provider TEXT NOT NULL, agent_path TEXT, created_at_ms INTEGER)"
                )
                _add_agent_task(
                    connection,
                    home,
                    parent_id="parent-handoff",
                    child_id="child-handoff",
                    recipient="/root/worker",
                    encrypted_content=ciphertext,
                    task_message=ciphertext,
                    handoff_message="Inspect module A and report exact file evidence.",
                    created_at_ms=100,
                )
                connection.commit()

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                translated = build_chat_request({
                    "model": "deepseek-v4-pro",
                    "input": [{
                        "type": "agent_message",
                        "author": "/root",
                        "recipient": "/root/worker",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Message Type: NEW_TASK\nPayload:\n",
                            },
                            {
                                "type": "encrypted_content",
                                "encrypted_content": ciphertext,
                            },
                        ],
                    }],
                })

        content = translated.payload["messages"][0]["content"]
        self.assertIn("Inspect module A and report exact file evidence.", content)
        self.assertNotIn(ciphertext, content)

    def test_v2_agent_followup_resolves_the_matching_parent_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            database = home / "state_5.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE threads ("
                    "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, source TEXT NOT NULL, "
                    "model_provider TEXT NOT NULL, agent_path TEXT, created_at_ms INTEGER)"
                )
                _add_agent_task(
                    connection,
                    home,
                    parent_id="parent-followup",
                    child_id="child-followup",
                    recipient="/root/worker",
                    encrypted_content="gAAAAABinitial-opaque-payload==",
                    task_message="gAAAAABinitial-parent-opaque-payload==",
                    handoff_message="Initial child task.",
                    created_at_ms=100,
                )
                connection.commit()

            parent_rollout = home / "sessions" / "parent-parent-followup.jsonl"
            child_rollout = home / "sessions" / "child-child-followup.jsonl"
            _append_rollout(parent_rollout, [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": _handoff_text(
                            "/root/worker",
                            "Follow-up child task.",
                        ),
                    }],
                },
                {
                    "type": "function_call",
                    "name": "followup_task",
                    "call_id": "call-followup",
                    "arguments": json.dumps({
                        "target": "/root/worker",
                        "message": "gAAAAABfollowup-parent-opaque-payload==",
                    }),
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-followup",
                    "output": "",
                },
            ])
            _append_rollout(child_rollout, [{
                "type": "agent_message",
                "author": "/root",
                "recipient": "/root/worker",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Message Type: NEW_TASK\nPayload:\n",
                    },
                    {
                        "type": "encrypted_content",
                        "encrypted_content": "gAAAAABfollowup-opaque-payload==",
                    },
                ],
            }])

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                translated = build_chat_request({
                    "model": "deepseek-v4-pro",
                    "input": [{
                        "type": "agent_message",
                        "author": "/root",
                        "recipient": "/root/worker",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Message Type: NEW_TASK\nPayload:\n",
                            },
                            {
                                "type": "encrypted_content",
                                "encrypted_content": "gAAAAABfollowup-opaque-payload==",
                            },
                        ],
                    }],
                })

        content = translated.payload["messages"][0]["content"]
        self.assertIn("Follow-up child task.", content)
        self.assertNotIn("Initial child task.", content)


class BridgeStreamTests(unittest.TestCase):
    def test_non_stream_completion_keeps_reasoning_and_parallel_tool_indexes(self) -> None:
        chunk = _completion_as_chunk({
            "id": "chat-json",
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning_content": "reason first",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "one", "arguments": "{}"},
                        },
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {"name": "two", "arguments": "{}"},
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        })

        delta = chunk["choices"][0]["delta"]
        self.assertEqual(delta["reasoning_content"], "reason first")
        self.assertEqual(
            [call["index"] for call in delta["tool_calls"]],
            [0, 1],
        )

    def test_stream_translates_text_and_usage_to_responses_events(self) -> None:
        translated = build_chat_request({
            "model": "deepseek-v4-pro",
            "input": "hello",
        })
        stream = ChatStreamTranslator(translated.tools)

        events = []
        events.extend(stream.start("resp-test"))
        events.extend(stream.feed({
            "id": "chat-1",
            "choices": [{"delta": {"content": "Hel"}, "finish_reason": None}],
        }))
        events.extend(stream.feed({
            "id": "chat-1",
            "choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        }))
        events.extend(stream.finish())

        kinds = [event["type"] for event in events]
        self.assertEqual(kinds[0], "response.created")
        self.assertEqual(kinds.count("response.output_text.delta"), 2)
        first_delta = kinds.index("response.output_text.delta")
        self.assertLess(kinds.index("response.output_item.added"), first_delta)
        self.assertLess(kinds.index("response.content_part.added"), first_delta)
        self.assertGreater(kinds.index("response.output_text.done"), first_delta)
        self.assertGreater(kinds.index("response.content_part.done"), first_delta)
        message = next(
            event["item"]
            for event in events
            if event["type"] == "response.output_item.done"
        )
        self.assertEqual(message["content"][0]["text"], "Hello")
        completed = events[-1]["response"]
        self.assertEqual(completed["usage"]["total_tokens"], 6)

    def test_stream_restores_namespace_and_custom_tool_types(self) -> None:
        translated = build_chat_request({
            "model": "deepseek-v4-pro",
            "input": "use tools",
            "tools": [
                {
                    "type": "namespace",
                    "name": "agents",
                    "tools": [{
                        "type": "function",
                        "name": "spawn_agent",
                        "parameters": {"type": "object"},
                    }],
                },
                {"type": "custom", "name": "apply_patch"},
            ],
        })
        stream = ChatStreamTranslator(translated.tools)
        stream.start("resp-tools")
        stream.feed({
            "choices": [{
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call-a",
                    "function": {
                        "name": "agents__spawn_agent",
                        "arguments": '{"agent_type":"worker"}',
                    },
                }]},
                "finish_reason": None,
            }],
        })
        stream.feed({
            "choices": [{
                "delta": {"tool_calls": [{
                    "index": 1,
                    "id": "call-b",
                    "function": {
                        "name": "apply_patch",
                        "arguments": json.dumps({"input": "*** Begin Patch"}),
                    },
                }]},
                "finish_reason": "tool_calls",
            }],
        })

        events = stream.finish()
        items = [event["item"] for event in events if event["type"] == "response.output_item.done"]

        self.assertEqual(items[0]["type"], "function_call")
        self.assertEqual(items[0]["namespace"], "agents")
        self.assertEqual(items[0]["name"], "spawn_agent")
        self.assertEqual(items[1]["type"], "custom_tool_call")
        self.assertEqual(items[1]["input"], "*** Begin Patch")

    def test_over_limit_namespace_is_dispatched_and_restored(self) -> None:
        bulk_tools = [{
            "type": "namespace",
            "name": "bulk",
            "description": "Large plugin namespace.",
            "tools": [
                {
                    "type": "function",
                    "name": f"tool_{index:03d}",
                    "description": f"Bulk tool {index}.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
                for index in range(129)
            ],
        }]
        translated = build_chat_request({
            "model": "deepseek-v4-pro",
            "input": "Use the final bulk tool.",
            "tools": bulk_tools,
        })

        self.assertLessEqual(len(translated.payload["tools"]), 128)
        selector = "bulk__tool_128"
        dispatcher = next(
            tool
            for tool in translated.payload["tools"]
            if selector
            in tool["function"]["parameters"]["properties"].get("tool", {}).get("enum", [])
        )
        stream = ChatStreamTranslator(translated.tools)
        stream.start("resp-dispatch")
        stream.feed({
            "choices": [{
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call-dispatch",
                    "function": {
                        "name": dispatcher["function"]["name"],
                        "arguments": json.dumps({
                            "tool": selector,
                            "arguments": {"path": "final.txt"},
                        }),
                    },
                }]},
                "finish_reason": "tool_calls",
            }],
        })

        item = next(
            event["item"]
            for event in stream.finish()
            if event.get("item", {}).get("type") == "function_call"
        )
        self.assertEqual(item["name"], "tool_128")
        self.assertEqual(item["namespace"], "bulk")
        self.assertEqual(json.loads(item["arguments"]), {"path": "final.txt"})

        continued = build_chat_request({
            "model": "deepseek-v4-pro",
            "input": [
                item,
                {
                    "type": "function_call_output",
                    "call_id": "call-dispatch",
                    "output": "done",
                },
            ],
            "tools": bulk_tools,
        })
        historical_call = continued.payload["messages"][0]["tool_calls"][0]["function"]
        self.assertEqual(historical_call["name"], dispatcher["function"]["name"])
        self.assertEqual(json.loads(historical_call["arguments"]), {
            "tool": selector,
            "arguments": {"path": "final.txt"},
        })

    def test_over_limit_top_level_tools_use_bounded_dispatchers(self) -> None:
        translated = build_chat_request({
            "model": "deepseek-v4-pro",
            "input": "Use the final standalone tool.",
            "tools": [
                {
                    "type": "function",
                    "name": f"tool_{index:03d}",
                    "description": f"Standalone tool {index}.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                    },
                }
                for index in range(129)
            ],
        })

        self.assertLessEqual(len(translated.payload["tools"]), 128)
        selector = "tool_128"
        dispatcher = next(
            tool
            for tool in translated.payload["tools"]
            if selector
            in tool["function"]["parameters"]["properties"].get("tool", {}).get("enum", [])
        )
        stream = ChatStreamTranslator(translated.tools)
        stream.start("resp-standalone-dispatch")
        stream.feed({
            "choices": [{
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call-standalone-dispatch",
                    "function": {
                        "name": dispatcher["function"]["name"],
                        "arguments": json.dumps({
                            "tool": selector,
                            "arguments": {"value": 128},
                        }),
                    },
                }]},
                "finish_reason": "tool_calls",
            }],
        })

        item = next(
            event["item"]
            for event in stream.finish()
            if event.get("item", {}).get("type") == "function_call"
        )
        self.assertEqual(item["name"], "tool_128")
        self.assertNotIn("namespace", item)
        self.assertEqual(json.loads(item["arguments"]), {"value": 128})

    def test_dispatcher_restores_custom_tool_and_rejects_unknown_selector(self) -> None:
        children = [
            {
                "type": "function",
                "name": f"tool_{index:03d}",
                "parameters": {"type": "object", "properties": {}},
            }
            for index in range(128)
        ]
        children.append({
            "type": "custom",
            "name": "patch_text",
            "description": "Apply raw patch text.",
        })
        translated = build_chat_request({
            "model": "deepseek-v4-pro",
            "input": "Patch it.",
            "tools": [{"type": "namespace", "name": "bulk", "tools": children}],
        })
        selector = "bulk__patch_text"
        dispatcher = next(
            tool
            for tool in translated.payload["tools"]
            if selector
            in tool["function"]["parameters"]["properties"].get("tool", {}).get("enum", [])
        )

        stream = ChatStreamTranslator(translated.tools)
        stream.start("resp-custom-dispatch")
        stream.feed({
            "choices": [{
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call-custom-dispatch",
                    "function": {
                        "name": dispatcher["function"]["name"],
                        "arguments": json.dumps({
                            "tool": selector,
                            "arguments": {"input": "*** Begin Patch"},
                        }),
                    },
                }]},
                "finish_reason": "tool_calls",
            }],
        })
        item = next(
            event["item"]
            for event in stream.finish()
            if event.get("item", {}).get("type") == "custom_tool_call"
        )
        self.assertEqual(item["namespace"], "bulk")
        self.assertEqual(item["name"], "patch_text")
        self.assertEqual(item["input"], "*** Begin Patch")

        invalid = ChatStreamTranslator(translated.tools)
        invalid.start("resp-invalid-dispatch")
        invalid.feed({
            "choices": [{
                "delta": {"tool_calls": [{
                    "index": 0,
                    "id": "call-invalid-dispatch",
                    "function": {
                        "name": dispatcher["function"]["name"],
                        "arguments": json.dumps({"tool": "unknown", "arguments": {}}),
                    },
                }]},
                "finish_reason": "tool_calls",
            }],
        })
        with self.assertRaises(BridgeError) as invalid_error:
            invalid.finish()
        self.assertEqual(invalid_error.exception.code, "invalid_tool_dispatch")

    def test_reasoning_emits_safe_tool_progress_without_exposing_raw_chain(self) -> None:
        translated = build_chat_request({
            "model": "deepseek-v4-pro",
            "input": "Inspect the workspace.",
            "tools": [{
                "type": "function",
                "name": "shell_command",
                "parameters": {"type": "object"},
            }],
        }, reasoning_secret="sk-test")
        stream = ChatStreamTranslator(translated.tools, reasoning_secret="sk-test")
        stream.start("resp-safe-summary")
        stream.feed({
            "choices": [{
                "delta": {
                    "reasoning_content": "Private step-by-step chain that must stay hidden.",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call-shell",
                        "function": {
                            "name": "shell_command",
                            "arguments": '{"command":"Get-ChildItem"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        })

        reasoning_item = next(
            event["item"]
            for event in stream.finish()
            if event.get("item", {}).get("type") == "reasoning"
        )

        self.assertEqual(reasoning_item["summary"], [{
            "type": "summary_text",
            "text": "本轮分析完成；下一步：检查本地状态并运行验证。",
        }])
        self.assertNotIn(
            "Private step-by-step chain",
            json.dumps(reasoning_item["summary"], ensure_ascii=False),
        )

    def test_reasoning_is_sealed_and_replayed_with_parallel_tool_calls(self) -> None:
        first = build_chat_request({
            "model": "deepseek-v4-pro",
            "input": "delegate",
            "tools": [{
                "type": "function",
                "name": "spawn_agent",
                "parameters": {"type": "object"},
            }],
        }, reasoning_secret="sk-test")
        stream = ChatStreamTranslator(first.tools, reasoning_secret="sk-test")
        stream.start("resp-reasoning")
        stream.feed({
            "choices": [{
                "delta": {"reasoning_content": "I should delegate. "},
                "finish_reason": None,
            }],
        })
        stream.feed({
            "choices": [{
                "delta": {
                    "content": "",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "function": {"name": "spawn_agent", "arguments": "{}"},
                        },
                        {
                            "index": 1,
                            "id": "call-2",
                            "function": {"name": "spawn_agent", "arguments": "{}"},
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }],
        })
        output = stream.finish()
        reasoning_item = next(
            event["item"]
            for event in output
            if event.get("item", {}).get("type") == "reasoning"
        )
        self.assertNotIn("I should delegate", reasoning_item["encrypted_content"])
        calls = [
            event["item"]
            for event in output
            if event.get("item", {}).get("type") == "function_call"
        ]

        second = build_chat_request({
            "model": "deepseek-v4-pro",
            "input": [
                reasoning_item,
                *calls,
                {"type": "function_call_output", "call_id": "call-1", "output": "one"},
                {"type": "function_call_output", "call_id": "call-2", "output": "two"},
            ],
            "tools": [{
                "type": "function",
                "name": "spawn_agent",
                "parameters": {"type": "object"},
            }],
        }, reasoning_secret="sk-test")

        assistant = second.payload["messages"][0]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["content"], "")
        self.assertEqual(assistant["reasoning_content"], "I should delegate. ")
        self.assertEqual(len(assistant["tool_calls"]), 2)
        self.assertEqual(second.payload["messages"][1]["role"], "tool")
        self.assertEqual(second.payload["messages"][2]["role"], "tool")


class BridgeHttpTests(unittest.TestCase):
    def test_loopback_bridge_forwards_auth_and_returns_responses_sse(self) -> None:
        captured: dict[str, object] = {}

        class UpstreamHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                captured["authorization"] = self.headers.get("Authorization")
                captured["body"] = json.loads(self.rfile.read(length))
                chunks = [
                    {
                        "id": "chat-test",
                        "choices": [{
                            "delta": {"reasoning_content": "brief reasoning"},
                            "finish_reason": None,
                        }],
                    },
                    {
                        "id": "chat-test",
                        "choices": [{
                            "delta": {"content": "READY"},
                            "finish_reason": "stop",
                        }],
                        "usage": {
                            "prompt_tokens": 5,
                            "completion_tokens": 3,
                            "total_tokens": 8,
                        },
                    },
                ]
                body = b"".join(
                    b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n"
                    for chunk in chunks
                ) + b"data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}/chat/completions"
        bridge = _BridgeServer(("127.0.0.1", 0), upstream_url)
        bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        bridge_thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{bridge.server_address[1]}/v1/responses",
                data=json.dumps({
                    "model": "deepseek-v4-pro",
                    "input": "reply READY",
                }).encode("utf-8"),
                headers={
                    "Authorization": "Bearer sk-test",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                output = response.read().decode("utf-8")
        finally:
            bridge.shutdown()
            bridge.server_close()
            upstream.shutdown()
            upstream.server_close()

        self.assertEqual(captured["authorization"], "Bearer sk-test")
        self.assertEqual(captured["body"]["reasoning_effort"], "high")
        self.assertIn('"type":"response.created"', output)
        self.assertIn('"type":"reasoning"', output)
        self.assertNotIn("brief reasoning", output)
        self.assertIn('"text":"READY"', output)
        self.assertIn('"type":"response.completed"', output)


if __name__ == "__main__":
    unittest.main()
