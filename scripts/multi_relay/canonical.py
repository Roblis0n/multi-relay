"""Immutable host-neutral request, tool, content, and event contracts."""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from .capabilities import (
    MAX_IMAGES_PER_REQUEST,
    MAX_TOTAL_IMAGE_BYTES,
    decode_image_base64,
    json_value,
    thaw_json,
    validate_image_url,
    validate_json_schema,
)
from .catalog import HOSTS, REASONING_EFFORTS
from .errors import ManagerError


_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_METADATA_FIELDS = frozenset(
    {"trace_id", "request_label", "agent_name", "user_id"}
)
_IGNORABLE_REQUEST_FIELDS = frozenset(
    {"background", "store", "service_tier", "include", "user"}
)
_REQUEST_FIELDS = frozenset(
    {
        "request_id",
        "host",
        "model_alias",
        "pool_id",
        "system_blocks",
        "developer_blocks",
        "messages",
        "tools",
        "tool_choice",
        "max_output_tokens",
        "temperature",
        "top_p",
        "stop",
        "seed",
        "parallel_tool_calls",
        "requested_reasoning_effort",
        "stream",
        "metadata",
        "warnings",
    }
)
_CONTENT_FIELDS = frozenset(
    {
        "kind",
        "text",
        "url",
        "media_type",
        "data",
        "tool_call_id",
        "tool_name",
        "arguments",
        "is_error",
        "result_content",
    }
)
_TOOL_FIELDS = frozenset({"name", "description", "input_schema", "strict"})
_EVENT_FIELDS = frozenset(
    {"kind", "response_id", "sequence", "block_index", "tool_call_id", "payload"}
)


def _request_invalid(message: str, **details: object) -> ManagerError:
    return ManagerError("request_invalid", message, details or None)


