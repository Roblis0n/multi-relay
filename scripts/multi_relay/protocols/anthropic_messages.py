"""Anthropic Messages ingress, upstream, and host-stream adapters."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from ..canonical import (
    CanonicalContentBlock,
    CanonicalEvent,
    CanonicalEventSequence,
    CanonicalMessage,
    CanonicalRequest,
    CanonicalTool,
    CanonicalUsage,
    EventKind,
    ToolChoice,
)
from ..errors import ManagerError
from .base import (
    ProviderErrorMetadata,
    discover_model_ids,
    extract_provider_error,
    iter_sse_json,
)


ANTHROPIC_VERSION = "2023-06-01"
_SUPPORTED_BETAS = frozenset(
    {
        "fine-grained-tool-streaming-2025-05-14",
        "prompt-caching-2024-07-31",
        "token-efficient-tools-2025-02-19",
    }
)
_REQUEST_FIELDS = frozenset(
    {
        "model",
        "max_tokens",
        "messages",
        "system",
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "stream",
        "stop_sequences",
        "metadata",
        "thinking",
    }
)
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_WIRE_TO_CANONICAL_STOP = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop_sequence",
    "refusal": "content_filter",
    "pause_turn": "pause_turn",
}
_CANONICAL_TO_WIRE_STOP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "stop_sequence": "stop_sequence",
    "content_filter": "refusal",
    "pause_turn": "pause_turn",
}


def _invalid(message: str, **details: object) -> ManagerError:
    return ManagerError("request_invalid", message, details or None)


def _protocol_error(message: str, **details: object) -> ManagerError:
    return ManagerError("protocol_error", message, details or None)


def _strict_object(
    value: object,
    allowed: frozenset[str],
    label: str,
    *,
    protocol: bool = False,
) -> Mapping[str, object]:
    error = _protocol_error if protocol else _invalid
    if not isinstance(value, Mapping):
        raise error(f"{label} must be an object.")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise error(f"{label} contains unsupported fields.", fields=unknown)
    return value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    selected = name.casefold()
    for key, value in headers.items():
        if (
            isinstance(key, str)
            and key.casefold() == selected
            and isinstance(value, str)
            and value.strip()
        ):
            return value.strip()
    return None


def _validate_headers(headers: Mapping[str, str]) -> None:
    if not isinstance(headers, Mapping):
        raise _invalid("Anthropic request headers must be an object.")
    version = _header(headers, "anthropic-version")
    if version is None:
        raise ManagerError(
            "anthropic_version_required",
            "Anthropic requests require the anthropic-version header.",
        )
    if version != ANTHROPIC_VERSION:
        raise ManagerError(
            "unsupported_anthropic_version",
            "The requested Anthropic API version is unsupported.",
            {"supported_version": ANTHROPIC_VERSION},
        )
    authorization = _header(headers, "authorization")
    api_key = _header(headers, "x-api-key")
    bearer_ok = (
        authorization is not None
        and authorization.casefold().startswith("bearer ")
        and bool(authorization[7:].strip())
    )
    if not api_key and not bearer_ok:
        raise ManagerError(
            "local_auth_required",
            "Anthropic requests require a local x-api-key or Bearer token.",
        )
    beta_header = _header(headers, "anthropic-beta")
    if beta_header is None:
        return
    requested = tuple(
        item.strip() for item in beta_header.split(",") if item.strip()
    )
    unknown = sorted(set(requested) - _SUPPORTED_BETAS)
    if unknown:
        raise ManagerError(
            "unsupported_anthropic_beta",
            "The request uses an unsupported Anthropic beta.",
            {"betas": unknown},
        )


def _json_object(value: object, label: str, *, protocol: bool = False) -> str:
    error = _protocol_error if protocol else _invalid
    if not isinstance(value, Mapping):
        raise error(f"{label} must be a JSON object.")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError):
        raise error(f"{label} must contain valid JSON values.") from None


def _load_json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise _invalid(f"{label} must be a JSON string.")
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        raise _invalid(f"{label} contains invalid JSON.") from None
    if not isinstance(decoded, dict):
        raise _invalid(f"{label} must decode to a JSON object.")
    return decoded


def _text_block(value: object, label: str) -> CanonicalContentBlock:
    data = _strict_object(value, frozenset({"type", "text"}), label)
    if data.get("type") != "text" or not isinstance(data.get("text"), str):
        raise _invalid(f"{label} requires string text.")
    return CanonicalContentBlock.text_block(data["text"])  # type: ignore[arg-type]


def _system_blocks(value: object) -> tuple[CanonicalContentBlock, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (CanonicalContentBlock.text_block(value),)
    if not isinstance(value, (list, tuple)):
        raise _invalid("Anthropic system must be text or an array of text blocks.")
    return tuple(
        _text_block(item, f"Anthropic system block {index}")
        for index, item in enumerate(value)
    )


def _content_blocks(
    value: object,
    *,
    label: str,
    allow_empty: bool = False,
    nested_result: bool = False,
) -> tuple[CanonicalContentBlock, ...]:
    if isinstance(value, str):
        return (CanonicalContentBlock.text_block(value),)
    if not isinstance(value, (list, tuple)):
        raise _invalid(f"{label} must be text or an array.")
    blocks: list[CanonicalContentBlock] = []
    for index, raw in enumerate(value):
        item_label = f"{label} block {index}"
        if not isinstance(raw, Mapping):
            raise _invalid(f"{item_label} must be an object.")
        kind = raw.get("type")
        if kind == "text":
            blocks.append(_text_block(raw, item_label))
            continue
        if kind in {"thinking", "redacted_thinking"}:
            raise ManagerError(
                "unsupported_thinking",
                "Anthropic thinking blocks are not exposed as ordinary content.",
            )
        if kind == "image":
            data = _strict_object(raw, frozenset({"type", "source"}), item_label)
            source = _strict_object(
                data.get("source"),
                frozenset({"type", "media_type", "data", "url"}),
                f"{item_label} source",
            )
            source_type = source.get("type")
            if source_type == "base64":
                source = _strict_object(
                    source,
                    frozenset({"type", "media_type", "data"}),
                    f"{item_label} source",
                )
                blocks.append(
                    CanonicalContentBlock.image_base64(
                        source.get("media_type"),  # type: ignore[arg-type]
                        source.get("data"),  # type: ignore[arg-type]
                    )
                )
            elif source_type == "url":
                source = _strict_object(
                    source,
                    frozenset({"type", "url"}),
                    f"{item_label} source",
                )
                blocks.append(
                    CanonicalContentBlock.image_url(
                        source.get("url")  # type: ignore[arg-type]
                    )
                )
            else:
                raise _invalid(f"Unsupported Anthropic image source: {source_type}.")
            continue
        if kind == "tool_use":
            if nested_result:
                raise _invalid("Anthropic tool results cannot contain tool_use blocks.")
            data = _strict_object(
                raw,
                frozenset({"type", "id", "name", "input"}),
                item_label,
            )
            blocks.append(
                CanonicalContentBlock.tool_call(
                    data.get("id"),  # type: ignore[arg-type]
                    data.get("name"),  # type: ignore[arg-type]
                    _json_object(data.get("input"), f"{item_label} input"),
                )
            )
            continue
        if kind == "tool_result":
            if nested_result:
                raise _invalid("Anthropic tool results cannot be nested.")
            data = _strict_object(
                raw,
                frozenset({"type", "tool_use_id", "content", "is_error"}),
                item_label,
            )
            result = _content_blocks(
                data.get("content", ""),
                label=f"{item_label} content",
                allow_empty=True,
                nested_result=True,
            )
            blocks.append(
                CanonicalContentBlock.tool_result(
                    data.get("tool_use_id"),  # type: ignore[arg-type]
                    result,
                    is_error=data.get("is_error", False),  # type: ignore[arg-type]
                )
            )
            continue
        raise _invalid(f"Unsupported Anthropic content block: {kind}.")
    if not blocks and not allow_empty:
        raise _invalid(f"{label} cannot be empty.")
    return tuple(blocks)


def _tool_choice(value: object) -> tuple[ToolChoice, bool | None]:
    if value is None:
        return ToolChoice.auto(), None
    data = _strict_object(
        value,
        frozenset({"type", "name", "disable_parallel_tool_use"}),
        "Anthropic tool_choice",
    )
    selected = data.get("type")
    if selected == "auto":
        choice = ToolChoice.auto()
    elif selected == "any":
        choice = ToolChoice("required")
    elif selected == "none":
        choice = ToolChoice("none")
    elif selected == "tool":
        choice = ToolChoice("tool", data.get("name"))  # type: ignore[arg-type]
    else:
        raise _invalid(f"Unsupported Anthropic tool_choice: {selected}.")
    raw_parallel = data.get("disable_parallel_tool_use")
    if raw_parallel is None:
        parallel = None
    elif isinstance(raw_parallel, bool):
        parallel = not raw_parallel
    else:
        raise _invalid("disable_parallel_tool_use must be boolean.")
    return choice, parallel


def _metadata(value: object) -> dict[str, object]:
    if value is None:
        return {}
    data = _strict_object(value, frozenset({"user_id"}), "Anthropic metadata")
    user_id = data.get("user_id")
    if user_id is None:
        return {}
    if not isinstance(user_id, str) or not user_id:
        raise _invalid("Anthropic metadata user_id must be non-empty text.")
    return {"user_id": user_id}


def _validate_stop_sequences(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise _invalid("Anthropic stop_sequences must be an array.")
    result = tuple(value)
    if len(result) > 4:
        raise _invalid("Anthropic stop_sequences supports at most four values.")
    if any(not isinstance(item, str) or not item for item in result):
        raise _invalid("Anthropic stop_sequences requires non-empty strings.")
    return result  # type: ignore[return-value]


def _wire_content(blocks: tuple[CanonicalContentBlock, ...]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for block in blocks:
        if block.kind == "text":
            result.append({"type": "text", "text": block.text})
        elif block.kind == "image_base64":
            result.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.media_type,
                        "data": block.data,
                    },
                }
            )
        elif block.kind == "image_url":
            result.append(
                {
                    "type": "image",
                    "source": {"type": "url", "url": block.url},
                }
            )
        elif block.kind == "tool_call":
            result.append(
                {
                    "type": "tool_use",
                    "id": block.tool_call_id,
                    "name": block.tool_name,
                    "input": _load_json_object(
                        block.arguments,
                        "Canonical tool call arguments",
                    ),
                }
            )
        elif block.kind == "tool_result":
            result.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_call_id,
                    "content": _wire_content(block.result_content),
                    "is_error": block.is_error,
                }
            )
        else:
            raise _invalid(f"Unsupported canonical content kind: {block.kind}.")
    return result


def _wire_tool_choice(request: CanonicalRequest) -> dict[str, object] | None:
    choice = request.tool_choice
    if choice.mode == "none":
        return None
    if choice.mode == "auto":
        payload: dict[str, object] = {"type": "auto"}
    elif choice.mode == "required":
        payload = {"type": "any"}
    elif choice.mode == "tool":
        payload = {"type": "tool", "name": choice.name}
    else:
        raise _invalid(f"Unsupported canonical tool choice: {choice.mode}.")
    if request.parallel_tool_calls is not None:
        payload["disable_parallel_tool_use"] = not request.parallel_tool_calls
    return payload


def _usage_values(value: object) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _protocol_error("Anthropic usage must be an object.")
    result: dict[str, int] = {}
    for name in _USAGE_FIELDS:
        raw = value.get(name)
        if raw is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise _protocol_error(f"Anthropic usage {name} is invalid.")
        result[name] = raw
    return result


def _canonical_usage(values: Mapping[str, int]) -> CanonicalUsage:
    input_tokens = values.get("input_tokens", 0)
    output_tokens = values.get("output_tokens", 0)
    cache_creation = values.get("cache_creation_input_tokens", 0)
    cache_read = values.get("cache_read_input_tokens", 0)
    return CanonicalUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens + cache_creation + cache_read,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


def _canonical_stop(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _WIRE_TO_CANONICAL_STOP:
        raise _protocol_error(f"Unsupported Anthropic stop reason: {value}.")
    return _WIRE_TO_CANONICAL_STOP[value]


class AnthropicInboundAdapter:
    """Parse the local Claude-compatible ``POST /v1/messages`` surface."""

    protocol = "anthropic-messages"

    def parse_request(
        self,
        payload: object,
        *,
        headers: Mapping[str, str],
        request_id: str,
        pool_id: str,
        host: str = "claude-code",
    ) -> CanonicalRequest:
        _validate_headers(headers)
        data = _strict_object(payload, _REQUEST_FIELDS, "Anthropic request")
        if data.get("thinking") is not None:
            raise ManagerError(
                "unsupported_thinking",
                "Anthropic extended thinking requires an explicit supported capability.",
            )
        model = data.get("model")
        if not isinstance(model, str) or not model:
            raise _invalid("Anthropic request requires a model alias.")
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, (list, tuple)) or not raw_messages:
            raise _invalid("Anthropic request requires a non-empty messages array.")
        messages: list[CanonicalMessage] = []
        for index, raw in enumerate(raw_messages):
            message = _strict_object(
                raw,
                frozenset({"role", "content"}),
                f"Anthropic message {index}",
            )
            role = message.get("role")
            if role not in {"user", "assistant"}:
                raise _invalid(f"Unsupported Anthropic message role: {role}.")
            blocks = _content_blocks(
                message.get("content"),
                label=f"Anthropic message {index} content",
            )
            if role == "assistant" and any(
                block.kind in {"image_url", "image_base64", "tool_result"}
                for block in blocks
            ):
                raise _invalid(
                    "Anthropic assistant history contains a user-only content block."
                )
            if role == "user" and any(block.kind == "tool_call" for block in blocks):
                raise _invalid("Anthropic user history cannot contain tool_use blocks.")
            messages.append(CanonicalMessage(role, blocks))  # type: ignore[arg-type]

        raw_tools = data.get("tools", [])
        if not isinstance(raw_tools, (list, tuple)):
            raise _invalid("Anthropic tools must be an array.")
        choice, parallel = _tool_choice(data.get("tool_choice"))
        max_tokens = data.get("max_tokens")
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens <= 0
        ):
            raise _invalid("Anthropic max_tokens must be a positive integer.")
        temperature = data.get("temperature")
        if temperature is not None and (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(float(temperature))
            or not 0 <= float(temperature) <= 1
        ):
            raise _invalid("Anthropic temperature must be between zero and one.")
        return CanonicalRequest(
            request_id=request_id,
            host=host,
            model_alias=model,
            pool_id=pool_id,
            system_blocks=_system_blocks(data.get("system")),
            messages=tuple(messages),
            tools=tuple(CanonicalTool.from_anthropic(item) for item in raw_tools),
            tool_choice=choice,
            max_output_tokens=max_tokens,
            temperature=temperature,  # type: ignore[arg-type]
            top_p=data.get("top_p"),  # type: ignore[arg-type]
            stop=_validate_stop_sequences(data.get("stop_sequences")),
            parallel_tool_calls=parallel,
            stream=data.get("stream", False),  # type: ignore[arg-type]
            metadata=_metadata(data.get("metadata")),
        )


@dataclass
class _OpenBlock:
    kind: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: str = ""


class _AnthropicStreamTranslator:
    def __init__(self, response_id: str | None = None) -> None:
        self.requested_response_id = response_id
        self.events: CanonicalEventSequence | None = None
        self.blocks: dict[int, _OpenBlock] = {}
        self.closed_blocks: set[int] = set()
        self.usage: dict[str, int] = {}
        self.finish_reason: str | None = None
        self.stop_sequence: str | None = None
        self.started = False
        self.completed = False

    def _ensure_sequence(self, payload: Mapping[str, object]) -> CanonicalEventSequence:
        if self.events is None:
            message = payload.get("message")
            wire_id = message.get("id") if isinstance(message, Mapping) else None
            selected = (
                self.requested_response_id
                or (wire_id if isinstance(wire_id, str) and wire_id else None)
                or "anthropic-stream"
            )
            self.events = CanonicalEventSequence(selected)
        return self.events

    @staticmethod
    def _index(payload: Mapping[str, object]) -> int:
        index = payload.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise _protocol_error("Anthropic content block index is invalid.")
        return index

    def feed(self, payload: object) -> list[CanonicalEvent]:
        if not isinstance(payload, Mapping):
            raise _protocol_error("Anthropic stream event must be an object.")
        if self.completed:
            raise _protocol_error("Anthropic stream continued after message_stop.")
        kind = payload.get("type")
        if not isinstance(kind, str):
            raise _protocol_error("Anthropic stream event type is missing.")
        if kind == "ping":
            return []
        if kind == "error":
            sequence = self._ensure_sequence(payload)
            self.completed = True
            metadata = extract_provider_error(payload)
            return [
                sequence.emit(
                    EventKind.ERROR,
                    payload={
                        "code": metadata.code or "upstream_error",
                        "message": metadata.message or "Anthropic upstream error.",
                    },
                )
            ]

        sequence = self._ensure_sequence(payload)
        output: list[CanonicalEvent] = []
        if kind == "message_start":
            if self.started:
                raise _protocol_error("Anthropic stream contains duplicate message_start.")
            message = payload.get("message")
            if not isinstance(message, Mapping):
                raise _protocol_error("Anthropic message_start is missing message data.")
            self.started = True
            self.usage.update(_usage_values(message.get("usage")))
            output.append(
                sequence.emit(
                    EventKind.RESPONSE_STARTED,
                    payload={"model": message.get("model")}
                    if isinstance(message.get("model"), str)
                    else {},
                )
            )
            return output
        if not self.started:
            self.started = True
            output.append(sequence.emit(EventKind.RESPONSE_STARTED))

        if kind == "content_block_start":
            index = self._index(payload)
            if index in self.blocks or index in self.closed_blocks:
                raise _protocol_error("Anthropic content block index was reused.")
            block = payload.get("content_block")
            if not isinstance(block, Mapping):
                raise _protocol_error("Anthropic content_block_start is invalid.")
            block_type = block.get("type")
            if block_type in {"thinking", "redacted_thinking"}:
                raise ManagerError(
                    "unsupported_thinking",
                    "Anthropic thinking blocks are not exposed as ordinary content.",
                )
            if block_type == "text":
                initial = block.get("text", "")
                if not isinstance(initial, str):
                    raise _protocol_error("Anthropic text block is invalid.")
                self.blocks[index] = _OpenBlock("text")
                output.append(
                    sequence.emit(
                        EventKind.CONTENT_BLOCK_STARTED,
                        block_index=index,
                        payload={"kind": "text"},
                    )
                )
                if initial:
                    output.append(
                        sequence.emit(
                            EventKind.TEXT_DELTA,
                            block_index=index,
                            payload={"delta": initial},
                        )
                    )
                return output
            if block_type == "tool_use":
                tool_id = block.get("id")
                name = block.get("name")
                if not isinstance(tool_id, str) or not tool_id:
                    raise _protocol_error("Anthropic tool_use id is missing.")
                if not isinstance(name, str) or not name:
                    raise _protocol_error("Anthropic tool_use name is missing.")
                initial_input = block.get("input", {})
                initial = _json_object(
                    initial_input,
                    "Anthropic tool_use input",
                    protocol=True,
                )
                if initial == "{}":
                    initial = ""
                self.blocks[index] = _OpenBlock(
                    "tool_call",
                    tool_call_id=tool_id,
                    tool_name=name,
                    arguments=initial,
                )
                output.extend(
                    [
                        sequence.emit(
                            EventKind.CONTENT_BLOCK_STARTED,
                            block_index=index,
                            payload={"kind": "tool_call"},
                        ),
                        sequence.emit(
                            EventKind.TOOL_CALL_STARTED,
                            block_index=index,
                            tool_call_id=tool_id,
                            payload={"name": name},
                        ),
                    ]
                )
                if initial:
                    output.append(
                        sequence.emit(
                            EventKind.TOOL_CALL_ARGUMENTS_DELTA,
                            block_index=index,
                            tool_call_id=tool_id,
                            payload={"delta": initial},
                        )
                    )
                return output
            raise _protocol_error(
                f"Unsupported Anthropic content block type: {block_type}."
            )

        if kind == "content_block_delta":
            index = self._index(payload)
            state = self.blocks.get(index)
            if state is None:
                raise _protocol_error("Anthropic delta references an unopened block.")
            delta = payload.get("delta")
            if not isinstance(delta, Mapping):
                raise _protocol_error("Anthropic content block delta is invalid.")
            delta_type = delta.get("type")
            if delta_type in {"thinking_delta", "signature_delta"}:
                raise ManagerError(
                    "unsupported_thinking",
                    "Anthropic thinking deltas are not exposed as ordinary content.",
                )
            if delta_type == "text_delta" and state.kind == "text":
                text = delta.get("text")
                if not isinstance(text, str):
                    raise _protocol_error("Anthropic text_delta is invalid.")
                output.append(
                    sequence.emit(
                        EventKind.TEXT_DELTA,
                        block_index=index,
                        payload={"delta": text},
                    )
                )
                return output
            if delta_type == "input_json_delta" and state.kind == "tool_call":
                fragment = delta.get("partial_json")
                if not isinstance(fragment, str):
                    raise _protocol_error("Anthropic input_json_delta is invalid.")
                state.arguments += fragment
                output.append(
                    sequence.emit(
                        EventKind.TOOL_CALL_ARGUMENTS_DELTA,
                        block_index=index,
                        tool_call_id=state.tool_call_id,
                        payload={"delta": fragment},
                    )
                )
                return output
            raise _protocol_error(
                "Anthropic delta type does not match its content block."
            )

        if kind == "content_block_stop":
            index = self._index(payload)
            state = self.blocks.pop(index, None)
            if state is None:
                raise _protocol_error("Anthropic stop references an unopened block.")
            if state.kind == "tool_call":
                arguments = state.arguments or "{}"
                try:
                    parsed = json.loads(arguments)
                except (json.JSONDecodeError, RecursionError):
                    raise _protocol_error(
                        "Anthropic tool input_json_delta did not form valid JSON."
                    ) from None
                if not isinstance(parsed, dict):
                    raise _protocol_error("Anthropic tool input must be a JSON object.")
                output.append(
                    sequence.emit(
                        EventKind.TOOL_CALL_COMPLETED,
                        block_index=index,
                        tool_call_id=state.tool_call_id,
                        payload={
                            "name": state.tool_name,
                            "arguments": arguments,
                        },
                    )
                )
            output.append(
                sequence.emit(
                    EventKind.CONTENT_BLOCK_COMPLETED,
                    block_index=index,
                )
            )
            self.closed_blocks.add(index)
            return output

        if kind == "message_delta":
            delta = payload.get("delta")
            if not isinstance(delta, Mapping):
                raise _protocol_error("Anthropic message_delta is invalid.")
            if "stop_reason" in delta:
                self.finish_reason = _canonical_stop(delta.get("stop_reason"))
            stop_sequence = delta.get("stop_sequence")
            if stop_sequence is not None and not isinstance(stop_sequence, str):
                raise _protocol_error("Anthropic stop_sequence is invalid.")
            self.stop_sequence = stop_sequence
            self.usage.update(_usage_values(payload.get("usage")))
            return output

        if kind == "message_stop":
            if self.blocks:
                raise _protocol_error("Anthropic message stopped with open content blocks.")
            if self.usage:
                output.append(
                    sequence.emit(
                        EventKind.USAGE,
                        payload=_canonical_usage(self.usage).to_dict(),
                    )
                )
            completion: dict[str, object] = {
                "finish_reason": self.finish_reason
            }
            if self.stop_sequence is not None:
                completion["stop_sequence"] = self.stop_sequence
            output.append(
                sequence.emit(EventKind.RESPONSE_COMPLETED, payload=completion)
            )
            self.completed = True
            return output

        raise _protocol_error(f"Unsupported Anthropic stream event: {kind}.")

    def finish(self) -> None:
        if self.events is not None and not self.completed:
            raise _protocol_error("Anthropic stream ended before message_stop.")


class AnthropicUpstreamAdapter:
    """Translate canonical requests to and from an Anthropic Messages upstream."""

    protocol = "anthropic-messages"

    def __init__(self, *, max_sse_event_bytes: int = 1024 * 1024) -> None:
        self.max_sse_event_bytes = max_sse_event_bytes

    def build_request(
        self,
        request: CanonicalRequest,
        *,
        model: str,
    ) -> Mapping[str, Any]:
        if not isinstance(model, str) or not model:
            raise _invalid("Anthropic upstream requires a model.")
        if request.max_output_tokens is None:
            raise _invalid("Anthropic upstream requires max_output_tokens.")
        if request.temperature is not None and not 0 <= float(request.temperature) <= 1:
            raise _invalid("Anthropic temperature must be between zero and one.")
        if len(request.stop) > 4:
            raise _invalid("Anthropic stop_sequences supports at most four values.")
        if request.seed is not None:
            raise _invalid("Anthropic Messages does not support deterministic seed.")
        if request.requested_reasoning_effort is not None:
            raise ManagerError(
                "unsupported_thinking",
                "Reasoning effort cannot be mapped to Anthropic thinking implicitly.",
            )
        messages: list[dict[str, object]] = []
        for message in request.messages:
            if message.role == "assistant" and any(
                block.kind in {"image_url", "image_base64", "tool_result"}
                for block in message.content
            ):
                raise _invalid(
                    "Anthropic assistant history contains a user-only content block."
                )
            if message.role == "user" and any(
                block.kind == "tool_call" for block in message.content
            ):
                raise _invalid("Anthropic user history cannot contain tool calls.")
            messages.append(
                {"role": message.role, "content": _wire_content(message.content)}
            )
        if not messages:
            raise _invalid("Anthropic upstream requires at least one message.")
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_output_tokens,
            "messages": messages,
            "stream": request.stream,
        }
        instructions = request.system_blocks + request.developer_blocks
        if instructions:
            payload["system"] = _wire_content(instructions)
        if request.tools and request.tool_choice.mode != "none":
            payload["tools"] = [item.to_anthropic() for item in request.tools]
            selected_choice = _wire_tool_choice(request)
            if selected_choice is not None:
                payload["tool_choice"] = selected_choice
        elif request.tool_choice.mode in {"required", "tool"}:
            raise _invalid("Anthropic tool choice requires declared tools.")
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.stop:
            payload["stop_sequences"] = list(request.stop)
        if request.metadata:
            unknown = sorted(set(request.metadata) - {"user_id"})
            if unknown:
                raise _invalid(
                    "Anthropic upstream cannot preserve canonical metadata fields.",
                    fields=unknown,
                )
            payload["metadata"] = dict(request.metadata)
        return payload

    def parse_response(
        self,
        payload: object,
        *,
        response_id: str | None = None,
    ) -> tuple[CanonicalEvent, ...]:
        if not isinstance(payload, Mapping):
            raise _protocol_error("Anthropic response must be an object.")
        if payload.get("type") == "error":
            translator = _AnthropicStreamTranslator(response_id)
            return tuple(translator.feed(payload))
        if payload.get("type") not in {None, "message"}:
            raise _protocol_error("Anthropic response type is invalid.")
        content = payload.get("content")
        if not isinstance(content, (list, tuple)):
            raise _protocol_error("Anthropic response content must be an array.")
        translator = _AnthropicStreamTranslator(response_id)
        events: list[CanonicalEvent] = []
        events.extend(
            translator.feed(
                {
                    "type": "message_start",
                    "message": {
                        "id": payload.get("id"),
                        "model": payload.get("model"),
                        "usage": payload.get("usage", {}),
                    },
                }
            )
        )
        for index, raw in enumerate(content):
            if not isinstance(raw, Mapping):
                raise _protocol_error("Anthropic response content block is invalid.")
            block_type = raw.get("type")
            if block_type in {"thinking", "redacted_thinking"}:
                raise ManagerError(
                    "unsupported_thinking",
                    "Anthropic thinking blocks are not exposed as ordinary content.",
                )
            if block_type == "text":
                text = raw.get("text")
                if not isinstance(text, str):
                    raise _protocol_error("Anthropic response text block is invalid.")
                events.extend(
                    translator.feed(
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": {"type": "text", "text": ""},
                        }
                    )
                )
                if text:
                    events.extend(
                        translator.feed(
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {"type": "text_delta", "text": text},
                            }
                        )
                    )
            elif block_type == "tool_use":
                tool_id = raw.get("id")
                name = raw.get("name")
                arguments = _json_object(
                    raw.get("input"),
                    "Anthropic response tool input",
                    protocol=True,
                )
                events.extend(
                    translator.feed(
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": name,
                                "input": {},
                            },
                        }
                    )
                )
                events.extend(
                    translator.feed(
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": arguments,
                            },
                        }
                    )
                )
            else:
                raise _protocol_error(
                    f"Unsupported Anthropic response content block: {block_type}."
                )
            events.extend(
                translator.feed({"type": "content_block_stop", "index": index})
            )
        events.extend(
            translator.feed(
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": payload.get("stop_reason"),
                        "stop_sequence": payload.get("stop_sequence"),
                    },
                    "usage": payload.get("usage", {}),
                }
            )
        )
        events.extend(translator.feed({"type": "message_stop"}))
        return tuple(events)

    def iter_events(
        self,
        chunks: Iterable[bytes | str],
        *,
        response_id: str | None = None,
    ) -> Iterator[CanonicalEvent]:
        decoded = iter_sse_json(chunks, max_event_bytes=self.max_sse_event_bytes)
        translator = _AnthropicStreamTranslator(response_id)
        try:
            for payload in decoded:
                for event in translator.feed(payload):
                    yield event
            translator.finish()
        finally:
            decoded.close()

    def classify_error_metadata(
        self,
        payload: object,
        headers: Mapping[str, str],
    ) -> ProviderErrorMetadata:
        metadata = extract_provider_error(payload)
        retry_after: float | None = metadata.retry_after_seconds
        header = _header(headers, "retry-after")
        if retry_after is None and header is not None:
            try:
                candidate = float(header)
            except ValueError:
                candidate = -1
            if math.isfinite(candidate) and candidate >= 0:
                retry_after = candidate
        return ProviderErrorMetadata(
            code=metadata.code,
            error_type=metadata.error_type,
            message=metadata.message,
            retry_after_seconds=retry_after,
            details=metadata.details,
        )

    def extract_error(
        self,
        payload: object,
        headers: Mapping[str, str],
    ) -> ProviderErrorMetadata:
        return self.classify_error_metadata(payload, headers)

    def discover_models(self, payload: object) -> tuple[str, ...]:
        return discover_model_ids(payload)


class AnthropicOutboundRenderer:
    """Render canonical events as Anthropic Messages streaming payloads."""

    def __init__(self, *, model: str = "relay") -> None:
        self.model = model
        self.usage: CanonicalUsage | None = None
        self.block_kinds: dict[int, str] = {}
        self.tool_arguments: dict[int, str] = {}

    def render(self, event: CanonicalEvent) -> tuple[dict[str, object], ...]:
        if event.kind is EventKind.RESPONSE_STARTED:
            selected_model = event.payload.get("model", self.model)
            if not isinstance(selected_model, str) or not selected_model:
                selected_model = self.model
            return (
                {
                    "type": "message_start",
                    "message": {
                        "id": event.response_id,
                        "type": "message",
                        "role": "assistant",
                        "model": selected_model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
            )
        if event.kind is EventKind.CONTENT_BLOCK_STARTED:
            assert event.block_index is not None
            kind = event.payload.get("kind")
            if kind == "reasoning":
                raise ManagerError(
                    "unsupported_thinking",
                    "Canonical reasoning cannot be rendered as ordinary Anthropic text.",
                )
            if kind == "tool_call":
                self.block_kinds[event.block_index] = "tool_call"
                return ()
            if kind != "text":
                raise _protocol_error(
                    f"Unsupported canonical Anthropic content kind: {kind}."
                )
            self.block_kinds[event.block_index] = "text"
            return (
                {
                    "type": "content_block_start",
                    "index": event.block_index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        if event.kind is EventKind.TEXT_DELTA:
            return (
                {
                    "type": "content_block_delta",
                    "index": event.block_index,
                    "delta": {
                        "type": "text_delta",
                        "text": event.payload.get("delta", ""),
                    },
                },
            )
        if event.kind is EventKind.REASONING_SUMMARY_DELTA:
            raise ManagerError(
                "unsupported_thinking",
                "Canonical reasoning cannot be rendered as ordinary Anthropic text.",
            )
        if event.kind is EventKind.TOOL_CALL_STARTED:
            assert event.block_index is not None
            name = event.payload.get("name")
            if not isinstance(name, str) or not name:
                raise _protocol_error("Canonical tool call name is missing.")
            self.block_kinds[event.block_index] = "tool_call"
            self.tool_arguments[event.block_index] = ""
            return (
                {
                    "type": "content_block_start",
                    "index": event.block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": event.tool_call_id,
                        "name": name,
                        "input": {},
                    },
                },
            )
        if event.kind is EventKind.TOOL_CALL_ARGUMENTS_DELTA:
            assert event.block_index is not None
            delta = event.payload.get("delta", "")
            assert isinstance(delta, str)
            self.tool_arguments[event.block_index] = (
                self.tool_arguments.get(event.block_index, "") + delta
            )
            return (
                {
                    "type": "content_block_delta",
                    "index": event.block_index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": delta,
                    },
                },
            )
        if event.kind is EventKind.TOOL_CALL_COMPLETED:
            assert event.block_index is not None
            arguments = event.payload.get(
                "arguments", self.tool_arguments.get(event.block_index, "{}")
            )
            if not isinstance(arguments, str):
                raise _protocol_error("Canonical tool arguments are invalid.")
            try:
                parsed = json.loads(arguments or "{}")
            except (json.JSONDecodeError, RecursionError):
                raise _protocol_error("Canonical tool arguments contain invalid JSON.") from None
            if not isinstance(parsed, dict):
                raise _protocol_error("Canonical tool arguments must be a JSON object.")
            self.tool_arguments[event.block_index] = arguments
            return ()
        if event.kind is EventKind.CONTENT_BLOCK_COMPLETED:
            return (
                {
                    "type": "content_block_stop",
                    "index": event.block_index,
                },
            )
        if event.kind is EventKind.USAGE:
            self.usage = CanonicalUsage.from_dict(event.payload)
            return ()
        if event.kind is EventKind.RESPONSE_COMPLETED:
            finish = event.payload.get("finish_reason", "stop")
            if finish is None:
                finish = "stop"
            if not isinstance(finish, str) or finish not in _CANONICAL_TO_WIRE_STOP:
                raise _protocol_error(f"Unsupported canonical finish reason: {finish}.")
            usage: dict[str, object] = {"output_tokens": 0}
            if self.usage is not None:
                usage = {
                    "input_tokens": self.usage.input_tokens,
                    "output_tokens": self.usage.output_tokens,
                }
                if self.usage.cache_creation_input_tokens:
                    usage["cache_creation_input_tokens"] = (
                        self.usage.cache_creation_input_tokens
                    )
                if self.usage.cache_read_input_tokens:
                    usage["cache_read_input_tokens"] = (
                        self.usage.cache_read_input_tokens
                    )
            return (
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": _CANONICAL_TO_WIRE_STOP[finish],
                        "stop_sequence": event.payload.get("stop_sequence"),
                    },
                    "usage": usage,
                },
                {"type": "message_stop"},
            )
        if event.kind is EventKind.ERROR:
            code = event.payload.get("code", "api_error")
            message = event.payload.get("message", "Relay request failed.")
            return (
                {
                    "type": "error",
                    "error": {"type": code, "message": message},
                },
            )
        raise _protocol_error(f"Unsupported canonical event: {event.kind.value}.")

    def render_event(self, event: CanonicalEvent) -> tuple[dict[str, object], ...]:
        return self.render(event)


__all__ = [
    "ANTHROPIC_VERSION",
    "AnthropicInboundAdapter",
    "AnthropicOutboundRenderer",
    "AnthropicUpstreamAdapter",
]
