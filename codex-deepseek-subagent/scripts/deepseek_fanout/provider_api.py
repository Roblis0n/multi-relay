"""Minimal, redacted DeepSeek model discovery."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .errors import ManagerError


REQUESTED_MODEL = "deepseek-v4-pro"
MODELS_URL = "https://api.deepseek.com/models"
MAX_RESPONSE_BYTES = 1_048_576


def discover_model(
    api_key: str,
    requested: str = REQUESTED_MODEL,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> str:
    """Verify that the requested model is present in the authenticated catalog."""

    if not api_key.startswith("sk-") or any(character in api_key for character in "\r\n\0"):
        raise ManagerError("invalid_api_key", "Enter a valid DeepSeek API Key.")
    request = urllib.request.Request(
        MODELS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "codex-deepseek-fanout/1",
        },
        method="GET",
    )
    try:
        with opener(request, timeout=15) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ManagerError(
                "authentication_failed",
                "DeepSeek rejected the credential stored on this computer.",
            ) from None
        raise ManagerError(
            "provider_unavailable",
            "DeepSeek model discovery returned an HTTP error.",
            {"http_status": exc.code},
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ManagerError(
            "provider_unavailable",
            "DeepSeek model discovery could not be completed.",
        ) from None

    if len(body) > MAX_RESPONSE_BYTES:
        raise ManagerError(
            "malformed_provider_response",
            "DeepSeek returned an unexpectedly large model catalog.",
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ManagerError(
            "malformed_provider_response",
            "DeepSeek returned an invalid model catalog.",
        ) from None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ManagerError(
            "malformed_provider_response",
            "DeepSeek returned an invalid model catalog.",
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
            "DeepSeek returned ambiguous identifiers for the requested model.",
            {"requested_model": requested},
        )
    if len(folded) == 1:
        return folded[0]
    raise ManagerError(
        "model_unavailable",
        f"DeepSeek does not currently expose the requested model {requested}.",
        {"requested_model": requested, "available_models": sorted(model_ids)},
    )
