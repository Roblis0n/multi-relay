#!/usr/bin/env python3

from __future__ import annotations

import base64
import copy
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay.canonical import (  # noqa: E402
    CanonicalContentBlock,
    CanonicalEvent,
    CanonicalEventSequence,
    CanonicalMessage,
    CanonicalRequest,
    CanonicalTool,
    CanonicalUsage,
    EventKind,
    RequestCommitTracker,
    ToolChoice,
)
from multi_relay.capabilities import (  # noqa: E402
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_REQUEST,
)
from multi_relay.model_capabilities import resolve_target_effort  # noqa: E402


def text(value: str) -> CanonicalContentBlock:
    return CanonicalContentBlock.text_block(value)


def sample_request() -> CanonicalRequest:
    return CanonicalRequest(
        request_id="req-1",
        host="codex",
        model_alias="multi-relay-general",
        pool_id="general",
        system_blocks=(text("System rule."),),
        developer_blocks=(text("Developer rule."),),
        messages=(
            CanonicalMessage("user", (text("First"),)),
            CanonicalMessage("assistant", (text("Second"),)),
            CanonicalMessage("user", (text("Third"),)),
        ),
        tools=(),
        tool_choice=ToolChoice.auto(),
        max_output_tokens=2048,
        temperature=0.25,
        top_p=0.9,
        stop=("END",),
        seed=42,
        parallel_tool_calls=True,
        requested_reasoning_effort="high",
        stream=True,
        metadata={"trace_id": "trace-1"},
    )


class CanonicalRequestTests(unittest.TestCase):
    def test_text_multiturn_system_and_developer_round_trip(self) -> None:
        original = sample_request()

        restored = CanonicalRequest.from_dict(original.to_dict())

        self.assertEqual(restored, original)
        self.assertEqual(
            [message.role for message in restored.messages],
            ["user", "assistant", "user"],
        )
        self.assertEqual(restored.system_blocks[0].text, "System rule.")
        self.assertEqual(restored.developer_blocks[0].text, "Developer rule.")

    def test_openai_and_anthropic_tools_map_to_same_canonical_tool(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "enum": ["one", "two"]},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }
        openai = CanonicalTool.from_openai(
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up a value.",
                    "parameters": copy.deepcopy(schema),
                },
            }
        )
        anthropic = CanonicalTool.from_anthropic(
            {
                "name": "lookup",
                "description": "Look up a value.",
                "input_schema": copy.deepcopy(schema),
            }
        )

        self.assertEqual(openai, anthropic)
        self.assertEqual(openai.to_anthropic()["input_schema"], schema)
        self.assertEqual(
            openai.to_openai_chat()["function"]["parameters"],
            schema,
        )
        with self.assertRaises(TypeError):
            openai.input_schema["type"] = "string"

    def test_tool_result_preserves_call_id_error_flag_and_block_order(self) -> None:
        result = CanonicalContentBlock.tool_result(
            "call-7",
            (
                text("first"),
                CanonicalContentBlock.image_url(
                    "https://images.example.test/result.png"
                ),
                text("last"),
            ),
            is_error=True,
        )
        request = sample_request()
        request = CanonicalRequest(
            **{
                **request.constructor_values(),
                "messages": (CanonicalMessage("user", (result,)),),
            }
        )

        restored = CanonicalRequest.from_dict(request.to_dict())
        block = restored.messages[0].content[0]

        self.assertEqual(block.tool_call_id, "call-7")
        self.assertTrue(block.is_error)
        self.assertEqual(
            [item.kind for item in block.result_content],
            ["text", "image_url", "text"],
        )

    def test_https_and_base64_images_are_validated_and_round_trip(self) -> None:
        raw = b"small-png-fixture"
        blocks = (
            CanonicalContentBlock.image_url(
                "https://cdn.example.test/image.webp?sig=fixture"
            ),
            CanonicalContentBlock.image_base64(
                "image/png",
                base64.b64encode(raw).decode("ascii"),
            ),
        )
        request = sample_request()
        request = CanonicalRequest(
            **{
                **request.constructor_values(),
                "messages": (CanonicalMessage("user", blocks),),
            }
        )

        restored = CanonicalRequest.from_dict(request.to_dict())

        self.assertEqual(restored, request)
        self.assertEqual(restored.messages[0].content[1].decoded_image_bytes(), raw)

    def test_invalid_or_oversized_images_and_image_count_are_rejected(self) -> None:
        cases = (
            (
                lambda: CanonicalContentBlock.image_url("http://example.test/a.png"),
                "invalid_image",
            ),
            (
                lambda: CanonicalContentBlock.image_base64("image/svg+xml", "AAAA"),
                "invalid_image",
            ),
            (
                lambda: CanonicalContentBlock.image_base64("image/png", "%%%"),
                "invalid_image",
            ),
            (
                lambda: CanonicalContentBlock.image_base64(
                    "image/png",
                    base64.b64encode(b"x" * (MAX_IMAGE_BYTES + 1)).decode("ascii"),
                ),
                "image_too_large",
            ),
        )
        for create, expected_code in cases:
            with self.subTest(code=expected_code):
                with self.assertRaises(ManagerError) as raised:
                    create()
                self.assertEqual(raised.exception.code, expected_code)

        request = sample_request()
        too_many = tuple(
            CanonicalContentBlock.image_url(
                f"https://images.example.test/{index}.png"
            )
            for index in range(MAX_IMAGES_PER_REQUEST + 1)
        )
        with self.assertRaises(ManagerError) as raised:
            CanonicalRequest(
                **{
                    **request.constructor_values(),
                    "messages": (CanonicalMessage("user", too_many),),
                }
            )
        self.assertEqual(raised.exception.code, "too_many_images")

    def test_unsupported_json_schema_keyword_has_deterministic_error(self) -> None:
        with self.assertRaises(ManagerError) as raised:
            CanonicalTool(
                name="lookup",
                description="Lookup",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "pattern": "secretly-unsupported",
                        }
                    },
                },
            )

        self.assertEqual(raised.exception.code, "unsupported_json_schema")
        self.assertEqual(raised.exception.details["keyword"], "pattern")
        self.assertEqual(raised.exception.details["path"], "$.properties.query")

    def test_unknown_ignorable_parameters_warn_and_semantic_parameters_fail(self) -> None:
        payload = sample_request().to_dict()
        payload["background"] = True

        parsed = CanonicalRequest.from_dict(payload)

        self.assertIn("ignored_parameter:background", parsed.warnings)

        payload = sample_request().to_dict()
        payload["response_format"] = {"type": "json_object"}
        with self.assertRaises(ManagerError) as raised:
            CanonicalRequest.from_dict(payload)
        self.assertEqual(raised.exception.code, "request_invalid")
        self.assertEqual(raised.exception.details["fields"], ["response_format"])

    def test_reasoning_effort_uses_requested_host_target_intersection(self) -> None:
        cases = (
            ("xhigh", {"low", "high", "xhigh"}, {"high", "xhigh"}, "xhigh"),
            ("xhigh", {"low", "high", "xhigh"}, {"high"}, "high"),
            ("low", {"low", "high"}, {"high"}, None),
            (None, {"low", "high"}, {"medium", "high"}, "high"),
            ("ultra", {"max", "ultra"}, {"max", "ultra"}, "ultra"),
        )
        for requested, host, target, expected in cases:
            with self.subTest(requested=requested, expected=expected):
                self.assertEqual(
                    resolve_target_effort(requested, host, target)[0],
                    expected,
                )