def _strict_fields(
    value: object,
    allowed: frozenset[str],
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _request_invalid(f"{label} must be a JSON object.")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _request_invalid(
            f"{label} contains unsupported fields.",
            fields=unknown,
        )
    return value


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _request_invalid(f"{field_name} must be a non-empty string.")
    return value.strip()


@dataclass(frozen=True)
class CanonicalContentBlock:
    """One ordered input block, including recursively ordered tool results."""

    kind: str
    text: str | None = None
    url: str | None = None
    media_type: str | None = None
    data: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: str | None = None
    is_error: bool = False
    result_content: tuple["CanonicalContentBlock", ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_content", tuple(self.result_content))
        if self.kind == "text":
            if not isinstance(self.text, str):
                raise _request_invalid("Text blocks require text.")
            if any(
                value is not None
                for value in (
                    self.url,
                    self.media_type,
                    self.data,
                    self.tool_call_id,
                    self.tool_name,
                    self.arguments,
                )
            ) or self.result_content or self.is_error:
                raise _request_invalid("Text block fields are inconsistent.")
            return
        if self.kind == "image_url":
            validate_image_url(self.url)
            if any(
                value is not None
                for value in (
                    self.text,
                    self.media_type,
                    self.data,
                    self.tool_call_id,
                    self.tool_name,
                    self.arguments,
                )
            ) or self.result_content or self.is_error:
                raise _request_invalid("Image URL block fields are inconsistent.")
            return
        if self.kind == "image_base64":
            decode_image_base64(self.media_type, self.data)
            if any(
                value is not None
                for value in (
                    self.text,
                    self.url,
                    self.tool_call_id,
                    self.tool_name,
                    self.arguments,
                )
            ):
                raise _request_invalid("Base64 image block fields are inconsistent.")
            if self.result_content or self.is_error:
                raise _request_invalid("Base64 image block fields are inconsistent.")
            return
        if self.kind == "tool_call":
            _nonempty(self.tool_call_id, "tool_call_id")
            if not isinstance(self.tool_name, str) or not _TOOL_NAME.fullmatch(
                self.tool_name
            ):
                raise _request_invalid("Tool call requires a valid tool name.")
            if not isinstance(self.arguments, str):
                raise _request_invalid("Tool call arguments must be a JSON string.")
            if any(
                value is not None
                for value in (self.text, self.url, self.media_type, self.data)
            ) or self.result_content or self.is_error:
                raise _request_invalid("Tool call block fields are inconsistent.")
            return
        if self.kind == "tool_result":
            _nonempty(self.tool_call_id, "tool_call_id")
            if any(
                value is not None
                for value in (
                    self.text,
                    self.url,
                    self.media_type,
                    self.data,
                    self.tool_name,
                    self.arguments,
                )
            ):
                raise _request_invalid("Tool result block fields are inconsistent.")
            if not isinstance(self.is_error, bool):
                raise _request_invalid("Tool result is_error must be boolean.")
            if any(item.kind == "tool_result" for item in self.result_content):
                raise _request_invalid("Tool results cannot contain nested tool results.")
            return
        raise _request_invalid(f"Unsupported canonical content kind: {self.kind}.")

    @classmethod
    def text_block(cls, text: str) -> "CanonicalContentBlock":
        return cls(kind="text", text=text)

    @classmethod
    def image_url(cls, url: str) -> "CanonicalContentBlock":
        return cls(kind="image_url", url=url)

    @classmethod
    def image_base64(cls, media_type: str, data: str) -> "CanonicalContentBlock":
        return cls(kind="image_base64", media_type=media_type, data=data)

    @classmethod
    def tool_result(
        cls,
        tool_call_id: str,
        content: tuple["CanonicalContentBlock", ...] | list["CanonicalContentBlock"],
        *,
        is_error: bool = False,
    ) -> "CanonicalContentBlock":
        return cls(
            kind="tool_result",
            tool_call_id=tool_call_id,
            is_error=is_error,
            result_content=tuple(content),
        )

    @classmethod
    def tool_call(
        cls,
        tool_call_id: str,
        name: str,
        arguments: str,
    ) -> "CanonicalContentBlock":
        return cls(
            kind="tool_call",
            tool_call_id=tool_call_id,
            tool_name=name,
            arguments=arguments,
        )

    def decoded_image_bytes(self) -> bytes:
        if self.kind != "image_base64":
            raise _request_invalid("Only base64 image blocks have decoded bytes.")
        return decode_image_base64(self.media_type, self.data)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"kind": self.kind}
        if self.kind == "text":
            payload["text"] = self.text
        elif self.kind == "image_url":
            payload["url"] = self.url
        elif self.kind == "image_base64":
            payload.update({"media_type": self.media_type, "data": self.data})
        elif self.kind == "tool_call":
            payload.update(
                {
                    "tool_call_id": self.tool_call_id,
                    "tool_name": self.tool_name,
                    "arguments": self.arguments,
                }
            )
        else:
            payload.update(
                {
                    "tool_call_id": self.tool_call_id,
                    "is_error": self.is_error,
                    "result_content": [item.to_dict() for item in self.result_content],
                }
            )
        return payload

    @classmethod
    def from_dict(cls, value: object) -> "CanonicalContentBlock":
        data = _strict_fields(value, _CONTENT_FIELDS, "Canonical content block")
        kind = data.get("kind")
        if kind == "text":
            return cls.text_block(data.get("text"))  # type: ignore[arg-type]
        if kind == "image_url":
            return cls.image_url(data.get("url"))  # type: ignore[arg-type]
        if kind == "image_base64":
            return cls.image_base64(
                data.get("media_type"),  # type: ignore[arg-type]
                data.get("data"),  # type: ignore[arg-type]
            )
        if kind == "tool_call":
            return cls.tool_call(
                data.get("tool_call_id"),  # type: ignore[arg-type]
                data.get("tool_name"),  # type: ignore[arg-type]
                data.get("arguments"),  # type: ignore[arg-type]
            )
        if kind == "tool_result":
            raw_content = data.get("result_content", [])
            if not isinstance(raw_content, (list, tuple)):
                raise _request_invalid("Tool result content must be an array.")
            return cls.tool_result(
                data.get("tool_call_id"),  # type: ignore[arg-type]
                tuple(cls.from_dict(item) for item in raw_content),
                is_error=data.get("is_error", False),  # type: ignore[arg-type]
            )
        raise _request_invalid(f"Unsupported canonical content kind: {kind}.")


@dataclass(frozen=True)
class CanonicalMessage:
    role: str
    content: tuple[CanonicalContentBlock, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", tuple(self.content))
        if self.role not in {"user", "assistant"}:
            raise _request_invalid(f"Unsupported conversation role: {self.role}.")
        if not self.content:
            raise _request_invalid("Conversation messages require at least one block.")
        if any(not isinstance(item, CanonicalContentBlock) for item in self.content):
            raise _request_invalid("Conversation content contains an invalid block.")

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "content": [item.to_dict() for item in self.content],
        }

    @classmethod
    def from_dict(cls, value: object) -> "CanonicalMessage":
        data = _strict_fields(value, frozenset({"role", "content"}), "Canonical message")
        raw_content = data.get("content")
        if not isinstance(raw_content, (list, tuple)):
            raise _request_invalid("Canonical message content must be an array.")
        return cls(
            role=data.get("role"),  # type: ignore[arg-type]
            content=tuple(CanonicalContentBlock.from_dict(item) for item in raw_content),
        )


@dataclass(frozen=True)
class CanonicalTool:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    strict: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _TOOL_NAME.fullmatch(self.name):
            raise _request_invalid("Tool name contains unsupported characters.")
        if not isinstance(self.description, str):
            raise _request_invalid("Tool description must be a string.")
        if not isinstance(self.strict, bool):
            raise _request_invalid("Tool strict must be boolean.")
        object.__setattr__(self, "input_schema", validate_json_schema(self.input_schema))

    @classmethod
    def from_openai(cls, value: object) -> "CanonicalTool":
        if not isinstance(value, Mapping) or value.get("type") != "function":
            raise _request_invalid("OpenAI tool must have type function.")
        if "function" in value:
            outer = _strict_fields(
                value,
                frozenset({"type", "function"}),
                "OpenAI tool",
            )
            function = outer.get("function")
            data = _strict_fields(
                function,
                frozenset({"name", "description", "parameters", "strict"}),
                "OpenAI function",
            )
        else:
            data = _strict_fields(
                value,
                frozenset({"type", "name", "description", "parameters", "strict"}),
                "OpenAI function",
            )
        return cls(
            name=data.get("name"),  # type: ignore[arg-type]
            description=data.get("description", ""),  # type: ignore[arg-type]
            input_schema=data.get(
                "parameters",
                {"type": "object", "properties": {}},
            ),  # type: ignore[arg-type]
            strict=data.get("strict", False),  # type: ignore[arg-type]
        )

    @classmethod
    def from_anthropic(cls, value: object) -> "CanonicalTool":
        data = _strict_fields(
            value,
            frozenset({"name", "description", "input_schema"}),
            "Anthropic tool",
        )
        return cls(
            name=data.get("name"),  # type: ignore[arg-type]
            description=data.get("description", ""),  # type: ignore[arg-type]
            input_schema=data.get(
                "input_schema",
                {"type": "object", "properties": {}},
            ),  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": thaw_json(self.input_schema),
            "strict": self.strict,
        }

    @classmethod
    def from_dict(cls, value: object) -> "CanonicalTool":
        data = _strict_fields(value, _TOOL_FIELDS, "Canonical tool")
        return cls(
            name=data.get("name"),  # type: ignore[arg-type]
            description=data.get("description", ""),  # type: ignore[arg-type]
            input_schema=data.get("input_schema"),  # type: ignore[arg-type]
            strict=data.get("strict", False),  # type: ignore[arg-type]
        )

    def to_openai_chat(self) -> dict[str, object]:
        function: dict[str, object] = {
            "name": self.name,
            "description": self.description,
            "parameters": thaw_json(self.input_schema),
        }
        if self.strict:
            function["strict"] = True
        return {"type": "function", "function": function}

    def to_openai_responses(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": thaw_json(self.input_schema),
        }
        if self.strict:
            payload["strict"] = True
        return payload

    def to_anthropic(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": thaw_json(self.input_schema),
        }


@dataclass(frozen=True)
class ToolChoice:
    mode: str
    name: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"auto", "none", "required", "tool"}:
            raise _request_invalid(f"Unsupported tool choice: {self.mode}.")
        if self.mode == "tool":
            if not isinstance(self.name, str) or not _TOOL_NAME.fullmatch(self.name):
                raise _request_invalid("Named tool choice requires a valid tool name.")
        elif self.name is not None:
            raise _request_invalid("Only named tool choice can contain a tool name.")

    @classmethod
    def auto(cls) -> "ToolChoice":
        return cls("auto")

    def to_dict(self) -> dict[str, object]:
        return {"mode": self.mode, "name": self.name}

    @classmethod
    def from_dict(cls, value: object) -> "ToolChoice":
        if isinstance(value, str):
            return cls(value)
        data = _strict_fields(value, frozenset({"mode", "name"}), "Tool choice")
        return cls(data.get("mode"), data.get("name"))  # type: ignore[arg-type]


