#!/usr/bin/env python3
"""Print the protected DeepSeek credential for Codex provider authentication."""

from __future__ import annotations

import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from deepseek_fanout.bridge import ensure_bridge
    from deepseek_fanout.credentials import credential_store
else:
    from .bridge import ensure_bridge
    from .credentials import credential_store


def main() -> int:
    try:
        secret = credential_store().read()
    except Exception:
        return 2
    if not secret:
        return 3
    try:
        ensure_bridge()
    except Exception:
        return 4
    sys.stdout.write(secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
