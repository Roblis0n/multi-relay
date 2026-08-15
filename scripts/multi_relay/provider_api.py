"""Minimal, redacted, provider-scoped model discovery."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from .catalog import ProviderSpec
from .errors import ManagerError


REQUESTED_MODEL = "deepseek-v4-pro"
MODELS_URL = "https://api.deepseek.com/models"
MAX_RESPONSE_BYTES = 1_048_576


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow ordinary redirects while refusing credential-bearing origin changes."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            old = urllib.parse.urlsplit(req.full_url)
            new = urllib.parse.urlsplit(newurl)

            def origin(parts: urllib.parse.SplitResult) -> tuple[str, str | None, int | None]:
                scheme = parts.scheme.casefold()
                port = parts.port
                if port is None:
                    port = 443 if scheme == "https" else (80 if scheme == "http" else None)
                host = parts.hostname.casefold() if parts.hostname else None
                return scheme, host, port

            old_origin = origin(old)
            new_origin = origin(new)
        except ValueError:
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "Malformed provider redirect blocked",
                headers,
                fp,
            ) from None
        if old_origin != new_origin:
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "Cross-origin provider redirect blocked",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_urlopen(request: urllib.request.Request, *, timeout: float) -> Any:
    opener = urllib.request.build_opener(_SameOriginRedirectHandler())
    return opener.open(request, timeout=timeout)


def discover_model(
    api_key: str,
    requested: str = REQUESTED_MODEL,
    *,
    provider: ProviderSpec | None = None,
    opener: Callable[..., Any] | None = None,
) -> str:
    """Verify that the requested model is present in the authenticated catalog."""

    provider_name = provider.name if provider is not None else "DeepSeek"
    protocol = provider.protocol if provider is not None else "deepseek-chat"
    auth_mode = provider.auth if provider is not None else "vault"
    if not isinstance(requested, str) or not requested.strip() or requested != requested.strip():
        raise ManagerError("invalid_model", "A provider model identifier is required.")
    if not isinstance(api_key, str) or any(character in api_key for character in "\r\n\0"):
        raise ManagerError("invalid_api_key", "Enter a valid provider credential.")
    if auth_mode == "vault" and not api_key:
        raise ManagerError("invalid_api_key", "Enter a valid provider credential.")
    if auth_mode == "vault" and protocol == "deepseek-chat" and not api_key.startswith("sk-"):
        raise ManagerError("invalid_api_key", "Enter a valid DeepSeek API Key.")
    if provider is not None and provider.protocol == "codex-native":
        raise ManagerError(
            "provider_unavailable",
            "Native Codex providers use the Codex model catalog directly.",
            {"provider": provider.id},
        )
    models_url = (
        f"{provider.base_url.rstrip('/')}/models"
        if provider is not None and provider.base_url is not None
        else MODELS_URL
    )
    headers = {
        "Accept": "application/json",
        "User-Agent": "codex-multi-relay/1",
    }
    if auth_mode == "vault":
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        models_url,
        headers=headers,
        method="GET",
    )
    try:
        selected_opener = opener or _safe_urlopen
        with selected_opener(request, timeout=15) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        if status in {401, 403}:
            raise ManagerError(
                "authentication_failed",
                f"{provider_name} rejected the credential stored on this computer.",
            ) from None
        raise ManagerError(
            "provider_unavailable",
            f"{provider_name} model discovery returned an HTTP error.",
            {"http_status": status},
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ManagerError(
            "provider_unavailable",
            f"{provider_name} model discovery could not be completed.",
        ) from None

    if len(body) > MAX_RESPONSE_BYTES:
        raise ManagerError(
            "malformed_provider_response",
            f"{provider_name} returned an unexpectedly large model catalog.",
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ManagerError(
            "malformed_provider_response",
            f"{provider_name} returned an invalid model catalog.",
        ) from None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ManagerError(
            "malformed_provider_response",
            f"{provider_name} returned an invalid model catalog.",
        )
    model_ids: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str):
            model_ids.append(model_id)
    exact = [model_id for model_id in model_ids if model_id == requested]
    if len(exact) == 1:
        return exact[0]
    folded = [model_id for model_id in model_ids if model_id.casefold() == requested.casefold()]
    if len(exact) > 1 or len(folded) > 1:
        raise ManagerError(
            "model_ambiguous",
            f"{provider_name} returned ambiguous identifiers for the requested model.",
            {"requested_model": requested},
        )
    if len(folded) == 1:
        return folded[0]
    raise ManagerError(
        "model_unavailable",
        f"{provider_name} does not currently expose the requested model {requested}.",
        {"requested_model": requested, "available_models": sorted(model_ids)},
    )
