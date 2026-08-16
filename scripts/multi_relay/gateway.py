#!/usr/bin/env python3
"""Unified loopback gateway for Responses and Anthropic Messages hosts."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "multi_relay"

from .canonical import CanonicalEvent, CanonicalRequest, EventKind
from .catalog import Catalog, CredentialRef, ExecutionTarget, load_catalog
from .credentials import (
    VaultLocator,
    credential_store,
    local_gateway_credential_store,
    read_credential_for_execution,
)
from .errors import ManagerError
from .failure import (
    NormalizedFailure,
    classify_http_failure,
    classify_transport_failure,
)
from .paths import resolve_paths
from .protocols import ChatCompletionsAdapter, ResponsesAdapter
from .protocols.anthropic_messages import (
    ANTHROPIC_VERSION,
    AnthropicInboundAdapter,
    AnthropicOutboundRenderer,
    AnthropicUpstreamAdapter,
)
from .protocols.base import ProviderErrorMetadata
from .rotation import RotationController, catalog_fingerprint
from .selection import SelectionRequirements
from .state import RuntimeStateStore
from .transaction import atomic_write


GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 42137
GATEWAY_BASE_URL = f"http://{GATEWAY_HOST}:{GATEWAY_PORT}/v1"
GATEWAY_SERVICE = "multi-relay-gateway"
GATEWAY_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_UPSTREAM_BYTES = 32 * 1024 * 1024
MAX_ERROR_BYTES = 1024 * 1024
TOKEN_LIFETIME_SECONDS = 12 * 60 * 60
_COMMIT_EVENTS = frozenset(
    {
        EventKind.CONTENT_BLOCK_STARTED,
        EventKind.TEXT_DELTA,
        EventKind.TOOL_CALL_STARTED,
        EventKind.TOOL_CALL_ARGUMENTS_DELTA,
    }
)


class GatewayError(ManagerError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, message, details)
        self.status = status


class GatewayExhausted(GatewayError):
    def __init__(self, attempts: Iterable[Mapping[str, object]]) -> None:
        self.attempts = tuple(dict(item) for item in attempts)
        super().__init__(
            "no_eligible_target",
            "Every eligible relay target failed before the response committed.",
            status=503,
            details={"attempts": [dict(item) for item in self.attempts]},
        )


class GatewayCancelled(GatewayError):
    def __init__(self) -> None:
        super().__init__("cancelled", "The host cancelled the request.", status=499)


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: set[Callable[[], None]] = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                pass

    def register(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._callbacks.add(callback)
        if self.cancelled:
            callback()

    def unregister(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._callbacks.discard(callback)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise GatewayCancelled()


def _noop() -> None:
    return


@dataclass
class AttemptResponse:
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    events: Iterable[CanonicalEvent] = ()
    error_body: bytes = b""
    provider_error: ProviderErrorMetadata | None = None
    close: Callable[[], None] = _noop


class RequestLifecycle:
    _TERMINAL = frozenset({"completed", "failed", "cancelled"})

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status = "selected"
        self.committed = False
        self.history: list[str] = ["selected"]

    def transition(self, status: str) -> None:
        if status not in {
            "selected",
            "attempting",
            "committed",
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError(status)
        with self._lock:
            if self.status in self._TERMINAL:
                return
            self.status = status
            self.history.append(status)
            if status == "committed":
                self.committed = True


@dataclass(frozen=True)
class RelayRoute:
    pool_id: str
    required_capabilities: frozenset[str] = frozenset()
    required_trust: str = "standard"
    context_tokens: int | None = None
    reasoning_effort: str | None = None
    forced_target_ids: tuple[str, ...] = ()


class GatewayExecution:
    def __init__(
        self,
        iterator: Iterator[CanonicalEvent],
        lifecycle: RequestLifecycle,
        attempts: list[dict[str, object]],
        cancellation: CancellationToken,
    ) -> None:
        self._iterator = iterator
        self._consumed = False
        self.lifecycle = lifecycle
        self._attempts = attempts
        self.cancellation = cancellation

    @property
    def attempts(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self._attempts)

    def __iter__(self) -> Iterator[CanonicalEvent]:
        if self._consumed:
            raise RuntimeError("Gateway execution streams can only be consumed once.")
        self._consumed = True
        return self._iterator


CredentialReader = Callable[[CredentialRef, str], str]
AttemptExecutor = Callable[
    [ExecutionTarget, CanonicalRequest, str | None, CancellationToken],
    AttemptResponse,
]


def _safe_attempt(target_id: str, failure: NormalizedFailure) -> dict[str, object]:
    return {
        "target_id": target_id,
        "failure_class": failure.failure_class.value,
        "code": failure.code,
        "http_status": failure.http_status,
    }


def _protocol_failure(
    error: ManagerError,
    *,
    provider_id: str,
    committed: bool,
    secret: str | None,
) -> NormalizedFailure:
    return classify_http_failure(
        None,
        b"{}",
        headers={"content-type": "application/json"},
        provider_error=ProviderErrorMetadata(
            code="protocol_error",
            error_type=error.code,
        ),
        provider_id=provider_id,
        committed=committed,
        secrets=(secret,) if secret else (),
    )


def _event_failure(
    event: CanonicalEvent,
    *,
    provider_id: str,
    committed: bool,
    secret: str | None,
) -> NormalizedFailure:
    code = event.payload.get("code")
    return classify_http_failure(
        None,
        b"{}",
        headers={"content-type": "application/json"},
        provider_error=ProviderErrorMetadata(
            code=code if isinstance(code, str) else "protocol_error",
        ),
        provider_id=provider_id,
        committed=committed,
        secrets=(secret,) if secret else (),
    )


def _safe_error_event(
    failure: NormalizedFailure,
    last: CanonicalEvent | None,
    request_id: str,
) -> CanonicalEvent:
    return CanonicalEvent(
        kind=EventKind.ERROR,
        response_id=last.response_id if last is not None else request_id,
        sequence=(last.sequence + 1) if last is not None else 0,
        payload={"code": failure.code, "message": failure.message},
    )


class GatewayApplication:
    """Compose catalog routing, vault access, rotation, and wire adapters."""

    def __init__(
        self,
        catalog: Catalog,
        *,
        rotation: RotationController,
        credential_reader: CredentialReader,
        attempt_executor: AttemptExecutor,
        request_token: str,
        shutdown_token: str,
        token_expires_at: float | None = None,
        time_source: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        nonce: str | None = None,
    ) -> None:
        if not request_token or not shutdown_token or hmac.compare_digest(
            request_token, shutdown_token
        ):
            raise ValueError("Gateway request and shutdown tokens must be distinct.")
        self.catalog = catalog
        self.rotation = rotation
        self.credential_reader = credential_reader
        self.attempt_executor = attempt_executor
        self.request_token = request_token
        self.shutdown_token = shutdown_token
        self.token_expires_at = token_expires_at
        self.time_source = time_source
        self.sleep = sleep
        self.nonce = nonce or secrets.token_urlsafe(18)
        self.catalog_hash = catalog_fingerprint(catalog)
        self._accepting = True
        self._active_lock = threading.Condition()
        self._active: set[CancellationToken] = set()
        self._slots = threading.BoundedSemaphore(catalog.concurrency)

    def _presented_token(self, headers: Mapping[str, str]) -> str | None:
        authorization = None
        api_key = None
        for key, value in headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            if key.casefold() == "authorization":
                authorization = value
            elif key.casefold() == "x-api-key":
                api_key = value
        if authorization and authorization.casefold().startswith("bearer "):
            return authorization[7:].strip()
        return api_key.strip() if api_key else None

    def authenticate_request(self, headers: Mapping[str, str]) -> None:
        if self.token_expires_at is not None and self.time_source() >= self.token_expires_at:
            raise GatewayError(
                "local_token_expired",
                "The local gateway token has expired.",
                status=401,
            )
        supplied = self._presented_token(headers)
        if supplied is None or not hmac.compare_digest(supplied, self.request_token):
            raise GatewayError(
                "authentication_failed",
                "A valid local gateway token is required.",
                status=401,
            )

    def authenticate_shutdown(self, headers: Mapping[str, str]) -> None:
        supplied = next(
            (
                value.strip()
                for key, value in headers.items()
                if isinstance(key, str)
                and key.casefold() == "x-multi-relay-shutdown-token"
                and isinstance(value, str)
            ),
            "",
        )
        if not supplied or not hmac.compare_digest(supplied, self.shutdown_token):
            raise GatewayError(
                "forbidden",
                "The shutdown token is invalid.",
                status=403,
            )

    def _host_config(self, host: str):
        config = self.catalog.hosts.get(host)
        if config is None or not config.enabled or config.default_pool is None:
            raise GatewayError(
                "host_disabled",
                f"The {host} gateway surface is disabled.",
                status=503,
            )
        return config

    def resolve_route(
        self,
        model: object,
        *,
        host: str,
        forced_provider_id: str | None = None,
    ) -> RelayRoute:
        config = self._host_config(host)
        if forced_provider_id is not None:
            candidates = tuple(
                target.id
                for target in self.catalog.targets
                if target.provider_id == forced_provider_id
                and host in target.host_compatibility
                and target.enabled
            )
            if not candidates:
                raise GatewayError(
                    "unknown_provider",
                    "The legacy provider route has no eligible target.",
                    status=404,
                )
            pool = next(
                (
                    item
                    for item in self.catalog.pools
                    if any(target in item.targets for target in candidates)
                    and host in item.host_compatibility
                ),
                None,
            )
            if pool is None:
                raise GatewayError(
                    "no_eligible_target",
                    "The legacy provider route has no compatible pool.",
                    status=503,
                )
            return RelayRoute(pool.id, forced_target_ids=candidates)
        if not isinstance(model, str) or not model:
            raise GatewayError("invalid_model", "A relay model alias is required.")
        if model == "multi-relay-default":
            return RelayRoute(config.default_pool)
        if model.startswith("multi-relay-agent-"):
            name = model.removeprefix("multi-relay-agent-")
            try:
                agent = self.catalog.agent(name)
            except ManagerError as error:
                raise GatewayError(error.code, str(error), status=404) from None
            if host not in agent.hosts:
                raise GatewayError("host_incompatible", "Agent alias is unavailable to this host.")
            return RelayRoute(
                agent.pool_id,
                agent.required_capabilities,
                agent.trust,
                agent.context_window,
                agent.reasoning_effort,
            )
        if model.startswith("multi-relay-"):
            pool_id = model.removeprefix("multi-relay-")
            try:
                pool = self.catalog.pool(pool_id)
            except ManagerError as error:
                raise GatewayError(error.code, str(error), status=404) from None
            if host not in pool.host_compatibility:
                raise GatewayError("host_incompatible", "Pool alias is unavailable to this host.")
            return RelayRoute(pool.id)
        raise GatewayError("invalid_model", "The request model must be a relay alias.")

    def model_listing(self) -> dict[str, object]:
        aliases: list[dict[str, object]] = [
            {"id": "multi-relay-default", "object": "model", "owned_by": "multi-relay"}
        ]
        aliases.extend(
            {
                "id": f"multi-relay-{pool.id}",
                "object": "model",
                "owned_by": "multi-relay",
                "capabilities": sorted(pool.required_capabilities),
            }
            for pool in self.catalog.pools
            if pool.enabled
        )
        aliases.extend(
            {
                "id": f"multi-relay-agent-{agent.name}",
                "object": "model",
                "owned_by": "multi-relay",
                "capabilities": sorted(agent.required_capabilities),
            }
            for agent in self.catalog.agents
        )
        return {"object": "list", "data": aliases}

    def _parse(
        self,
        surface: str,
        payload: object,
        headers: Mapping[str, str],
        request_id: str,
        forced_provider_id: str | None,
    ) -> tuple[CanonicalRequest, RelayRoute]:
        if not isinstance(payload, Mapping):
            raise GatewayError("invalid_json", "Request body must be a JSON object.")
        host = "codex" if surface == "responses" else "claude-code"
        route = self.resolve_route(
            payload.get("model"),
            host=host,
            forced_provider_id=forced_provider_id,
        )
        if surface == "responses":
            request = ResponsesAdapter().parse_request(
                payload,
                request_id=request_id,
                host=host,
                pool_id=route.pool_id,
            )
        elif surface == "messages":
            request = AnthropicInboundAdapter().parse_request(
                payload,
                headers=headers,
                request_id=request_id,
                host=host,
                pool_id=route.pool_id,
            )
        else:
            raise GatewayError("not_found", "Unknown gateway protocol surface.", status=404)
        if route.reasoning_effort and request.requested_reasoning_effort is None:
            request = replace(request, requested_reasoning_effort=route.reasoning_effort)
        return request, route

    def prepare_execution(
        self,
        surface: str,
        payload: object,
        *,
        headers: Mapping[str, str],
        request_id: str,
        forced_provider_id: str | None = None,
        cancellation: CancellationToken | None = None,
    ) -> GatewayExecution:
        normalized_headers = {
            str(key): str(value) for key, value in headers.items()
        }
        self.authenticate_request(normalized_headers)
        request, route = self._parse(
            surface,
            payload,
            normalized_headers,
            request_id,
            forced_provider_id,
        )
        lifecycle = RequestLifecycle()
        attempts: list[dict[str, object]] = []
        token = cancellation or CancellationToken()
        iterator = self._execute(request, route, lifecycle, attempts, token)
        return GatewayExecution(iterator, lifecycle, attempts, token)

    def _execute(
        self,
        request: CanonicalRequest,
        route: RelayRoute,
        lifecycle: RequestLifecycle,
        attempts: list[dict[str, object]],
        cancellation: CancellationToken,
    ) -> Iterator[CanonicalEvent]:
        requirements = SelectionRequirements(
            host=request.host,
            required_capabilities=(
                request.required_capabilities | route.required_capabilities
            ),
            context_tokens=route.context_tokens,
            required_trust=route.required_trust,
        )
        selection = self.rotation.select(route.pool_id, requirements)
        target_id = selection.selected_target_id
        if route.forced_target_ids and target_id not in route.forced_target_ids:
            target_id = route.forced_target_ids[0]
        if target_id is None:
            raise GatewayExhausted(
                item.to_dict() for item in selection.rejections
            )
        retried_same: set[str] = set()

        while target_id is not None:
            cancellation.raise_if_cancelled()
            lifecycle.transition("attempting")
            target = self.catalog.target(target_id)
            provider = self.catalog.provider(target.provider_id)
            protocol = target.protocol or provider.protocol
            secret: str | None = None
            response: AttemptResponse | None = None
            close_response: Callable[[], None] | None = None
            failure: NormalizedFailure | None = None
            last_event: CanonicalEvent | None = None
            buffered: list[CanonicalEvent] = []
            completed = False
            try:
                if target.credential_id is not None:
                    reference = self.catalog.credential(
                        target.credential_id, provider_id=target.provider_id
                    )
                    secret = self.credential_reader(reference, protocol)
                cancellation.raise_if_cancelled()
                response = self.attempt_executor(target, request, secret, cancellation)
                close_lock = threading.Lock()
                closed = False

                def close_once() -> None:
                    nonlocal closed
                    with close_lock:
                        if closed:
                            return
                        closed = True
                    response.close()

                close_response = close_once
                cancellation.register(close_response)
                if response.status < 200 or response.status >= 300:
                    failure = classify_http_failure(
                        response.status,
                        response.error_body,
                        headers=response.headers,
                        provider_error=response.provider_error,
                        provider_id=provider.id,
                        committed=False,
                        secrets=(secret,) if secret else (),
                    )
                else:
                    try:
                        for event in response.events:
                            cancellation.raise_if_cancelled()
                            last_event = event
                            if event.kind is EventKind.ERROR:
                                failure = _event_failure(
                                    event,
                                    provider_id=provider.id,
                                    committed=lifecycle.committed,
                                    secret=secret,
                                )
                                break
                            if not lifecycle.committed:
                                buffered.append(event)
                                if request.stream and event.kind in _COMMIT_EVENTS:
                                    lifecycle.transition("committed")
                                    for pending in buffered:
                                        yield pending
                                    buffered.clear()
                            else:
                                yield event
                            if event.kind is EventKind.RESPONSE_COMPLETED:
                                completed = True
                        if failure is None and not completed:
                            raise ManagerError(
                                "protocol_error",
                                "Upstream response ended before completion.",
                            )
                    except GatewayCancelled:
                        raise
                    except ManagerError as error:
                        failure = _protocol_failure(
                            error,
                            provider_id=provider.id,
                            committed=lifecycle.committed,
                            secret=secret,
                        )
                    except BaseException as error:
                        if cancellation.cancelled:
                            raise GatewayCancelled() from None
                        failure = classify_transport_failure(
                            error,
                            provider_id=provider.id,
                            committed=lifecycle.committed,
                            secrets=(secret,) if secret else (),
                        )
            except GatewayCancelled:
                lifecycle.transition("cancelled")
                raise
            except BaseException as error:
                if cancellation.cancelled:
                    lifecycle.transition("cancelled")
                    raise GatewayCancelled() from None
                if isinstance(error, ManagerError):
                    failure = classify_http_failure(
                        401,
                        b"{}",
                        headers={"content-type": "application/json"},
                        provider_error=ProviderErrorMetadata(code="authentication_error"),
                        provider_id=provider.id,
                        secrets=(secret,) if secret else (),
                    )
                else:
                    failure = classify_transport_failure(
                        error,
                        provider_id=provider.id,
                        committed=lifecycle.committed,
                        secrets=(secret,) if secret else (),
                    )
            finally:
                if response is not None:
                    assert close_response is not None
                    cancellation.unregister(close_response)
                    try:
                        close_response()
                    except Exception:
                        pass
                secret = None

            if failure is None and completed:
                if not lifecycle.committed:
                    lifecycle.transition("committed")
                    for pending in buffered:
                        yield pending
                lifecycle.transition("completed")
                return
            assert failure is not None
            if lifecycle.committed:
                yield _safe_error_event(failure, last_event, request.request_id)
                lifecycle.transition("failed")
                return

            attempts.append(_safe_attempt(target.id, failure))
            pool = self.catalog.pool(route.pool_id)
            retry_after = failure.retry.retry_after_seconds
            if (
                failure.retry.retry_same_target
                and retry_after is not None
                and retry_after <= pool.max_rate_limit_wait_seconds
                and target.id not in retried_same
            ):
                retried_same.add(target.id)
                cancellation.raise_if_cancelled()
                self.sleep(float(retry_after))
                cancellation.raise_if_cancelled()
                continue
            selection = self.rotation.record_failure(
                route.pool_id,
                target.id,
                failure,
                expected_generation=selection.generation,
                requirements=requirements,
            )
            target_id = selection.selected_target_id
            if route.forced_target_ids and target_id not in route.forced_target_ids:
                target_id = next(
                    (
                        item
                        for item in route.forced_target_ids
                        if item != target.id
                    ),
                    None,
                )
            if target_id is None or not failure.retry.failover_allowed:
                lifecycle.transition("failed")
                raise GatewayExhausted(attempts)

        lifecycle.transition("failed")
        raise GatewayExhausted(attempts)

    @contextmanager
    def request_scope(self, token: CancellationToken) -> Iterator[None]:
        if not self._accepting:
            raise GatewayError("shutting_down", "Gateway is shutting down.", status=503)
        self._slots.acquire()
        with self._active_lock:
            if not self._accepting:
                self._slots.release()
                raise GatewayError("shutting_down", "Gateway is shutting down.", status=503)
            self._active.add(token)
        try:
            yield
        finally:
            with self._active_lock:
                self._active.discard(token)
                self._active_lock.notify_all()
            self._slots.release()

    def begin_shutdown(self, grace_seconds: float = 5.0) -> None:
        with self._active_lock:
            self._accepting = False
            deadline = time.monotonic() + max(0.0, grace_seconds)
            while self._active and time.monotonic() < deadline:
                self._active_lock.wait(deadline - time.monotonic())
            remaining = tuple(self._active)
        for token in remaining:
            token.cancel()


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirect blocked", headers, fp)


def _endpoint(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    path = urllib.parse.urlsplit(base).path.rstrip("/")
    return base if path.endswith(suffix) else base + suffix


class HttpAttemptExecutor:
    def __init__(self, catalog: Catalog, *, timeout: float = 600.0) -> None:
        self.catalog = catalog
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_RejectRedirect())

    def __call__(
        self,
        target: ExecutionTarget,
        request: CanonicalRequest,
        credential: str | None,
        cancellation: CancellationToken,
    ) -> AttemptResponse:
        provider = self.catalog.provider(target.provider_id)
        protocol = target.protocol or provider.protocol
        if protocol == "codex-native" or provider.base_url is None or target.model is None:
            raise ManagerError("unsupported_target", "Native targets cannot use the HTTP gateway.")
        selected_request = request
        if target.max_output_tokens is not None:
            if request.max_output_tokens is None:
                selected_request = replace(request, max_output_tokens=target.max_output_tokens)
            elif request.max_output_tokens > target.max_output_tokens:
                raise ManagerError("request_invalid", "Requested output exceeds target limit.")
        if protocol == "responses-compatible":
            adapter: Any = ResponsesAdapter()
            suffix = "/responses"
        elif protocol in {"chat-completions-compatible", "deepseek-chat"}:
            adapter = ChatCompletionsAdapter()
            suffix = "/chat/completions"
        elif protocol == "anthropic-messages":
            adapter = AnthropicUpstreamAdapter()
            suffix = "/messages"
        else:
            raise ManagerError("unsupported_provider_protocol", "Provider protocol is unsupported.")
        payload = adapter.build_request(selected_request, model=target.model)
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if request.stream else "application/json",
            "User-Agent": "multi-relay-gateway/1",
        }
        if provider.auth_mode == "vault":
            if not credential:
                raise ManagerError("credential_missing", "Provider credential is unavailable.")
            if protocol == "anthropic-messages":
                headers["x-api-key"] = credential
                headers["anthropic-version"] = ANTHROPIC_VERSION
            else:
                headers["Authorization"] = f"Bearer {credential}"
        upstream = urllib.request.Request(
            _endpoint(provider.base_url, suffix),
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        cancellation.raise_if_cancelled()
        try:
            response = self.opener.open(upstream, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            try:
                body = error.read(MAX_ERROR_BYTES + 1)[:MAX_ERROR_BYTES]
                response_headers = {key: value for key, value in error.headers.items()}
            finally:
                error.close()
            provider_error = None
            try:
                provider_error = adapter.classify_error_metadata(
                    json.loads(body.decode("utf-8")), response_headers
                )
            except Exception:
                pass
            return AttemptResponse(
                status=error.code,
                headers=response_headers,
                error_body=body,
                provider_error=provider_error,
            )
        except urllib.error.URLError as error:
            reason = error.reason
            raise reason if isinstance(reason, BaseException) else ConnectionError() from None
        cancellation.register(response.close)
        response_headers = {key: value for key, value in response.headers.items()}
        content_type = response.headers.get("Content-Type", "").casefold()
        if "text/event-stream" in content_type:
            events = adapter.iter_events(response)
            return AttemptResponse(
                status=getattr(response, "status", 200),
                headers=response_headers,
                events=events,
                close=response.close,
            )
        try:
            raw = response.read(MAX_UPSTREAM_BYTES + 1)
            if len(raw) > MAX_UPSTREAM_BYTES:
                raise ManagerError("protocol_error", "Upstream response is too large.")
            decoded = json.loads(raw.decode("utf-8"))
            events = adapter.parse_response(decoded)
        finally:
            cancellation.unregister(response.close)
            response.close()
        return AttemptResponse(
            status=getattr(response, "status", 200),
            headers=response_headers,
            events=events,
        )


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _responses_result(events: Iterable[CanonicalEvent], model: str) -> dict[str, object]:
    response_id = "relay-response"
    texts: dict[int, str] = {}
    tools: dict[int, dict[str, object]] = {}
    usage: dict[str, object] | None = None
    error: dict[str, object] | None = None
    for event in events:
        response_id = event.response_id
        if event.kind is EventKind.TEXT_DELTA:
            assert event.block_index is not None
            texts[event.block_index] = texts.get(event.block_index, "") + str(event.payload["delta"])
        elif event.kind is EventKind.TOOL_CALL_STARTED:
            assert event.block_index is not None
            tools[event.block_index] = {
                "type": "function_call",
                "call_id": event.tool_call_id,
                "name": event.payload.get("name"),
                "arguments": "",
            }
        elif event.kind is EventKind.TOOL_CALL_ARGUMENTS_DELTA:
            assert event.block_index is not None
            tools.setdefault(event.block_index, {"type": "function_call"})["arguments"] = (
                str(tools.get(event.block_index, {}).get("arguments", ""))
                + str(event.payload.get("delta", ""))
            )
        elif event.kind is EventKind.USAGE:
            usage = dict(event.payload)
        elif event.kind is EventKind.ERROR:
            error = dict(event.payload)
    output: list[dict[str, object]] = []
    for index in sorted(set(texts) | set(tools)):
        if index in texts:
            output.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": texts[index]}],
                }
            )
        else:
            output.append(tools[index])
    return {
        "id": response_id,
        "object": "response",
        "status": "failed" if error else "completed",
        "model": model,
        "output": output,
        "usage": usage,
        "error": error,
    }


def _anthropic_result(events: Iterable[CanonicalEvent], model: str) -> dict[str, object]:
    response_id = "relay-message"
    texts: dict[int, str] = {}
    tools: dict[int, dict[str, object]] = {}
    usage: dict[str, object] = {"input_tokens": 0, "output_tokens": 0}
    finish = "end_turn"
    for event in events:
        response_id = event.response_id
        if event.kind is EventKind.TEXT_DELTA:
            assert event.block_index is not None
            texts[event.block_index] = texts.get(event.block_index, "") + str(event.payload["delta"])
        elif event.kind is EventKind.TOOL_CALL_STARTED:
            assert event.block_index is not None
            tools[event.block_index] = {
                "type": "tool_use",
                "id": event.tool_call_id,
                "name": event.payload.get("name"),
                "arguments": "",
            }
        elif event.kind is EventKind.TOOL_CALL_ARGUMENTS_DELTA:
            assert event.block_index is not None
            tools[event.block_index]["arguments"] = str(
                tools[event.block_index].get("arguments", "")
            ) + str(event.payload.get("delta", ""))
        elif event.kind is EventKind.USAGE:
            usage = dict(event.payload)
        elif event.kind is EventKind.RESPONSE_COMPLETED:
            finish = {
                "stop": "end_turn",
                "tool_calls": "tool_use",
                "length": "max_tokens",
                "stop_sequence": "stop_sequence",
            }.get(str(event.payload.get("finish_reason")), "end_turn")
    content: list[dict[str, object]] = []
    for index in sorted(set(texts) | set(tools)):
        if index in texts:
            content.append({"type": "text", "text": texts[index]})
        else:
            tool = dict(tools[index])
            raw = tool.pop("arguments", "{}")
            try:
                tool["input"] = json.loads(str(raw) or "{}")
            except json.JSONDecodeError:
                tool["input"] = {}
            content.append(tool)
    return {
        "id": response_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": finish,
        "stop_sequence": None,
        "usage": usage,
    }


class GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], application: GatewayApplication) -> None:
        super().__init__(address, _GatewayHandler)
        self.application = application

    def graceful_shutdown(self) -> None:
        self.application.begin_shutdown()
        self.shutdown()


def _loopback_host(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(f"//{value}")
        hostname = parsed.hostname
        if hostname is None:
            return False
        if hostname.casefold() == "localhost":
            return True
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MultiRelayGateway/1"

    @property
    def app(self) -> GatewayApplication:
        return self.server.application  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, status: int, payload: object) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def _send_error_payload(self, error: BaseException) -> None:
        if isinstance(error, GatewayError):
            status, code, message, details = error.status, error.code, str(error), error.details
        elif isinstance(error, ManagerError):
            code, message, details = error.code, str(error), error.details
            status = 400 if code not in {"no_eligible_target"} else 503
        else:
            status, code, message, details = 500, "gateway_error", "The gateway request failed.", {}
        self._send_json(
            status,
            {"error": {"type": code, "code": code, "message": message, "details": details}},
        )

    def _validate_transport(self) -> None:
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                raise GatewayError("loopback_required", "Only loopback clients are accepted.")
        except ValueError:
            raise GatewayError("loopback_required", "Only loopback clients are accepted.") from None
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.scheme or parsed.netloc or self.path.startswith("//"):
            raise GatewayError("proxy_request_rejected", "Absolute request URIs are rejected.")
        host = self.headers.get("Host", "")
        if not _loopback_host(host):
            raise GatewayError("invalid_host", "The Host header must name a loopback address.")
        total = sum(len(key) + len(value) + 4 for key, value in self.headers.items())
        if total > MAX_HEADER_BYTES:
            raise GatewayError("headers_too_large", "Request headers are too large.", status=431)

    def _body(self) -> object:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if content_type != "application/json":
            raise GatewayError("unsupported_content_type", "Content-Type must be application/json.", status=415)
        if self.headers.get("Transfer-Encoding"):
            raise GatewayError("invalid_length", "Chunked request bodies are unsupported.", status=411)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise GatewayError("invalid_length", "Content-Length is invalid.") from None
        if length < 1:
            raise GatewayError("invalid_length", "A JSON request body is required.")
        if length > MAX_REQUEST_BYTES:
            raise GatewayError("request_too_large", "Request body is too large.", status=413)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GatewayError("invalid_json", "Request body contains invalid JSON.") from None
        if not isinstance(payload, dict):
            raise GatewayError("invalid_json", "Request body must be a JSON object.")
        return payload

    def do_GET(self) -> None:
        try:
            self._validate_transport()
            path = urllib.parse.urlsplit(self.path).path
            if path == "/health":
                state = self.app.rotation.store.load(self.app.rotation.catalog_hash)
                self._send_json(
                    200,
                    {
                        "service": GATEWAY_SERVICE,
                        "version": GATEWAY_VERSION,
                        "pid": os.getpid(),
                        "port": self.server.server_address[1],
                        "nonce": self.app.nonce,
                        "catalog_hash": self.app.catalog_hash,
                        "generation": state.generation,
                        "protocols": ["responses", "messages"],
                    },
                )
                return
            self.app.authenticate_request(self.headers)
            if path == "/v1/models":
                self._send_json(200, self.app.model_listing())
                return
            if path == "/_multi-relay/pools":
                state = self.app.rotation.store.load(self.app.rotation.catalog_hash)
                self._send_json(
                    200,
                    {
                        "pools": [
                            {
                                "id": pool.id,
                                "active_target_id": (
                                    state.pools.get(pool.id).active_target_id
                                    if state.pools.get(pool.id)
                                    else None
                                ),
                            }
                            for pool in self.app.catalog.pools
                        ]
                    },
                )
                return
            raise GatewayError("not_found", "Route not found.", status=404)
        except BaseException as error:
            self._send_error_payload(error)

    def do_POST(self) -> None:
        try:
            self._validate_transport()
            path = urllib.parse.urlsplit(self.path).path
            if path == "/_shutdown":
                self.app.authenticate_shutdown(self.headers)
                self._body()
                self._send_json(200, {"status": "stopping"})
                threading.Thread(
                    target=self.server.graceful_shutdown,  # type: ignore[attr-defined]
                    daemon=True,
                ).start()
                return
            forced_provider = None
            if path == "/v1/responses":
                surface = "responses"
            elif path == "/v1/messages":
                surface = "messages"
            else:
                match = re.fullmatch(
                    r"/(?:v1/)?providers/(?P<provider>[a-z0-9][a-z0-9_-]*)/responses",
                    path,
                )
                if match:
                    surface = "responses"
                    forced_provider = match.group("provider")
                else:
                    raise GatewayError("not_found", "Route not found.", status=404)
            self.app.authenticate_request(self.headers)
            payload = self._body()
            token = CancellationToken()
            request_id = f"req-{secrets.token_hex(12)}"
            execution = self.app.prepare_execution(
                surface,
                payload,
                headers=self.headers,
                request_id=request_id,
                forced_provider_id=forced_provider,
                cancellation=token,
            )
            with self.app.request_scope(token):
                if bool(payload.get("stream", surface == "responses")):
                    self._stream(surface, execution)
                else:
                    events = list(execution)
                    result = (
                        _responses_result(events, str(payload.get("model")))
                        if surface == "responses"
                        else _anthropic_result(events, str(payload.get("model")))
                    )
                    self._send_json(200, result)
        except (BrokenPipeError, ConnectionResetError):
            try:
                token.cancel()  # type: ignore[possibly-undefined]
            except UnboundLocalError:
                pass
        except BaseException as error:
            self._send_error_payload(error)

    def _stream(self, surface: str, execution: GatewayExecution) -> None:
        iterator = iter(execution)
        try:
            first = next(iterator)
        except StopIteration:
            raise GatewayError("protocol_error", "Gateway produced no response.", status=502) from None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        responses = ResponsesAdapter()
        anthropic = AnthropicOutboundRenderer(model="multi-relay")

        def write(event: CanonicalEvent) -> None:
            payloads = (
                (responses.render_event(event),)
                if surface == "responses"
                else anthropic.render(event)
            )
            for payload in payloads:
                kind = str(payload.get("type", "message"))
                self.wfile.write(f"event: {kind}\n".encode("utf-8"))
                self.wfile.write(b"data: " + _json_bytes(payload) + b"\n\n")
                self.wfile.flush()

        write(first)
        for event in iterator:
            write(event)


@dataclass(frozen=True)
class GatewayProcessState:
    pid: int
    port: int
    catalog_hash: str
    generation: int
    token_target: str
    nonce: str

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "port": self.port,
            "catalog_hash": self.catalog_hash,
            "generation": self.generation,
            "token_target": self.token_target,
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(cls, value: object) -> "GatewayProcessState":
        if not isinstance(value, Mapping) or set(value) != {
            "pid", "port", "catalog_hash", "generation", "token_target", "nonce"
        }:
            raise GatewayError("gateway_state_invalid", "Gateway state is invalid.", status=500)
        return cls(
            pid=int(value["pid"]),
            port=int(value["port"]),
            catalog_hash=str(value["catalog_hash"]),
            generation=int(value["generation"]),
            token_target=str(value["token_target"]),
            nonce=str(value["nonce"]),
        )


def load_gateway_state(path: Path) -> GatewayProcessState | None:
    try:
        return GatewayProcessState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


class GatewayController:
    """Start and validate one detached gateway without placing tokens in argv."""

    def __init__(
        self,
        *,
        codex_home: Path | None = None,
        catalog_path: Path | None = None,
        state_path: Path | None = None,
        host: str = GATEWAY_HOST,
        port: int = GATEWAY_PORT,
        token_store: Any | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        paths = resolve_paths(str(codex_home) if codex_home else None)
        if catalog_path is not None:
            self.catalog_path = catalog_path
        else:
            candidates = (
                paths.catalog,
                paths.relay_state_dir / "catalog.json",
                paths.legacy_state_dir / "catalog.json",
            )
            self.catalog_path = next(
                (candidate for candidate in candidates if candidate.is_file()),
                paths.catalog,
            )
        self.state_path = state_path or paths.gateway_state
        self.host = host
        self.port = port
        self.token_store = token_store or local_gateway_credential_store()
        self.popen = popen
        self._shutdown_token: str | None = None

    def _health(self, timeout: float = 0.5) -> dict[str, object] | None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(f"http://{self.host}:{self.port}/health", timeout=timeout) as response:
                value = json.loads(response.read(8192).decode("utf-8"))
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _matches(health: Mapping[str, object], state: GatewayProcessState) -> bool:
        return (
            health.get("service") == GATEWAY_SERVICE
            and health.get("version") == GATEWAY_VERSION
            and health.get("pid") == state.pid
            and health.get("port") == state.port
            and health.get("nonce") == state.nonce
            and health.get("catalog_hash") == state.catalog_hash
        )

    def ensure(self, timeout: float = 5.0) -> GatewayProcessState:
        health = self._health()
        state = load_gateway_state(self.state_path)
        if health is not None:
            if state is not None and self._matches(health, state):
                return state
            raise GatewayError("gateway_port_conflict", "Gateway port is occupied by a stale or foreign process.", status=500)
        catalog = load_catalog(self.catalog_path)
        request_token = secrets.token_urlsafe(32)
        shutdown_token = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(18)
        self.token_store.store(request_token)
        self._shutdown_token = shutdown_token
        env = os.environ.copy()
        env.update(
            {
                "MULTI_RELAY_REQUEST_TOKEN": request_token,
                "MULTI_RELAY_SHUTDOWN_TOKEN": shutdown_token,
                "MULTI_RELAY_GATEWAY_NONCE": nonce,
            }
        )
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--serve",
            "--catalog",
            str(self.catalog_path),
            "--state",
            str(self.state_path),
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        options: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "env": env,
        }
        if os.name == "nt":
            options["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            )
        else:
            options["start_new_session"] = True
        self.popen(command, **options)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            health = self._health()
            state = load_gateway_state(self.state_path)
            if health is not None and state is not None and self._matches(health, state):
                return state
            time.sleep(0.05)
        raise GatewayError("gateway_start_failed", "Gateway did not become ready.", status=500)

    def stop(self, timeout: float = 5.0) -> bool:
        if self._shutdown_token is None:
            return False
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/_shutdown",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Multi-Relay-Shutdown-Token": self._shutdown_token,
            },
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status == 200
        except Exception:
            return False


def _default_credential_reader(reference: CredentialRef, protocol: str) -> str:
    return read_credential_for_execution(reference, protocol=protocol)


def _credential_available(catalog: Catalog, reference: CredentialRef) -> bool:
    try:
        protocol = catalog.provider(reference.provider_id).protocol
        return credential_store(
            provider_id=reference.provider_id,
            credential_id=reference.id,
            protocol=protocol,
            vault_target=reference.vault_target,
        ).exists()
    except Exception:
        return False


def serve_gateway(
    catalog: Catalog,
    *,
    state_path: Path,
    host: str = GATEWAY_HOST,
    port: int = GATEWAY_PORT,
) -> None:
    request_token = os.environ.get("MULTI_RELAY_REQUEST_TOKEN", "")
    shutdown_token = os.environ.get("MULTI_RELAY_SHUTDOWN_TOKEN", "")
    nonce = os.environ.get("MULTI_RELAY_GATEWAY_NONCE", "")
    if not request_token or not shutdown_token or not nonce:
        raise GatewayError("gateway_environment_invalid", "Gateway startup tokens are missing.", status=500)
    paths = resolve_paths()
    runtime_store = RuntimeStateStore(paths.runtime_state, lock_path=paths.runtime_state_lock)
    rotation = RotationController(
        catalog,
        runtime_store,
        credential_available=lambda reference: _credential_available(
            catalog, reference
        ),
    )
    app = GatewayApplication(
        catalog,
        rotation=rotation,
        credential_reader=_default_credential_reader,
        attempt_executor=HttpAttemptExecutor(catalog),
        request_token=request_token,
        shutdown_token=shutdown_token,
        token_expires_at=time.monotonic() + TOKEN_LIFETIME_SECONDS,
        nonce=nonce,
    )
    server = GatewayHTTPServer((host, port), app)
    state = runtime_store.load(rotation.catalog_hash)
    process_state = GatewayProcessState(
        pid=os.getpid(),
        port=server.server_address[1],
        catalog_hash=rotation.catalog_hash,
        generation=state.generation,
        token_target=VaultLocator("local-gateway", "session").target,
        nonce=nonce,
    )
    atomic_write(
        state_path,
        (json.dumps(process_state.to_dict(), sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        current = load_gateway_state(state_path)
        if current is not None and current.nonce == nonce:
            try:
                state_path.unlink()
            except FileNotFoundError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--host", default=GATEWAY_HOST)
    parser.add_argument("--port", type=int, default=GATEWAY_PORT)
    args = parser.parse_args(argv)
    if not args.serve or args.catalog is None or args.state is None:
        parser.error("--serve, --catalog, and --state are required")
    try:
        serve_gateway(
            load_catalog(args.catalog),
            state_path=args.state,
            host=args.host,
            port=args.port,
        )
    except (GatewayError, ManagerError, OSError):
        return 2
    return 0


__all__ = [
    "AttemptResponse",
    "CancellationToken",
    "GatewayApplication",
    "GATEWAY_BASE_URL",
    "GatewayCancelled",
    "GatewayController",
    "GatewayError",
    "GatewayExecution",
    "GatewayExhausted",
    "GatewayHTTPServer",
    "GatewayProcessState",
    "HttpAttemptExecutor",
    "MAX_REQUEST_BYTES",
    "load_gateway_state",
    "serve_gateway",
]


if __name__ == "__main__":
    raise SystemExit(main())
