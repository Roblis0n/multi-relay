#!/usr/bin/env python3
"""Reject removed runtime configuration behavior outside migration diagnostics."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "scripts" / "multi_relay"
FORBIDDEN = (
    "multi_agent_version",
    "multi_agent_v2 = false",
    "hide_spawn_agent_metadata = true",
    "model_catalog_json",
    "deepseek-v4-flash",
    "DeepSeek.toml",
    "Codex DeepSeek Relay",
    "deepseek_fanout",
    "DeepSeek fan-out",
    "codex-deepseek-relay",
    "codex-deepseek-subagent",
    "CODEX-DEEPSEEK-FANOUT",
    "CODEX-DEEPSEEK-SUBAGENT",
    "codex-deepseek-responses-bridge",
    "X-Codex-DeepSeek-Bridge-Pid",
    "codex-deepseek-reasoning-v1",
    "codex-deepseek-api-key",
)
ALLOWED = {
    "migration.py": set(FORBIDDEN),
    "compatibility.py": {"model_catalog_json"},
    "paths.py": {"codex-deepseek-relay", "codex-deepseek-subagent"},
    "branding.py": {"codex-deepseek-relay", "codex-deepseek-subagent"},
    "instructions.py": {"CODEX-DEEPSEEK-FANOUT"},
    "toml_config.py": {"CODEX-DEEPSEEK-FANOUT"},
    "credentials.py": {"codex-deepseek-api-key"},
    "bridge.py": {
        "codex-deepseek-relay",
        "codex-deepseek-responses-bridge",
        "X-Codex-DeepSeek-Bridge-Pid",
        "codex-deepseek-reasoning-v1",
    },
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
