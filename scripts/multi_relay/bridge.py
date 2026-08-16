#!/usr/bin/env python3
"""Loopback Responses-to-Chat bridge for Codex Multi Relay providers."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from multi_relay.catalog import Catalog, ProviderSpec, load_catalog
    from multi_relay.errors import ManagerError
else:
    from .catalog import Catalog, ProviderSpec, load_catalog
    from .errors import ManagerError


BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 42137
BRIDGE_BASE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/v1"
BRIDGE_HEALTH_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/health"
BRIDGE_SERVICE = "codex-multi-relay-chat-bridge"
LEGACY_BRIDGE_SERVICE = "codex-deepseek-responses-bridge"
BRIDGE_VERSION = 3
DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
REQUIRED_MODEL = "deepseek-v4-pro"
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_UPSTREAM_ERROR_BYTES = 1024 * 1024
MAX_TOOLS = 128
_REASONING_PREFIX = "cmr1:"
_LEGACY_REASONING_PREFIX = "dsr1:"
_VALID_TOOL_NAME = re.compile(r"[^A-Za-z0-9_-]+")
_HANDOFF_START = re.compile(
    r"^\[(?P<label>Relay|DeepSeek) task: (?P<target>[^\]\r\n]+)\][ \t]*$"
)


class BridgeError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward a provider credential through an HTTP redirect."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "Provider redirect blocked",
            headers,
            fp,
        )


def _open_upstream(request: urllib.request.Request, *, timeout: float) -> Any:
    return urllib.request.build_opener(_RejectRedirectHandler()).open(
        request,
        timeout=timeout,
    )


def _validate_upstream_url(
    value: str,
    provider: ProviderSpec | None = None,
) -> str:
    """Validate a configured chat endpoint without allowing URL credential smuggling."""

    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise BridgeError("invalid_upstream_url", "The provider upstream URL is invalid.", 500) from None
    expected_path = (
        parsed.path == "/chat/completions"
        if provider is None
        else parsed.path.endswith("/chat/completions")
    )
    no_credential_or_suffix = (
        parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and expected_path
    )
    official = parsed.scheme == "https" and parsed.hostname == "api.deepseek.com" and port in {None, 443}
    secure_custom = parsed.scheme == "https" and parsed.hostname is not None
    loopback_fixture = (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    )
    allowed_network = (
        official or loopback_fixture
        if provider is None
        else secure_custom or loopback_fixture
    )
    if not no_credential_or_suffix or not allowed_network:
        raise BridgeError(
            "invalid_upstream_url",
            "The bridge requires a configured HTTPS chat endpoint or a loopback fixture.",
            500,
        )
    return value


def _chat_completions_url(provider: ProviderSpec) -> str:
    if provider.base_url is None:
        raise BridgeError(
            "invalid_upstream_url",
            f"Provider {provider.id} has no chat base URL.",
            500,
        )
    base_url = provider.base_url.rstrip("/")
    base_path = urllib.parse.urlsplit(base_url).path.rstrip("/")
    endpoint = (
        base_url
        if base_path.endswith("/chat/completions")
        else f"{base_url}/chat/completions"
    )
    return _validate_upstream_url(endpoint, provider)


@dataclass(frozen=True)
class ToolRoute:
    chat_name: str
    name: str
    namespace: str | None
    custom: bool
    dispatch_targets: dict[str, ToolRoute] | None = None


@dataclass(frozen=True)
class ChatRequest:
    payload: dict[str, Any]
    tools: dict[str, ToolRoute]


@dataclass(frozen=True)
class BridgeRoute:
    provider: ProviderSpec
    upstream_url: str
    allowed_models: frozenset[str]


def _text_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                raise BridgeError("unsupported_content", "Unsupported Responses content item.")
            item_type = item.get("type")
            if item_type in {"input_text", "output_text", "text"}:
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                continue
            if item_type == "encrypted_content":
                raise BridgeError(
                    "unresolved_protected_content",
                    "Encrypted Codex content cannot be forwarded as plaintext.",
                )
            if item_type in {"input_image", "input_audio", "input_file"}:
                raise BridgeError(
                    "unsupported_media",
                    "The selected Chat Completions child agent is configured for text-only input.",
                )
            raise BridgeError(
                "unsupported_content",
                f"Unsupported Responses content type: {item_type!r}.",
            )
        return "\n".join(parts)
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, str):
            return content
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    return str(value)


def _read_rollout_payloads(path: Path) -> Any:
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            if isinstance(payload, dict):
                yield payload


def _agent_ciphertext_occurrence(
    path: Path,
    *,
    author: str,
    recipient: str,
    encrypted_content: str,
) -> int | None:
    occurrence = 0
    matches: list[int] = []
    for payload in _read_rollout_payloads(path):
        if (
            payload.get("type") != "agent_message"
            or payload.get("author") != author
            or payload.get("recipient") != recipient
        ):
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "encrypted_content":
                continue
            if item.get("encrypted_content") == encrypted_content:
                matches.append(occurrence)
            occurrence += 1
    return matches[0] if len(matches) == 1 else None


def _assistant_text(payload: dict[str, Any]) -> str:
    if payload.get("type") != "message" or payload.get("role") != "assistant":
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        item.get("text")
        for item in content
        if isinstance(item, dict)
        and item.get("type") in {"output_text", "input_text"}
        and isinstance(item.get("text"), str)
    ]
    return "\n".join(parts)


def _explicit_agent_handoffs(text: str) -> list[tuple[str, str]]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    handoffs: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        matched = _HANDOFF_START.fullmatch(lines[index])
        if matched is None:
            index += 1
            continue
        target = matched.group("target").strip()
        closing = f"[/{matched.group('label')} task: {target}]"
        end = index + 1
        while end < len(lines) and lines[end].strip() != closing:
            end += 1
        if end >= len(lines):
            index += 1
            continue
        message = "\n".join(lines[index + 1 : end]).strip()
        if target and message:
            handoffs.append((target, message))
        index = end + 1
    return handoffs


def _looks_like_protected_agent_text(message: str) -> bool:
    return message.strip().startswith("gAAAA")


def _dispatched_agent_messages(path: Path, recipient: str) -> list[str]:
    calls: list[dict[str, object]] = []
    by_call_id: dict[str, dict[str, object]] = {}
    pending_handoffs: dict[str, list[str]] = {}
    for payload in _read_rollout_payloads(path):
        payload_type = payload.get("type")
        if payload_type == "message":
            for target, message in _explicit_agent_handoffs(_assistant_text(payload)):
                pending_handoffs.setdefault(target, []).append(message)
            continue
        call_id = payload.get("call_id")
        if payload_type == "function_call":
            name = payload.get("name")
            short_name = name.rsplit("__", 1)[-1] if isinstance(name, str) else None
            if short_name not in {"spawn_agent", "followup_task", "send_message"}:
                continue
            arguments = payload.get("arguments")
            try:
                parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            message = parsed.get("message")
            if not isinstance(call_id, str) or not isinstance(message, str):
                continue
            target = parsed.get("target") if short_name != "spawn_agent" else None
            handoff_target = parsed.get("task_name") if short_name == "spawn_agent" else target
            handoff_queue = (
                pending_handoffs.get(handoff_target)
                if isinstance(handoff_target, str)
                else None
            )
            if handoff_queue:
                explicit_message = handoff_queue.pop(0)
                if _looks_like_protected_agent_text(message) or explicit_message == message:
                    message = explicit_message
                else:
                    message = ""
            record: dict[str, object] = {
                "call_id": call_id,
                "message": message,
                "recipient": target if isinstance(target, str) else None,
                "spawn": short_name == "spawn_agent",
                "completed": False,
            }
            calls.append(record)
            by_call_id[call_id] = record
            continue
        if payload_type != "function_call_output" or not isinstance(call_id, str):
            continue
        record = by_call_id.get(call_id)
        if record is None:
            continue
        record["completed"] = True
        if not record.get("spawn"):
            continue
        output = payload.get("output")
        try:
            parsed_output = json.loads(output) if isinstance(output, str) else output
        except json.JSONDecodeError:
            continue
        if isinstance(parsed_output, dict) and isinstance(parsed_output.get("task_name"), str):
            record["recipient"] = parsed_output["task_name"]
    return [
        str(record["message"])
        for record in calls
        if record.get("completed") and record.get("recipient") == recipient
    ]


def _thread_spawn_source(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    subagent = parsed.get("subagent")
    if not isinstance(subagent, dict):
        return None
    thread_spawn = subagent.get("thread_spawn")
    return thread_spawn if isinstance(thread_spawn, dict) else None


def _is_opaque_agent_message(message: str, encrypted_content: str) -> bool:
    stripped = message.strip()
    return (
        not stripped
        or stripped == encrypted_content
        or _looks_like_protected_agent_text(stripped)
    )


def _resolve_agent_task(
    *,
    author: str,
    recipient: str,
    encrypted_content: str,
    codex_home: Path | None = None,
    provider_id: str = "deepseek",
) -> str:
    home = codex_home or Path(
        os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    )
    matches: dict[tuple[str, str, str], str] = {}
    for database in sorted(home.expanduser().glob("state_*.sqlite"), reverse=True):
        try:
            connection = sqlite3.connect(
                database.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=0.2,
            )
        except sqlite3.Error:
            continue
        try:
            rows = connection.execute(
                "SELECT id, rollout_path, source FROM threads "
                "WHERE model_provider = ? AND agent_path = ? "
                "ORDER BY created_at_ms DESC",
                (provider_id, recipient),
            ).fetchall()
            for child_id, child_rollout, source in rows:
                child_path = Path(str(child_rollout))
                occurrence = _agent_ciphertext_occurrence(
                    child_path,
                    author=author,
                    recipient=recipient,
                    encrypted_content=encrypted_content,
                )
                if occurrence is None:
                    continue
                spawn_source = _thread_spawn_source(source)
                parent_id = (
                    spawn_source.get("parent_thread_id")
                    if isinstance(spawn_source, dict)
                    else None
                )
                if not isinstance(parent_id, str):
                    continue
                parent_row = connection.execute(
                    "SELECT rollout_path FROM threads WHERE id = ?",
                    (parent_id,),
                ).fetchone()
                if not parent_row:
                    continue
                dispatched = _dispatched_agent_messages(Path(str(parent_row[0])), recipient)
                message = dispatched[occurrence] if occurrence < len(dispatched) else None
                if (
                    isinstance(message, str)
                    and not _is_opaque_agent_message(message, encrypted_content)
                ):
                    matches[(str(child_id), parent_id, message)] = message
        except sqlite3.Error:
            continue
        finally:
            connection.close()
    if len(matches) == 1:
        return next(iter(matches.values()))
    raise BridgeError(
        "unresolved_agent_message",
        "The encrypted Codex child task could not be matched exactly to its local "
        "parent spawn record; the ciphertext was not sent to DeepSeek.",
        409,
    )


def _agent_message_content(
    value: object,
    *,
    author: str,
    recipient: str,
    codex_home: Path | None,
    provider_id: str,
) -> str:
    if not isinstance(value, list):
        return _text_value(value)
    encrypted = [
        item.get("encrypted_content")
        for item in value
        if isinstance(item, dict) and item.get("type") == "encrypted_content"
    ]
    if not encrypted:
        return _text_value(value)
    if len(encrypted) != 1 or not isinstance(encrypted[0], str):
        raise BridgeError(
            "unresolved_agent_message",
            "A protected Codex agent message must contain exactly one encrypted task.",
            409,
        )
    parts = [
        _text_value([item])
        for item in value
        if isinstance(item, dict) and item.get("type") != "encrypted_content"
    ]
    parts.append(
        _resolve_agent_task(
            author=author,
            recipient=recipient,
            encrypted_content=encrypted[0],
            codex_home=codex_home,
            provider_id=provider_id,
        )
    )
    return "\n".join(part for part in parts if part)


def _reasoning_key(secret: str, provider_id: str) -> bytes:
    return hashlib.sha256(
        b"codex-multi-relay-reasoning-v1\0"
        + provider_id.encode("utf-8")
        + b"\0"
        + secret.encode("utf-8")
    ).digest()


def _legacy_reasoning_key(secret: str) -> bytes:
    return hashlib.sha256(b"codex-deepseek-reasoning-v1\0" + secret.encode("utf-8")).digest()


def _reasoning_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(
            hmac.new(key, b"stream\0" + nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        )
        counter += 1
    return bytes(output[:length])


def _seal_reasoning(text: str, secret: str, provider_id: str = "deepseek") -> str:
    raw = text.encode("utf-8")
    nonce = os.urandom(16)
    key = _reasoning_key(secret, provider_id)
    stream = _reasoning_keystream(key, nonce, len(raw))
    ciphertext = bytes(left ^ right for left, right in zip(raw, stream))
    tag = hmac.new(key, b"tag\0" + nonce + ciphertext, hashlib.sha256).digest()[:16]
    token = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")
    return _REASONING_PREFIX + token


def _seal_legacy_reasoning(text: str, secret: str) -> str:
    """Create the former DeepSeek token only for compatibility verification."""

    raw = text.encode("utf-8")
    nonce = os.urandom(16)
    key = _legacy_reasoning_key(secret)
    stream = _reasoning_keystream(key, nonce, len(raw))
    ciphertext = bytes(left ^ right for left, right in zip(raw, stream))
    tag = hmac.new(key, b"tag\0" + nonce + ciphertext, hashlib.sha256).digest()[:16]
    token = base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")
    return _LEGACY_REASONING_PREFIX + token


def _open_reasoning(
    value: object,
    secret: str | None,
    provider_id: str = "deepseek",
    *,
    allow_legacy: bool | None = None,
) -> str | None:
    if not isinstance(value, str) or not value.startswith(
        (_REASONING_PREFIX, _LEGACY_REASONING_PREFIX)
    ):
        return None
    if not secret:
        raise BridgeError("reasoning_key_missing", "DeepSeek reasoning replay key is unavailable.")
    try:
        prefix = (
            _REASONING_PREFIX
            if value.startswith(_REASONING_PREFIX)
            else _LEGACY_REASONING_PREFIX
        )
        sealed = base64.urlsafe_b64decode(value[len(prefix) :].encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise BridgeError("invalid_reasoning", "DeepSeek reasoning replay data is invalid.") from None
    if len(sealed) < 32:
        raise BridgeError("invalid_reasoning", "DeepSeek reasoning replay data is invalid.")
    nonce, tag, ciphertext = sealed[:16], sealed[16:32], sealed[32:]
    if prefix == _LEGACY_REASONING_PREFIX:
        legacy_allowed = provider_id == "deepseek" if allow_legacy is None else allow_legacy
        if not legacy_allowed:
            raise BridgeError(
                "invalid_reasoning",
                "Legacy DeepSeek reasoning cannot be replayed through another provider.",
            )
        key = _legacy_reasoning_key(secret)
    else:
        key = _reasoning_key(secret, provider_id)
    expected = hmac.new(key, b"tag\0" + nonce + ciphertext, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expected):
        raise BridgeError("invalid_reasoning", "DeepSeek reasoning replay data failed validation.")
    stream = _reasoning_keystream(key, nonce, len(ciphertext))
    try:
        return bytes(left ^ right for left, right in zip(ciphertext, stream)).decode("utf-8")
    except UnicodeDecodeError:
        raise BridgeError("invalid_reasoning", "DeepSeek reasoning replay data is invalid.") from None


class _ToolRegistry:
    def __init__(self) -> None:
        self.routes: dict[str, ToolRoute] = {}
        self._by_origin: dict[tuple[str | None, str], str] = {}
        self._dispatcher_by_target: dict[str, str] = {}

    def _allocate(self, namespace: str | None, name: str) -> str:
        origin = (namespace, name)
        existing = self._by_origin.get(origin)
        if existing:
            return existing
        raw = f"{namespace}__{name}" if namespace else name
        base = _VALID_TOOL_NAME.sub("_", raw).strip("_") or "tool"
        base = base[:64]
        candidate = base
        if candidate in self.routes:
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
            candidate = f"{base[:55]}_{digest}"
        self._by_origin[origin] = candidate
        return candidate

    def register(
        self,
        name: str,
        *,
        namespace: str | None,
        custom: bool,
    ) -> ToolRoute:
        chat_name = self._allocate(namespace, name)
        route = ToolRoute(
            chat_name=chat_name,
            name=name,
            namespace=namespace,
            custom=custom,
        )
        self.routes[chat_name] = route
        return route

    def register_dispatcher(
        self,
        label: str,
        targets: dict[str, ToolRoute],
    ) -> ToolRoute:
        raw = f"codex_dispatch_{label}"
        base = _VALID_TOOL_NAME.sub("_", raw).strip("_") or "codex_dispatch"
        base = base[:64]
        candidate = base
        counter = 0
        while candidate in self.routes:
            counter += 1
            digest = hashlib.sha256(f"{raw}\0{counter}".encode("utf-8")).hexdigest()[:8]
            candidate = f"{base[:55]}_{digest}"
        route = ToolRoute(
            chat_name=candidate,
            name=candidate,
            namespace=None,
            custom=False,
            dispatch_targets=dict(targets),
        )
        self.routes[candidate] = route
        for target_name in targets:
            self._dispatcher_by_target[target_name] = candidate
        return route

    def chat_name(self, namespace: str | None, name: str) -> str:
        existing = self._by_origin.get((namespace, name))
        if existing:
            return existing
        return self.register(name, namespace=namespace, custom=False).chat_name

    def dispatcher_for(self, chat_name: str) -> ToolRoute | None:
        dispatcher_name = self._dispatcher_by_target.get(chat_name)
        return self.routes.get(dispatcher_name) if dispatcher_name else None


def _custom_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "Raw input for this Codex custom tool.",
            }
        },
        "required": ["input"],
        "additionalProperties": False,
    }


def _chat_tool(
    tool: dict[str, Any],
    registry: _ToolRegistry,
    *,
    namespace: str | None = None,
) -> dict[str, Any]:
    tool_type = tool.get("type")
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        raise BridgeError("invalid_tool", "A Codex tool is missing its name.")
    custom = tool_type == "custom"
    if tool_type not in {"function", "custom"}:
        raise BridgeError("unsupported_tool", f"Unsupported Codex tool type: {tool_type!r}.")
    route = registry.register(name, namespace=namespace, custom=custom)
    description = tool.get("description")
    function: dict[str, Any] = {
        "name": route.chat_name,
        "description": description if isinstance(description, str) else "Codex tool.",
        "parameters": _custom_parameters() if custom else tool.get("parameters", {}),
    }
    if not isinstance(function["parameters"], dict):
        function["parameters"] = {}
    return {"type": "function", "function": function}


def _dispatcher_tool(
    tools: list[dict[str, Any]],
    registry: _ToolRegistry,
    *,
    label: str,
) -> dict[str, Any]:
    targets: dict[str, ToolRoute] = {}
    contracts: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function")
        if not isinstance(function, dict):
            raise BridgeError("invalid_tool", "A translated tool has no function contract.", 500)
        chat_name = function.get("name")
        if not isinstance(chat_name, str):
            raise BridgeError("invalid_tool", "A translated tool has no function name.", 500)
        route = registry.routes.get(chat_name)
        if route is None or route.dispatch_targets is not None:
            raise BridgeError("invalid_tool", "A translated tool route is unavailable.", 500)
        targets[chat_name] = route
        origin = f"{route.namespace}.{route.name}" if route.namespace else route.name
        contracts.append({
            "selector": chat_name,
            "tool": origin,
            "description": function.get("description", "Codex tool."),
            "parameters": function.get("parameters", {}),
        })
    dispatcher = registry.register_dispatcher(label, targets)
    contract_text = json.dumps(contracts, ensure_ascii=False, separators=(",", ":"))
    return {
        "type": "function",
        "function": {
            "name": dispatcher.chat_name,
            "description": (
                "Dispatch exactly one Codex tool. Select its selector in `tool` and put "
                "that tool's JSON arguments in `arguments`. Tool contracts: "
                f"{contract_text}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "enum": list(targets),
                        "description": "Selector of the concrete Codex tool to call.",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments matching the selected tool contract.",
                        "additionalProperties": True,
                    },
                },
                "required": ["tool", "arguments"],
                "additionalProperties": False,
            },
        },
    }


def _chunk_dispatchers(
    translated: list[dict[str, Any]],
    registry: _ToolRegistry,
) -> list[dict[str, Any]]:
    chunk_size = max(16, (len(translated) + MAX_TOOLS - 1) // MAX_TOOLS)
    return [
        _dispatcher_tool(
            translated[start : start + chunk_size],
            registry,
            label=f"group_{start // chunk_size + 1}",
        )
        for start in range(0, len(translated), chunk_size)
    ]


def _compress_namespaced_tools(
    translated: list[dict[str, Any]],
    registry: _ToolRegistry,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    first_position: dict[str, int] = {}
    for index, tool in enumerate(translated):
        function = tool.get("function")
        chat_name = function.get("name") if isinstance(function, dict) else None
        route = registry.routes.get(chat_name) if isinstance(chat_name, str) else None
        if route is None or route.namespace is None:
            continue
        groups.setdefault(route.namespace, []).append(tool)
        first_position.setdefault(route.namespace, index)

    selected: set[str] = set()
    remaining = len(translated)
    candidates = sorted(
        groups,
        key=lambda namespace: (-len(groups[namespace]), first_position[namespace], namespace),
    )
    for namespace in candidates:
        if remaining <= MAX_TOOLS:
            break
        size = len(groups[namespace])
        if size < 2:
            continue
        selected.add(namespace)
        remaining -= size - 1

    if remaining > MAX_TOOLS:
        return _chunk_dispatchers(translated, registry)

    dispatchers = {
        namespace: _dispatcher_tool(
            groups[namespace],
            registry,
            label=f"namespace_{namespace}",
        )
        for namespace in selected
    }
    compressed: list[dict[str, Any]] = []
    inserted: set[str] = set()
    for tool in translated:
        function = tool.get("function")
        chat_name = function.get("name") if isinstance(function, dict) else None
        route = registry.routes.get(chat_name) if isinstance(chat_name, str) else None
        namespace = route.namespace if route else None
        if namespace in selected:
            if namespace not in inserted:
                compressed.append(dispatchers[namespace])
                inserted.add(namespace)
            continue
        compressed.append(tool)
    return compressed


def _translate_tools(
    value: object,
    registry: _ToolRegistry,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BridgeError("invalid_tools", "Responses tools must be an array.")
    translated: list[dict[str, Any]] = []
    for tool in value:
        if not isinstance(tool, dict):
            raise BridgeError("invalid_tool", "A Responses tool is not an object.")
        if tool.get("type") == "web_search":
            if tool.get("external_web_access") is False:
                continue
            raise BridgeError(
                "unsupported_tool",
                "Hosted web search is unavailable through the Chat Completions bridge.",
            )
        if tool.get("type") == "namespace":
            namespace = tool.get("name")
            children = tool.get("tools")
            if not isinstance(namespace, str) or not isinstance(children, list):
                raise BridgeError("invalid_tool", "A Codex tool namespace is incomplete.")
            for child in children:
                if not isinstance(child, dict):
                    raise BridgeError("invalid_tool", "A namespaced tool is not an object.")
                translated.append(_chat_tool(child, registry, namespace=namespace))
            continue
        namespace = tool.get("namespace")
        translated.append(
            _chat_tool(
                tool,
                registry,
                namespace=namespace if isinstance(namespace, str) else None,
            )
        )
    if len(translated) > MAX_TOOLS:
        translated = _compress_namespaced_tools(translated, registry)
    return translated


def _translate_messages(
    value: object,
    registry: _ToolRegistry,
    instructions: str | None,
    reasoning_secret: str | None,
    codex_home: Path | None,
    provider_id: str = "deepseek",
    replay_reasoning: bool = True,
) -> list[dict[str, Any]]:
    if isinstance(value, str):
        value = [{"type": "message", "role": "user", "content": value}]
    if not isinstance(value, list):
        raise BridgeError("invalid_input", "Responses input must be text or an array.")
    system_parts = [instructions] if instructions else []
    messages: list[dict[str, Any]] = []
    pending_reasoning = ""
    last_assistant_index: int | None = None
    last_was_tool_call = False
    for item in value:
        if not isinstance(item, dict):
            raise BridgeError("invalid_input", "A Responses input item is not an object.")
        item_type = item.get("type", "message")
        if item_type == "message":
            role = item.get("role")
            content = _text_value(item.get("content"))
            if role in {"system", "developer"}:
                if content:
                    system_parts.append(content)
            elif role in {"user", "assistant"}:
                message: dict[str, Any] = {"role": role, "content": content}
                if role == "assistant":
                    if pending_reasoning:
                        message["reasoning_content"] = pending_reasoning
                    messages.append(message)
                    last_assistant_index = len(messages) - 1
                else:
                    messages.append(message)
                    pending_reasoning = ""
                    last_assistant_index = None
                last_was_tool_call = False
            else:
                raise BridgeError("unsupported_role", f"Unsupported message role: {role!r}.")
            continue
        if item_type == "agent_message":
            author = item.get("author")
            recipient = item.get("recipient")
            if not isinstance(author, str) or not isinstance(recipient, str):
                raise BridgeError(
                    "invalid_agent_message",
                    "A Codex V2 agent message is missing its route.",
                )
            content = _agent_message_content(
                item.get("content"),
                author=author,
                recipient=recipient,
                codex_home=codex_home,
                provider_id=provider_id,
            )
            messages.append({
                "role": "user",
                "content": f"[Codex agent message: {author} -> {recipient}]\n{content}",
            })
            pending_reasoning = ""
            last_assistant_index = None
            last_was_tool_call = False
            continue
        if item_type == "reasoning":
            if not replay_reasoning:
                last_assistant_index = None
                last_was_tool_call = False
                continue
            opened = _open_reasoning(
                item.get("encrypted_content"),
                reasoning_secret,
                provider_id,
                allow_legacy=replay_reasoning,
            )
            if opened:
                pending_reasoning += opened
            last_assistant_index = None
            last_was_tool_call = False
            continue
        if item_type in {"function_call", "custom_tool_call"}:
            name = item.get("name")
            call_id = item.get("call_id")
            namespace = item.get("namespace")
            if not isinstance(name, str) or not isinstance(call_id, str):
                raise BridgeError("invalid_tool_history", "A historical tool call is incomplete.")
            selected_namespace = namespace if isinstance(namespace, str) else None
            chat_name = registry.chat_name(selected_namespace, name)
            if item_type == "custom_tool_call":
                arguments = json.dumps(
                    {"input": _text_value(item.get("input"))},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            else:
                arguments = item.get("arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            dispatcher = registry.dispatcher_for(chat_name)
            if dispatcher is not None:
                try:
                    selected_arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    selected_arguments = arguments
                arguments = json.dumps(
                    {"tool": chat_name, "arguments": selected_arguments},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                chat_name = dispatcher.chat_name
            chat_call = {
                "id": call_id,
                "type": "function",
                "function": {"name": chat_name, "arguments": arguments},
            }
            if (
                last_assistant_index is not None
                and last_assistant_index == len(messages) - 1
                and messages[last_assistant_index].get("role") == "assistant"
                and (last_was_tool_call or "tool_calls" not in messages[last_assistant_index])
            ):
                assistant = messages[last_assistant_index]
                assistant.setdefault("tool_calls", []).append(chat_call)
                if not isinstance(assistant.get("content"), str):
                    assistant["content"] = ""
                if pending_reasoning and "reasoning_content" not in assistant:
                    assistant["reasoning_content"] = pending_reasoning
            else:
                assistant = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [chat_call],
                }
                if pending_reasoning:
                    assistant["reasoning_content"] = pending_reasoning
                messages.append(assistant)
                last_assistant_index = len(messages) - 1
            pending_reasoning = ""
            last_was_tool_call = True
            continue
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                raise BridgeError("invalid_tool_history", "A tool output is missing call_id.")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _text_value(item.get("output")),
                }
            )
            last_assistant_index = None
            last_was_tool_call = False
            continue
        if item_type == "compaction":
            raise BridgeError(
                "unsupported_compaction",
                "Opaque Codex compaction items cannot be translated for DeepSeek.",
            )
        raise BridgeError(
            "unsupported_input",
            f"Unsupported Responses input type: {item_type!r}.",
        )
    if system_parts:
        messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})
    if not messages:
        messages.append({"role": "user", "content": "Continue."})
    return messages


def _effort(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("effort")
    return "max" if value in {"ultra", "max", "xhigh"} else "high"


def _legacy_build_chat_request(
    body: dict[str, Any],
    *,
    provider: ProviderSpec | None = None,
    allowed_models: set[str] | frozenset[str] | None = None,
    reasoning_secret: str | None = None,
    codex_home: Path | None = None,
) -> ChatRequest:
    """Translate one Codex Responses request for a selected chat provider."""

    if not isinstance(body, dict):
        raise BridgeError("invalid_request", "Responses request body must be an object.")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise BridgeError("unsupported_model", "A routed provider model is required.")
    if provider is None and model != REQUIRED_MODEL:
        raise BridgeError(
            "unsupported_model",
            f"This bridge is pinned to {REQUIRED_MODEL}.",
        )
    if provider is not None and provider.protocol not in {
        "chat-completions-compatible",
        "deepseek-chat",
    }:
        raise BridgeError(
            "unsupported_provider_protocol",
            f"Provider {provider.id} does not use the chat bridge.",
        )
    if allowed_models is not None and model not in allowed_models:
        raise BridgeError(
            "unsupported_model",
            "The requested model is not assigned to this provider route.",
        )
    provider_id = provider.id if provider is not None else "deepseek"
    deepseek_mode = provider is None or provider.protocol == "deepseek-chat"
    registry = _ToolRegistry()
    tools = _translate_tools(body.get("tools"), registry)
    instructions = body.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise BridgeError("invalid_instructions", "Responses instructions must be text.")
    payload: dict[str, Any] = {
        "model": model,
        "messages": _translate_messages(
            body.get("input", []),
            registry,
            instructions,
            reasoning_secret,
            codex_home,
            provider_id,
            deepseek_mode,
        ),
        "stream": True,
    }
    if deepseek_mode:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = _effort(body.get("reasoning"))
    if tools:
        payload["tools"] = tools
    max_tokens = body.get("max_output_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
        payload["max_tokens"] = max_tokens
    return ChatRequest(payload=payload, tools=dict(registry.routes))


class _LegacyChatStreamTranslator:
    """Stateful Chat Completions chunk-to-Responses event translator."""

    def __init__(
        self,
        tools: dict[str, ToolRoute],
        *,
        reasoning_secret: str | None = None,
        provider_id: str = "deepseek",
        preserve_reasoning: bool = True,
    ) -> None:
        self.tools = tools
        self.reasoning_secret = reasoning_secret
        self.provider_id = provider_id
        self.preserve_reasoning = preserve_reasoning
        self.response_id = ""
        self.message_id = f"msg_{uuid.uuid4().hex}"
        self.text_parts: list[str] = []
        self.text_started = False
        self.reasoning_parts: list[str] = []
        self.calls: dict[int, dict[str, str]] = {}
        self.usage: dict[str, Any] = {
            "input_tokens": 0,
            "input_tokens_details": None,
            "output_tokens": 0,
            "output_tokens_details": None,
            "total_tokens": 0,
        }
        self.finished = False

    def _start_text(self) -> list[dict[str, Any]]:
        if self.text_started:
            return []
        self.text_started = True
        return [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": self.message_id,
                    "status": "in_progress",
                    "content": [],
                },
            },
            {
                "type": "response.content_part.added",
                "item_id": self.message_id,
                "output_index": 0,
                "content_index": 0,
                "part": {
                    "type": "output_text",
                    "text": "",
                    "annotations": [],
                },
            },
        ]

    def start(self, response_id: str | None = None) -> list[dict[str, Any]]:
        if not self.response_id:
            self.response_id = response_id or f"resp_{uuid.uuid4().hex}"
        return [{"type": "response.created", "response": {"id": self.response_id}}]

    def feed(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.response_id:
            self.start()
        events: list[dict[str, Any]] = []
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens", 0)
            completion = usage.get("completion_tokens", 0)
            total = usage.get("total_tokens", 0)
            if all(isinstance(value, int) for value in (prompt, completion, total)):
                self.usage = {
                    "input_tokens": prompt,
                    "input_tokens_details": None,
                    "output_tokens": completion,
                    "output_tokens_details": {
                        "reasoning_tokens": (
                            usage.get("completion_tokens_details", {}) or {}
                        ).get("reasoning_tokens", 0)
                    },
                    "total_tokens": total,
                }
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return events
        choice = choices[0]
        if not isinstance(choice, dict):
            return events
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return events
        content = delta.get("content")
        if isinstance(content, str) and content:
            events.extend(self._start_text())
            self.text_parts.append(content)
            events.append({
                "type": "response.output_text.delta",
                "item_id": self.message_id,
                "output_index": 0,
                "content_index": 0,
                "delta": content,
            })
        reasoning = delta.get("reasoning_content")
        if self.preserve_reasoning and isinstance(reasoning, str) and reasoning:
            self.reasoning_parts.append(reasoning)
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                index = tool_call.get("index", 0)
                if not isinstance(index, int):
                    continue
                state = self.calls.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": ""},
                )
                call_id = tool_call.get("id")
                if isinstance(call_id, str):
                    state["id"] = call_id
                function = tool_call.get("function")
                if isinstance(function, dict):
                    name = function.get("name")
                    arguments = function.get("arguments")
                    if isinstance(name, str) and name:
                        current_name = state["name"]
                        if not current_name:
                            state["name"] = name
                        elif name == current_name or current_name.endswith(name):
                            pass
                        elif name.startswith(current_name):
                            state["name"] = name
                        else:
                            state["name"] += name
                    if isinstance(arguments, str):
                        state["arguments"] += arguments
        return events

    @staticmethod
    def _custom_input(arguments: str) -> str:
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
        if isinstance(parsed, dict) and isinstance(parsed.get("input"), str):
            return parsed["input"]
        return arguments

    @staticmethod
    def _resolve_dispatch(route: ToolRoute, arguments: str) -> tuple[ToolRoute, str]:
        try:
            wrapper = json.loads(arguments)
        except json.JSONDecodeError:
            raise BridgeError(
                "invalid_tool_dispatch",
                "The upstream provider returned malformed dispatcher arguments.",
                502,
            ) from None
        if not isinstance(wrapper, dict):
            raise BridgeError(
                "invalid_tool_dispatch",
                "The upstream provider returned a non-object dispatcher call.",
                502,
            )
        selector = wrapper.get("tool")
        targets = route.dispatch_targets or {}
        target = targets.get(selector) if isinstance(selector, str) else None
        if target is None:
            raise BridgeError(
                "invalid_tool_dispatch",
                "The upstream provider selected an unknown Codex tool.",
                502,
            )
        if "arguments" not in wrapper:
            raise BridgeError(
                "invalid_tool_dispatch",
                "The upstream provider omitted the selected Codex tool arguments.",
                502,
            )
        selected_arguments = wrapper["arguments"]
        if target.custom and isinstance(selected_arguments, str):
            selected_arguments = {"input": selected_arguments}
        if isinstance(selected_arguments, str):
            try:
                parsed_arguments = json.loads(selected_arguments)
            except json.JSONDecodeError:
                raise BridgeError(
                    "invalid_tool_dispatch",
                    "The upstream provider returned malformed selected-tool arguments.",
                    502,
                ) from None
            selected_arguments = parsed_arguments
        if not isinstance(selected_arguments, dict):
            raise BridgeError(
                "invalid_tool_dispatch",
                "The upstream provider returned non-object selected-tool arguments.",
                502,
            )
        return target, json.dumps(
            selected_arguments,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _safe_progress_summary(self) -> str:
        steps: list[str] = []
        for _, call in sorted(self.calls.items()):
            route = self.tools.get(call["name"])
            if route and route.dispatch_targets is not None:
                route, _ = self._resolve_dispatch(route, call["arguments"])
            name = route.name if route else call["name"]
            namespace = route.namespace if route else None
            if name == "shell_command":
                step = "检查本地状态并运行验证"
            elif name == "apply_patch":
                step = "应用文件修改"
            elif namespace == "web" or name in {"web__run", "web_run"}:
                step = "检索并核对外部资料"
            elif name == "view_image":
                step = "检查图像内容"
            elif namespace == "agents" or name in {
                "spawn_agent",
                "followup_task",
                "send_message",
                "wait_agent",
                "list_agents",
            }:
                step = "协调子任务"
            else:
                origin = f"{namespace}.{name}" if namespace else name
                step = f"调用工具 {origin}"
            if step not in steps:
                steps.append(step)
        if not steps:
            return "本轮分析完成；正在形成结论。"
        return f"本轮分析完成；下一步：{'；'.join(steps)}。"

    def finish(self) -> list[dict[str, Any]]:
        if self.finished:
            return []
        if not self.response_id:
            self.start()
        self.finished = True
        events: list[dict[str, Any]] = []
        reasoning = "".join(self.reasoning_parts)
        if reasoning:
            if not self.reasoning_secret:
                raise BridgeError(
                    "reasoning_key_missing",
                    "DeepSeek reasoning could not be preserved for tool continuation.",
                    500,
                )
            events.append(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "reasoning",
                        "id": f"rs_{uuid.uuid4().hex}",
                        "summary": [{
                            "type": "summary_text",
                            "text": self._safe_progress_summary(),
                        }],
                        "encrypted_content": _seal_reasoning(
                            reasoning,
                            self.reasoning_secret,
                            self.provider_id,
                        ),
                    },
                }
            )
        text = "".join(self.text_parts)
        if text:
            events.extend(self._start_text())
            completed_part = {
                "type": "output_text",
                "text": text,
                "annotations": [],
            }
            events.extend([
                {
                    "type": "response.output_text.done",
                    "item_id": self.message_id,
                    "output_index": 0,
                    "content_index": 0,
                    "text": text,
                },
                {
                    "type": "response.content_part.done",
                    "item_id": self.message_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": completed_part,
                },
            ])
            events.append(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": self.message_id,
                        "status": "completed",
                        "content": [completed_part],
                    },
                }
            )
        for _, call in sorted(self.calls.items()):
            call_id = call["id"] or f"call_{uuid.uuid4().hex}"
            route = self.tools.get(call["name"])
            arguments = call["arguments"]
            if route and route.dispatch_targets is not None:
                route, arguments = self._resolve_dispatch(route, arguments)
            if route and route.custom:
                item: dict[str, Any] = {
                    "type": "custom_tool_call",
                    "call_id": call_id,
                    "name": route.name,
                    "input": self._custom_input(arguments),
                }
            else:
                item = {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": route.name if route else call["name"],
                    "arguments": arguments or "{}",
                }
            if route and route.namespace:
                item["namespace"] = route.namespace
            events.append({"type": "response.output_item.done", "item": item})
        events.append(
            {
                "type": "response.completed",
                "response": {"id": self.response_id, "usage": self.usage},
            }
        )
        return events


if __package__ in {None, ""}:
    from multi_relay.protocols.chat_completions import (
        ChatStreamTranslator,
        build_chat_request,
    )
else:
    from .protocols.chat_completions import (
        ChatStreamTranslator,
        build_chat_request,
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_upstream_events(response: Any) -> Any:
    data_lines: list[bytes] = []
    for raw in response:
        line = raw.rstrip(b"\r\n")
        if not line:
            if data_lines:
                data = b"\n".join(data_lines)
                data_lines.clear()
                if data == b"[DONE]":
                    return
                try:
                    event = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise BridgeError("invalid_upstream_stream", "The upstream provider returned invalid SSE.", 502)
                if isinstance(event, dict):
                    yield event
            continue
        if line.startswith(b"data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        data = b"\n".join(data_lines)
        if data != b"[DONE]":
            try:
                event = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise BridgeError("invalid_upstream_stream", "The upstream provider returned invalid SSE.", 502)
            if isinstance(event, dict):
                yield event


def _legacy_deepseek_provider(upstream_url: str) -> ProviderSpec:
    suffix = "/chat/completions"
    base_url = upstream_url[: -len(suffix)] if upstream_url.endswith(suffix) else upstream_url
    return ProviderSpec.from_dict(
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "protocol": "deepseek-chat",
            "base_url": base_url.rstrip("/"),
            "auth": "vault",
            "capabilities": ["text", "tools"],
            "context_window": 1_000_000,
            "enabled": True,
        }
    )


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        upstream_url: str = DEEPSEEK_CHAT_URL,
        *,
        catalog: Catalog | None = None,
    ) -> None:
        routes: dict[str, BridgeRoute] = {}
        if catalog is None:
            validated_upstream = _validate_upstream_url(upstream_url)
            legacy_provider = _legacy_deepseek_provider(validated_upstream)
            routes["deepseek"] = BridgeRoute(
                provider=legacy_provider,
                upstream_url=validated_upstream,
                allowed_models=frozenset({REQUIRED_MODEL}),
            )
        else:
            for provider in catalog.providers:
                if not provider.enabled or provider.protocol not in {
                    "chat-completions-compatible",
                    "deepseek-chat",
                }:
                    continue
                models = frozenset(
                    agent.model
                    for agent in catalog.agents
                    if agent.provider == provider.id and agent.model is not None
                )
                routes[provider.id] = BridgeRoute(
                    provider=provider,
                    upstream_url=_chat_completions_url(provider),
                    allowed_models=models,
                )
            if not routes:
                raise BridgeError(
                    "provider_unavailable",
                    "The catalog contains no enabled chat providers.",
                    500,
                )
            validated_upstream = next(iter(routes.values())).upstream_url
        super().__init__(address, _BridgeHandler)
        self.upstream_url = validated_upstream
        self.routes = routes

    def route_for_path(self, path: str) -> BridgeRoute | None:
        parsed = urllib.parse.urlsplit(path)
        if parsed.query or parsed.fragment:
            return None
        if parsed.path in {"/responses", "/v1/responses"}:
            return self.routes.get("deepseek")
        matched = re.fullmatch(
            r"/(?:v1/)?providers/(?P<provider>[a-z0-9][a-z0-9_-]*)/responses",
            parsed.path,
        )
        return self.routes.get(matched.group("provider")) if matched else None


class _BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CodexMultiRelayBridge/3"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: int, payload: object) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error: BridgeError) -> None:
        self._send_json(
            error.status,
            {"error": {"type": error.code, "code": error.code, "message": str(error)}},
        )

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "service": BRIDGE_SERVICE,
                "version": BRIDGE_VERSION,
                "pid": os.getpid(),
            },
        )

    def do_POST(self) -> None:
        if self.path == "/_shutdown":
            expected = str(os.getpid())
            supplied = self.headers.get("X-Codex-Multi-Relay-Bridge-Pid") or self.headers.get(
                "X-Codex-DeepSeek-Bridge-Pid"
            )
            if supplied != expected:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            self._send_json(HTTPStatus.OK, {"status": "stopping"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        route = self.server.route_for_path(self.path)  # type: ignore[attr-defined]
        if route is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            self._handle_responses(route)
        except BridgeError as error:
            self._send_error(error)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _handle_responses(self, route: BridgeRoute) -> None:
        encoding = self.headers.get("Content-Encoding", "identity").casefold()
        if encoding not in {"", "identity"}:
            raise BridgeError("unsupported_encoding", "Compressed request bodies are unsupported.", 415)
        length_text = self.headers.get("Content-Length")
        try:
            length = int(length_text or "0")
        except ValueError:
            raise BridgeError("invalid_length", "Invalid request length.") from None
        if length < 1 or length > MAX_REQUEST_BYTES:
            raise BridgeError("invalid_length", "Responses request size is invalid.", 413)
        authorization = self.headers.get("Authorization", "")
        if route.provider.auth == "vault":
            valid_bearer = authorization.startswith("Bearer ") and len(authorization) > len("Bearer ")
            if route.provider.protocol == "deepseek-chat":
                valid_bearer = authorization.startswith("Bearer sk-")
            if not valid_bearer or any(character in authorization for character in "\r\n\0"):
                raise BridgeError(
                    "authentication_failed",
                    f"A bearer credential for provider {route.provider.id} is required.",
                    401,
                )
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BridgeError("invalid_json", "Responses request JSON is invalid.") from None
        secret = authorization[len("Bearer ") :] if authorization.startswith("Bearer ") else ""
        translated = build_chat_request(
            body,
            provider=route.provider,
            allowed_models=route.allowed_models,
            reasoning_secret=secret or None,
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "codex-multi-relay-chat-bridge/3",
        }
        if route.provider.auth == "vault":
            headers["Authorization"] = authorization
        upstream = urllib.request.Request(
            route.upstream_url,
            data=_json_bytes(translated.payload),
            headers=headers,
            method="POST",
        )
        try:
            # The server constructor has already restricted this URL to the
            # official HTTPS endpoint or an explicit loopback test fixture.
            response = _open_upstream(upstream, timeout=600)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                try:
                    exc.read(MAX_UPSTREAM_ERROR_BYTES)
                except Exception:
                    # The status is authoritative; a truncated provider error
                    # body must not tear down the loopback client connection.
                    pass
            finally:
                try:
                    exc.close()
                except Exception:
                    pass
            if 300 <= status < 400:
                raise BridgeError(
                    "provider_redirect_blocked",
                    f"Provider {route.provider.id} attempted an unsafe redirect.",
                    502,
                ) from None
            raise BridgeError(
                "deepseek_http_error",
                f"Provider {route.provider.id} returned HTTP {status}.",
                status,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BridgeError(
                "deepseek_unavailable",
                f"Provider {route.provider.id} could not be reached by the local bridge.",
                502,
            ) from None
        with response:
            content_type = response.headers.get("Content-Type", "").casefold()
            if "text/event-stream" not in content_type:
                raw = response.read(MAX_REQUEST_BYTES + 1)
                if len(raw) > MAX_REQUEST_BYTES:
                    raise BridgeError("upstream_too_large", "The upstream provider response was too large.", 502)
                try:
                    completion = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise BridgeError("invalid_upstream_response", "The upstream provider returned invalid JSON.", 502)
                chunks = [_completion_as_chunk(completion)]
            else:
                chunks = _read_upstream_events(response)

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            translator = ChatStreamTranslator(
                translated.tools,
                reasoning_secret=secret or None,
                provider_id=route.provider.id,
                preserve_reasoning=route.provider.protocol == "deepseek-chat",
            )
            self._write_events(translator.start())
            try:
                for chunk in chunks:
                    self._write_events(translator.feed(chunk))
                self._write_events(translator.finish())
            except BridgeError as error:
                self._write_events(
                    [{
                        "type": "response.failed",
                        "response": {
                            "id": translator.response_id,
                            "error": {"code": error.code, "message": str(error)},
                        },
                    }]
                )

    def _write_events(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            kind = event["type"]
            self.wfile.write(f"event: {kind}\n".encode("utf-8"))
            self.wfile.write(b"data: " + _json_bytes(event) + b"\n\n")
        if events:
            self.wfile.flush()


def _completion_as_chunk(completion: object) -> dict[str, Any]:
    if not isinstance(completion, dict):
        raise BridgeError("invalid_upstream_response", "DeepSeek response is not an object.", 502)
    choices = completion.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise BridgeError("invalid_upstream_response", "DeepSeek response has no choice.", 502)
    first = choices[0]
    message = first.get("message")
    if not isinstance(message, dict):
        raise BridgeError("invalid_upstream_response", "DeepSeek response has no message.", 502)
    tool_calls = message.get("tool_calls")
    indexed_calls: list[dict[str, Any]] | None = None
    if isinstance(tool_calls, list):
        indexed_calls = []
        for index, tool_call in enumerate(tool_calls):
            if not isinstance(tool_call, dict):
                continue
            indexed_calls.append({**tool_call, "index": index})
    return {
        "id": completion.get("id"),
        "choices": [{
            "delta": {
                "content": message.get("content"),
                "reasoning_content": message.get("reasoning_content"),
                "tool_calls": indexed_calls,
            },
            "finish_reason": first.get("finish_reason"),
        }],
        "usage": completion.get("usage"),
    }


def _health(timeout: float = 0.5) -> dict[str, Any] | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(BRIDGE_HEALTH_URL, timeout=timeout) as response:
            payload = json.loads(response.read(4096).decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


_gateway_controller: Any | None = None


def ensure_bridge(timeout: float = 5.0, *, codex_home: Path | None = None) -> None:
    """Compatibility entry point delegated to the unified gateway controller."""

    global _gateway_controller
    try:
        if __package__ in {None, ""}:
            from multi_relay.gateway import GatewayController
        else:
            from .gateway import GatewayController
        controller = GatewayController(codex_home=codex_home)
        controller.ensure(timeout=timeout)
        _gateway_controller = controller
    except ManagerError as error:
        raise BridgeError(error.code, str(error), getattr(error, "status", 500)) from None


def _stop_bridge_health(current: dict[str, Any]) -> bool:
    service = current.get("service")
    if service not in {BRIDGE_SERVICE, LEGACY_BRIDGE_SERVICE}:
        return False
    pid = current.get("pid")
    if not isinstance(pid, int):
        return False
    header = (
        "X-Codex-DeepSeek-Bridge-Pid"
        if service == LEGACY_BRIDGE_SERVICE
        else "X-Codex-Multi-Relay-Bridge-Pid"
    )
    request = urllib.request.Request(
        f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/_shutdown",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            header: str(pid),
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=1) as response:
            return response.status == HTTPStatus.OK
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def stop_bridge() -> bool:
    current = _health()
    return _stop_bridge_health(current) if current else False


def _installed_catalog_path(codex_home: Path | None = None) -> Path | None:
    home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    for path in (
        home / "codex-multi-relay" / "catalog.json",
        home / "codex-deepseek-relay" / "catalog.json",
    ):
        if path.is_file():
            return path
    return None


def _installed_catalog(codex_home: Path | None = None) -> Catalog | None:
    path = _installed_catalog_path(codex_home)
    if path is None:
        return None
    try:
        return load_catalog(path)
    except Exception as exc:
        raise BridgeError(
            "catalog_invalid",
            "The installed provider catalog could not be loaded.",
            500,
        ) from exc


def serve(
    upstream_url: str = DEEPSEEK_CHAT_URL,
    *,
    catalog: Catalog | None = None,
) -> None:
    server = _BridgeServer(
        (BRIDGE_HOST, BRIDGE_PORT),
        upstream_url,
        catalog=catalog or _installed_catalog(),
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--catalog")
    args = parser.parse_args(argv)
    if not args.serve:
        parser.error("--serve is required")
    try:
        catalog = load_catalog(Path(args.catalog)) if args.catalog else None
        serve(catalog=catalog)
    except (OSError, BridgeError, ManagerError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
