"""Validated, secret-free provider and child-agent catalog."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .errors import ManagerError


CATALOG_SCHEMA_VERSION = 1
PROTOCOLS = frozenset(
    {
        "codex-native",
        "responses-compatible",
        "chat-completions-compatible",
        "deepseek-chat",
    }
)
CAPABILITIES = frozenset({"text", "vision", "audio", "tools", "web"})
TRUST_LEVELS = frozenset({"standard", "high"})
SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})
REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
AUTH_MODES = frozenset({"codex", "vault", "none"})
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_MCP_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_PROVIDER_FIELDS = frozenset(
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
_AGENT_FIELDS = frozenset(
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
_CATALOG_FIELDS = frozenset({"schema_version", "concurrency", "providers", "agents"})
_CAPABILITY_ORDER = {name: index for index, name in enumerate(("text", "vision", "audio", "tools", "web"))}


def _invalid(message: str, *, field: str | None = None) -> ManagerError:
    details = {"field": field} if field is not None else None
    return ManagerError("catalog_invalid", message, details)


def _strict_mapping(
    value: object,
    fields: frozenset[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(f"{label} must be a JSON object.")
    actual = set(value)
    unknown = sorted(actual - fields)
    missing = sorted(fields - actual)
    if unknown:
        raise _invalid(f"{label} contains unsupported fields: {', '.join(unknown)}.")
    if missing:
        raise _invalid(f"{label} is missing required fields: {', '.join(missing)}.")
    return value


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise _invalid(
            f"{field} must use lowercase ASCII letters, digits, underscores, or hyphens.",
            field=field,
        )
    return value


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{field} must be a non-empty string.", field=field)
    return value.strip()


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, field)


def _positive_int(value: object, field: str, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _invalid(f"{field} must be a positive integer.", field=field)
    return value


def _capability_set(value: object, field: str) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise _invalid(f"{field} must be a non-empty array.", field=field)
    if not all(isinstance(item, str) for item in value):
        raise _invalid(f"{field} entries must be strings.", field=field)
    result = frozenset(value)
    if len(result) != len(value):
        raise _invalid(f"{field} contains duplicate capabilities.", field=field)
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


def _mcp_value(value: object, field: str) -> Any:
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid(f"{field} contains a non-finite number.", field=field)
        return value
    if isinstance(value, list):
        return [_mcp_value(item, field) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise _invalid(f"{field} contains an invalid key.", field=field)
            normalized[key] = _mcp_value(item, field)
        return normalized
    raise _invalid(f"{field} contains a value that cannot be rendered to TOML.", field=field)


def _mcp_servers(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise _invalid("mcp_servers must be a JSON object.", field="mcp_servers")
    result: dict[str, dict[str, Any]] = {}
    for raw_name, raw_config in value.items():
        if not isinstance(raw_name, str) or not _MCP_IDENTIFIER.fullmatch(raw_name):
            raise _invalid("MCP server names contain unsupported characters.", field="mcp_servers")
        if not isinstance(raw_config, Mapping):
            raise _invalid("Each MCP server must be a JSON object.", field="mcp_servers")
        config = {
            key: _mcp_value(item, f"mcp_servers.{raw_name}.{key}")
            for key, item in raw_config.items()
        }
        if "url" in config and isinstance(config.get("url"), str) and config["url"].strip():
            _validate_upstream_url(config["url"].strip())
        result[raw_name] = config
    return result


def _skills(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise _invalid("skills must be a JSON array.", field="skills")
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            path = item.strip()
            if not path:
                raise _invalid("Skill paths cannot be empty.", field="skills")
            result.append({"path": path, "enabled": True})
            continue
        if not isinstance(item, Mapping):
            raise _invalid("Each skill must be a path or JSON object.", field="skills")
        unknown = set(item) - {"path", "enabled"}
        if unknown:
            raise _invalid("Skill entries support only path and enabled.", field="skills")
        path = _nonempty_string(item.get("path"), "skills.path")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise _invalid("skills.enabled must be a boolean.", field="skills.enabled")
        result.append({"path": path, "enabled": enabled})
    return tuple(result)


def _has_concrete_mcp(servers: Mapping[str, Mapping[str, Any]]) -> bool:
    return any(
        any(isinstance(config.get(key), str) and bool(config[key].strip()) for key in ("url", "command"))
        for config in servers.values()
    )


@dataclass(frozen=True)
class ProviderSpec:
    """One provider exposed to catalog agents."""

    id: str
    name: str
    protocol: str
    base_url: str | None
    auth: str
    capabilities: frozenset[str]
    context_window: int | None
    enabled: bool

    @classmethod
    def from_dict(cls, value: object) -> "ProviderSpec":
        data = _strict_mapping(value, _PROVIDER_FIELDS, "Provider")
        provider_id = _identifier(data["id"], "provider.id")
        name = _nonempty_string(data["name"], "provider.name")
        protocol = _nonempty_string(data["protocol"], "provider.protocol")
        if protocol not in PROTOCOLS:
            raise _invalid(f"Unsupported provider protocol: {protocol}.", field="provider.protocol")
        auth = _nonempty_string(data["auth"], "provider.auth")
        if auth not in AUTH_MODES:
            raise _invalid(f"Unsupported provider auth mode: {auth}.", field="provider.auth")
        if protocol == "codex-native" and auth != "codex":
            raise _invalid("codex-native providers must use Codex authentication.", field="provider.auth")
        if protocol != "codex-native" and auth == "codex":
            raise _invalid("Custom providers must use vault or no authentication.", field="provider.auth")
        if protocol == "deepseek-chat" and auth != "vault":
            raise _invalid("DeepSeek chat providers require vault authentication.", field="provider.auth")
        capabilities = _capability_set(data["capabilities"], "provider.capabilities")
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
                raise _invalid("codex-native providers must not define base_url.", field="provider.base_url")
            base_url = None
        else:
            base_url = _validate_upstream_url(_nonempty_string(raw_url, "provider.base_url"))
        context_window = _positive_int(
            data["context_window"], "provider.context_window", optional=True
        )
        enabled = data["enabled"]
        if not isinstance(enabled, bool):
            raise _invalid("provider.enabled must be a boolean.", field="provider.enabled")
        return cls(
            id=provider_id,
            name=name,
            protocol=protocol,
            base_url=base_url,
            auth=auth,
            capabilities=capabilities,
            context_window=context_window,
            enabled=enabled,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "auth": self.auth,
            "capabilities": _capability_list(self.capabilities),
            "context_window": self.context_window,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class AgentSpec:
    """One custom Codex child agent and its routing boundary."""

    name: str
    description: str
    provider: str
    model: str | None
    reasoning_effort: str | None
    context_window: int | None
    capabilities: frozenset[str]
    trust: str
    priority: int
    sandbox_mode: str
    mcp_servers: Mapping[str, Mapping[str, Any]]
    skills: tuple[Mapping[str, Any], ...]
    developer_instructions: str

    @classmethod
    def from_dict(cls, value: object) -> "AgentSpec":
        data = _strict_mapping(value, _AGENT_FIELDS, "Agent")
        name = _identifier(data["name"], "agent.name")
        description = _nonempty_string(data["description"], "agent.description")
        provider_id = _identifier(data["provider"], "agent.provider")
        model = _optional_string(data["model"], "agent.model")
        effort = _optional_string(data["reasoning_effort"], "agent.reasoning_effort")
        if effort is not None and effort not in REASONING_EFFORTS:
            raise _invalid(f"Unsupported reasoning effort: {effort}.", field="agent.reasoning_effort")
        context_window = _positive_int(data["context_window"], "agent.context_window", optional=True)
        capabilities = _capability_set(data["capabilities"], "agent.capabilities")
        trust = _nonempty_string(data["trust"], "agent.trust")
        if trust not in TRUST_LEVELS:
            raise _invalid(f"Unsupported trust level: {trust}.", field="agent.trust")
        priority = data["priority"]
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
            raise _invalid("agent.priority must be a non-negative integer.", field="agent.priority")
        sandbox_mode = _nonempty_string(data["sandbox_mode"], "agent.sandbox_mode")
        if sandbox_mode not in SANDBOX_MODES:
            raise _invalid(f"Unsupported sandbox mode: {sandbox_mode}.", field="agent.sandbox_mode")
        servers = _mcp_servers(data["mcp_servers"])
        if "web" in capabilities and not _has_concrete_mcp(servers):
            raise ManagerError(
                "web_requires_mcp",
                "A web-capable agent must configure a concrete MCP server.",
                {"agent": name},
            )
        skills = _skills(data["skills"])
        instructions = _nonempty_string(
            data["developer_instructions"], "agent.developer_instructions"
        )
        return cls(
            name=name,
            description=description,
            provider=provider_id,
            model=model,
            reasoning_effort=effort,
            context_window=context_window,
            capabilities=capabilities,
            trust=trust,
            priority=priority,
            sandbox_mode=sandbox_mode,
            mcp_servers=servers,
            skills=skills,
            developer_instructions=instructions,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "context_window": self.context_window,
            "capabilities": _capability_list(self.capabilities),
            "trust": self.trust,
            "priority": self.priority,
            "sandbox_mode": self.sandbox_mode,
            "mcp_servers": {
                name: dict(config) for name, config in sorted(self.mcp_servers.items())
            },
            "skills": [dict(skill) for skill in self.skills],
            "developer_instructions": self.developer_instructions,
        }


@dataclass(frozen=True)
class Catalog:
    """Validated catalog used by setup, rendering, and routing."""

    schema_version: int
    concurrency: int
    providers: tuple[ProviderSpec, ...]
    agents: tuple[AgentSpec, ...]

    @classmethod
    def from_dict(cls, value: object) -> "Catalog":
        data = _strict_mapping(value, _CATALOG_FIELDS, "Catalog")
        schema_version = data["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != CATALOG_SCHEMA_VERSION
        ):
            raise ManagerError(
                "unsupported_catalog_schema",
                f"Catalog schema must be {CATALOG_SCHEMA_VERSION}.",
                {"schema_version": schema_version},
            )
        concurrency = _positive_int(data["concurrency"], "catalog.concurrency")
        raw_providers = data["providers"]
        raw_agents = data["agents"]
        if not isinstance(raw_providers, list) or not raw_providers:
            raise _invalid("Catalog providers must be a non-empty array.", field="providers")
        if not isinstance(raw_agents, list) or not raw_agents:
            raise _invalid("Catalog agents must be a non-empty array.", field="agents")
        raw_provider_ids = [
            item.get("id") for item in raw_providers if isinstance(item, Mapping)
        ]
        folded_provider_ids = [
            item.casefold() for item in raw_provider_ids if isinstance(item, str)
        ]
        if len(folded_provider_ids) != len(set(folded_provider_ids)):
            raise ManagerError("duplicate_provider", "Provider identifiers must be unique.")
        providers = tuple(ProviderSpec.from_dict(item) for item in raw_providers)
        provider_keys = [item.id.casefold() for item in providers]
        if len(set(provider_keys)) != len(provider_keys):
            raise ManagerError("duplicate_provider", "Provider identifiers must be unique.")
        provider_map = {item.id.casefold(): item for item in providers}

        raw_agent_ids = [item.get("name") for item in raw_agents if isinstance(item, Mapping)]
        folded_agent_ids = [item.casefold() for item in raw_agent_ids if isinstance(item, str)]
        if len(folded_agent_ids) != len(set(folded_agent_ids)):
            raise ManagerError("duplicate_agent", "Agent names must be unique.")
        parsed_agents = tuple(AgentSpec.from_dict(item) for item in raw_agents)
        agent_keys = [item.name.casefold() for item in parsed_agents]
        if len(set(agent_keys)) != len(agent_keys):
            raise ManagerError("duplicate_agent", "Agent names must be unique.")
        agents: list[AgentSpec] = []
        for item in parsed_agents:
            provider = provider_map.get(item.provider.casefold())
            if provider is None:
                raise ManagerError(
                    "unknown_provider",
                    f"Agent {item.name} references an unknown provider.",
                    {"agent": item.name, "provider": item.provider},
                )
            unsupported = item.capabilities - provider.capabilities
            if unsupported:
                raise ManagerError(
                    "capability_unsupported",
                    f"Agent {item.name} exceeds provider {provider.id} capabilities.",
                    {"agent": item.name, "capabilities": sorted(unsupported)},
                )
            if provider.protocol != "codex-native" and item.model is None:
                raise ManagerError(
                    "invalid_model",
                    f"Agent {item.name} requires an explicit provider model.",
                    {"agent": item.name, "provider": provider.id},
                )
            if (
                item.context_window is not None
                and provider.context_window is not None
                and item.context_window > provider.context_window
            ):
                raise ManagerError(
                    "capability_unsupported",
                    f"Agent {item.name} context exceeds provider {provider.id}.",
                    {"agent": item.name, "provider": provider.id},
                )
            agents.append(replace(item, provider=provider.id))
        return cls(
            schema_version=CATALOG_SCHEMA_VERSION,
            concurrency=concurrency,
            providers=providers,
            agents=tuple(agents),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "concurrency": self.concurrency,
            "providers": [item.to_dict() for item in self.providers],
            "agents": [item.to_dict() for item in self.agents],
        }

    def provider(self, provider_id: str) -> ProviderSpec:
        matches = [item for item in self.providers if item.id.casefold() == provider_id.casefold()]
        if not matches:
            raise ManagerError(
                "unknown_provider",
                f"Unknown provider: {provider_id}.",
                {"provider": provider_id},
            )
        return matches[0]

    def agent(self, name: str) -> AgentSpec:
        matches = [item for item in self.agents if item.name.casefold() == name.casefold()]
        if not matches:
            raise ManagerError("invalid_role", f"Unknown agent: {name}.", {"agent": name})
        return matches[0]


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
            raw = source.encode("utf-8") if stripped.startswith("{") else Path(source).read_bytes()
        else:
            raise _invalid("Unsupported catalog source.")
        decoded = json.loads(raw.decode("utf-8"))
    except ManagerError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagerError("catalog_invalid", "Catalog JSON could not be read or decoded.") from exc
    return Catalog.from_dict(decoded)


def save_catalog_bytes(catalog: Catalog) -> bytes:
    """Return deterministic, human-readable JSON without credential material."""

    return (json.dumps(catalog.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def route_agent(
    catalog: Catalog,
    required_capabilities: set[str] | frozenset[str],
    high_risk: bool = False,
) -> AgentSpec | None:
    """Select a qualifying child deterministically, or require the parent."""

    required = frozenset(required_capabilities)
    if required - CAPABILITIES:
        return None
    candidates = []
    for item in catalog.agents:
        provider = catalog.provider(item.provider)
        if not provider.enabled:
            continue
        if not required.issubset(item.capabilities):
            continue
        if high_risk and item.trust != "high":
            continue
        candidates.append(item)
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


def _deepseek_agent(name: str) -> dict[str, object]:
    description, rule, priority = _ROLE_DETAILS[name]
    instructions = (
        f"You are Codex's {name} child agent. {rule} "
        "This is a text-only model: never claim to have inspected images, video, screenshots, "
        "audio, or other non-text inputs. Follow the parent's task boundary, report concrete "
        "evidence, and return control when the bounded task is complete."
    )
    return {
        "name": name,
        "description": description,
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "reasoning_effort": None,
        "context_window": 1_000_000,
        "capabilities": ["text", "tools"],
        "trust": "standard",
        "priority": priority,
        "sandbox_mode": "workspace-write" if name == "worker" else "read-only",
        "mcp_servers": {},
        "skills": [],
        "developer_instructions": instructions,
    }


def default_catalog(preset: str = "hybrid") -> Catalog:
    """Return the installable hybrid catalog or its native-only boundary preset."""

    if preset not in {"hybrid", "native"}:
        raise ManagerError("invalid_preset", f"Unsupported catalog preset: {preset}.")
    native_provider = {
        "id": "codex",
        "name": "Native Codex",
        "protocol": "codex-native",
        "base_url": None,
        "auth": "codex",
        "capabilities": ["text", "vision", "audio", "tools", "web"],
        "context_window": None,
        "enabled": True,
    }
    reviewer = {
        "name": "reviewer",
        "description": "High-trust native reviewer for risky or media-dependent work.",
        "provider": "codex",
        "model": None,
        "reasoning_effort": None,
        "context_window": None,
        "capabilities": ["text", "vision", "audio", "tools"],
        "trust": "high",
        "priority": 100,
        "sandbox_mode": "read-only",
        "mcp_servers": {},
        "skills": [],
        "developer_instructions": (
            "Review high-risk, media-dependent, or provider-boundary work without modifying files. "
            "Return evidence and recommendations to the parent; the parent retains final verification."
        ),
    }
    if preset == "native":
        return Catalog.from_dict(
            {
                "schema_version": CATALOG_SCHEMA_VERSION,
                "concurrency": 8,
                "providers": [native_provider],
                "agents": [reviewer],
            }
        )
    deepseek_provider = {
        "id": "deepseek",
        "name": "DeepSeek",
        "protocol": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "auth": "vault",
        "capabilities": ["text", "tools"],
        "context_window": 1_000_000,
        "enabled": True,
    }
    return Catalog.from_dict(
        {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "concurrency": 8,
            "providers": [deepseek_provider, native_provider],
            "agents": [_deepseek_agent(name) for name in ("default", "worker", "explorer")]
            + [reviewer],
        }
    )
