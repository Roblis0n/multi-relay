"""Protocol-neutral provider error metadata contracts."""

from __future__ import annotations

import math
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from ..canonical import CanonicalEvent, CanonicalRequest
from ..errors import ManagerError


@dataclass(frozen=True)
class ProviderErrorMetadata:
    """Stable, non-transport metadata extracted by a provider adapter."""

    code: str | None = None
    error_type: str | None = None
    message: str | None = None
    retry_after_seconds: float | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        retry_after = self.retry_after_seconds
        if retry_after is not None and (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, (int, float))
            or not math.isfinite(float(retry_after))
            or float(retry_after) < 0
        ):
            raise ValueError("retry_after_seconds must be a finite non-negative number")
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        if retry_after is not None:
            object.__setattr__(self, "retry_after_seconds", float(retry_after))


class ProviderAdapter(Protocol):
    """The error-extraction boundary implemented by concrete protocol adapters."""

    def extract_error(
        self,
        payload: object,
        headers: Mapping[str, str],
    ) -> ProviderErrorMetadata: ...


class ProtocolAdapter(ProviderAdapter, Protocol):
    """Complete host-neutral boundary implemented by every wire protocol."""

    protocol: str

    def build_request(
        self,
        request: CanonicalRequest,
        *,
        model: str,
    ) -> Mapping[str, Any]: ...

    def parse_response(
        self,
        payload: object,
        *,
        response_id: str | None = None,
    ) -> tuple[CanonicalEvent, ...]: ...

    def iter_events(
        self,
        chunks: Iterable[bytes | str],
        *,
        response_id: str | None = None,
    ) -> Iterator[CanonicalEvent]: ...

    def classify_error_metadata(
        self,
        payload: object,
        headers: Mapping[str, str],
    ) -> ProviderErrorMetadata: ...

    def discover_models(self, payload: object) -> tuple[str, ...]: ...


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _retry_seconds(value: object) -> float | None:
    if isinstance(value, str) and value.isascii() and value.strip().isdigit():
        value = int(value.strip())
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        return None
    return float(value)


def extract_provider_error(payload: object) -> ProviderErrorMetadata:
    """Extract common OpenAI/DeepSeek/Anthropic error envelope fields.

    Concrete adapters may replace this conservative extractor when a provider
    publishes a more specific stable error contract.
    """

    if not isinstance(payload, Mapping):
        return ProviderErrorMetadata()
    nested = payload.get("error")
    error = nested if isinstance(nested, Mapping) else payload
    code = _string(error.get("code"))
    error_type = _string(error.get("type"))
    if code is None:
        code = error_type
    if code is None:
        code = _string(payload.get("code"))
    if error_type is None:
        top_type = _string(payload.get("type"))
        error_type = None if top_type == "error" else top_type
    message = _string(error.get("message")) or _string(payload.get("message"))
    retry_after = None
    for key in ("retry_after_seconds", "retry_after"):
        retry_after = _retry_seconds(error.get(key))
        if retry_after is None:
            retry_after = _retry_seconds(payload.get(key))
        if retry_after is not None:
            break
    return ProviderErrorMetadata(
        code=code,
        error_type=error_type,
        message=message,
        retry_after_seconds=retry_after,
    )


def discover_model_ids(payload: object) -> tuple[str, ...]:
    """Extract a deterministic model id list from common discovery envelopes."""

    if not isinstance(payload, Mapping):
        return ()
    raw_models = payload.get("data", payload.get("models", ()))
    if not isinstance(raw_models, (list, tuple)):
        return ()
    models: set[str] = set()
    for item in raw_models:
        selected = item.get("id") if isinstance(item, Mapping) else item
        if isinstance(selected, str) and selected.strip():
            models.add(selected.strip())
    return tuple(sorted(models))


def _event_boundary(buffer: bytes) -> tuple[int, int] | None:
    candidates = tuple(
        (index, len(marker))
        for marker in (b"\r\n\r\n", b"\n\n", b"\r\r")
        if (index := buffer.find(marker)) >= 0
    )
    return min(candidates) if candidates else None


def iter_sse_json(
    chunks: Iterable[bytes | str],
    *,
    max_event_bytes: int = 1024 * 1024,
) -> Iterator[object]:
    """Incrementally decode bounded SSE JSON across arbitrary byte boundaries."""

    if (
        isinstance(max_event_bytes, bool)
        or not isinstance(max_event_bytes, int)
        or max_event_bytes < 1
    ):
        raise ValueError("max_event_bytes must be a positive integer")
    buffer = b""
    closer = getattr(chunks, "close", None)

    def decode_record(record: bytes) -> object | None:
        normalized = record.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        data_lines: list[bytes] = []
        for line in normalized.split(b"\n"):
            if line.startswith(b"data:"):
                value = line[5:]
                data_lines.append(value[1:] if value.startswith(b" ") else value)
        if not data_lines:
            return None
        data = b"\n".join(data_lines)
        if data == b"[DONE]":
            return _DONE
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise ManagerError(
                "protocol_error",
                "Upstream SSE event contains invalid JSON.",
            ) from None

    try:
        for chunk in chunks:
            if isinstance(chunk, str):
                raw = chunk.encode("utf-8")
            elif isinstance(chunk, (bytes, bytearray)):
                raw = bytes(chunk)
            else:
                raise ManagerError(
                    "protocol_error",
                    "Upstream SSE yielded an unsupported chunk type.",
                )
            buffer += raw
            while (boundary := _event_boundary(buffer)) is not None:
                index, marker_size = boundary
                record = buffer[:index]
                buffer = buffer[index + marker_size :]
                if len(record) > max_event_bytes:
                    raise ManagerError(
                        "protocol_error",
                        "Upstream SSE event exceeds the configured size limit.",
                    )
                decoded = decode_record(record)
                if decoded is _DONE:
                    return
                if decoded is not None:
                    yield decoded
            if len(buffer) > max_event_bytes:
                raise ManagerError(
                    "protocol_error",
                    "Upstream SSE event exceeds the configured size limit.",
                )
        if buffer.strip():
            if len(buffer) > max_event_bytes:
                raise ManagerError(
                    "protocol_error",
                    "Upstream SSE event exceeds the configured size limit.",
                )
            decoded = decode_record(buffer)
            if decoded is not None and decoded is not _DONE:
                yield decoded
    finally:
        if callable(closer):
            closer()


_DONE = object()


__all__ = [
    "ProtocolAdapter",
    "ProviderAdapter",
    "ProviderErrorMetadata",
    "discover_model_ids",
    "extract_provider_error",
    "iter_sse_json",
]