def _image_blocks(
    blocks: tuple[CanonicalContentBlock, ...],
) -> tuple[CanonicalContentBlock, ...]:
    result: list[CanonicalContentBlock] = []
    for block in blocks:
        if block.kind in {"image_url", "image_base64"}:
            result.append(block)
        if block.kind == "tool_result":
            result.extend(_image_blocks(block.result_content))
    return tuple(result)


@dataclass(frozen=True)
class CanonicalRequest:
    request_id: str
    host: str
    model_alias: str
    pool_id: str
    system_blocks: tuple[CanonicalContentBlock, ...] = ()
    developer_blocks: tuple[CanonicalContentBlock, ...] = ()
    messages: tuple[CanonicalMessage, ...] = ()
    tools: tuple[CanonicalTool, ...] = ()
    tool_choice: ToolChoice = field(default_factory=ToolChoice.auto)
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: tuple[str, ...] = ()
    seed: int | None = None
    parallel_tool_calls: bool | None = None
    requested_reasoning_effort: str | None = None
    stream: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.request_id, "request_id")
        if self.host not in HOSTS:
            raise _request_invalid(f"Unsupported host: {self.host}.")
        _nonempty(self.model_alias, "model_alias")
        _nonempty(self.pool_id, "pool_id")
        object.__setattr__(self, "system_blocks", tuple(self.system_blocks))
        object.__setattr__(self, "developer_blocks", tuple(self.developer_blocks))
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "stop", tuple(self.stop))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        for label, blocks in (
            ("system", self.system_blocks),
            ("developer", self.developer_blocks),
        ):
            if any(block.kind != "text" for block in blocks):
                raise _request_invalid(f"{label} blocks must contain text only.")
        if any(not isinstance(item, CanonicalMessage) for item in self.messages):
            raise _request_invalid("messages contains an invalid item.")
        if any(not isinstance(item, CanonicalTool) for item in self.tools):
            raise _request_invalid("tools contains an invalid item.")
        names = [item.name for item in self.tools]
        if len(names) != len(set(names)):
            raise _request_invalid("Tool names must be unique.")
        if not isinstance(self.tool_choice, ToolChoice):
            raise _request_invalid("tool_choice is invalid.")
        if self.tool_choice.mode == "tool" and self.tool_choice.name not in set(names):
            raise _request_invalid("Named tool choice references an unknown tool.")
        if (
            self.max_output_tokens is not None
            and (
                isinstance(self.max_output_tokens, bool)
                or not isinstance(self.max_output_tokens, int)
                or self.max_output_tokens <= 0
            )
        ):
            raise _request_invalid("max_output_tokens must be a positive integer.")
        self._validate_float("temperature", self.temperature, 0.0, 2.0)
        self._validate_float("top_p", self.top_p, 0.0, 1.0)
        if any(not isinstance(item, str) or not item for item in self.stop):
            raise _request_invalid("stop must contain non-empty strings.")
        if len(self.stop) > 8:
            raise _request_invalid("stop contains too many sequences.")
        if self.seed is not None and (
            isinstance(self.seed, bool) or not isinstance(self.seed, int)
        ):
            raise _request_invalid("seed must be an integer or null.")
        if self.parallel_tool_calls is not None and not isinstance(
            self.parallel_tool_calls, bool
        ):
            raise _request_invalid("parallel_tool_calls must be boolean or null.")
        if (
            self.requested_reasoning_effort is not None
            and self.requested_reasoning_effort not in REASONING_EFFORTS
        ):
            raise _request_invalid("requested_reasoning_effort is unsupported.")
        if not isinstance(self.stream, bool):
            raise _request_invalid("stream must be boolean.")
        if not isinstance(self.metadata, Mapping):
            raise _request_invalid("metadata must be a JSON object.")
        unknown_metadata = sorted(set(self.metadata) - _METADATA_FIELDS)
        if unknown_metadata:
            raise _request_invalid(
                "metadata contains unsupported fields.",
                fields=unknown_metadata,
            )
        frozen_metadata = json_value(self.metadata, path="$.metadata")
        assert isinstance(frozen_metadata, Mapping)
        object.__setattr__(self, "metadata", frozen_metadata)
        if any(not isinstance(item, str) or not item for item in self.warnings):
            raise _request_invalid("warnings must contain non-empty strings.")

        all_blocks = self.system_blocks + self.developer_blocks + tuple(
            block for message in self.messages for block in message.content
        )
        images = _image_blocks(all_blocks)
        if len(images) > MAX_IMAGES_PER_REQUEST:
            raise ManagerError(
                "too_many_images",
                "Request contains too many images.",
                {"max_images": MAX_IMAGES_PER_REQUEST},
            )
        total_image_bytes = sum(
            len(block.decoded_image_bytes())
            for block in images
            if block.kind == "image_base64"
        )
        if total_image_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ManagerError(
                "images_too_large",
                "Combined base64 images exceed the request size limit.",
                {"max_bytes": MAX_TOTAL_IMAGE_BYTES},
            )

    @staticmethod
    def _validate_float(
        name: str,
        value: float | None,
        minimum: float,
        maximum: float,
    ) -> None:
        if value is None:
            return
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not minimum <= float(value) <= maximum
        ):
            raise _request_invalid(f"{name} is outside the supported range.")

    @property
    def required_capabilities(self) -> frozenset[str]:
        all_blocks = self.system_blocks + self.developer_blocks + tuple(
            block for message in self.messages for block in message.content
        )
        capabilities = {"text"}
        if _image_blocks(all_blocks):
            capabilities.add("vision")
        if self.tools or any(
            block.kind in {"tool_call", "tool_result"} for block in all_blocks
        ):
            capabilities.add("tool_calling")
        return frozenset(capabilities)

    def constructor_values(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "host": self.host,
            "model_alias": self.model_alias,
            "pool_id": self.pool_id,
            "system_blocks": self.system_blocks,
            "developer_blocks": self.developer_blocks,
            "messages": self.messages,
            "tools": self.tools,
            "tool_choice": self.tool_choice,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "stop": self.stop,
            "seed": self.seed,
            "parallel_tool_calls": self.parallel_tool_calls,
            "requested_reasoning_effort": self.requested_reasoning_effort,
            "stream": self.stream,
            "metadata": self.metadata,
            "warnings": self.warnings,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "host": self.host,
            "model_alias": self.model_alias,
            "pool_id": self.pool_id,
            "system_blocks": [item.to_dict() for item in self.system_blocks],
            "developer_blocks": [item.to_dict() for item in self.developer_blocks],
            "messages": [item.to_dict() for item in self.messages],
            "tools": [item.to_dict() for item in self.tools],
            "tool_choice": self.tool_choice.to_dict(),
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "stop": list(self.stop),
            "seed": self.seed,
            "parallel_tool_calls": self.parallel_tool_calls,
            "requested_reasoning_effort": self.requested_reasoning_effort,
            "stream": self.stream,
            "metadata": thaw_json(self.metadata),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: object) -> "CanonicalRequest":
        if not isinstance(value, Mapping):
            raise _request_invalid("Canonical request must be a JSON object.")
        unknown = set(value) - _REQUEST_FIELDS
        ignorable = sorted(unknown & _IGNORABLE_REQUEST_FIELDS)
        semantic = sorted(unknown - _IGNORABLE_REQUEST_FIELDS)
        if semantic:
            raise _request_invalid(
                "Request contains unsupported semantic fields.",
                fields=semantic,
            )

        def array(name: str) -> tuple[object, ...]:
            raw = value.get(name, [])
            if not isinstance(raw, (list, tuple)):
                raise _request_invalid(f"{name} must be an array.")
            return tuple(raw)

        existing_warnings = array("warnings")
        warnings = tuple(existing_warnings) + tuple(
            f"ignored_parameter:{name}" for name in ignorable
        )
        return cls(
            request_id=value.get("request_id"),  # type: ignore[arg-type]
            host=value.get("host"),  # type: ignore[arg-type]
            model_alias=value.get("model_alias"),  # type: ignore[arg-type]
            pool_id=value.get("pool_id"),  # type: ignore[arg-type]
            system_blocks=tuple(
                CanonicalContentBlock.from_dict(item)
                for item in array("system_blocks")
            ),
            developer_blocks=tuple(
                CanonicalContentBlock.from_dict(item)
                for item in array("developer_blocks")
            ),
            messages=tuple(
                CanonicalMessage.from_dict(item) for item in array("messages")
            ),
            tools=tuple(CanonicalTool.from_dict(item) for item in array("tools")),
            tool_choice=ToolChoice.from_dict(value.get("tool_choice", "auto")),
            max_output_tokens=value.get("max_output_tokens"),  # type: ignore[arg-type]
            temperature=value.get("temperature"),  # type: ignore[arg-type]
            top_p=value.get("top_p"),  # type: ignore[arg-type]
            stop=tuple(array("stop")),  # type: ignore[arg-type]
            seed=value.get("seed"),  # type: ignore[arg-type]
            parallel_tool_calls=value.get("parallel_tool_calls"),  # type: ignore[arg-type]
            requested_reasoning_effort=value.get(
                "requested_reasoning_effort"
            ),  # type: ignore[arg-type]
            stream=value.get("stream", True),  # type: ignore[arg-type]
            metadata=value.get("metadata", {}),  # type: ignore[arg-type]
            warnings=warnings,  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class CanonicalUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
            self.cache_creation_input_tokens,
            self.cache_read_input_tokens,
        )
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in values):
            raise ManagerError(
                "canonical_event_invalid",
                "Usage counters must be non-negative integers.",
            )
        if self.total_tokens < self.input_tokens + self.output_tokens:
            raise ManagerError(
                "canonical_event_invalid",
                "Total usage cannot be smaller than input plus output usage.",
            )

    def to_dict(self) -> dict[str, int]:
        payload = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }
        if self.cache_creation_input_tokens:
            payload["cache_creation_input_tokens"] = self.cache_creation_input_tokens
        if self.cache_read_input_tokens:
            payload["cache_read_input_tokens"] = self.cache_read_input_tokens
        return payload

    @classmethod
    def from_dict(cls, value: object) -> "CanonicalUsage":
        data = _strict_fields(
            value,
            frozenset(
                {
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                }
            ),
            "Canonical usage",
        )
        return cls(
            input_tokens=data.get("input_tokens"),  # type: ignore[arg-type]
            output_tokens=data.get("output_tokens"),  # type: ignore[arg-type]
            total_tokens=data.get("total_tokens"),  # type: ignore[arg-type]
            cache_creation_input_tokens=data.get(
                "cache_creation_input_tokens", 0
            ),  # type: ignore[arg-type]
            cache_read_input_tokens=data.get(
                "cache_read_input_tokens", 0
            ),  # type: ignore[arg-type]
        )


