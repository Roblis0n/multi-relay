"""OpenAI Responses wire adapter for the host-neutral canonical protocol."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
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


_REQUEST_FIELDS = frozenset(
    {
        "model",
        "input",
        "instructions",
        "tools",
        "tool_choice",
        "max_output_tokens",
        "temperature",
        "top_p",
        "stream",
        "parallel_tool_calls",
        "reasoning",
        "metadata",
        "store",
        "background",
        "include",
        "service_tier",
        "user",
    }
)


def _invalid(message: str, **details: object) -> ManagerError:
    return ManagerError("request_invalid", message, details or None)


def _content_blocks(value: object) -> tuple[CanonicalContentBlock, ...]:
    if isinstance(value, str):
        return (CanonicalContentBlock.text_block(value),)
    if not isinstance(value, (list, tuple)):
        raise _invalid("Responses message content must be text or an array.")
    blocks: list[CanonicalContentBlock] = []
    for item in value:
        if isinstance(item, str):
            blocks.append(CanonicalContentBlock.text_block(item))
            continue
        if not isinstance(item, Mapping):
            raise _invalid("Responses content block must be an object.")
        kind = item.get("type")
        if kind in {"input_text", "output_text", "text"}:
            text = item.get("text")
            if not isinstance(text, str):
                raise _invalid("Responses text block requires text.")
            blocks.append(CanonicalContentBlock.text_block(text))
            continue
        if kind in {"input_image", "image_url"}:
            image = item.get("image_url", item.get("url"))
            if isinstance(image, Mapping):
                image = image.get("url")
            if not isinstance(image, str):
                raise _invalid("Responses image block requires an image URL.")
            if image.startswith("data:") and ";base64," in image:
                header, data = image.split(",", 1)
                media_type = header[5:].removesuffix(";base64")
                blocks.append(CanonicalContentBlock.image_base64(media_type, data))
            else:
                blocks.append(CanonicalContentBlock.image_url(image))
            continue
        raise _invalid(f"Unsupported Responses content block: {kind}.")
    if not blocks:
        raise _invalid("Responses message content cannot be empty.")
    return tuple(blocks)


def _tool_choice(value: object) -> ToolChoice:
    if value is None:
        return ToolChoice.auto()
    if isinstance(value, str):
        return ToolChoice(value)
    if not isinstance(value, Mapping):
        raise _invalid("Responses tool_choice is invalid.")
    if value.get("type") == "function":
        name = value.get("name")
        if name is None and isinstance(value.get("function"), Mapping):
            name = value["function"].get("name")  # type: ignore[index]
        return ToolChoice("tool", name)  # type: ignore[arg-type]
    raise _invalid("Responses tool_choice is unsupported.")


def _tool_choice_payload(choice: ToolChoice) -> object:
    if choice.mode in {"auto", "none", "required"}:
        return choice.mode
    return {"type": "function", "name": choice.name}


def _responses_content(blocks: tuple[CanonicalContentBlock, ...]) -> list[dict[str, object]]:
    content: list[dict[str, object]] = []
    for block in blocks:
        if block.kind == "text":
            content.append({"type": "input_text", "text": block.text})
        elif block.kind == "image_url":
            content.append({"type": "input_image", "image_url": block.url})
        elif block.kind == "image_base64":
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{block.media_type};base64,{block.data}",
                }
            )
    return content


def _tool_output(block: CanonicalContentBlock) -> object:
    if len(block.result_content) == 1 and block.result_content[0].kind == "text":
        return block.result_content[0].text or ""
    return _responses_content(block.result_content)


class ResponsesAdapter:
    protocol = "responses-compatible"

    def __init__(self, *, max_sse_event_bytes: int = 1024 * 1024) -> None:
        self.max_sse_event_bytes = max_sse_event_bytes

    def parse_request(
        self,
        payload: object,
        *,
        request_id: str,
        host: str,
        pool_id: str,
    ) -> CanonicalRequest:
        if not isinstance(payload, Mapping):
            raise _invalid("Responses request must be an object.")
        unknown = sorted(set(payload) - _REQUEST_FIELDS)
        if unknown:
            raise _invalid(
                "Responses request contains unsupported semantic fields.",
                fields=unknown,
            )
        model = payload.get("model")
        if not isinstance(model, str) or not model:
            raise _invalid("Responses request requires a model alias.")
        raw_input = payload.get("input", [])
        if isinstance(raw_input, str):
            raw_input = [
                {"type": "message", "role": "user", "content": raw_input}
            ]
        if not isinstance(raw_input, (list, tuple)):
            raise _invalid("Responses input must be text or an array.")

        system: list[CanonicalContentBlock] = []
        developer: list[CanonicalContentBlock] = []
        messages: list[CanonicalMessage] = []
        warnings: list[str] = []
        instructions = payload.get("instructions")
        if isinstance(instructions, str) and instructions:
            developer.append(CanonicalContentBlock.text_block(instructions))
        elif instructions is not None:
            raise _invalid("Responses instructions must be text or null.")
        for item in raw_input:
            if not isinstance(item, Mapping):
                raise _invalid("Responses input item must be an object.")
            kind = item.get("type", "message")
            if kind == "message":
                role = item.get("role")
                blocks = _content_blocks(item.get("content", ""))
                if role == "system":
                    system.extend(blocks)
                elif role == "developer":
                    developer.extend(blocks)
                elif role in {"user", "assistant"}:
                    messages.append(CanonicalMessage(role, blocks))
                else:
                    raise _invalid(f"Unsupported Responses message role: {role}.")
                continue
            if kind in {"function_call", "custom_tool_call"}:
                call_id = item.get("call_id", item.get("id"))
                name = item.get("name")
                arguments = item.get("arguments", item.get("input", ""))
                if not isinstance(arguments, str):
                    raise _invalid("Responses tool call arguments must be text.")
                messages.append(
                    CanonicalMessage(
                        "assistant",
                        (
                            CanonicalContentBlock.tool_call(
                                call_id,  # type: ignore[arg-type]
                                name,  # type: ignore[arg-type]
                                arguments,
                            ),
                        ),
                    )
                )
                continue
            if kind in {"function_call_output", "custom_tool_call_output"}:
                call_id = item.get("call_id", item.get("id"))
                output = item.get("output", "")
                blocks = _content_blocks(output)
                messages.append(
                    CanonicalMessage(
                        "user",
                        (
                            CanonicalContentBlock.tool_result(
                                call_id,  # type: ignore[arg-type]
                                blocks,
                                is_error=item.get("is_error", False),  # type: ignore[arg-type]
                            ),
                        ),
                    )
                )
                continue
            if kind == "reasoning":
                warnings.append("ignored_input_item:reasoning")
                continue
            raise _invalid(f"Unsupported Responses input item: {kind}.")

        raw_tools = payload.get("tools", [])
        if not isinstance(raw_tools, (list, tuple)):
            raise _invalid("Responses tools must be an array.")
        reasoning = payload.get("reasoning")
        if isinstance(reasoning, Mapping):
            effort = reasoning.get("effort")
        elif isinstance(reasoning, str):
            effort = reasoning
        elif reasoning is None:
            effort = None
        else:
            raise _invalid("Responses reasoning configuration is invalid.")
        ignored = sorted(
            key
            for key in payload
            if key in {"store", "background", "include", "service_tier", "user"}
        )
        warnings.extend(f"ignored_parameter:{name}" for name in ignored)
        return CanonicalRequest(
            request_id=request_id,
            host=host,
            model_alias=model,
            pool_id=pool_id,
            system_blocks=tuple(system),
            developer_blocks=tuple(developer),
            messages=tuple(messages),
            tools=tuple(CanonicalTool.from_openai(item) for item in raw_tools),
            tool_choice=_tool_choice(payload.get("tool_choice")),
            max_output_tokens=payload.get("max_output_tokens"),  # type: ignore[arg-type]
            temperature=payload.get("temperature"),  # type: ignore[arg-type]
            top_p=payload.get("top_p"),  # type: ignore[arg-type]
            parallel_tool_calls=payload.get("parallel_tool_calls"),  # type: ignore[arg-type]
            requested_reasoning_effort=effort,  # type: ignore[arg-type]
            stream=payload.get("stream", True),  # type: ignore[arg-type]
            metadata=payload.get("metadata", {}),  # type: ignore[arg-type]
            warnings=tuple(warnings),
        )

    def build_request(
        self,
        request: CanonicalRequest,
        *,
        model: str,
    ) -> Mapping[str, Any]:
        input_items: list[dict[str, object]] = []
        if request.system_blocks:
            input_items.append(
                {
                    "type": "message",
                    "role": "system",
                    "content": _responses_content(request.system_blocks),
                }
            )
        if request.developer_blocks:
            input_items.append(
                {
                    "type": "message",
                    "role": "developer",
                    "content": _responses_content(request.developer_blocks),
                }
            )
        for message in request.messages:
            ordinary = tuple(
                block
                for block in message.content
                if block.kind in {"text", "image_url", "image_base64"}
            )
            if ordinary:
                input_items.append(
                    {
                        "type": "message",
                        "role": message.role,
                        "content": _responses_content(ordinary),
                    }
                )
            for block in message.content:
                if block.kind == "tool_call":
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": block.tool_call_id,
                            "name": block.tool_name,
                            "arguments": block.arguments,
                        }
                    )
                elif block.kind == "tool_result":
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": block.tool_call_id,
                            "output": _tool_output(block),
                            "is_error": block.is_error,
                        }
                    )
        payload: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "stream": request.stream,
        }
        if request.tools:
            payload["tools"] = [item.to_openai_responses() for item in request.tools]
            payload["tool_choice"] = _tool_choice_payload(request.tool_choice)
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if request.parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = request.parallel_tool_calls
        if request.requested_reasoning_effort is not None:
            payload["reasoning"] = {"effort": request.requested_reasoning_effort}
        if request.metadata:
            payload["metadata"] = dict(request.metadata)
        return payload

    def render_event(self, event: CanonicalEvent) -> dict[str, object]:
        base: dict[str, object] = {"sequence_number": event.sequence}
        if event.kind is EventKind.RESPONSE_STARTED:
            return {**base, "type": "response.created", "response": {"id": event.response_id}}
        if event.kind is EventKind.TEXT_DELTA:
            return {
                **base,
                "type": "response.output_text.delta",
                "output_index": event.block_index,
                "delta": event.payload.get("delta", ""),
            }
        if event.kind is EventKind.REASONING_SUMMARY_DELTA:
            return {
                **base,
                "type": "response.reasoning_summary_text.delta",
                "output_index": event.block_index,
                "delta": event.payload.get("delta", ""),
            }
        if event.kind is EventKind.TOOL_CALL_STARTED:
            return {
                **base,
                "type": "response.output_item.added",
                "output_index": event.block_index,
                "item": {
                    "type": "function_call",
                    "call_id": event.tool_call_id,
                    "name": event.payload.get("name"),
                    "arguments": "",
                },
            }
        if event.kind is EventKind.TOOL_CALL_ARGUMENTS_DELTA:
            return {
                **base,
                "type": "response.function_call_arguments.delta",
                "output_index": event.block_index,
                "item_id": event.tool_call_id,
                "delta": event.payload.get("delta", ""),
            }
        if event.kind is EventKind.TOOL_CALL_COMPLETED:
            return {
                **base,
                "type": "response.function_call_arguments.done",
                "output_index": event.block_index,
                "item_id": event.tool_call_id,
                "name": event.payload.get("name"),
                "arguments": event.payload.get("arguments", ""),
            }
        if event.kind is EventKind.CONTENT_BLOCK_STARTED:
            return {
                **base,
                "type": "response.content_part.added",
                "output_index": event.block_index,
                "part": dict(event.payload),
            }
        if event.kind is EventKind.CONTENT_BLOCK_COMPLETED:
            return {
                **base,
                "type": "response.content_part.done",
                "output_index": event.block_index,
            }
        if event.kind is EventKind.USAGE:
            return {**base, "type": "response.usage", "usage": dict(event.payload)}
        if event.kind is EventKind.RESPONSE_COMPLETED:
            return {
                **base,
                "type": "response.completed",
                "response": {"id": event.response_id, **dict(event.payload)},
            }
        return {
            **base,
            "type": "response.failed",
            "response": {"id": event.response_id, "error": dict(event.payload)},
        }

    def parse_response(
        self,
        payload: object,
        *,
        response_id: str | None = None,
    ) -> tuple[CanonicalEvent, ...]:
        if not isinstance(payload, Mapping):
            raise ManagerError("protocol_error", "Responses result must be an object.")
        selected_id = response_id or (
            payload.get("id") if isinstance(payload.get("id"), str) else None
        ) or "responses-result"
        sequence = CanonicalEventSequence(selected_id)
        events = [sequence.emit(EventKind.RESPONSE_STARTED)]
        output = payload.get("output", [])
        if not isinstance(output, (list, tuple)):
            raise ManagerError("protocol_error", "Responses output must be an array.")
        block = 0
        for item in output:
            if not isinstance(item, Mapping):
                continue
            kind = item.get("type")
            if kind == "message":
                for content in item.get("content", []):
                    if isinstance(content, Mapping) and isinstance(content.get("text"), str):
                        events.append(
                            sequence.emit(
                                EventKind.TEXT_DELTA,
                                block_index=block,
                                payload={"delta": content["text"]},
                            )
                        )
                        block += 1
            elif kind == "function_call":
                call_id = item.get("call_id", item.get("id"))
                events.extend(
                    [
                        sequence.emit(
                            EventKind.TOOL_CALL_STARTED,
                            block_index=block,
                            tool_call_id=call_id,  # type: ignore[arg-type]
                            payload={"name": item.get("name")},
                        ),
                        sequence.emit(
                            EventKind.TOOL_CALL_COMPLETED,
                            block_index=block,
                            tool_call_id=call_id,  # type: ignore[arg-type]
                            payload={
                                "name": item.get("name"),
                                "arguments": item.get("arguments", ""),
                            },
                        ),
                    ]
                )
                block += 1
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            events.append(
                sequence.emit(
                    EventKind.USAGE,
                    payload=CanonicalUsage.from_dict(usage).to_dict(),
                )
            )
        events.append(sequence.emit(EventKind.RESPONSE_COMPLETED))
        return tuple(events)

    def iter_events(
        self,
        chunks: Iterable[bytes | str],
        *,
        response_id: str | None = None,
    ) -> Iterator[CanonicalEvent]:
        decoded = iter_sse_json(chunks, max_event_bytes=self.max_sse_event_bytes)
        sequence: CanonicalEventSequence | None = None
        tool_blocks: dict[int, tuple[str, str]] = {}
        completed = False
        try:
            for payload in decoded:
                if not isinstance(payload, Mapping):
                    raise ManagerError("protocol_error", "Responses SSE event must be an object.")
                wire_kind = payload.get("type")
                if sequence is None:
                    wire_response = payload.get("response")
                    wire_id = (
                        wire_response.get("id")
                        if isinstance(wire_response, Mapping)
                        else None
                    )
                    sequence = CanonicalEventSequence(
                        response_id
                        or (wire_id if isinstance(wire_id, str) else None)
                        or "responses-stream"
                    )
                if wire_kind == "response.created":
                    yield sequence.emit(EventKind.RESPONSE_STARTED)
                    continue
                index = payload.get("output_index", 0)
                if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                    raise ManagerError("protocol_error", "Responses output index is invalid.")
                if wire_kind == "response.output_text.delta":
                    yield sequence.emit(
                        EventKind.TEXT_DELTA,
                        block_index=index,
                        payload={"delta": payload.get("delta", "")},
                    )
                elif wire_kind == "response.reasoning_summary_text.delta":
                    yield sequence.emit(
                        EventKind.REASONING_SUMMARY_DELTA,
                        block_index=index,
                        payload={"delta": payload.get("delta", "")},
                    )
                elif wire_kind in {
                    "response.function_call_arguments.delta",
                    "response.function_call_arguments.done",
                }:
                    call_id = payload.get("item_id", payload.get("call_id"))
                    name = payload.get("name", "unknown_tool")
                    if index not in tool_blocks:
                        if not isinstance(call_id, str) or not call_id:
                            raise ManagerError("protocol_error", "Responses tool call id is missing.")
                        if not isinstance(name, str) or not name:
                            name = "unknown_tool"
                        tool_blocks[index] = (call_id, name)
                        yield sequence.emit(
                            EventKind.TOOL_CALL_STARTED,
                            block_index=index,
                            tool_call_id=call_id,
                            payload={"name": name},
                        )
                    stable_id, stable_name = tool_blocks[index]
                    if wire_kind.endswith(".delta"):
                        yield sequence.emit(
                            EventKind.TOOL_CALL_ARGUMENTS_DELTA,
                            block_index=index,
                            tool_call_id=stable_id,
                            payload={"delta": payload.get("delta", "")},
                        )
                    else:
                        yield sequence.emit(
                            EventKind.TOOL_CALL_COMPLETED,
                            block_index=index,
                            tool_call_id=stable_id,
                            payload={
                                "name": stable_name,
                                "arguments": payload.get("arguments", ""),
                            },
                        )
                elif wire_kind == "response.completed":
                    response = payload.get("response")
                    usage = response.get("usage") if isinstance(response, Mapping) else None
                    if isinstance(usage, Mapping):
                        yield sequence.emit(
                            EventKind.USAGE,
                            payload=CanonicalUsage.from_dict(usage).to_dict(),
                        )
                    yield sequence.emit(EventKind.RESPONSE_COMPLETED)
                    completed = True
                elif wire_kind in {"response.failed", "error"}:
                    yield sequence.emit(EventKind.ERROR, payload={"code": "upstream_error"})
                    completed = True
            if sequence is not None and not completed:
                yield sequence.emit(EventKind.RESPONSE_COMPLETED)
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


__all__ = ["ResponsesAdapter"]
