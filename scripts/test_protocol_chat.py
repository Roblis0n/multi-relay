#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay.canonical import (  # noqa: E402
    CanonicalContentBlock,
    CanonicalMessage,
    CanonicalRequest,
    CanonicalTool,
    EventKind,
)
from multi_relay.protocols.chat_completions import (  # noqa: E402
    ChatCompletionsAdapter,
    ChatStreamTranslator,
    build_chat_request,
)


def request_fixture() -> CanonicalRequest:
    tool = CanonicalTool(
        "lookup",
        "Look up a value.",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    return CanonicalRequest(
        request_id="req-chat",
        host="codex",
        model_alias="relay-general",
        pool_id="general",
        system_blocks=(CanonicalContentBlock.text_block("system"),),
        developer_blocks=(CanonicalContentBlock.text_block("developer"),),
        messages=(
            CanonicalMessage(
                "user",
                (CanonicalContentBlock.text_block("hello"),),
            ),
        ),
        tools=(tool,),
        max_output_tokens=100,
        temperature=0.2,
        top_p=0.8,
        requested_reasoning_effort="high",
        stream=True,
    )


def sse(payload: object, *, crlf: bool = False) -> bytes:
    newline = "\r\n" if crlf else "\n"
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}{newline}{newline}".encode("utf-8")


class ClosableChunks:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class ChatProtocolTests(unittest.TestCase):
    def test_canonical_request_builds_chat_payload(self) -> None:
        payload = ChatCompletionsAdapter().build_request(
            request_fixture(),
            model="upstream-model",
        )

        self.assertEqual(payload["model"], "upstream-model")
        self.assertEqual(
            [message["role"] for message in payload["messages"]],
            ["system", "developer", "user"],
        )
        self.assertEqual(payload["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_nonstream_and_stream_cover_text_reasoning_tools_usage_and_finish(self) -> None:
        completion = {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "message": {
                        "reasoning_content": "summary",
                        "content": "answer",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": '{"query":"one"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 3,
                "total_tokens": 8,
            },
        }
        nonstream = ChatCompletionsAdapter().parse_response(completion)
        kinds = [event.kind for event in nonstream]
        self.assertIn(EventKind.REASONING_SUMMARY_DELTA, kinds)
        self.assertIn(EventKind.TEXT_DELTA, kinds)
        self.assertIn(EventKind.TOOL_CALL_COMPLETED, kinds)
        self.assertIn(EventKind.USAGE, kinds)
        self.assertEqual(kinds[-1], EventKind.RESPONSE_COMPLETED)

        chunks = [
            sse(
                {
                    "id": "chatcmpl-1",
                    "choices": [
                        {
                            "delta": {"reasoning_content": "summary"},
                            "finish_reason": None,
                        }
                    ],
                },
                crlf=True,
            ),
            sse(
                {
                    "id": "chatcmpl-1",
                    "choices": [
                        {
                            "delta": {"content": "answer"},
                            "finish_reason": None,
                        }
                    ],
                }
            ),
            sse(
                {
                    "id": "chatcmpl-1",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"query":"one"}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 3,
                        "total_tokens": 8,
                    },
                }
            ),
            b"\r\n",
            sse("[DONE]"),
        ]
        stream = list(ChatCompletionsAdapter().iter_events(chunks))
        stream_kinds = [event.kind for event in stream]
        for kind in (
            EventKind.REASONING_SUMMARY_DELTA,
            EventKind.TEXT_DELTA,
            EventKind.TOOL_CALL_STARTED,
            EventKind.TOOL_CALL_ARGUMENTS_DELTA,
            EventKind.TOOL_CALL_COMPLETED,
            EventKind.USAGE,
            EventKind.RESPONSE_COMPLETED,
        ):
            self.assertIn(kind, stream_kinds)

    def test_utf8_can_split_at_any_byte_boundary(self) -> None:
        raw = sse(
            {
                "id": "chatcmpl-utf8",
                "choices": [
                    {"delta": {"content": "你好"}, "finish_reason": "stop"}
                ],
            }
        ) + sse("[DONE]")
        chunks = [raw[index : index + 1] for index in range(len(raw))]

        events = list(ChatCompletionsAdapter().iter_events(chunks))

        deltas = [
            event.payload["delta"]
            for event in events
            if event.kind is EventKind.TEXT_DELTA
        ]
        self.assertEqual(deltas, ["你好"])

    def test_oversized_sse_event_fails_closed(self) -> None:
        adapter = ChatCompletionsAdapter(max_sse_event_bytes=64)
        with self.assertRaisesRegex(Exception, "SSE event"):
            list(adapter.iter_events([b"data: " + (b"x" * 100)]))

    def test_closing_consumer_closes_upstream_iterator(self) -> None:
        source = ClosableChunks(
            [
                sse(
                    {
                        "id": "chatcmpl-close",
                        "choices": [
                            {"delta": {"content": "one"}, "finish_reason": None}
                        ],
                    }
                )
            ]
        )
        events = ChatCompletionsAdapter().iter_events(source)
        next(events)

        events.close()

        self.assertTrue(source.closed)

    def test_old_bridge_names_are_compatibility_reexports(self) -> None:
        from multi_relay import bridge

        self.assertIs(bridge.build_chat_request, build_chat_request)
        self.assertIs(bridge.ChatStreamTranslator, ChatStreamTranslator)
        translated = build_chat_request(
            {"model": "deepseek-v4-pro", "input": "hello"}
        )
        self.assertEqual(translated.payload["messages"][-1]["content"], "hello")


if __name__ == "__main__":
    unittest.main()
