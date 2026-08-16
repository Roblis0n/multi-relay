"""Protocol-neutral provider error metadata contracts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol


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
