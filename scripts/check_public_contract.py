#!/usr/bin/env python3
"""Offline checks for the public, host-neutral Multi Relay contract."""

from __future__ import annotations

import argparse
import ast
import compileall
import json
import re
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PACKAGE = SCRIPTS / "multi_relay"
sys.path.insert(0, str(SCRIPTS))

from multi_relay.catalog import default_catalog  # noqa: E402
from multi_relay.cli import build_parser  # noqa: E402
from multi_relay.manager import RelayManager  # noqa: E402
from multi_relay.paths import resolve_paths  # noqa: E402


SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_token",
    "secret",
    "password",
    "credential_value",
}
CORE_FILES = {
    "canonical.py",
    "catalog.py",
    "failure.py",
    "gateway.py",
    "rotation.py",
    "selection.py",
    "state.py",
}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def _runtime_import_contract(violations: list[str]) -> None:
    for path in sorted(PACKAGE.rglob("*.py")):
        imports = _imports(_tree(path))
        for name in imports:
            if any(part.startswith("test_") or part == "tests" for part in name.split(".")):
                violations.append(f"runtime imports test module: {path.relative_to(ROOT)} -> {name}")
    core = [PACKAGE / name for name in CORE_FILES]
    core.extend((PACKAGE / "protocols").glob("*.py"))
    for path in sorted(core):
        for name in _imports(_tree(path)):
            if name == "hosts" or name.startswith("hosts.") or ".hosts." in name:
                violations.append(f"neutral core imports host adapter: {path.relative_to(ROOT)} -> {name}")


def _host_secret_contract(violations: list[str]) -> None:
    for path in sorted((PACKAGE / "hosts").glob("*.py")):
        tree = _tree(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module == "credentials" or module.endswith(".credentials"):
                blocked = [alias.name for alias in node.names if alias.name != "gateway_auth_command"]
                if blocked:
                    violations.append(
                        f"host imports credential reader: {path.relative_to(ROOT)} -> {', '.join(blocked)}"
                    )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "get" or not node.args:
                continue
            owner = node.func.value
            is_environment = (
                isinstance(owner, ast.Attribute)
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "os"
                and owner.attr == "environ"
            )
            key = node.args[0].value if isinstance(node.args[0], ast.Constant) else None
            if is_environment and isinstance(key, str) and (
                "KEY" in key.upper() or "TOKEN" in key.upper() or "SECRET" in key.upper()
            ):
                violations.append(
                    f"host reads upstream secret environment: {path.relative_to(ROOT)} -> {key}"
                )


def _secret_like_keys(value: object, *, location: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            child_location = f"{location}.{key}"
            if normalized in SECRET_KEYS:
                found.append(child_location)
            found.extend(_secret_like_keys(child, location=child_location))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_secret_like_keys(child, location=f"{location}[{index}]"))
    return found


def _json_contract(violations: list[str]) -> None:
    catalog = default_catalog()
    catalog_payload = catalog.to_dict()
    catalog_bytes = json.dumps(catalog_payload, sort_keys=True).encode("utf-8")
    with tempfile.TemporaryDirectory(prefix="multi-relay-contract-") as directory:
        root = Path(directory)
        paths = resolve_paths(
            str(root / "codex"),
            state_home=root / "state",
            platform="linux",
            user_home=root / "user",
        )
        manager = RelayManager(paths, "fixture-codex")
        manifest = manager._manifest_payload(
            catalog,
            catalog_bytes,
            {},
            {},
            {},
            previous={},
            selection=None,
            instruction_file_preexisted=False,
            config_preexisted=False,
        )
    for label, payload in (("catalog", catalog_payload), ("manifest", manifest)):
        for location in _secret_like_keys(payload):
            violations.append(f"{label} contains secret-like key: {location}")


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(
        (
            item
            for item in parser._actions
            if isinstance(item, argparse._SubParsersAction)
        ),
        None,
    )
    return {} if action is None else dict(action.choices)


def _cli_contract(violations: list[str]) -> None:
    readmes = [ROOT / "README.md", ROOT / "README_EN.md"]
    blocks: list[str] = []
    for path in readmes:
        text = path.read_text(encoding="utf-8")
        match = re.search(r"## CLI[^\n]*\n.*?```text\n(.*?)```", text, flags=re.DOTALL)
        if match is None:
            violations.append(f"missing CLI index: {path.name}")
            continue
        blocks.append(match.group(1).strip())
    if len(blocks) == 2 and blocks[0] != blocks[1]:
        violations.append("README CLI indexes differ")
    if not blocks:
        return
    index = blocks[0]
    commands = _subcommands(build_parser())
    for name, parser in commands.items():
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", index) is None:
            violations.append(f"CLI command missing from docs: {name}")
        children = _subcommands(parser)
        for child in children:
            phrase = rf"(?<![\w-]){re.escape(name)}(?:\s+|[^\n]*\|){re.escape(child)}(?![\w-])"
            if re.search(phrase, index) is None:
                violations.append(f"CLI subcommand missing from docs: {name} {child}")


def _compatibility_contract(violations: list[str]) -> None:
    required_tests = {
        "manager class alias": ("test_rebrand.py", "FanoutManager"),
        "legacy state directories": ("test_rebrand.py", "legacy_state_dirs"),
        "legacy ownership marker": ("test_instructions.py", "CODEX-DEEPSEEK-FANOUT"),
        "legacy vault target": ("test_credentials.py", "codex-deepseek-api-key"),
        "legacy bridge service": ("test_bridge.py", "LEGACY_BRIDGE_SERVICE"),
        "legacy gateway shutdown": ("test_gateway.py", '"/_shutdown"'),
    }
    for label, (filename, marker) in required_tests.items():
        text = (SCRIPTS / filename).read_text(encoding="utf-8")
        if marker not in text:
            violations.append(f"compatibility shim lacks explicit test: {label}")


def main() -> int:
    violations: list[str] = []
    _runtime_import_contract(violations)
    _host_secret_contract(violations)
    _json_contract(violations)
    _cli_contract(violations)
    _compatibility_contract(violations)
    if not compileall.compile_dir(str(SCRIPTS), quiet=1):
        violations.append("scripts package does not compile")
    if violations:
        print("Public contract violations:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("public contract: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