class EventKind(str, Enum):
    RESPONSE_STARTED = "response_started"
    CONTENT_BLOCK_STARTED = "content_block_started"
    TEXT_DELTA = "text_delta"
    REASONING_SUMMARY_DELTA = "reasoning_summary_delta"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_ARGUMENTS_DELTA = "tool_call_arguments_delta"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    CONTENT_BLOCK_COMPLETED = "content_block_completed"
    USAGE = "usage"
    RESPONSE_COMPLETED = "response_completed"
    ERROR = "error"


_BLOCK_EVENTS = frozenset(
    {
        EventKind.CONTENT_BLOCK_STARTED,
        EventKind.TEXT_DELTA,
        EventKind.REASONING_SUMMARY_DELTA,
        EventKind.TOOL_CALL_STARTED,
        EventKind.TOOL_CALL_ARGUMENTS_DELTA,
        EventKind.TOOL_CALL_COMPLETED,
        EventKind.CONTENT_BLOCK_COMPLETED,
    }
)
_TOOL_EVENTS = frozenset(
    {
        EventKind.TOOL_CALL_STARTED,
        EventKind.TOOL_CALL_ARGUMENTS_DELTA,
        EventKind.TOOL_CALL_COMPLETED,
    }
)


@dataclass(frozen=True)
class CanonicalEvent:
    kind: EventKind
    response_id: str
    sequence: int
    block_index: int | None = None
    tool_call_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EventKind):
            try:
                object.__setattr__(self, "kind", EventKind(self.kind))
            except ValueError:
                raise ManagerError(
                    "canonical_event_invalid",
                    f"Unsupported canonical event kind: {self.kind}.",
                ) from None
        if not isinstance(self.response_id, str) or not self.response_id:
            raise ManagerError(
                "canonical_event_invalid",
                "Canonical event requires a response id.",
            )
        if (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ManagerError(
                "canonical_event_invalid",
                "Canonical event sequence must be a non-negative integer.",
            )
        if self.kind in _BLOCK_EVENTS:
            if (
                isinstance(self.block_index, bool)
                or not isinstance(self.block_index, int)
                or self.block_index < 0
            ):
                raise ManagerError(
                    "canonical_event_invalid",
                    "Block event requires a non-negative block index.",
                )
        elif self.block_index is not None:
            raise ManagerError(
                "canonical_event_invalid",
                "Non-block event cannot contain a block index.",
            )
        if self.kind in _TOOL_EVENTS:
            if not isinstance(self.tool_call_id, str) or not self.tool_call_id:
                raise ManagerError(
                    "canonical_event_invalid",
                    "Tool event requires a tool call id.",
                )
        elif self.tool_call_id is not None:
            raise ManagerError(
                "canonical_event_invalid",
                "Non-tool event cannot contain a tool call id.",
            )
        frozen = json_value(self.payload, path="$.event.payload")
        if not isinstance(frozen, Mapping):
            raise ManagerError(
                "canonical_event_invalid",
                "Canonical event payload must be an object.",
            )
        object.__setattr__(self, "payload", frozen)
        if self.kind in {EventKind.TEXT_DELTA, EventKind.REASONING_SUMMARY_DELTA}:
            if not isinstance(self.payload.get("delta"), str):
                raise ManagerError(
                    "canonical_event_invalid",
                    "Text delta event requires a string delta.",
                )
        if self.kind is EventKind.TOOL_CALL_ARGUMENTS_DELTA and not isinstance(
            self.payload.get("delta"), str
        ):
            raise ManagerError(
                "canonical_event_invalid",
                "Tool argument delta event requires a string delta.",
            )
        if self.kind is EventKind.USAGE:
            CanonicalUsage.from_dict(self.payload)

    @property
    def commits_request(self) -> bool:
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "response_id": self.response_id,
            "sequence": self.sequence,
            "block_index": self.block_index,
            "tool_call_id": self.tool_call_id,
            "payload": thaw_json(self.payload),
        }

    @classmethod
    def from_dict(cls, value: object) -> "CanonicalEvent":
        data = _strict_fields(value, _EVENT_FIELDS, "Canonical event")
        try:
            kind = EventKind(data.get("kind"))
        except ValueError:
            raise ManagerError(
                "canonical_event_invalid",
                "Canonical event kind is invalid.",
            ) from None
        return cls(
            kind=kind,
            response_id=data.get("response_id"),  # type: ignore[arg-type]
            sequence=data.get("sequence"),  # type: ignore[arg-type]
            block_index=data.get("block_index"),  # type: ignore[arg-type]
            tool_call_id=data.get("tool_call_id"),  # type: ignore[arg-type]
            payload=data.get("payload", {}),  # type: ignore[arg-type]
        )


