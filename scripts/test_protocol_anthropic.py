#!/usr/bin/env python3

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay.canonical import (  # noqa: E402
    CanonicalContentBlock,
    CanonicalEventSequence,
    CanonicalMessage,
    CanonicalRequest,
    EventKind,
    RequestCommitTracker,
)
from multi_relay.failure import FailureClass, classify_http_failure  # noqa: E402
from multi_relay.protocols.anthropic_messages import (  # noqa: E402
    AnthropicInboundAdapter,
    AnthropicOutboundRenderer,
    AnthropicUpstreamAdapter,
)


HEADERS = {
    "anthropic-version": "2023-06-01",
    "authorization": "Bearer local-test-token",
}


def sse(event: str, payload: object) -> bytes:
    return (
        f"event: {event}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


class AnthropicProtocolTests(unittest.TestCase):
    def test_minimal_claude_messages_request_and_nonstream_response(self) -> None:
        inbound = AnthropicInboundAdapter().parse_request(
            {
                "model": "relay-general",
                "max_tokens": 256,
                "system": "Be concise.",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers=HEADERS,
            request_id="req-claude",
            pool_id="general",
        )

        self.assertEqual(inbound.host, "claude-code")
        self.assertEqual(inbound.system_blocks[0].text, "Be concise.")
        self.assertEqual(inbound.messages[0].content[0].text, "Hello")
        upstream = AnthropicUpstreamAdapter().build_request(
            inbound,
            model="claude-upstream",
        )
        self.assertEqual(upstream["model"], "claude-upstream")
        self.assertEqual(upstream["max_tokens"], 256)
        self.assertEqual(upstream["messages"][0]["role"], "user")

        events = AnthropicUpstreamAdapter().parse_response(
            {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "Hi"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 1},
            }
        )
        self.assertIn(EventKind.TEXT_DELTA, [event.kind for event in events])
        self.assertEqual(events[-1].payload["finish_reason"], "stop")

    def test_streaming_text_fixture_maps_message_lifecycle(self) -> None:
        chunks = [
            sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg-stream",
                        "usage": {"input_tokens": 4, "output_tokens": 0},
                    },
                },
            ),
            sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "你好"},
                },
            ),
            sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": 0},
            ),
            sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 2},
                },
            ),
            sse("message_stop", {"type": "message_stop"}),
        ]

        events = list(AnthropicUpstreamAdapter().iter_events(chunks))
        kinds = [event.kind for event in events]

        self.assertEqual(kinds[0], EventKind.RESPONSE_STARTED)
        self.assertIn(EventKind.CONTENT_BLOCK_STARTED, kinds)
        self.assertIn(EventKind.TEXT_DELTA, kinds)
        self.assertIn(EventKind.CONTENT_BLOCK_COMPLETED, kinds)
        self.assertIn(EventKind.USAGE, kinds)
        self.assertEqual(kinds[-1], EventKind.RESPONSE_COMPLETED)

    def test_tool_use_and_tool_result_round_trip_with_multiple_blocks(self) -> None:
        payload = {
            "model": "relay-general",
            "max_tokens": 512,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Checking"},
                        {
                            "type": "tool_use",
                            "id": "toolu-1",
                            "name": "lookup",
                            "input": {"query": "one"},
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu-2",
                            "name": "lookup",
                            "input": {"query": "two"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu-1",
                            "content": [{"type": "text", "text": "first"}],
                            "is_error": False,
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu-2",
                            "content": [{"type": "text", "text": "failed"}],
                            "is_error": True,
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "name": "lookup",
                    "description": "Lookup",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }
            ],
        }
        canonical = AnthropicInboundAdapter().parse_request(
            payload,
            headers=HEADERS,
            request_id="req-tools",
            pool_id="general",
        )

        rebuilt = AnthropicUpstreamAdapter().build_request(
            canonical,
            model="relay-general",
        )
        restored = AnthropicInboundAdapter().parse_request(
            rebuilt,
            headers=HEADERS,
            request_id="req-tools-restored",
            pool_id="general",
        )

        self.assertEqual(canonical.messages, restored.messages)
        self.assertEqual(canonical.tools, restored.tools)
        self.assertEqual(
            [block.kind for block in canonical.messages[0].content],
            ["text", "tool_call", "tool_call"],
        )
        self.assertTrue(canonical.messages[1].content[1].is_error)

    def test_base64_image_maps_without_writing_a_file(self) -> None:
        raw = b"image-fixture"
        canonical = AnthropicInboundAdapter().parse_request(
            {
                "model": "relay-vision",
                "max_tokens": 32,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": base64.b64encode(raw).decode("ascii"),
                                },
                            }
                        ],
                    }
                ],
            },
            headers=HEADERS,
            request_id="req-image",
            pool_id="vision",
        )

        image = canonical.messages[0].content[0]
        self.assertEqual(image.decoded_image_bytes(), raw)
        rebuilt = AnthropicUpstreamAdapter().build_request(
            canonical,
            model="vision-upstream",
        )
        self.assertEqual(
            rebuilt["messages"][0]["content"][0]["source"]["type"],
            "base64",
        )

    def test_unknown_beta_thinking_and_semantic_fields_fail_explicitly(self) -> None:
        base = {
            "model": "relay-general",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hello"}],
        }
        with self.assertRaises(ManagerError) as beta:
            AnthropicInboundAdapter().parse_request(
                base,
                headers={**HEADERS, "anthropic-beta": "unknown-beta-2099-01-01"},
                request_id="req-beta",
                pool_id="general",
            )
        self.assertEqual(beta.exception.code, "unsupported_anthropic_beta")

        with self.assertRaises(ManagerError) as thinking:
            AnthropicInboundAdapter().parse_request(
                {**base, "thinking": {"type": "enabled", "budget_tokens": 1024}},
                headers=HEADERS,
                request_id="req-thinking",
                pool_id="general",
            )
        self.assertEqual(thinking.exception.code, "unsupported_thinking")

        with self.assertRaises(ManagerError) as unknown:
            AnthropicInboundAdapter().parse_request(
                {**base, "output_format": {"type": "json"}},
                headers=HEADERS,
                request_id="req-field",
                pool_id="general",
            )
        self.assertEqual(unknown.exception.code, "request_invalid")

    def test_stop_reasons_map_deterministically(self) -> None:
        cases = {
            "end_turn": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "stop_sequence": "stop_sequence",
        }
        for wire, canonical in cases.items():
            with self.subTest(wire=wire):
                chunks = [
                    sse(
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {
                                "id": f"msg-{wire}",
                                "usage": {"input_tokens": 1, "output_tokens": 0},
                            },
                        },
                    ),
                    sse(
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": wire},
                            "usage": {"output_tokens": 1},
                        },
                    ),
                    sse("message_stop", {"type": "message_stop"}),
                ]
                events = list(AnthropicUpstreamAdapter().iter_events(chunks))
                self.assertEqual(events[-1].payload["finish_reason"], canonical)

    def test_anthropic_error_envelope_normalizes_to_failure_class(self) -> None:
        adapter = AnthropicUpstreamAdapter()
        metadata = adapter.classify_error_metadata(
            {
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "message": "Rate limited",
                },
            },
            {},
        )

        failure = classify_http_failure(
            status=429,
            body=b'{"type":"error"}',
            headers={"content-type": "application/json"},
            provider_error=metadata,
        )

        self.assertEqual(failure.failure_class, FailureClass.RATE_LIMITED)

    def test_openai_tool_events_render_as_anthropic_input_json_delta(self) -> None:
        sequence = CanonicalEventSequence("resp-tools")
        started = sequence.emit(
            EventKind.TOOL_CALL_STARTED,
            block_index=1,
            tool_call_id="call-1",
            payload={"name": "lookup"},
        )
        delta = sequence.emit(
            EventKind.TOOL_CALL_ARGUMENTS_DELTA,
            block_index=1,
            tool_call_id="call-1",
            payload={"delta": '{"query":"one"}'},
        )
        renderer = AnthropicOutboundRenderer()

        start_wire = renderer.render(started)
        delta_wire = renderer.render(delta)

        self.assertEqual(start_wire[0]["type"], "content_block_start")
        self.assertEqual(start_wire[0]["content_block"]["type"], "tool_use")
        self.assertEqual(
            delta_wire[0]["delta"]["type"],
            "input_json_delta",
        )

    def test_fragmented_tool_json_and_cache_usage_are_preserved(self) -> None:
        chunks = [
            sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg-cache",
                        "usage": {
                            "input_tokens": 2,
                            "output_tokens": 0,
                            "cache_creation_input_tokens": 3,
                            "cache_read_input_tokens": 5,
                        },
                    },
                },
            ),
            sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu-cache",
                        "name": "lookup",
                        "input": {},
                    },
                },
            ),
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"query":',
                    },
                },
            ),
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '"one"}',
                    },
                },
            ),
            sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 4},
                },
            ),
            sse("message_stop", {"type": "message_stop"}),
        ]

        events = list(AnthropicUpstreamAdapter().iter_events(chunks))
        completed = next(
            event for event in events if event.kind is EventKind.TOOL_CALL_COMPLETED
        )
        usage = next(event for event in events if event.kind is EventKind.USAGE)

        self.assertEqual(completed.payload["arguments"], '{"query":"one"}')
        self.assertEqual(usage.payload["cache_creation_input_tokens"], 3)
        self.assertEqual(usage.payload["cache_read_input_tokens"], 5)
        self.assertEqual(usage.payload["total_tokens"], 14)

    def test_first_delivered_content_block_event_commits_request(self) -> None:
        sequence = CanonicalEventSequence("resp-commit")
        started = sequence.emit(EventKind.RESPONSE_STARTED)
        block = sequence.emit(
            EventKind.CONTENT_BLOCK_STARTED,
            block_index=0,
            payload={"kind": "text"},
        )
        tracker = RequestCommitTracker()

        tracker.observe(started, delivered=False)
        self.assertTrue(tracker.observe(block, delivered=True))
        self.assertTrue(tracker.committed)


if __name__ == "__main__":
    unittest.main()
