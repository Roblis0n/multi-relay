"""Validation boundaries shared by canonical requests and protocol adapters."""

from __future__ import annotations

import base64
import binascii
import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from .errors import ManagerError


ALLOWED_IMAGE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES_PER_REQUEST = 20
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_URL_LENGTH = 8192
MAX_JSON_SCHEMA_BYTES = 1024 * 1024
MAX_JSON_SCHEMA_DEPTH = 16
MAX_JSON_SCHEMA_PROPERTIES = 512

_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "enum",
        "items",
        "additionalProperties",
        "description",
        "title",
        "default",
    }
)
_JSON_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)


def freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json(item) for item in value]
    return value


def json_value(value: object, *, path: str = "$") -> Any:
    """Return an immutable, finite JSON value or raise a safe request error."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ManagerError(
            "request_invalid",
            "Request JSON contains a non-finite number.",
            {"path": path},
        )
    if isinstance(value, (list, tuple)):
        return tuple(
            json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ManagerError(
                    "request_invalid",
                    "Request JSON object keys must be non-empty strings.",
                    {"path": path},
                )
            normalized[key] = json_value(item, path=f"{path}.{key}")
        return MappingProxyType(normalized)
    raise ManagerError(
        "request_invalid",
        "Request contains a value that cannot be represented in JSON.",
        {"path": path},
    )


def validate_image_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IMAGE_URL_LENGTH:
        raise ManagerError("invalid_image", "Image URL is missing or too long.")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise ManagerError("invalid_image", "Image URL is malformed.") from None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ManagerError(
            "invalid_image",
            "Image URLs must use HTTPS and cannot contain credentials or fragments.",
        )
    return value


def decode_image_base64(media_type: object, data: object) -> bytes:
    if media_type not in ALLOWED_IMAGE_MIME_TYPES:
        raise ManagerError(
            "invalid_image",
            "Image MIME type is not supported.",
            {"media_type": media_type if isinstance(media_type, str) else None},
        )
    if not isinstance(data, str) or not data:
        raise ManagerError("invalid_image", "Base64 image data is missing.")
    maximum_encoded = ((MAX_IMAGE_BYTES + 2) // 3) * 4
    if len(data) > maximum_encoded:
        raise ManagerError(
            "image_too_large",
            "Decoded image exceeds the per-image size limit.",
            {"max_bytes": MAX_IMAGE_BYTES},
        )
    try:
        decoded = base64.b64decode(data.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise ManagerError("invalid_image", "Image data is not valid base64.") from None
    if len(decoded) > MAX_IMAGE_BYTES:
        raise ManagerError(
            "image_too_large",
            "Decoded image exceeds the per-image size limit.",
            {"max_bytes": MAX_IMAGE_BYTES},
        )
    return decoded


def _unsupported_schema(keyword: str, path: str) -> ManagerError:
    return ManagerError(
        "unsupported_json_schema",
        f"JSON Schema keyword is not supported: {keyword}.",
        {"keyword": keyword, "path": path},
    )


def _schema(
    value: object,
    *,
    path: str,
    depth: int,
    property_counter: list[int],
) -> dict[str, Any]:
    if depth > MAX_JSON_SCHEMA_DEPTH:
        raise ManagerError(
            "unsupported_json_schema",
            "JSON Schema nesting exceeds the supported limit.",
            {"path": path, "max_depth": MAX_JSON_SCHEMA_DEPTH},
        )
    if not isinstance(value, Mapping):
        raise ManagerError(
            "request_invalid",
            "Tool input schema must be a JSON object.",
            {"path": path},
        )
    unknown = sorted(set(value) - _SCHEMA_KEYWORDS)
    if unknown:
        raise _unsupported_schema(unknown[0], path)
    normalized: dict[str, Any] = {}

    if "type" in value:
        schema_type = value["type"]
        if not isinstance(schema_type, str) or schema_type not in _JSON_SCHEMA_TYPES:
            raise ManagerError(
                "request_invalid",
                "JSON Schema type is invalid.",
                {"path": f"{path}.type"},
            )
        normalized["type"] = schema_type

    if "properties" in value:
        raw_properties = value["properties"]
        if not isinstance(raw_properties, Mapping):
            raise ManagerError(
                "request_invalid",
                "JSON Schema properties must be an object.",
                {"path": f"{path}.properties"},
            )
        property_counter[0] += len(raw_properties)
        if property_counter[0] > MAX_JSON_SCHEMA_PROPERTIES:
            raise ManagerError(
                "unsupported_json_schema",
                "JSON Schema contains too many properties.",
                {"path": path, "max_properties": MAX_JSON_SCHEMA_PROPERTIES},
            )
        properties: dict[str, Any] = {}
        for raw_name, raw_schema in raw_properties.items():
            if not isinstance(raw_name, str) or not raw_name:
                raise ManagerError(
                    "request_invalid",
                    "JSON Schema property names must be non-empty strings.",
                    {"path": f"{path}.properties"},
                )
            properties[raw_name] = _schema(
                raw_schema,
                path=f"{path}.properties.{raw_name}",
                depth=depth + 1,
                property_counter=property_counter,
            )
        normalized["properties"] = properties

    if "required" in value:
        required = value["required"]
        if (
            not isinstance(required, (list, tuple))
            or any(not isinstance(item, str) or not item for item in required)
            or len(required) != len(set(required))
        ):
            raise ManagerError(
                "request_invalid",
                "JSON Schema required must contain unique property names.",
                {"path": f"{path}.required"},
            )
        properties = value.get("properties", {})
        if isinstance(properties, Mapping) and set(required) - set(properties):
            raise ManagerError(
                "request_invalid",
                "JSON Schema required references an unknown property.",
                {"path": f"{path}.required"},
            )
        normalized["required"] = list(required)

    if "enum" in value:
        enum = value["enum"]
        if not isinstance(enum, (list, tuple)) or not enum:
            raise ManagerError(
                "request_invalid",
                "JSON Schema enum must be a non-empty array.",
                {"path": f"{path}.enum"},
            )
        normalized["enum"] = [
            thaw_json(json_value(item, path=f"{path}.enum[{index}]"))
            for index, item in enumerate(enum)
        ]

    if "items" in value:
        normalized["items"] = _schema(
            value["items"],
            path=f"{path}.items",
            depth=depth + 1,
            property_counter=property_counter,
        )

    if "additionalProperties" in value:
        additional = value["additionalProperties"]
        if isinstance(additional, bool):
            normalized["additionalProperties"] = additional
        elif isinstance(additional, Mapping):
            normalized["additionalProperties"] = _schema(
                additional,
                path=f"{path}.additionalProperties",
                depth=depth + 1,
                property_counter=property_counter,
            )
        else:
            raise ManagerError(
                "request_invalid",
                "JSON Schema additionalProperties must be boolean or an object.",
                {"path": f"{path}.additionalProperties"},
            )

    for keyword in ("description", "title"):
        if keyword in value:
            selected = value[keyword]
            if not isinstance(selected, str):
                raise ManagerError(
                    "request_invalid",
                    f"JSON Schema {keyword} must be a string.",
                    {"path": f"{path}.{keyword}"},
                )
            normalized[keyword] = selected

    if "default" in value:
        normalized["default"] = thaw_json(
            json_value(value["default"], path=f"{path}.default")
        )
    return normalized


def validate_json_schema(value: object) -> Mapping[str, Any]:
    """Validate and freeze the explicitly supported JSON Schema subset."""

    normalized = _schema(value, path="$", depth=0, property_counter=[0])
    if normalized.get("type") not in {None, "object"}:
        raise ManagerError(
            "request_invalid",
            "Tool input schema root must have type object.",
            {"path": "$.type"},
        )
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > MAX_JSON_SCHEMA_BYTES:
        raise ManagerError(
            "unsupported_json_schema",
            "JSON Schema exceeds the supported size limit.",
            {"max_bytes": MAX_JSON_SCHEMA_BYTES},
        )
    return freeze_json(normalized)


__all__ = [
    "ALLOWED_IMAGE_MIME_TYPES",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGES_PER_REQUEST",
    "MAX_TOTAL_IMAGE_BYTES",
    "decode_image_base64",
    "freeze_json",
    "json_value",
    "thaw_json",
    "validate_image_url",
    "validate_json_schema",
]
