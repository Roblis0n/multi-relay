#!/usr/bin/env python3
"""Reject removed runtime configuration behavior outside migration diagnostics."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "scripts" / "deepseek_fanout"
FORBIDDEN = (
    "multi_agent_version",
    "multi_agent_v2 = false",
    "hide_spawn_agent_metadata = true",
    "model_catalog_json",
    "deepseek-v4-flash",
    "DeepSeek.toml",
)
ALLOWED = {
    "migration.py": set(FORBIDDEN),
    "compatibility.py": {"model_catalog_json"},
}


def main() -> int:
    violations: list[str] = []
    for path in sorted(PACKAGE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        allowed = ALLOWED.get(path.name, set())
        for phrase in FORBIDDEN:
            if phrase in text and phrase not in allowed:
                violations.append(f"{path.relative_to(ROOT)}: {phrase}")
    if violations:
        print("Removed runtime behavior found:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("runtime contract: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
