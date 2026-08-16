#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay.canonical import (  # noqa: E402
    CanonicalEventSequence,
    EventKind,
)
from multi_relay.protocols.responses import ResponsesAdapter  # noqa: E402
from multi_relay.protocols.chat_completions import ChatCompletionsAdapter  # noqa: E402


def sse(payload: object) -> bytes:
    return (
        "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
    ).encode("utf-8")


class ResponsesProtocolTests(unittest.TestCase):
    def test_responses_input_can_build_chat_upstream_without_protocol_import_cycle(self) -> None:
        canonical = ResponsesAdapter().parse_request(
            {
                "model": "relay-general",
                "input": "hello",
                "stream": True,
            },
            request_id="req-cross-protocol",
            host="codex",
            pool_id="general",
        )

        chat = ChatCompletionsAdapter().build_request(
            canonical,
            model="chat-model",
        )

        self.assertEqual(chat["model"], "chat-model")
        self.assertEqual(chat["messages"], [{"role": "user", "content": "hello"}])

    def test_responses_request_round_trips_roles_tools_calls_and_results(self) -> None:
        payload = {
            "model": "relay-general",
            "input": [
                {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": "system"}],
                },
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "developer"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "question"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "lookup",
                    "arguments": '{"query":"one"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "result",
                    "is_error": False,
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }
            ],
            "tool_choice": "auto",
            "max_output_tokens": 100,
            "stream": True,
        }
        adapter = ResponsesAdapter()

        canonical = adapter.parse_request(
            payload,
            request_id="req-responses",
            host="codex",
            pool_id="general",
        )
        rebuilt = adapter.build_request(canonical, model="relay-general")
        restored = adapter.parse_request(
            rebuilt,
            request_id="req-restored",
            host="codex",
            pool_id="general",
        )

        self.assertEqual(canonical.system_blocks, restored.system_blocks)
        self.assertEqual(canonical.developer_blocks, restored.developer_blocks)
        self.assertEqual(canonical.messages, restored.messages)
        self.assertEqual(canonical.tools, restored.tools)
        self.assertEqual(
            canonical.messages[1].content[0].kind,
            "tool_call",
        )
        self.assertEqual(
            canonical.messages[2].content[0].tool_call_id,
            "call-1",
        )

    def test_rendered_canonical_events_use_responses_event_shapes(self) -> None:
        adapter = ResponsesAdapter()
        sequence = CanonicalEventSequence("resp-1")
        started = sequence.emit(EventKind.RESPONSE_STARTED)
        delta = sequence.emit(
            EventKind.TEXT_DELTA,
            block_index=0,
            payload={"delta": "hello"},
        )
        complete = sequence.emit(EventKind.RESPONSE_COMPLETED)

        rendered = [adapter.render_event(item) for item in (started, delta, complete)]

        self.assertEqual(rendered[0]["type"], "response.created")
        self.assertEqual(rendered[1]["type"], "response.output_text.delta")
        self.assertEqual(rendered[1]["delta"], "hello")
        self.assertEqual(rendered[2]["type"], "response.completed")

    def test_responses_sse_parses_text_tool_and_usage_events(self) -> None:
        chunks = [
            sse({"type": "response.created", "response": {"id": "resp-up"}}),
            sse(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "delta": "hello",
                }
            ),
            sse(
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 1,
                    "item_id": "call-1",
                    "delta": '{"query":"one"}',
                    "name": "lookup",
                }
            ),
            sse(
                {
                    "type": "response.function_call_arguments.done",
                    "output_index": 1,
                    "item_id": "call-1",
                    "arguments": '{"query":"one"}',
                    "name": "lookup",
                }
            ),
            sse(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp-up",
                        "usage": {
                            "input_tokens": 4,
                            "output_tokens": 2,
                            "total_tokens": 6,
                        },
                    },
                }
            ),
            b"data: [DONE]\n\n",
        ]

        events = list(ResponsesAdapter().iter_events(chunks))
        kinds = [event.kind for event in events]

        self.assertIn(EventKind.RESPONSE_STARTED, kinds)
        self.assertIn(EventKind.TEXT_DELTA, kinds)
        self.assertIn(EventKind.TOOL_CALL_STARTED, kinds)
        self.assertIn(EventKind.TOOL_CALL_ARGUMENTS_DELTA, kinds)
        self.assertIn(EventKind.TOOL_CALL_COMPLETED, kinds)
        self.assertIn(EventKind.USAGE, kinds)
        self.assertEqual(kinds[-1], EventKind.RESPONSE_COMPLETED)


if __name__ == "__main__":
    unittest.main()
