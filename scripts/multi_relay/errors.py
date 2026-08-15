"""Domain errors returned by Codex Multi Relay."""

from __future__ import annotations

from typing import Any


class ManagerError(RuntimeError):
    """A safe, structured manager failure."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}
