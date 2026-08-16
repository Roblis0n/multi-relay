#!/usr/bin/env python3
"""Print one protected provider credential for Codex authentication."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from multi_relay.bridge import ensure_bridge
    from multi_relay.credentials import credential_store, local_gateway_credential_store
else:
    from .bridge import ensure_bridge
    from .credentials import credential_store, local_gateway_credential_store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--credential", default="primary")
    parser.add_argument("--protocol")
    parser.add_argument("--vault-target")
    parser.add_argument("--no-start-bridge", action="store_true")
    parser.add_argument("--codex-home")
    parser.add_argument("--gateway", action="store_true")
    try:
        args = parser.parse_args([] if argv is None else argv)
    except SystemExit:
        return 2
    if args.gateway:
        try:
            if args.codex_home:
                ensure_bridge(codex_home=Path(args.codex_home))
            else:
                ensure_bridge()
        except Exception:
            return 4
        try:
            secret = local_gateway_credential_store().read()
        except Exception:
            return 2
        if not secret:
            return 3
    else:
        try:
            secret = credential_store(
                provider_id=args.provider,
                credential_id=args.credential,
                protocol=args.protocol,
                vault_target=args.vault_target,
            ).read()
        except Exception:
            return 2
        if not secret:
            return 3
        if not args.no_start_bridge:
            try:
                if args.codex_home:
                    ensure_bridge(codex_home=Path(args.codex_home))
                else:
                    ensure_bridge()
            except Exception:
                return 4
    sys.stdout.write(secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
