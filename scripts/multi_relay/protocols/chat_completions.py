"""Canonical Chat Completions adapter plus legacy bridge compatibility exports."""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from ..canonical import (
    CanonicalContentBlock,
    CanonicalEvent,
    CanonicalEventSequence,
    CanonicalMessage,
    CanonicalRequest,
    CanonicalUsage,
    EventKind,
)
from ..errors import ManagerError
from .base import (
    ProviderErrorMetadata,
    discover_model_ids,
    extract_provider_error,
    iter_sse_json,
)


def _legacy_bridge() -> Any:
    return importlib.import_module("multi_relay.bridge")


def build_chat_request(*args: Any, **kwargs: Any) -> Any:
    """Compatibility call into the preserved legacy request implementation."""

    return _legacy_bridge()._legacy_build_chat_request(*args, **kwargs)


class ChatStreamTranslator:
    """Compatibility wrapper around the preserved legacy stream translator."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._delegate = _legacy_bridge()._LegacyChatStreamTranslator(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def start(self, *args: Any, **kwargs: Any) -> Any:
        return self._delegate.start(*args, **kwargs)

    def feed(self, *args: Any, **kwargs: Any) -> Any:
        return self._delegate.feed(*args, **kwargs)

    def finish(self, *args: Any, **kwargs: Any) -> Any:
        return self._delegate.finish(*args, **kwargs)


def _content_payload(blocks: tuple[CanonicalContentBlock, ...]) -> object:
    if all(block.kind == "text" for block in blocks):
        return "\n".join(block.text or "" for block in blocks)
    content: list[dict[str, object]] = []
    for block in blocks:
        if block.kind == "text":
            content.append({"type": "text", "text": block.text})
        elif block.kind == "image_url":
            content.append(
                {"type": "image_url", "image_url": {"url": block.url}}
            )
        elif block.kind == "image_base64":
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{block.media_type};base64,{block.data}"
                    },
                }
            )
    return content


def _tool_result_text(block: CanonicalContentBlock) -> str:
    parts: list[str] = []
    for item in block.result_content:
        if item.kind == "text":
            parts.append(item.text or "")
        elif item.kind == "image_url":
            parts.append(item.url or "")
        elif item.kind == "image_base64":
            parts.append(f"data:{item.media_type};base64,{item.data}")
    return "\n".join(parts)


def _chat_messages(request: CanonicalRequest) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    if request.system_blocks:
        messages.append(
            {"role": "system", "content": _content_payload(request.system_blocks)}
        )
    if request.developer_blocks:
        messages.append(
            {
                "role": "developer",
                "content": _content_payload(request.developer_blocks),
            }
        )
    for message in request.messages:
        ordinary = tuple(
            block
            for block in message.content
            if block.kind in {"text", "image_url", "image_base64"}
        )
        calls = tuple(block for block in message.content if block.kind == "tool_call")
        results = tuple(block for block in message.content if block.kind == "tool_result")
        if ordinary or calls:
            payload: dict[str, object] = {
                "role": message.role,
                "content": _content_payload(ordinary) if ordinary else None,
            }
            if calls:
                if message.role != "assistant":
                    raise ManagerError(
                        "request_invalid",
                        "Historical tool calls must use the assistant role.",
                    )
                payload["tool_calls"] = [
                    {
                        "id": block.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": block.tool_name,
                            "arguments": block.arguments,
                        },
                    }
                    for block in calls
                ]
            messages.append(payload)
        for result in results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "content": _tool_result_text(result),
                }
            )
    return messages


def _tool_choice(request: CanonicalRequest) -> object:
    choice = request.tool_choice
    if choice.mode in {"auto", "none", "required"}:
        return choice.mode
    return {"type": "function", "function": {"name": choice.name}}


def _usage(value: object) -> CanonicalUsage | None:
    if not isinstance(value, Mapping):
        return None
    prompt = value.get("prompt_tokens", value.get("input_tokens", 0))
    completion = value.get("completion_tokens", value.get("output_tokens", 0))
    total = value.get("total_tokens")
    if total is None and isinstance(prompt, int) and isinstance(completion, int):
        total = prompt + completion
    try:
        return CanonicalUsage(prompt, completion, total)  # type: ignore[arg-type]
    except (ManagerError, TypeError):
        raise ManagerError(
            "protocol_error",
            "Chat completion usage metadata is invalid.",
        ) from None


class _CanonicalChatTranslator:
    def __init__(self, response_id: str | None = None) -> None:
        self.response_id = response_id
        self.events: CanonicalEventSequence | None = None
        self.next_block = 0
        self.reasoning_block: int | None = None
        self.text_block: int | None = None
        self.tool_blocks: dict[int, int] = {}
        self.tool_ids: dict[int, str] = {}
        self.tool_names: dict[int, str] = {}
        self.tool_arguments: dict[int, str] = {}
        self.completed_tools: set[int] = set()
        self.closed_blocks: set[int] = set()
        self.finish_reason: str | None = None
        self.usage_emitted = False

    def _ensure_started(self, chunk: Mapping[str, object]) -> list[CanonicalEvent]:
        if self.events is not None:
            return []
        selected = self.response_id or (
            chunk.get("id") if isinstance(chunk.get("id"), str) else None
        ) or "chat-response"
        self.events = CanonicalEventSequence(selected)
        return [self.events.emit(EventKind.RESPONSE_STARTED)]

    def _start_block(self, kind: str) -> tuple[int, CanonicalEvent]:
        assert self.events is not None
        block = self.next_block
        self.next_block += 1
        return block, self.events.emit(
            EventKind.CONTENT_BLOCK_STARTED,
            block_index=block,
            payload={"kind": kind},
        )

    def feed(self, chunk: object) -> list[CanonicalEvent]:
        if not isinstance(chunk, Mapping):
            raise ManagerError("protocol_error", "Chat stream chunk must be an object.")
        output = self._ensure_started(chunk)
        assert self.events is not None
        choices = chunk.get("choices", [])
        if choices is None:
            choices = []
        if not isinstance(choices, (list, tuple)):
            raise ManagerError("protocol_error", "Chat stream choices must be an array.")
        first = choices[0] if choices and isinstance(choices[0], Mapping) else {}
        delta = first.get("delta", {}) if isinstance(first, Mapping) else {}
        if not isinstance(delta, Mapping):
            raise ManagerError("protocol_error", "Chat stream delta must be an object.")

        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            if self.reasoning_block is None:
                self.reasoning_block, started = self._start_block("reasoning")
                output.append(started)
            output.append(
                self.events.emit(
                    EventKind.REASONING_SUMMARY_DELTA,
                    block_index=self.reasoning_block,
                    payload={"delta": reasoning},
                )
            )
        content = delta.get("content")
        if isinstance(content, str) and content:
            if self.text_block is None:
                self.text_block, started = self._start_block("text")
                output.append(started)
            output.append(
                self.events.emit(
                    EventKind.TEXT_DELTA,
                    block_index=self.text_block,
                    payload={"delta": content},
                )
            )

        calls = delta.get("tool_calls", [])
        if calls is None:
            calls = []
        if not isinstance(calls, (list, tuple)):
            raise ManagerError("protocol_error", "Chat tool calls must be an array.")
        for position, raw_call in enumerate(calls):
            if not isinstance(raw_call, Mapping):
                raise ManagerError("protocol_error", "Chat tool call must be an object.")
            raw_index = raw_call.get("index", position)
            if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
                raise ManagerError("protocol_error", "Chat tool call index is invalid.")
            function = raw_call.get("function", {})
            if not isinstance(function, Mapping):
                raise ManagerError("protocol_error", "Chat tool function is invalid.")
            call_id = raw_call.get("id")
            name = function.get("name")
            if raw_index not in self.tool_blocks:
                if not isinstance(call_id, str) or not call_id:
                    call_id = f"call-{raw_index}"
                if not isinstance(name, str) or not name:
                    name = "unknown_tool"
                block, _ = self._start_block("tool_call")
                self.tool_blocks[raw_index] = block
                self.tool_ids[raw_index] = call_id
                self.tool_names[raw_index] = name
                self.tool_arguments[raw_index] = ""
                output.append(
                    self.events.emit(
                        EventKind.TOOL_CALL_STARTED,
                        block_index=block,
                        tool_call_id=call_id,
                        payload={"name": name},
                    )
                )
            elif isinstance(call_id, str) and call_id != self.tool_ids[raw_index]:
                raise ManagerError("protocol_error", "Chat tool call id changed mid-stream.")
            if isinstance(name, str) and name:
                current_name = self.tool_names[raw_index]
                if current_name == "unknown_tool":
                    self.tool_names[raw_index] = name
                elif current_name != name:
                    raise ManagerError("protocol_error", "Chat tool name changed mid-stream.")
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments:
                self.tool_arguments[raw_index] += arguments
                output.append(
                    self.events.emit(
                        EventKind.TOOL_CALL_ARGUMENTS_DELTA,
                        block_index=self.tool_blocks[raw_index],
                        tool_call_id=self.tool_ids[raw_index],
                        payload={"delta": arguments},
                    )
                )

        finish = first.get("finish_reason") if isinstance(first, Mapping) else None
        if isinstance(finish, str):
            self.finish_reason = finish
        selected_usage = _usage(chunk.get("usage"))
        if selected_usage is not None:
            output.append(
                self.events.emit(EventKind.USAGE, payload=selected_usage.to_dict())
            )
            self.usage_emitted = True
        return output

    def finish(self) -> list[CanonicalEvent]:
        if self.events is None:
            self.events = CanonicalEventSequence(self.response_id or "chat-response")
            output = [self.events.emit(EventKind.RESPONSE_STARTED)]
        else:
            output = []
        for index in sorted(self.tool_blocks):
            if index not in self.completed_tools:
                block = self.tool_blocks[index]
                output.append(
                    self.events.emit(
                        EventKind.TOOL_CALL_COMPLETED,
                        block_index=block,
                        tool_call_id=self.tool_ids[index],
                        payload={
                            "name": self.tool_names[index],
                            "arguments": self.tool_arguments[index],
                        },
                    )
                )
                self.completed_tools.add(index)
        block_ids = [
            block
            for block in (self.reasoning_block, self.text_block)
            if block is not None
        ] + [self.tool_blocks[index] for index in sorted(self.tool_blocks)]
        for block in block_ids:
            if block not in self.closed_blocks:
                output.append(
                    self.events.emit(
                        EventKind.CONTENT_BLOCK_COMPLETED,
                        block_index=block,
                        payload={},
                    )
                )
                self.closed_blocks.add(block)
        output.append(
            self.events.emit(
                EventKind.RESPONSE_COMPLETED,
                payload={"finish_reason": self.finish_reason},
            )
        )
        return output


class ChatCompletionsAdapter:
    protocol = "chat-completions-compatible"

    def __init__(self, *, max_sse_event_bytes: int = 1024 * 1024) -> None:
        self.max_sse_event_bytes = max_sse_event_bytes

    def build_request(
        self,
        request: CanonicalRequest,
        *,
        model: str,
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": _chat_messages(request),
            "stream": request.stream,
        }
        if request.tools:
            payload["tools"] = [item.to_openai_chat() for item in request.tools]
            payload["tool_choice"] = _tool_choice(request)
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop:
            payload["stop"] = list(request.stop)
        if request.seed is not None:
            payload["seed"] = request.seed
        if request.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = request.parallel_tool_calls
        if request.requested_reasoning_effort is not None:
            payload["reasoning_effort"] = request.requested_reasoning_effort
        if request.stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def parse_response(
        self,
        payload: object,
        *,
        response_id: str | None = None,
    ) -> tuple[CanonicalEvent, ...]:
        if not isinstance(payload, Mapping):
            raise ManagerError("protocol_error", "Chat completion must be an object.")
        choices = payload.get("choices")
        if not isinstance(choices, (list, tuple)) or not choices or not isinstance(
            choices[0], Mapping
        ):
            raise ManagerError("protocol_error", "Chat completion has no choice.")
        first = choices[0]
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise ManagerError("protocol_error", "Chat completion has no message.")
        calls = message.get("tool_calls")
        indexed_calls = []
        if isinstance(calls, (list, tuple)):
            indexed_calls = [
                {**dict(call), "index": index}
                for index, call in enumerate(calls)
                if isinstance(call, Mapping)
            ]
        chunk = {
            "id": payload.get("id"),
            "choices": [
                {
                    "delta": {
                        "reasoning_content": message.get("reasoning_content"),
                        "content": message.get("content"),
                        "tool_calls": indexed_calls,
                    },
                    "finish_reason": first.get("finish_reason"),
                }
            ],
            "usage": payload.get("usage"),
        }
        translator = _CanonicalChatTranslator(response_id)
        return tuple(translator.feed(chunk) + translator.finish())

    def iter_events(
        self,
        chunks: Iterable[bytes | str],
        *,
        response_id: str | None = None,
    ) -> Iterator[CanonicalEvent]:
        translator = _CanonicalChatTranslator(response_id)
        decoded = iter_sse_json(
            chunks,
            max_event_bytes=self.max_sse_event_bytes,
        )
        try:
            for payload in decoded:
                for event in translator.feed(payload):
                    yield event
            for event in translator.finish():
                yield event
        finally:
            decoded.close()

    def classify_error_metadata(
        self,
        payload: object,
        headers: Mapping[str, str],
    ) -> ProviderErrorMetadata:
        del headers
        return extract_provider_error(payload)

    def extract_error(
        self,
        payload: object,
        headers: Mapping[str, str],
    ) -> ProviderErrorMetadata:
        return self.classify_error_metadata(payload, headers)

    def discover_models(self, payload: object) -> tuple[str, ...]:
        return discover_model_ids(payload)


__all__ = [
    "ChatCompletionsAdapter",
    "ChatStreamTranslator",
    "build_chat_request",
]