class CanonicalEventSequence:
    """Allocate deterministic sequence numbers and stable tool ids per block."""

    def __init__(self, response_id: str) -> None:
        self.response_id = _nonempty(response_id, "response_id")
        self._next_sequence = 0
        self._tool_ids: dict[int, str] = {}

    def emit(
        self,
        kind: EventKind,
        *,
        block_index: int | None = None,
        tool_call_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> CanonicalEvent:
        try:
            selected_kind = kind if isinstance(kind, EventKind) else EventKind(kind)
        except ValueError:
            raise ManagerError(
                "canonical_event_invalid",
                f"Unsupported canonical event kind: {kind}.",
            ) from None
        if selected_kind in _TOOL_EVENTS and isinstance(block_index, int):
            existing = self._tool_ids.get(block_index)
            if existing is not None and existing != tool_call_id:
                raise ManagerError(
                    "canonical_event_invalid",
                    "Tool call id changed for an existing block index.",
                    {"block_index": block_index},
                )
            if tool_call_id is not None:
                self._tool_ids[block_index] = tool_call_id
        event = CanonicalEvent(
            kind=selected_kind,
            response_id=self.response_id,
            sequence=self._next_sequence,
            block_index=block_index,
            tool_call_id=tool_call_id,
            payload=payload or {},
        )
        self._next_sequence += 1
        return event


class RequestCommitTracker:
    """Record the first canonical event actually delivered to the host."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._committed = False
        self._committed_sequence: int | None = None

    @property
    def committed(self) -> bool:
        with self._lock:
            return self._committed

    @property
    def committed_sequence(self) -> int | None:
        with self._lock:
            return self._committed_sequence

    def observe(self, event: CanonicalEvent, *, delivered: bool) -> bool:
        if not delivered or not event.commits_request:
            return False
        with self._lock:
            if self._committed:
                return False
            self._committed = True
            self._committed_sequence = event.sequence
            return True


__all__ = [
    "CanonicalContentBlock",
    "CanonicalEvent",
    "CanonicalEventSequence",
    "CanonicalMessage",
    "CanonicalRequest",
    "CanonicalTool",
    "CanonicalUsage",
    "EventKind",
    "RequestCommitTracker",
    "ToolChoice",
]
