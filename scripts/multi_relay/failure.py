"""Secret-safe normalization of provider and transport failures."""

from __future__ import annotations

import json
import math
import re
import socket
import ssl
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, BinaryIO

from .credentials import redact_secret
from .protocols.base import ProviderErrorMetadata, extract_provider_error


MAX_ERROR_BODY_BYTES = 1_048_576
_MAX_DETAIL_TEXT = 512
_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{3,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|authorization|access[_-]?token|auth[_-]?token|client[_-]?secret|secret)\b\s*[:=]\s*[^\s,;]+"
    ),
)
_SENSITIVE_DETAIL_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "access_token",
        "auth_token",
        "token",
        "client_secret",
        "secret",
        "auth",
    }
)


class FailureClass(str, Enum):
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    AUTH_INVALID = "auth_invalid"
    MODEL_UNAVAILABLE = "model_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROTOCOL_ERROR = "protocol_error"
    REQUEST_INVALID = "request_invalid"
    CONTEXT_EXCEEDED = "context_exceeded"
    POLICY_BLOCKED = "policy_blocked"
    CANCELLED = "cancelled"
    NO_ELIGIBLE_TARGET = "no_eligible_target"


@dataclass(frozen=True)
class RetryDirective:
    """A classifier decision consumed by rotation without re-reading raw errors."""

    retry_same_target: bool = False
    failover_allowed: bool = False
    disable_credential: bool = False
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        value = self.retry_after_seconds
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            raise ValueError("retry_after_seconds must be finite and non-negative")
        if value is not None:
            object.__setattr__(self, "retry_after_seconds", float(value))

    def to_dict(self) -> dict[str, object]:
        return {
            "retry_same_target": self.retry_same_target,
            "failover_allowed": self.failover_allowed,
            "disable_credential": self.disable_credential,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass(frozen=True)
class NormalizedFailure:
    """One stable, secret-free failure returned to the gateway and selector."""

    failure_class: FailureClass
    code: str
    message: str
    retry: RetryDirective
    http_status: int | None = None
    provider_id: str | None = None
    committed: bool = False
    resumable: bool = False
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.committed and (
            self.resumable
            or self.retry.retry_same_target
            or self.retry.failover_allowed
        ):
            raise ValueError("committed failures cannot be retried, resumed, or failed over")
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    def to_dict(self) -> dict[str, object]:
        return {
            "failure_class": self.failure_class.value,
            "code": self.code,
            "message": self.message,
            "retry": self.retry.to_dict(),
            "http_status": self.http_status,
            "provider_id": self.provider_id,
            "committed": self.committed,
            "resumable": self.resumable,
            "details": dict(self.details),
        }


_CODE_CLASSES: dict[str, FailureClass] = {
    "insufficient_quota": FailureClass.QUOTA_EXHAUSTED,
    "quota_exhausted": FailureClass.QUOTA_EXHAUSTED,
    "quota_exceeded": FailureClass.QUOTA_EXHAUSTED,
    "billing_hard_limit_reached": FailureClass.QUOTA_EXHAUSTED,
    "insufficient_balance": FailureClass.QUOTA_EXHAUSTED,
    "account_balance_exhausted": FailureClass.QUOTA_EXHAUSTED,
    "rate_limit_error": FailureClass.RATE_LIMITED,
    "rate_limit_exceeded": FailureClass.RATE_LIMITED,
    "rate_limited": FailureClass.RATE_LIMITED,
    "too_many_requests": FailureClass.RATE_LIMITED,
    "invalid_api_key": FailureClass.AUTH_INVALID,
    "authentication_error": FailureClass.AUTH_INVALID,
    "authentication_failed": FailureClass.AUTH_INVALID,
    "permission_error": FailureClass.AUTH_INVALID,
    "model_not_found": FailureClass.MODEL_UNAVAILABLE,
    "model_unavailable": FailureClass.MODEL_UNAVAILABLE,
    "overloaded_error": FailureClass.PROVIDER_UNAVAILABLE,
    "service_unavailable": FailureClass.PROVIDER_UNAVAILABLE,
    "server_error": FailureClass.PROVIDER_UNAVAILABLE,
    "api_error": FailureClass.PROVIDER_UNAVAILABLE,
    "malformed_provider_response": FailureClass.PROTOCOL_ERROR,
    "protocol_error": FailureClass.PROTOCOL_ERROR,
    "invalid_request": FailureClass.REQUEST_INVALID,
    "invalid_request_error": FailureClass.REQUEST_INVALID,
    "bad_request": FailureClass.REQUEST_INVALID,
    "context_length_exceeded": FailureClass.CONTEXT_EXCEEDED,
    "context_window_exceeded": FailureClass.CONTEXT_EXCEEDED,
    "prompt_too_long": FailureClass.CONTEXT_EXCEEDED,
    "content_policy_violation": FailureClass.POLICY_BLOCKED,
    "content_policy_error": FailureClass.POLICY_BLOCKED,
    "safety_error": FailureClass.POLICY_BLOCKED,
    "moderation_blocked": FailureClass.POLICY_BLOCKED,
    "cancelled": FailureClass.CANCELLED,
    "canceled": FailureClass.CANCELLED,
}

_MESSAGES: dict[FailureClass, str] = {
    FailureClass.QUOTA_EXHAUSTED: "The selected provider target has no available quota.",
    FailureClass.RATE_LIMITED: "The selected provider target is temporarily rate limited.",
    FailureClass.AUTH_INVALID: "The selected provider rejected its stored credential.",
    FailureClass.MODEL_UNAVAILABLE: "The selected model is unavailable from this provider.",
    FailureClass.PROVIDER_UNAVAILABLE: "The selected provider is temporarily unavailable.",
    FailureClass.PROTOCOL_ERROR: "The provider returned an invalid or unsupported response.",
    FailureClass.REQUEST_INVALID: "The provider rejected the request as invalid.",
    FailureClass.CONTEXT_EXCEEDED: "The request exceeds the selected model's context window.",
    FailureClass.POLICY_BLOCKED: "The provider declined the request under its content policy.",
    FailureClass.CANCELLED: "The request was cancelled by the host.",
    FailureClass.NO_ELIGIBLE_TARGET: "No eligible provider target is currently available.",
}

_PATTERNS: tuple[tuple[FailureClass, tuple[str, ...]], ...] = (
    (
        FailureClass.CONTEXT_EXCEEDED,
        ("context length exceeded", "context window exceeded", "prompt is too long"),
    ),
    (
        FailureClass.POLICY_BLOCKED,
        ("content policy violation", "blocked by content policy", "safety policy refusal"),
    ),
    (
        FailureClass.QUOTA_EXHAUSTED,
        ("insufficient balance", "insufficient quota", "billing hard limit reached"),
    ),
    (
        FailureClass.RATE_LIMITED,
        ("rate limit exceeded", "too many requests"),
    ),
    (
        FailureClass.AUTH_INVALID,
        ("invalid api key", "authentication failed"),
    ),
    (
        FailureClass.MODEL_UNAVAILABLE,
        ("model not found", "requested model is unavailable"),
    ),
)


def _redact_text(value: object, secrets: Iterable[str]) -> str:
    known = tuple(item for item in secrets if isinstance(item, str) and item)
    text = redact_secret(value, *known)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = "".join(" " if character in "\r\n\0" else character for character in text)
    return text[:_MAX_DETAIL_TEXT]


def _safe_detail(value: object, secrets: tuple[str, ...], depth: int = 0) -> object:
    if depth >= 4:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "[NON_FINITE]"
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 32:
                result["truncated"] = True
                break
            safe_key = _redact_text(key, secrets)
            normalized_key = str(key).strip().casefold().replace("-", "_")
            result[safe_key] = (
                "[REDACTED]"
                if normalized_key in _SENSITIVE_DETAIL_KEYS
                else _safe_detail(item, secrets, depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_detail(item, secrets, depth + 1) for item in value[:32]]
    return f"<{type(value).__name__}>"


def _safe_code(value: str | None, secrets: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    normalized = _redact_text(value.strip().casefold().replace("-", "_"), secrets)
    return normalized if _SAFE_CODE.fullmatch(normalized) else None


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    selected = name.casefold()
    for key, value in headers.items():
        if isinstance(key, str) and key.casefold() == selected and isinstance(value, str):
            return value
    return None


def parse_retry_after(
    value: object,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse an HTTP Retry-After delay without consulting wall time in tests."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        delay = float(value)
        return delay if math.isfinite(delay) and delay >= 0 else None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped.isascii() and stripped.isdigit():
        return float(int(stripped))
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds())


def _read_error_body(
    body: bytes | bytearray | memoryview | str | BinaryIO | None,
    limit: int,
) -> tuple[bytes, bool, bool]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("error body limit must be a positive integer")
    try:
        if body is None:
            raw = b""
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        elif isinstance(body, (bytes, bytearray, memoryview)):
            raw = bytes(body)
        else:
            value = body.read(limit + 1)
            raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    except Exception:
        return b"", False, True
    return raw[:limit], len(raw) > limit, False


def _parse_error_body(
    raw: bytes,
    content_type: str | None,
    *,
    truncated: bool,
) -> tuple[object | None, str | None, bool, bool]:
    if not raw:
        return None, None, False, False
    media_type = (content_type or "").split(";", 1)[0].strip().casefold()
    is_json = media_type == "application/json" or media_type.endswith("+json")
    is_text = media_type.startswith("text/")
    if not media_type:
        is_json = raw.lstrip().startswith((b"{", b"["))
        is_text = not is_json
    if not is_json and not is_text:
        return None, None, False, True
    if truncated:
        return None, None, False, False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, None, True, False
    if not is_json:
        return None, text, False, False
    try:
        return json.loads(text), text, False, False
    except (json.JSONDecodeError, RecursionError):
        return None, text, True, False


def _class_from_status(status: int | None) -> FailureClass | None:
    if status == 402:
        return FailureClass.QUOTA_EXHAUSTED
    if status == 429:
        return FailureClass.RATE_LIMITED
    if status in {401, 403}:
        return FailureClass.AUTH_INVALID
    if status == 404:
        return FailureClass.MODEL_UNAVAILABLE
    if status is not None and (status >= 500 or status == 408):
        return FailureClass.PROVIDER_UNAVAILABLE
    if status in {400, 413, 422}:
        return FailureClass.REQUEST_INVALID
    return None


def _class_from_pattern(message: str | None) -> FailureClass | None:
    if not message:
        return None
    folded = message.casefold()
    for failure_class, patterns in _PATTERNS:
        if any(pattern in folded for pattern in patterns):
            return failure_class
    return None


def _retry_directive(
    failure_class: FailureClass,
    *,
    retry_after_seconds: float | None,
    committed: bool,
) -> RetryDirective:
    failover_classes = {
        FailureClass.QUOTA_EXHAUSTED,
        FailureClass.RATE_LIMITED,
        FailureClass.AUTH_INVALID,
        FailureClass.MODEL_UNAVAILABLE,
        FailureClass.PROVIDER_UNAVAILABLE,
        FailureClass.PROTOCOL_ERROR,
    }
    retry_same = failure_class == FailureClass.PROVIDER_UNAVAILABLE or (
        failure_class == FailureClass.RATE_LIMITED and retry_after_seconds is not None
    )
    if committed:
        retry_same = False
    return RetryDirective(
        retry_same_target=retry_same,
        failover_allowed=not committed and failure_class in failover_classes,
        disable_credential=failure_class == FailureClass.AUTH_INVALID,
        retry_after_seconds=retry_after_seconds,
    )


def classify_http_failure(
    status: int | None,
    body: bytes | bytearray | memoryview | str | BinaryIO | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    provider_error: ProviderErrorMetadata | None = None,
    provider_id: str | None = None,
    committed: bool = False,
    secrets: Iterable[str] = (),
    now: datetime | None = None,
    body_limit: int = MAX_ERROR_BODY_BYTES,
) -> NormalizedFailure:
    """Classify one bounded provider HTTP failure as code, then status, then pattern."""

    safe_secrets = tuple(item for item in secrets if isinstance(item, str) and item)
    raw, truncated, read_failed = _read_error_body(body, body_limit)
    raw_content_type = _header(headers, "content-type")
    content_type = _redact_text(raw_content_type, safe_secrets) if raw_content_type else None
    payload, body_text, malformed, unsupported_type = _parse_error_body(
        raw,
        content_type,
        truncated=truncated,
    )
    metadata = provider_error or extract_provider_error(payload)
    provider_code = _safe_code(metadata.code, safe_secrets)
    error_type = _safe_code(metadata.error_type, safe_secrets)
    generic_request_codes = {"invalid_request", "invalid_request_error", "bad_request"}
    classified = (
        None
        if provider_code in generic_request_codes
        else _CODE_CLASSES.get(provider_code or "")
    )
    if classified is None:
        classified = _class_from_status(status)
    if classified is None and provider_code in generic_request_codes:
        classified = FailureClass.REQUEST_INVALID
    if classified is None and not (truncated or read_failed or malformed or unsupported_type):
        classified = _class_from_pattern(metadata.message or body_text)
    if classified is None:
        classified = FailureClass.PROTOCOL_ERROR

    retry_after = metadata.retry_after_seconds
    if retry_after is None:
        retry_after = parse_retry_after(_header(headers, "retry-after"), now=now)
    derived_code = provider_code
    if derived_code is None:
        if truncated:
            derived_code = "error_body_too_large"
        elif read_failed:
            derived_code = "error_body_read_failed"
        elif malformed:
            derived_code = "malformed_provider_error"
        elif unsupported_type:
            derived_code = "unsupported_error_content_type"
        elif status is not None:
            derived_code = f"http_{status}"
        else:
            derived_code = "unknown_provider_error"

    details: dict[str, object] = {
        "committed": bool(committed),
        "resumable": False,
        "body_truncated": truncated,
        "malformed_body": malformed,
        "body_read_failed": read_failed,
        "unsupported_content_type": unsupported_type,
    }
    if provider_id is not None:
        details["provider_id"] = _redact_text(provider_id, safe_secrets)
    if status is not None:
        details["http_status"] = status
    if content_type is not None:
        details["content_type"] = content_type
    if provider_code is not None:
        details["provider_code"] = provider_code
    if error_type is not None:
        details["provider_error_type"] = error_type
    if retry_after is not None:
        details["retry_after_seconds"] = retry_after
    if metadata.message:
        details["provider_message"] = _redact_text(metadata.message, safe_secrets)
    if metadata.details:
        details["provider_details"] = _safe_detail(
            metadata.details,
            safe_secrets,
        )
    return NormalizedFailure(
        failure_class=classified,
        code=derived_code,
        message=_MESSAGES[classified],
        retry=_retry_directive(
            classified,
            retry_after_seconds=retry_after,
            committed=bool(committed),
        ),
        http_status=status,
        provider_id=(
            _redact_text(provider_id, safe_secrets) if provider_id is not None else None
        ),
        committed=bool(committed),
        resumable=False,
        details=details,
    )


def classify_transport_failure(
    error: BaseException,
    *,
    provider_id: str | None = None,
    committed: bool = False,
    secrets: Iterable[str] = (),
) -> NormalizedFailure:
    """Normalize DNS, connection, TLS, and timeout failures without echoing them."""

    safe_secrets = tuple(item for item in secrets if isinstance(item, str) and item)
    if isinstance(error, socket.gaierror):
        kind = "dns"
    elif isinstance(error, ConnectionRefusedError):
        kind = "connection_refused"
    elif isinstance(error, ssl.SSLError):
        kind = "tls"
    elif isinstance(error, (TimeoutError, socket.timeout)):
        kind = "timeout"
    elif isinstance(error, ConnectionError):
        kind = "connection"
    else:
        kind = "transport"
    details: dict[str, object] = {
        "transport": kind,
        "exception_type": type(error).__name__,
        "committed": bool(committed),
        "resumable": False,
    }
    if provider_id is not None:
        details["provider_id"] = _redact_text(provider_id, safe_secrets)
    failure_class = FailureClass.PROVIDER_UNAVAILABLE
    return NormalizedFailure(
        failure_class=failure_class,
        code=f"{kind}_error",
        message=_MESSAGES[failure_class],
        retry=_retry_directive(
            failure_class,
            retry_after_seconds=None,
            committed=bool(committed),
        ),
        provider_id=(
            _redact_text(provider_id, safe_secrets) if provider_id is not None else None
        ),
        committed=bool(committed),
        resumable=False,
        details=details,
    )


__all__ = [
    "FailureClass",
    "MAX_ERROR_BODY_BYTES",
    "NormalizedFailure",
    "RetryDirective",
    "classify_http_failure",
    "classify_transport_failure",
    "parse_retry_after",
]
