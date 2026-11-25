"""Shared digest helpers (one implementation for the whole package)."""

from __future__ import annotations

import hashlib


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = ["sha256_hex"]
