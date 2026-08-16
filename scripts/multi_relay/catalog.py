"""Validated, secret-free Multi Relay catalog domain model."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from .credentials import credential_target, legacy_credential_target
from .errors import ManagerError


CATALOG_SCHEMA_VERSION = 2
LEGACY_CATALOG_SCHEMA_VERSION = 1
PROTOCOLS = frozenset(
    {
        "codex-native",
        "responses-compatible",
        "chat-completions-compatible",
        "deepseek-chat",
        "anthropic-messages",
    }
)
CAPABILITIES = frozenset(
    {"text", "vision", "audio", "tool_calling", "server_web_search"}
)
HOST_NAMES = ("codex", "claude-code")
HOSTS = frozenset(HOST_NAMES)
POOL_STRATEGIES = frozenset({"sticky", "timed"})
TRUST_LEVELS = frozenset({"standard", "high"})
SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})
REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
AUTH_MODES = frozenset({"vault", "none", "host-native"})
HOST_SCOPES = frozenset({"user", "project"})

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MCP_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_CAPABILITY_ORDER = {
    name: index
    for index, name in enumerate(
        ("text", "vision", "audio", "tool_calling", "server_web_search")
    )
}
_REASONING_ORDER = {
    name: index
    for index, name in enumerate(
        ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
    )
}
_LEGACY_TO_CAPABILITY = {"tools": "tool_calling", "web": "server_web_search"}
_CAPABILITY_TO_LEGACY = {
    "tool_calling": "tools",
    "server_web_search": "web",
}

_CATALOG_FIELDS = frozenset(
    {
        "schema_version",
        "concurrency",
        "providers",
        "credentials",
        "targets",
        "pools",
        "agents",
        "hosts",
    }
)
_LEGACY_CATALOG_FIELDS = frozenset(
    {"schema_version", "concurrency", "providers", "agents"}
)
_PROVIDER_REQUIRED_FIELDS = frozenset(
    {"id", "name", "protocol", "base_url", "auth_mode", "capabilities", "enabled"}
)
_PROVIDER_OPTIONAL_FIELDS = frozenset({"models_endpoint"})
_LEGACY_PROVIDER_FIELDS = frozenset(
    {
        "id",
        "name",
        "protocol",
        "base_url",
        "auth",
        "capabilities",
        "context_window",
        "enabled",
    }
)
_CREDENTIAL_FIELDS = frozenset(
    {"id", "provider_id", "vault_target", "enabled", "created_at", "label"}
)
_TARGET_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "provider_id",
        "model",
        "credential_id",
        "capabilities",
        "context_window",
        "reasoning_efforts",
        "trust",
        "host_compatibility",
        "enabled",
    }
)
_TARGET_OPTIONAL_FIELDS = frozenset(
    {"protocol", "max_output_tokens", "metadata"}
)
_COOLDOWN_FIELDS = frozenset(
    {
        "quota_seconds",
        "rate_limit_seconds",
        "auth_seconds",
        "provider_seconds",
    }
)
_POOL_FIELDS = frozenset(
    {
        "id",
        "targets",
        "strategy",
        "duration_seconds",
        "max_rate_limit_wait_seconds",
        "cooldown",
        "required_capabilities",
        "host_compatibility",
        "enabled",
    }
)
_AGENT_REQUIRED_FIELDS = frozenset(
    {
        "name",
        "description",
        "developer_instructions",
        "pool_id",
        "required_capabilities",
        "trust",
        "priority",
        "sandbox_mode",
        "hosts",
    }
)
_AGENT_OPTIONAL_FIELDS = frozenset(
    {
        "fallback_pool_id",
        "reasoning_effort",
        "context_window",
        "tools",
        "mcp_servers",
        "skills",
    }
)
_LEGACY_AGENT_FIELDS = frozenset(
    {
        "name",
        "description",
        "provider",
        "model",
        "reasoning_effort",
        "context_window",
        "capabilities",
        "trust",
        "priority",
        "sandbox_mode",
        "mcp_servers",
        "skills",
        "developer_instructions",
    }
)
_HOST_REQUIRED_FIELDS = frozenset({"enabled"})
_HOST_OPTIONAL_FIELDS = frozenset({"scope", "default_pool"})

_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "auth_token",
        "bearer_token",
        "client_secret",
        "password",
        "secret",
        "token",
    }
)
_SAFE_TOKEN_FIELDS = frozenset(
    {"max_output_tokens", "credential_id", "vault_target", "auth_mode"}
)
_BEARER_VALUE = re.compile(r"(?i)(?:^|\s)bearer\s+\S+")
_KEY_VALUE = re.compile(r"(?i)(?:api[_ -]?key|authorization)\s*[:=]\s*\S+")
_SK_VALUE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}")


def _invalid(message: str, *, field_name: str | None = None) -> ManagerError:
    details = {"field": field_name} if field_name is not None else None
    return ManagerError("catalog_invalid", message, details)


def _strict_mapping(
    value: object,
    required: frozenset[str],
    label: str,
    *,
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(f"{label} must be a JSON object.")
    actual = set(value)
    allowed = required | optional
    unknown = sorted(actual - allowed)
    missing = sorted(required - actual)
    if unknown:
        raise _invalid(f"{label} contains unsupported fields: {', '.join(unknown)}.")
    if missing:
        raise _invalid(f"{label} is missing required fields: {', '.join(missing)}.")
    return value


def _identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise _invalid(
            f"{field_name} must use lowercase ASCII letters, digits, underscores, or hyphens.",
            field_name=field_name,
        )
    return value


def _nonempty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{field_name} must be a non-empty string.", field_name=field_name)
    return value.strip()


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, field_name)


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid(f"{field_name} must be a boolean.", field_name=field_name)
    return value


def _integer(
    value: object,
    field_name: str,
    *,
    minimum: int = 0,
    optional: bool = False,
) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise _invalid(
            f"{field_name} must be a {qualifier} integer.",
            field_name=field_name,
        )
    return value


def _string_tuple(
    value: object,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "" if allow_empty else " non-empty"
        raise _invalid(
            f"{field_name} must be a{qualifier} JSON array.",
            field_name=field_name,
        )
    result: list[str] = []
    for item in value:
        result.append(_nonempty_string(item, field_name))
    if len(result) != len(set(result)):
        raise _invalid(f"{field_name} contains duplicate entries.", field_name=field_name)
    return tuple(result)


def _capability_set(
    value: object,
    field_name: str,
    *,
    legacy: bool = False,
    allow_empty: bool = False,
) -> frozenset[str]:
    raw = _string_tuple(value, field_name, allow_empty=allow_empty)
    normalized = tuple(_LEGACY_TO_CAPABILITY.get(item, item) for item in raw) if legacy else raw
    result = frozenset(normalized)
    if len(result) != len(normalized):
        raise _invalid(
            f"{field_name} contains duplicate capabilities.",
            field_name=field_name,
        )
    unsupported = sorted(result - CAPABILITIES)
    if unsupported:
        raise ManagerError(
            "invalid_capability",
            f"Unsupported capabilities: {', '.join(unsupported)}.",
            {"capabilities": unsupported},
        )
    return result


def _capability_list(value: frozenset[str]) -> list[str]:
    return sorted(value, key=_CAPABILITY_ORDER.__getitem__)


def _legacy_capability_set(value: frozenset[str]) -> frozenset[str]:
    return frozenset(_CAPABILITY_TO_LEGACY.get(item, item) for item in value)


def _reasoning_tuple(value: object, field_name: str) -> tuple[str, ...]:
    raw = _string_tuple(value, field_name, allow_empty=True)
    unsupported = sorted(set(raw) - REASONING_EFFORTS)
    if unsupported:
        raise _invalid(
            f"Unsupported reasoning efforts: {', '.join(unsupported)}.",
            field_name=field_name,
        )
    return tuple(sorted(raw, key=_REASONING_ORDER.__getitem__))


def _host_tuple(value: object, field_name: str) -> tuple[str, ...]:
    raw = _string_tuple(value, field_name)
    unsupported = sorted(set(raw) - HOSTS)
    if unsupported:
        raise _invalid(
            f"Unsupported hosts: {', '.join(unsupported)}.",
            field_name=field_name,
        )
    return tuple(name for name in HOST_NAMES if name in raw)


def _validate_upstream_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise ManagerError("unsafe_provider_url", "Provider URL is malformed.") from None
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ManagerError(
            "unsafe_provider_url",
            "Provider URLs cannot contain credentials, query strings, or fragments.",
        )
    if not parsed.netloc or not host:
        raise ManagerError("unsafe_provider_url", "Provider URL must include a host.")
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ManagerError(
            "unsafe_provider_url",
            "Provider URL must use HTTPS; HTTP is allowed only for loopback providers.",
        )
    return value.rstrip("/")


def _secret_field(name: str) -> bool:
    snake_case = re.sub(r"(?<!^)(?=[A-Z])", "_", name.strip())
    normalized = re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")
    if normalized in _SAFE_TOKEN_FIELDS:
        return False
    return (
        normalized in _SECRET_FIELD_NAMES
        or normalized.endswith("_api_key")
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
        or normalized.endswith("_token")
    )


def _secret_value(value: str) -> bool:
    return bool(
        _BEARER_VALUE.search(value)
        or _KEY_VALUE.search(value)
        or _SK_VALUE.search(value)
    )


def _assert_secret_free(value: object) -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            if isinstance(raw_key, str) and _secret_field(raw_key):
                raise ManagerError(
                    "secret_not_allowed",
                    "Catalogs store credential references only; import secrets into the operating-system vault.",
                )
            _assert_secret_free(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_secret_free(item)
        return
    if isinstance(value, str) and _secret_value(value):
        raise ManagerError(
            "secret_not_allowed",
            "Catalogs store credential references only; import secrets into the operating-system vault.",
        )


def _json_value(value: object, field_name: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid(
                f"{field_name} contains a non-finite number.",
                field_name=field_name,
            )
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item, field_name) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            if not isinstance(key, str) or not key:
                raise _invalid(
                    f"{field_name} contains an invalid key.",
                    field_name=field_name,
                )
            normalized[key] = _json_value(item, f"{field_name}.{key}")
        return normalized
    raise _invalid(
        f"{field_name} contains a value that cannot be represented in JSON.",
        field_name=field_name,
    )


def _mcp_value(value: object, field_name: str) -> Any:
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid(
                f"{field_name} contains a non-finite number.",
                field_name=field_name,
            )
        return value
    if isinstance(value, (list, tuple)):
        return [_mcp_value(item, field_name) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            if not isinstance(key, str) or not key:
                raise _invalid(
                    f"{field_name} contains an invalid key.",
                    field_name=field_name,
                )
            normalized[key] = _mcp_value(item, f"{field_name}.{key}")
        return normalized
    raise _invalid(
        f"{field_name} contains a value that cannot be rendered to TOML.",
        field_name=field_name,
    )


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _freeze_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _mcp_servers(value: object) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        raise _invalid("mcp_servers must be a JSON object.", field_name="mcp_servers")
    result: dict[str, dict[str, Any]] = {}
    for raw_name, raw_config in sorted(value.items(), key=lambda pair: str(pair[0])):
        if not isinstance(raw_name, str) or not _MCP_IDENTIFIER.fullmatch(raw_name):
            raise _invalid(
                "MCP server names contain unsupported characters.",
                field_name="mcp_servers",
            )
        if not isinstance(raw_config, Mapping):
            raise _invalid(
                "Each MCP server must be a JSON object.",
                field_name="mcp_servers",
            )
        config = {
            key: _mcp_value(item, f"mcp_servers.{raw_name}.{key}")
            for key, item in sorted(raw_config.items())
        }
        if (
            "url" in config
            and isinstance(config.get("url"), str)
            and config["url"].strip()
        ):
            _validate_upstream_url(config["url"].strip())
        result[raw_name] = config
    return MappingProxyType(
        {
            name: _freeze_value(config)
            for name, config in result.items()
        }
    )


def _skills(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise _invalid("skills must be a JSON array.", field_name="skills")
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            path = item.strip()
            if not path:
                raise _invalid("Skill paths cannot be empty.", field_name="skills")
            result.append(
                MappingProxyType({"path": path, "enabled": True})
            )
            continue
        data = _strict_mapping(
            item,
            frozenset({"path"}),
            "Skill",
            optional=frozenset({"enabled"}),
        )
        path = _nonempty_string(data["path"], "skills.path")
        enabled = _boolean(data.get("enabled", True), "skills.enabled")
        result.append(
            MappingProxyType({"path": path, "enabled": enabled})
        )
    return tuple(result)


def _tools(value: object) -> tuple[str, ...]:
    return _string_tuple(value, "agent.tools", allow_empty=True)


def _has_concrete_mcp(servers: Mapping[str, Mapping[str, Any]]) -> bool:
    return any(
        any(
            isinstance(config.get(key), str) and bool(config[key].strip())
            for key in ("url", "command")
        )
        for config in servers.values()
    )


def _validate_created_at(value: object) -> str:
    created_at = _nonempty_string(value, "credential.created_at")
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid(
            "credential.created_at must be an ISO-8601 timestamp.",
            field_name="credential.created_at",
        ) from None
    return created_at


def _require_array(value: object, field_name: str) -> list[object]:
    if not isinstance(value, list):
        raise _invalid(f"{field_name} must be a JSON array.", field_name=field_name)
    return value


def _unique(items: tuple[Any, ...], attribute: str, code: str, label: str) -> None:
    values = [getattr(item, attribute).casefold() for item in items]
    if len(values) != len(set(values)):
        raise ManagerError(code, f"{label} identifiers must be unique.")


@dataclass(frozen=True)
class ProviderSpec:
    """One upstream service and its maximum declared capabilities."""

    id: str
    name: str
    protocol: str
    base_url: str | None
    auth_mode: str
    capabilities: frozenset[str]
    models_endpoint: str | None
    enabled: bool
    _legacy_context_window: int | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_dict(cls, value: object) -> "ProviderSpec":
        if isinstance(value, Mapping) and set(value) == _LEGACY_PROVIDER_FIELDS:
            return cls.from_legacy_dict(value)
        data = _strict_mapping(
            value,
            _PROVIDER_REQUIRED_FIELDS,
            "Provider",
            optional=_PROVIDER_OPTIONAL_FIELDS,
        )
        provider_id = _identifier(data["id"], "provider.id")
        name = _nonempty_string(data["name"], "provider.name")
        protocol = _nonempty_string(data["protocol"], "provider.protocol")
        if protocol not in PROTOCOLS:
            raise _invalid(
                f"Unsupported provider protocol: {protocol}.",
                field_name="provider.protocol",
            )
        auth_mode = _nonempty_string(data["auth_mode"], "provider.auth_mode")
        if auth_mode not in AUTH_MODES:
            raise _invalid(
                f"Unsupported provider auth mode: {auth_mode}.",
                field_name="provider.auth_mode",
            )
        raw_url = data["base_url"]
        if protocol == "codex-native":
            if raw_url is not None:
                raise _invalid(
                    "codex-native providers must not define base_url.",
                    field_name="provider.base_url",
                )
            if auth_mode != "host-native":
                raise _invalid(
                    "codex-native providers must use host-native authentication.",
                    field_name="provider.auth_mode",
                )
            base_url = None
        else:
            base_url = _validate_upstream_url(
                _nonempty_string(raw_url, "provider.base_url")
            )
            if auth_mode == "host-native":
                raise _invalid(
                    "HTTP providers must use vault or no authentication.",
                    field_name="provider.auth_mode",
                )
        capabilities = _capability_set(
            data["capabilities"],
            "provider.capabilities",
        )
        models_endpoint = _optional_string(
            data.get("models_endpoint"),
            "provider.models_endpoint",
        )
        if models_endpoint is not None and not models_endpoint.startswith("/"):
            raise _invalid(
                "provider.models_endpoint must be an absolute URL path.",
                field_name="provider.models_endpoint",
            )
        enabled = _boolean(data["enabled"], "provider.enabled")
        return cls(
            id=provider_id,
            name=name,
            protocol=protocol,
            base_url=base_url,
            auth_mode=auth_mode,
            capabilities=capabilities,
            models_endpoint=models_endpoint,
            enabled=enabled,
        )

    @classmethod
    def from_legacy_dict(cls, value: object) -> "ProviderSpec":
        data = _strict_mapping(value, _LEGACY_PROVIDER_FIELDS, "Legacy provider")
        provider_id = _identifier(data["id"], "provider.id")
        name = _nonempty_string(data["name"], "provider.name")
        protocol = _nonempty_string(data["protocol"], "provider.protocol")
        if protocol not in PROTOCOLS - {"anthropic-messages"}:
            raise _invalid(
                f"Unsupported provider protocol: {protocol}.",
                field_name="provider.protocol",
            )
        auth = _nonempty_string(data["auth"], "provider.auth")
        if auth not in {"codex", "vault", "none"}:
            raise _invalid(
                f"Unsupported provider auth mode: {auth}.",
                field_name="provider.auth",
            )
        if protocol == "codex-native" and auth != "codex":
            raise _invalid(
                "codex-native providers must use Codex authentication.",
                field_name="provider.auth",
            )
        if protocol != "codex-native" and auth == "codex":
            raise _invalid(
                "Custom providers must use vault or no authentication.",
                field_name="provider.auth",
            )
        if protocol == "deepseek-chat" and auth != "vault":
            raise _invalid(
                "DeepSeek chat providers require vault authentication.",
                field_name="provider.auth",
            )
        capabilities = _capability_set(
            data["capabilities"],
            "provider.capabilities",
            legacy=True,
        )
        if protocol in {"chat-completions-compatible", "deepseek-chat"} and capabilities & {
            "vision",
            "audio",
        }:
            raise ManagerError(
                "capability_unsupported",
                f"{protocol} is a text adapter and cannot declare vision or audio.",
                {"provider": provider_id},
            )
        raw_url = data["base_url"]
        if protocol == "codex-native":
            if raw_url is not None:
                raise _invalid(
                    "codex-native providers must not define base_url.",
                    field_name="provider.base_url",
                )
            base_url = None
        else:
            base_url = _validate_upstream_url(
                _nonempty_string(raw_url, "provider.base_url")
            )
        context_window = _integer(
            data["context_window"],
            "provider.context_window",
            minimum=1,
            optional=True,
        )
        return cls(
            id=provider_id,
            name=name,
            protocol=protocol,
            base_url=base_url,
            auth_mode="host-native" if auth == "codex" else auth,
            capabilities=capabilities,
            models_endpoint=None,
            enabled=_boolean(data["enabled"], "provider.enabled"),
            _legacy_context_window=context_window,
        )

    @property
    def auth(self) -> str:
        """Schema 1 compatibility projection for existing adapters."""

        return "codex" if self.auth_mode == "host-native" else self.auth_mode

    @property
    def context_window(self) -> int | None:
        """Schema 1 compatibility projection for existing renderers."""

        return self._legacy_context_window

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "auth_mode": self.auth_mode,
            "capabilities": _capability_list(self.capabilities),
            "models_endpoint": self.models_endpoint,
            "enabled": self.enabled,
        }

    def to_legacy_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "auth": self.auth,
            "capabilities": sorted(_legacy_capability_set(self.capabilities)),
            "context_window": self.context_window,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class CredentialRef:
    """A non-secret pointer to one credential in the operating-system vault."""

    id: str
    provider_id: str
    vault_target: str
    enabled: bool
    created_at: str
    label: str

    @classmethod
    def from_dict(cls, value: object) -> "CredentialRef":
        data = _strict_mapping(value, _CREDENTIAL_FIELDS, "Credential")
        return cls(
            id=_identifier(data["id"], "credential.id"),
            provider_id=_identifier(data["provider_id"], "credential.provider_id"),
            vault_target=_nonempty_string(
                data["vault_target"],
                "credential.vault_target",
            ),
            enabled=_boolean(data["enabled"], "credential.enabled"),
            created_at=_validate_created_at(data["created_at"]),
            label=_nonempty_string(data["label"], "credential.label"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "vault_target": self.vault_target,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "label": self.label,
        }


@dataclass(frozen=True)
class ExecutionTarget:
    """The smallest rotation unit: provider, model, and credential together."""

    id: str
    provider_id: str
    protocol: str | None
    model: str | None
    credential_id: str | None
    capabilities: frozenset[str]
    context_window: int | None
    max_output_tokens: int | None
    reasoning_efforts: tuple[str, ...]
    trust: str
    host_compatibility: tuple[str, ...]
    enabled: bool
    metadata: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: object) -> "ExecutionTarget":
        data = _strict_mapping(
            value,
            _TARGET_REQUIRED_FIELDS,
            "Execution target",
            optional=_TARGET_OPTIONAL_FIELDS,
        )
        protocol = _optional_string(data.get("protocol"), "target.protocol")
        if protocol is not None and protocol not in PROTOCOLS:
            raise _invalid(
                f"Unsupported target protocol: {protocol}.",
                field_name="target.protocol",
            )
        trust = _nonempty_string(data["trust"], "target.trust")
        if trust not in TRUST_LEVELS:
            raise _invalid(
                f"Unsupported trust level: {trust}.",
                field_name="target.trust",
            )
        metadata = _json_value(data.get("metadata", {}), "target.metadata")
        if not isinstance(metadata, dict):
            raise _invalid(
                "target.metadata must be a JSON object.",
                field_name="target.metadata",
            )
        _assert_secret_free(metadata)
        return cls(
            id=_identifier(data["id"], "target.id"),
            provider_id=_identifier(data["provider_id"], "target.provider_id"),
            protocol=protocol,
            model=_optional_string(data["model"], "target.model"),
            credential_id=(
                None
                if data["credential_id"] is None
                else _identifier(data["credential_id"], "target.credential_id")
            ),
            capabilities=_capability_set(
                data["capabilities"],
                "target.capabilities",
            ),
            context_window=_integer(
                data["context_window"],
                "target.context_window",
                minimum=1,
                optional=True,
            ),
            max_output_tokens=_integer(
                data.get("max_output_tokens"),
                "target.max_output_tokens",
                minimum=1,
                optional=True,
            ),
            reasoning_efforts=_reasoning_tuple(
                data["reasoning_efforts"],
                "target.reasoning_efforts",
            ),
            trust=trust,
            host_compatibility=_host_tuple(
                data["host_compatibility"],
                "target.host_compatibility",
            ),
            enabled=_boolean(data["enabled"], "target.enabled"),
            metadata=_freeze_value(metadata),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "provider_id": self.provider_id,
            "protocol": self.protocol,
            "model": self.model,
            "credential_id": self.credential_id,
            "capabilities": _capability_list(self.capabilities),
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "reasoning_efforts": list(self.reasoning_efforts),
            "trust": self.trust,
            "host_compatibility": list(self.host_compatibility),
            "enabled": self.enabled,
            "metadata": _json_value(self.metadata, "target.metadata"),
        }


@dataclass(frozen=True)
class CooldownPolicy:
    quota_seconds: int
    rate_limit_seconds: int
    auth_seconds: int
    provider_seconds: int

    @classmethod
    def from_dict(cls, value: object) -> "CooldownPolicy":
        data = _strict_mapping(value, _COOLDOWN_FIELDS, "Cooldown policy")
        return cls(
            quota_seconds=_integer(
                data["quota_seconds"],
                "pool.cooldown.quota_seconds",
            ),
            rate_limit_seconds=_integer(
                data["rate_limit_seconds"],
                "pool.cooldown.rate_limit_seconds",
            ),
            auth_seconds=_integer(
                data["auth_seconds"],
                "pool.cooldown.auth_seconds",
            ),
            provider_seconds=_integer(
                data["provider_seconds"],
                "pool.cooldown.provider_seconds",
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "quota_seconds": self.quota_seconds,
            "rate_limit_seconds": self.rate_limit_seconds,
            "auth_seconds": self.auth_seconds,
            "provider_seconds": self.provider_seconds,
        }


@dataclass(frozen=True)
class TargetPool:
    """An ordered target collection with sticky or timed rotation semantics."""

    id: str
    targets: tuple[str, ...]
    strategy: str
    duration_seconds: int | None
    max_rate_limit_wait_seconds: int
    cooldown: CooldownPolicy
    required_capabilities: frozenset[str]
    host_compatibility: tuple[str, ...]
    enabled: bool

    @classmethod
    def from_dict(cls, value: object) -> "TargetPool":
        data = _strict_mapping(value, _POOL_FIELDS, "Target pool")
        strategy = _nonempty_string(data["strategy"], "pool.strategy")
        if strategy not in POOL_STRATEGIES:
            raise _invalid(
                f"Unsupported pool strategy: {strategy}.",
                field_name="pool.strategy",
            )
        duration = _integer(
            data["duration_seconds"],
            "pool.duration_seconds",
            minimum=1,
            optional=True,
        )
        if strategy == "sticky" and duration is not None:
            raise _invalid(
                "Sticky pools cannot define duration_seconds.",
                field_name="pool.duration_seconds",
            )
        if strategy == "timed" and duration is None:
            raise _invalid(
                "Timed pools require duration_seconds.",
                field_name="pool.duration_seconds",
            )
        target_ids = tuple(
            _identifier(item, "pool.targets")
            for item in _string_tuple(data["targets"], "pool.targets")
        )
        return cls(
            id=_identifier(data["id"], "pool.id"),
            targets=target_ids,
            strategy=strategy,
            duration_seconds=duration,
            max_rate_limit_wait_seconds=_integer(
                data["max_rate_limit_wait_seconds"],
                "pool.max_rate_limit_wait_seconds",
            ),
            cooldown=CooldownPolicy.from_dict(data["cooldown"]),
            required_capabilities=_capability_set(
                data["required_capabilities"],
                "pool.required_capabilities",
            ),
            host_compatibility=_host_tuple(
                data["host_compatibility"],
                "pool.host_compatibility",
            ),
            enabled=_boolean(data["enabled"], "pool.enabled"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "targets": list(self.targets),
            "strategy": self.strategy,
            "duration_seconds": self.duration_seconds,
            "max_rate_limit_wait_seconds": self.max_rate_limit_wait_seconds,
            "cooldown": self.cooldown.to_dict(),
            "required_capabilities": _capability_list(
                self.required_capabilities
            ),
            "host_compatibility": list(self.host_compatibility),
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class HostConfig:
    """One host integration configuration."""

    host: str
    enabled: bool
    scope: str | None
    default_pool: str | None

    @classmethod
    def from_dict(cls, host: str, value: object) -> "HostConfig":
        if host not in HOSTS:
            raise _invalid(f"Unsupported host: {host}.", field_name="hosts")
        data = _strict_mapping(
            value,
            _HOST_REQUIRED_FIELDS,
            f"Host {host}",
            optional=_HOST_OPTIONAL_FIELDS,
        )
        scope = _optional_string(data.get("scope"), f"hosts.{host}.scope")
        if scope is not None and scope not in HOST_SCOPES:
            raise _invalid(
                f"Unsupported host scope: {scope}.",
                field_name=f"hosts.{host}.scope",
            )
        default_pool = data.get("default_pool")
        if default_pool is not None:
            default_pool = _identifier(
                default_pool,
                f"hosts.{host}.default_pool",
            )
        return cls(
            host=host,
            enabled=_boolean(data["enabled"], f"hosts.{host}.enabled"),
            scope=scope,
            default_pool=default_pool,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "scope": self.scope,
            "default_pool": self.default_pool,
        }


@dataclass(frozen=True)
class AgentProfile:
    """A host-visible role that routes through a target pool."""

    name: str
    description: str
    developer_instructions: str
    pool_id: str
    required_capabilities: frozenset[str]
    fallback_pool_id: str | None
    reasoning_effort: str | None
    context_window: int | None
    trust: str
    priority: int
    sandbox_mode: str
    tools: tuple[str, ...]
    mcp_servers: Mapping[str, Mapping[str, Any]]
    skills: tuple[Mapping[str, Any], ...]
    hosts: tuple[str, ...]
    _legacy_provider: str | None = field(default=None, repr=False, compare=False)
    _legacy_model: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_dict(cls, value: object) -> "AgentProfile":
        if isinstance(value, Mapping) and set(value) == _LEGACY_AGENT_FIELDS:
            return cls.from_legacy_dict(value)
        data = _strict_mapping(
            value,
            _AGENT_REQUIRED_FIELDS,
            "Agent profile",
            optional=_AGENT_OPTIONAL_FIELDS,
        )
        effort = _optional_string(
            data.get("reasoning_effort"),
            "agent.reasoning_effort",
        )
        if effort is not None and effort not in REASONING_EFFORTS:
            raise _invalid(
                f"Unsupported reasoning effort: {effort}.",
                field_name="agent.reasoning_effort",
            )
        trust = _nonempty_string(data["trust"], "agent.trust")
        if trust not in TRUST_LEVELS:
            raise _invalid(
                f"Unsupported trust level: {trust}.",
                field_name="agent.trust",
            )
        sandbox_mode = _nonempty_string(
            data["sandbox_mode"],
            "agent.sandbox_mode",
        )
        if sandbox_mode not in SANDBOX_MODES:
            raise _invalid(
                f"Unsupported sandbox mode: {sandbox_mode}.",
                field_name="agent.sandbox_mode",
            )
        priority = _integer(data["priority"], "agent.priority")
        return cls(
            name=_identifier(data["name"], "agent.name"),
            description=_nonempty_string(data["description"], "agent.description"),
            developer_instructions=_nonempty_string(
                data["developer_instructions"],
                "agent.developer_instructions",
            ),
            pool_id=_identifier(data["pool_id"], "agent.pool_id"),
            required_capabilities=_capability_set(
                data["required_capabilities"],
                "agent.required_capabilities",
            ),
            fallback_pool_id=(
                None
                if data.get("fallback_pool_id") is None
                else _identifier(
                    data["fallback_pool_id"],
                    "agent.fallback_pool_id",
                )
            ),
            reasoning_effort=effort,
            context_window=_integer(
                data.get("context_window"),
                "agent.context_window",
                minimum=1,
                optional=True,
            ),
            trust=trust,
            priority=priority,
            sandbox_mode=sandbox_mode,
            tools=_tools(data.get("tools", [])),
            mcp_servers=_mcp_servers(data.get("mcp_servers", {})),
            skills=_skills(data.get("skills", [])),
            hosts=_host_tuple(data["hosts"], "agent.hosts"),
        )

    @classmethod
    def from_legacy_dict(cls, value: object) -> "AgentProfile":
        data = _strict_mapping(value, _LEGACY_AGENT_FIELDS, "Legacy agent")
        effort = _optional_string(
            data["reasoning_effort"],
            "agent.reasoning_effort",
        )
        if effort is not None and effort not in REASONING_EFFORTS:
            raise _invalid(
                f"Unsupported reasoning effort: {effort}.",
                field_name="agent.reasoning_effort",
            )
        capabilities = _capability_set(
            data["capabilities"],
            "agent.capabilities",
            legacy=True,
        )
        trust = _nonempty_string(data["trust"], "agent.trust")
        if trust not in TRUST_LEVELS:
            raise _invalid(
                f"Unsupported trust level: {trust}.",
                field_name="agent.trust",
            )
        sandbox_mode = _nonempty_string(
            data["sandbox_mode"],
            "agent.sandbox_mode",
        )
        if sandbox_mode not in SANDBOX_MODES:
            raise _invalid(
                f"Unsupported sandbox mode: {sandbox_mode}.",
                field_name="agent.sandbox_mode",
            )
        servers = _mcp_servers(data["mcp_servers"])
        if "server_web_search" in capabilities and not _has_concrete_mcp(servers):
            raise ManagerError(
                "web_requires_mcp",
                "A web-capable agent must configure a concrete MCP server.",
                {"agent": data.get("name")},
            )
        name = _identifier(data["name"], "agent.name")
        return cls(
            name=name,
            description=_nonempty_string(data["description"], "agent.description"),
            developer_instructions=_nonempty_string(
                data["developer_instructions"],
                "agent.developer_instructions",
            ),
            pool_id=f"{name}-pool",
            required_capabilities=capabilities,
            fallback_pool_id=None,
            reasoning_effort=effort,
            context_window=_integer(
                data["context_window"],
                "agent.context_window",
                minimum=1,
                optional=True,
            ),
            trust=trust,
            priority=_integer(data["priority"], "agent.priority"),
            sandbox_mode=sandbox_mode,
            tools=(),
            mcp_servers=servers,
            skills=_skills(data["skills"]),
            hosts=("codex",),
            _legacy_provider=_identifier(data["provider"], "agent.provider"),
            _legacy_model=_optional_string(data["model"], "agent.model"),
        )

    @property
    def provider(self) -> str:
        """Schema 1 compatibility projection for existing host adapters."""

        return self._legacy_provider or ""

    @property
    def model(self) -> str | None:
        """Schema 1 compatibility projection for existing host adapters."""

        return self._legacy_model

    @property
    def capabilities(self) -> frozenset[str]:
        """Schema 1 capability names for existing routing call sites."""

        return _legacy_capability_set(self.required_capabilities)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "developer_instructions": self.developer_instructions,
            "pool_id": self.pool_id,
            "required_capabilities": _capability_list(
                self.required_capabilities
            ),
            "fallback_pool_id": self.fallback_pool_id,
            "reasoning_effort": self.reasoning_effort,
            "context_window": self.context_window,
            "trust": self.trust,
            "priority": self.priority,
            "sandbox_mode": self.sandbox_mode,
            "tools": list(self.tools),
            "mcp_servers": {
                name: _mcp_value(config, f"mcp_servers.{name}")
                for name, config in sorted(self.mcp_servers.items())
            },
            "skills": [dict(skill) for skill in self.skills],
            "hosts": list(self.hosts),
        }

    def to_legacy_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "context_window": self.context_window,
            "capabilities": sorted(self.capabilities),
            "trust": self.trust,
            "priority": self.priority,
            "sandbox_mode": self.sandbox_mode,
            "mcp_servers": {
                name: _mcp_value(config, f"mcp_servers.{name}")
                for name, config in sorted(self.mcp_servers.items())
            },
            "skills": [dict(skill) for skill in self.skills],
            "developer_instructions": self.developer_instructions,
        }


# Preserve the public import used by the existing Codex host modules.
AgentSpec = AgentProfile


@dataclass(frozen=True)
class Catalog:
    """Validated schema 2 catalog used by management and routing."""

    schema_version: int
    concurrency: int
    providers: tuple[ProviderSpec, ...]
    credentials: tuple[CredentialRef, ...]
    targets: tuple[ExecutionTarget, ...]
    pools: tuple[TargetPool, ...]
    agents: tuple[AgentProfile, ...]
    hosts: Mapping[str, HostConfig]

    @classmethod
    def from_dict(cls, value: object) -> "Catalog":
        if not isinstance(value, Mapping):
            raise _invalid("Catalog must be a JSON object.")
        _assert_secret_free(value)
        schema_version = value.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ManagerError(
                "unsupported_catalog_schema",
                f"Catalog schema must be {CATALOG_SCHEMA_VERSION}.",
                {"schema_version": schema_version},
            )
        if schema_version == LEGACY_CATALOG_SCHEMA_VERSION:
            return cls._from_schema2_dict(legacy_catalog_to_schema2_dict(value))
        if schema_version != CATALOG_SCHEMA_VERSION:
            raise ManagerError(
                "unsupported_catalog_schema",
                f"Catalog schema must be {CATALOG_SCHEMA_VERSION}.",
                {"schema_version": schema_version},
            )
        return cls._from_schema2_dict(value)

    @classmethod
    def from_legacy_dict(cls, value: object) -> "Catalog":
        """Explicit schema 1 compatibility entry point for the migrator."""

        if not isinstance(value, Mapping):
            raise _invalid("Legacy catalog must be a JSON object.")
        _assert_secret_free(value)
        return cls._from_schema2_dict(legacy_catalog_to_schema2_dict(value))

    @classmethod
    def _from_schema2_dict(cls, value: object) -> "Catalog":
        data = _strict_mapping(value, _CATALOG_FIELDS, "Catalog")
        if data["schema_version"] != CATALOG_SCHEMA_VERSION:
            raise ManagerError(
                "unsupported_catalog_schema",
                f"Catalog schema must be {CATALOG_SCHEMA_VERSION}.",
                {"schema_version": data["schema_version"]},
            )
        concurrency = _integer(
            data["concurrency"],
            "catalog.concurrency",
            minimum=1,
        )
        providers = tuple(
            ProviderSpec.from_dict(item)
            for item in _require_array(data["providers"], "catalog.providers")
        )
        credentials = tuple(
            CredentialRef.from_dict(item)
            for item in _require_array(
                data["credentials"],
                "catalog.credentials",
            )
        )
        targets = tuple(
            ExecutionTarget.from_dict(item)
            for item in _require_array(data["targets"], "catalog.targets")
        )
        pools = tuple(
            TargetPool.from_dict(item)
            for item in _require_array(data["pools"], "catalog.pools")
        )
        agents = tuple(
            AgentProfile.from_dict(item)
            for item in _require_array(data["agents"], "catalog.agents")
        )
        raw_hosts = data["hosts"]
        if not isinstance(raw_hosts, Mapping):
            raise _invalid("catalog.hosts must be a JSON object.", field_name="hosts")
        unknown_hosts = sorted(set(raw_hosts) - HOSTS)
        if unknown_hosts:
            raise _invalid(
                f"Unsupported hosts: {', '.join(unknown_hosts)}.",
                field_name="hosts",
            )
        hosts = {
            name: HostConfig.from_dict(name, raw_hosts[name])
            for name in HOST_NAMES
            if name in raw_hosts
        }

        _unique(providers, "id", "duplicate_provider", "Provider")
        credential_keys = [
            (item.provider_id.casefold(), item.id.casefold())
            for item in credentials
        ]
        if len(credential_keys) != len(set(credential_keys)):
            raise ManagerError(
                "duplicate_credential",
                "Credential identifiers must be unique within each provider.",
            )
        _unique(targets, "id", "duplicate_target", "Target")
        _unique(pools, "id", "duplicate_pool", "Pool")
        _unique(agents, "name", "duplicate_agent", "Agent")

        provider_map = {item.id: item for item in providers}
        credential_map = {
            (item.provider_id, item.id): item for item in credentials
        }
        target_map = {item.id: item for item in targets}
        pool_map = {item.id: item for item in pools}

        for credential in credentials:
            if credential.provider_id not in provider_map:
                raise ManagerError(
                    "unknown_provider",
                    f"Credential {credential.id} references an unknown provider.",
                    {"credential": credential.id, "provider": credential.provider_id},
                )

        for target in targets:
            provider = provider_map.get(target.provider_id)
            if provider is None:
                raise ManagerError(
                    "unknown_provider",
                    f"Target {target.id} references an unknown provider.",
                    {"target": target.id, "provider": target.provider_id},
                )
            credential = (
                credential_map.get(
                    (target.provider_id, target.credential_id)
                )
                if target.credential_id is not None
                else None
            )
            if target.credential_id is not None and credential is None:
                raise ManagerError(
                    "unknown_credential",
                    f"Target {target.id} references an unknown credential.",
                    {"target": target.id, "credential": target.credential_id},
                )
            if credential is not None and credential.provider_id != provider.id:
                raise ManagerError(
                    "credential_provider_mismatch",
                    f"Target {target.id} uses a credential from another provider.",
                    {"target": target.id, "credential": credential.id},
                )
            if provider.auth_mode == "vault" and credential is None:
                raise ManagerError(
                    "credential_required",
                    f"Target {target.id} requires a credential reference.",
                    {"target": target.id, "provider": provider.id},
                )
            if provider.auth_mode != "vault" and credential is not None:
                raise ManagerError(
                    "credential_not_allowed",
                    f"Target {target.id} cannot use a credential reference.",
                    {"target": target.id, "provider": provider.id},
                )
            unsupported = target.capabilities - provider.capabilities
            if unsupported:
                raise ManagerError(
                    "capability_unsupported",
                    f"Target {target.id} exceeds provider {provider.id} capabilities.",
                    {"target": target.id, "capabilities": sorted(unsupported)},
                )
            effective_protocol = target.protocol or provider.protocol
            if (
                target.protocol is not None
                and target.protocol != provider.protocol
            ):
                raise ManagerError(
                    "protocol_mismatch",
                    f"Target {target.id} cannot override its provider protocol.",
                    {
                        "target": target.id,
                        "provider": provider.id,
                    },
                )
            if effective_protocol == "codex-native":
                if target.host_compatibility != ("codex",):
                    raise ManagerError(
                        "host_incompatible",
                        f"Native target {target.id} is available only to Codex.",
                        {"target": target.id},
                    )
            elif target.model is None:
                raise ManagerError(
                    "invalid_model",
                    f"Target {target.id} requires an explicit model.",
                    {"target": target.id},
                )

        for pool in pools:
            selected_targets: list[ExecutionTarget] = []
            for target_id in pool.targets:
                target = target_map.get(target_id)
                if target is None:
                    raise ManagerError(
                        "unknown_target",
                        f"Pool {pool.id} references an unknown target.",
                        {"pool": pool.id, "target": target_id},
                    )
                selected_targets.append(target)
            for target in selected_targets:
                unsupported = pool.required_capabilities - target.capabilities
                if unsupported:
                    raise ManagerError(
                        "capability_unsupported",
                        f"Pool {pool.id} target {target.id} lacks required capabilities.",
                        {"pool": pool.id, "target": target.id},
                    )
                if not set(pool.host_compatibility).issubset(
                    target.host_compatibility
                ):
                    raise ManagerError(
                        "host_incompatible",
                        f"Pool {pool.id} exceeds target {target.id} host compatibility.",
                        {"pool": pool.id, "target": target.id},
                    )

        for host in hosts.values():
            if host.default_pool is None:
                continue
            pool = pool_map.get(host.default_pool)
            if pool is None:
                raise ManagerError(
                    "unknown_pool",
                    f"Host {host.host} references an unknown default pool.",
                    {"host": host.host, "pool": host.default_pool},
                )
            if host.enabled and host.host not in pool.host_compatibility:
                raise ManagerError(
                    "host_incompatible",
                    f"Host {host.host} cannot use pool {pool.id}.",
                    {"host": host.host, "pool": pool.id},
                )

        resolved_agents: list[AgentProfile] = []
        for agent in agents:
            pool = pool_map.get(agent.pool_id)
            if pool is None:
                raise ManagerError(
                    "unknown_pool",
                    f"Agent {agent.name} references an unknown pool.",
                    {"agent": agent.name, "pool": agent.pool_id},
                )
            if agent.fallback_pool_id is not None:
                fallback = pool_map.get(agent.fallback_pool_id)
                if fallback is None:
                    raise ManagerError(
                        "unknown_pool",
                        f"Agent {agent.name} references an unknown fallback pool.",
                        {"agent": agent.name, "pool": agent.fallback_pool_id},
                    )
                if fallback.id == pool.id:
                    raise _invalid(
                        "Agent fallback_pool_id must differ from pool_id.",
                        field_name="agent.fallback_pool_id",
                    )
            if not set(agent.hosts).issubset(pool.host_compatibility):
                raise ManagerError(
                    "host_incompatible",
                    f"Agent {agent.name} exceeds pool {pool.id} host compatibility.",
                    {"agent": agent.name, "pool": pool.id},
                )
            eligible: list[ExecutionTarget] = []
            for target_id in pool.targets:
                target = target_map[target_id]
                if not agent.required_capabilities.issubset(target.capabilities):
                    continue
                if (
                    agent.context_window is not None
                    and target.context_window is not None
                    and target.context_window < agent.context_window
                ):
                    continue
                if agent.trust == "high" and target.trust != "high":
                    continue
                if (
                    agent.reasoning_effort is not None
                    and target.reasoning_efforts
                    and agent.reasoning_effort not in target.reasoning_efforts
                ):
                    continue
                if not set(agent.hosts).issubset(target.host_compatibility):
                    continue
                eligible.append(target)
            if not eligible:
                raise ManagerError(
                    "no_eligible_target",
                    f"Agent {agent.name} has no eligible target in pool {pool.id}.",
                    {"agent": agent.name, "pool": pool.id},
                )
            primary = eligible[0]
            resolved_agents.append(
                replace(
                    agent,
                    _legacy_provider=primary.provider_id,
                    _legacy_model=primary.model,
                )
            )

        return cls(
            schema_version=CATALOG_SCHEMA_VERSION,
            concurrency=concurrency,
            providers=providers,
            credentials=credentials,
            targets=targets,
            pools=pools,
            agents=tuple(resolved_agents),
            hosts=MappingProxyType(hosts),
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "concurrency": self.concurrency,
            "providers": [item.to_dict() for item in self.providers],
            "credentials": [item.to_dict() for item in self.credentials],
            "targets": [item.to_dict() for item in self.targets],
            "pools": [item.to_dict() for item in self.pools],
            "agents": [item.to_dict() for item in self.agents],
            "hosts": {
                name: self.hosts[name].to_dict()
                for name in HOST_NAMES
                if name in self.hosts
            },
        }
        _assert_secret_free(payload)
        return payload

    def provider(self, provider_id: str) -> ProviderSpec:
        for item in self.providers:
            if item.id.casefold() == provider_id.casefold():
                return item
        raise ManagerError(
            "unknown_provider",
            f"Unknown provider: {provider_id}.",
            {"provider": provider_id},
        )

    def credential(
        self,
        credential_id: str,
        provider_id: str | None = None,
    ) -> CredentialRef:
        """Resolve a provider-scoped credential id, rejecting unscoped ambiguity."""

        matches = [
            item
            for item in self.credentials
            if item.id.casefold() == credential_id.casefold()
            and (
                provider_id is None
                or item.provider_id.casefold() == provider_id.casefold()
            )
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ManagerError(
                "ambiguous_credential",
                f"Credential {credential_id} exists for multiple providers.",
                {"credential": credential_id},
            )
        raise ManagerError(
            "unknown_credential",
            f"Unknown credential: {credential_id}.",
            {
                "credential": credential_id,
                "provider": provider_id,
            },
        )

    def target(self, target_id: str) -> ExecutionTarget:
        for item in self.targets:
            if item.id.casefold() == target_id.casefold():
                return item
        raise ManagerError(
            "unknown_target",
            f"Unknown target: {target_id}.",
            {"target": target_id},
        )

    def pool(self, pool_id: str) -> TargetPool:
        for item in self.pools:
            if item.id.casefold() == pool_id.casefold():
                return item
        raise ManagerError(
            "unknown_pool",
            f"Unknown pool: {pool_id}.",
            {"pool": pool_id},
        )

    def agent(self, name: str) -> AgentProfile:
        for item in self.agents:
            if item.name.casefold() == name.casefold():
                return item
        raise ManagerError(
            "invalid_role",
            f"Unknown agent: {name}.",
            {"agent": name},
        )


def _legacy_vault_target(provider_id: str) -> str:
    return legacy_credential_target(provider_id)


def _target_identifier(
    provider_id: str,
    model: str | None,
    signature: tuple[object, ...],
    used: set[str],
) -> str:
    raw_model = model or "native"
    slug = re.sub(r"[^a-z0-9]+", "-", raw_model.casefold()).strip("-") or "model"
    base = f"{provider_id}-{slug}"[:56].rstrip("-")
    candidate = base
    if candidate in used:
        digest = hashlib.sha256(repr(signature).encode("utf-8")).hexdigest()[:8]
        candidate = f"{base[:47].rstrip('-')}-{digest}"
    suffix = 2
    unique = candidate
    while unique in used:
        unique = f"{candidate[:58]}-{suffix}"
        suffix += 1
    used.add(unique)
    return unique


def legacy_catalog_to_schema2_dict(value: object) -> dict[str, object]:
    """Return the deterministic schema 2 representation of a schema 1 catalog."""

    data = _strict_mapping(value, _LEGACY_CATALOG_FIELDS, "Legacy catalog")
    schema = data["schema_version"]
    if (
        isinstance(schema, bool)
        or not isinstance(schema, int)
        or schema != LEGACY_CATALOG_SCHEMA_VERSION
    ):
        raise ManagerError(
            "unsupported_catalog_schema",
            "Legacy catalog schema must be 1.",
            {"schema_version": schema},
        )
    concurrency = _integer(
        data["concurrency"],
        "catalog.concurrency",
        minimum=1,
    )
    raw_providers = _require_array(data["providers"], "catalog.providers")
    raw_agents = _require_array(data["agents"], "catalog.agents")

    raw_provider_ids = [
        item.get("id") for item in raw_providers if isinstance(item, Mapping)
    ]
    folded_provider_ids = [
        item.casefold() for item in raw_provider_ids if isinstance(item, str)
    ]
    if len(folded_provider_ids) != len(set(folded_provider_ids)):
        raise ManagerError("duplicate_provider", "Provider identifiers must be unique.")
    providers = tuple(ProviderSpec.from_legacy_dict(item) for item in raw_providers)
    _unique(providers, "id", "duplicate_provider", "Provider")
    provider_map = {item.id: item for item in providers}

    raw_agent_ids = [
        item.get("name") for item in raw_agents if isinstance(item, Mapping)
    ]
    folded_agent_ids = [
        item.casefold() for item in raw_agent_ids if isinstance(item, str)
    ]
    if len(folded_agent_ids) != len(set(folded_agent_ids)):
        raise ManagerError("duplicate_agent", "Agent names must be unique.")
    legacy_agents = tuple(
        AgentProfile.from_legacy_dict(item) for item in raw_agents
    )
    _unique(legacy_agents, "name", "duplicate_agent", "Agent")

    credentials: list[dict[str, object]] = []
    credential_by_provider: dict[str, str] = {}
    for provider in providers:
        if provider.auth_mode != "vault":
            continue
        credential_id = "primary"
        credential_by_provider[provider.id] = credential_id
        credentials.append(
            {
                "id": credential_id,
                "provider_id": provider.id,
                "vault_target": _legacy_vault_target(provider.id),
                "enabled": True,
                "created_at": "1970-01-01T00:00:00Z",
                "label": "Migrated credential",
            }
        )

    target_groups: dict[tuple[object, ...], dict[str, object]] = {}
    agent_signatures: dict[str, tuple[object, ...]] = {}
    for agent in legacy_agents:
        provider = provider_map.get(agent.provider)
        if provider is None:
            raise ManagerError(
                "unknown_provider",
                f"Agent {agent.name} references an unknown provider.",
                {"agent": agent.name, "provider": agent.provider},
            )
        unsupported = agent.required_capabilities - provider.capabilities
        if unsupported:
            raise ManagerError(
                "capability_unsupported",
                f"Agent {agent.name} exceeds provider {provider.id} capabilities.",
                {"agent": agent.name, "capabilities": sorted(unsupported)},
            )
        if provider.protocol != "codex-native" and agent.model is None:
            raise ManagerError(
                "invalid_model",
                f"Agent {agent.name} requires an explicit provider model.",
                {"agent": agent.name, "provider": provider.id},
            )
        if (
            agent.context_window is not None
            and provider.context_window is not None
            and agent.context_window > provider.context_window
        ):
            raise ManagerError(
                "capability_unsupported",
                f"Agent {agent.name} context exceeds provider {provider.id}.",
                {"agent": agent.name, "provider": provider.id},
            )
        credential_id = credential_by_provider.get(provider.id)
        signature: tuple[object, ...] = (
            provider.id,
            agent.model,
            credential_id,
            tuple(_capability_list(agent.required_capabilities)),
        )
        agent_signatures[agent.name] = signature
        group = target_groups.setdefault(
            signature,
            {
                "provider": provider,
                "model": agent.model,
                "credential_id": credential_id,
                "capabilities": agent.required_capabilities,
                "contexts": [],
                "reasoning_efforts": set(),
                "high_trust": False,
            },
        )
        if agent.context_window is not None:
            contexts = group["contexts"]
            assert isinstance(contexts, list)
            contexts.append(agent.context_window)
        if agent.reasoning_effort is not None:
            efforts = group["reasoning_efforts"]
            assert isinstance(efforts, set)
            efforts.add(agent.reasoning_effort)
        if agent.trust == "high":
            group["high_trust"] = True

    targets: list[dict[str, object]] = []
    target_by_signature: dict[tuple[object, ...], str] = {}
    used_target_ids: set[str] = set()
    for signature in sorted(target_groups, key=repr):
        group = target_groups[signature]
        provider = group["provider"]
        assert isinstance(provider, ProviderSpec)
        model = group["model"]
        assert model is None or isinstance(model, str)
        target_id = _target_identifier(
            provider.id,
            model,
            signature,
            used_target_ids,
        )
        target_by_signature[signature] = target_id
        contexts = group["contexts"]
        assert isinstance(contexts, list)
        efforts = group["reasoning_efforts"]
        assert isinstance(efforts, set)
        target_context = (
            provider.context_window
            if provider.context_window is not None
            else max(contexts, default=None)
        )
        capabilities = group["capabilities"]
        assert isinstance(capabilities, frozenset)
        targets.append(
            {
                "id": target_id,
                "provider_id": provider.id,
                "protocol": None,
                "model": model,
                "credential_id": group["credential_id"],
                "capabilities": _capability_list(capabilities),
                "context_window": target_context,
                "max_output_tokens": None,
                "reasoning_efforts": sorted(
                    efforts,
                    key=lambda item: _REASONING_ORDER[item],
                ),
                "trust": "high" if group["high_trust"] else "standard",
                "host_compatibility": ["codex"],
                "enabled": True,
                "metadata": {"migrated_from_schema": 1},
            }
        )

    pools: list[dict[str, object]] = []
    agents: list[dict[str, object]] = []
    for agent in legacy_agents:
        target_id = target_by_signature[agent_signatures[agent.name]]
        pool_id = f"{agent.name}-pool"
        pools.append(
            {
                "id": pool_id,
                "targets": [target_id],
                "strategy": "sticky",
                "duration_seconds": None,
                "max_rate_limit_wait_seconds": 30,
                "cooldown": {
                    "quota_seconds": 86400,
                    "rate_limit_seconds": 60,
                    "auth_seconds": 3600,
                    "provider_seconds": 30,
                },
                "required_capabilities": _capability_list(
                    agent.required_capabilities
                ),
                "host_compatibility": ["codex"],
                "enabled": True,
            }
        )
        profile = replace(agent, pool_id=pool_id)
        agents.append(profile.to_dict())

    first_pool = pools[0]["id"] if pools else None
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "concurrency": concurrency,
        "providers": [item.to_dict() for item in providers],
        "credentials": credentials,
        "targets": targets,
        "pools": pools,
        "agents": agents,
        "hosts": {
            "codex": {
                "enabled": True,
                "scope": None,
                "default_pool": first_pool,
            },
            "claude-code": {
                "enabled": False,
                "scope": "user",
                "default_pool": None,
            },
        },
    }


def load_catalog(source: Path | str | bytes | bytearray | Mapping[str, Any]) -> Catalog:
    """Load a catalog from a path, JSON value, or already-decoded mapping."""

    if isinstance(source, Mapping):
        return Catalog.from_dict(source)
    try:
        if isinstance(source, Path):
            raw = source.read_bytes()
        elif isinstance(source, (bytes, bytearray)):
            raw = bytes(source)
        elif isinstance(source, str):
            stripped = source.lstrip()
            raw = (
                source.encode("utf-8")
                if stripped.startswith("{")
                else Path(source).read_bytes()
            )
        else:
            raise _invalid("Unsupported catalog source.")
        decoded = json.loads(raw.decode("utf-8"))
    except ManagerError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagerError(
            "catalog_invalid",
            "Catalog JSON could not be read or decoded.",
        ) from exc
    return Catalog.from_dict(decoded)


def save_catalog_bytes(catalog: Catalog) -> bytes:
    """Return deterministic, human-readable JSON without credential material."""

    payload = catalog.to_dict()
    _assert_secret_free(payload)
    return (
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _canonical_required(
    value: set[str] | frozenset[str],
) -> frozenset[str] | None:
    normalized = frozenset(_LEGACY_TO_CAPABILITY.get(item, item) for item in value)
    if normalized - CAPABILITIES:
        return None
    return normalized


def route_agent(
    catalog: Catalog,
    required_capabilities: set[str] | frozenset[str],
    high_risk: bool = False,
) -> AgentProfile | None:
    """Select a qualifying role deterministically, or require the parent."""

    required = _canonical_required(required_capabilities)
    if required is None:
        return None
    candidates: list[AgentProfile] = []
    for agent in catalog.agents:
        if not required.issubset(agent.required_capabilities):
            continue
        if high_risk and agent.trust != "high":
            continue
        pool = catalog.pool(agent.pool_id)
        if not pool.enabled:
            continue
        eligible = False
        for target_id in pool.targets:
            target = catalog.target(target_id)
            provider = catalog.provider(target.provider_id)
            credential_enabled = (
                target.credential_id is None
                or catalog.credential(
                    target.credential_id,
                    provider_id=target.provider_id,
                ).enabled
            )
            if (
                target.enabled
                and provider.enabled
                and credential_enabled
                and required.issubset(target.capabilities)
                and (not high_risk or target.trust == "high")
            ):
                eligible = True
                break
        if eligible:
            candidates.append(agent)
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item.priority, item.name.casefold()))


_ROLE_DETAILS = {
    "default": (
        "General-purpose DeepSeek child for independent bounded tasks.",
        "Complete only the independent bounded task assigned to the default role.",
        10,
    ),
    "worker": (
        "DeepSeek implementation child for isolated file ownership.",
        "Edit only files explicitly assigned to the worker role; never overlap another child's write set.",
        20,
    ),
    "explorer": (
        "DeepSeek research child for read-heavy repository exploration.",
        "Treat the explorer role as read-heavy: inspect and report, and do not edit files unless the parent explicitly grants an isolated write set.",
        30,
    ),
}


def _agent_profile(
    name: str,
    pool_id: str,
    *,
    capabilities: list[str],
    hosts: list[str],
    trust: str,
    sandbox_mode: str,
    description: str,
    instructions: str,
    priority: int,
) -> dict[str, object]:
    return {
        "name": name,
        "description": description,
        "developer_instructions": instructions,
        "pool_id": pool_id,
        "required_capabilities": capabilities,
        "fallback_pool_id": None,
        "reasoning_effort": None,
        "context_window": None,
        "trust": trust,
        "priority": priority,
        "sandbox_mode": sandbox_mode,
        "tools": [],
        "mcp_servers": {},
        "skills": [],
        "hosts": hosts,
    }


def _default_payload(preset: str) -> dict[str, object]:
    cooldown = {
        "quota_seconds": 86400,
        "rate_limit_seconds": 60,
        "auth_seconds": 3600,
        "provider_seconds": 30,
    }
    native_provider = {
        "id": "codex",
        "name": "Native Codex",
        "protocol": "codex-native",
        "base_url": None,
        "auth_mode": "host-native",
        "capabilities": [
            "text",
            "vision",
            "audio",
            "tool_calling",
            "server_web_search",
        ],
        "models_endpoint": None,
        "enabled": True,
    }
    native_target = {
        "id": "codex-native",
        "provider_id": "codex",
        "protocol": None,
        "model": None,
        "credential_id": None,
        "capabilities": ["text", "vision", "audio", "tool_calling"],
        "context_window": None,
        "max_output_tokens": None,
        "reasoning_efforts": [],
        "trust": "high",
        "host_compatibility": ["codex"],
        "enabled": True,
        "metadata": {"kind": "native-subscription"},
    }
    native_pool = {
        "id": "native-review",
        "targets": ["codex-native"],
        "strategy": "sticky",
        "duration_seconds": None,
        "max_rate_limit_wait_seconds": 30,
        "cooldown": cooldown,
        "required_capabilities": ["text", "vision", "audio", "tool_calling"],
        "host_compatibility": ["codex"],
        "enabled": True,
    }
    reviewer = _agent_profile(
        "reviewer",
        "native-review",
        capabilities=["text", "vision", "audio", "tool_calling"],
        hosts=["codex"],
        trust="high",
        sandbox_mode="read-only",
        description="High-trust native reviewer for risky or media-dependent work.",
        instructions=(
            "Review high-risk, media-dependent, or provider-boundary work without modifying files. "
            "Return evidence and recommendations to the parent; the parent retains final verification."
        ),
        priority=100,
    )
    if preset == "native":
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "concurrency": 8,
            "providers": [native_provider],
            "credentials": [],
            "targets": [native_target],
            "pools": [native_pool],
            "agents": [reviewer],
            "hosts": {
                "codex": {
                    "enabled": True,
                    "scope": None,
                    "default_pool": "native-review",
                },
                "claude-code": {
                    "enabled": False,
                    "scope": "user",
                    "default_pool": None,
                },
            },
        }

    deepseek_provider = {
        "id": "deepseek",
        "name": "DeepSeek",
        "protocol": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "auth_mode": "vault",
        "capabilities": ["text", "tool_calling"],
        "models_endpoint": "/models",
        "enabled": True,
    }
    deepseek_credential = {
        "id": "primary",
        "provider_id": "deepseek",
        "vault_target": credential_target(
            "deepseek",
            "primary",
            protocol="deepseek-chat",
        ),
        "enabled": True,
        "created_at": "1970-01-01T00:00:00Z",
        "label": "Primary",
    }
    deepseek_target = {
        "id": "deepseek-primary",
        "provider_id": "deepseek",
        "protocol": None,
        "model": "deepseek-v4-pro",
        "credential_id": "primary",
        "capabilities": ["text", "tool_calling"],
        "context_window": 1_000_000,
        "max_output_tokens": None,
        "reasoning_efforts": ["high", "max", "ultra"],
        "trust": "standard",
        "host_compatibility": ["codex", "claude-code"],
        "enabled": True,
        "metadata": {},
    }
    general_pool = {
        "id": "general",
        "targets": ["deepseek-primary"],
        "strategy": "sticky",
        "duration_seconds": None,
        "max_rate_limit_wait_seconds": 30,
        "cooldown": cooldown,
        "required_capabilities": ["text", "tool_calling"],
        "host_compatibility": ["codex", "claude-code"],
        "enabled": True,
    }
    profiles = []
    for role in ("default", "worker", "explorer"):
        description, rule, priority = _ROLE_DETAILS[role]
        profiles.append(
            _agent_profile(
                role,
                "general",
                capabilities=["text", "tool_calling"],
                hosts=["codex", "claude-code"],
                trust="standard",
                sandbox_mode=(
                    "workspace-write" if role == "worker" else "read-only"
                ),
                description=description,
                instructions=(
                    f"You are Codex's {role} child agent. {rule} "
                    "This is a text-only model: never claim to have inspected images, video, screenshots, "
                    "audio, or other non-text inputs. Follow the parent's task boundary, report concrete "
                    "evidence, and return control when the bounded task is complete."
                ),
                priority=priority,
            )
        )
    profiles.append(reviewer)
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "concurrency": 8,
        "providers": [deepseek_provider, native_provider],
        "credentials": [deepseek_credential],
        "targets": [deepseek_target, native_target],
        "pools": [general_pool, native_pool],
        "agents": profiles,
        "hosts": {
            "codex": {
                "enabled": True,
                "scope": None,
                "default_pool": "general",
            },
            "claude-code": {
                "enabled": False,
                "scope": "user",
                "default_pool": "general",
            },
        },
    }


def default_catalog(preset: str = "hybrid") -> Catalog:
    """Return the installable hybrid catalog or its native-only boundary preset."""

    if preset not in {"hybrid", "native"}:
        raise ManagerError(
            "invalid_preset",
            f"Unsupported catalog preset: {preset}.",
        )
    return Catalog.from_dict(_default_payload(preset))