class CanonicalEventTests(unittest.TestCase):
    def test_event_sequence_keeps_block_index_and_tool_id_stable(self) -> None:
        events = CanonicalEventSequence("resp-1")
        started = events.emit(
            EventKind.TOOL_CALL_STARTED,
            block_index=2,
            tool_call_id="call-2",
            payload={"name": "lookup"},
        )
        delta = events.emit(
            EventKind.TOOL_CALL_ARGUMENTS_DELTA,
            block_index=2,
            tool_call_id="call-2",
            payload={"delta": '{"query":'},
        )
        completed = events.emit(
            EventKind.TOOL_CALL_COMPLETED,
            block_index=2,
            tool_call_id="call-2",
            payload={"arguments": '{"query":"one"}'},
        )

        self.assertEqual([started.sequence, delta.sequence, completed.sequence], [0, 1, 2])
        self.assertTrue(all(item.block_index == 2 for item in (started, delta, completed)))
        self.assertTrue(
            all(item.tool_call_id == "call-2" for item in (started, delta, completed))
        )
        self.assertEqual(CanonicalEvent.from_dict(delta.to_dict()), delta)

        with self.assertRaises(ManagerError) as raised:
            events.emit(
                EventKind.TOOL_CALL_ARGUMENTS_DELTA,
                block_index=2,
                tool_call_id="changed-call",
                payload={"delta": "}"},
            )
        self.assertEqual(raised.exception.code, "canonical_event_invalid")

    def test_first_delivered_visible_event_sets_committed_once(self) -> None:
        events = CanonicalEventSequence("resp-commit")
        started = events.emit(EventKind.RESPONSE_STARTED)
        text_delta = events.emit(
            EventKind.TEXT_DELTA,
            block_index=0,
            payload={"delta": "hello"},
        )
        usage = events.emit(
            EventKind.USAGE,
            payload=CanonicalUsage(
                input_tokens=10,
                output_tokens=2,
                total_tokens=12,
            ).to_dict(),
        )
        tracker = RequestCommitTracker()

        self.assertFalse(tracker.observe(started, delivered=False))
        self.assertFalse(tracker.committed)
        self.assertTrue(tracker.observe(text_delta, delivered=True))
        self.assertTrue(tracker.committed)
        self.assertEqual(tracker.committed_sequence, text_delta.sequence)
        self.assertFalse(tracker.observe(usage, delivered=True))
        self.assertEqual(tracker.committed_sequence, text_delta.sequence)


if __name__ == "__main__":
    unittest.main()
