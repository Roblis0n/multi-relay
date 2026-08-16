#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay import catalog as catalog_module  # noqa: E402
from multi_relay.catalog import (  # noqa: E402
    CATALOG_SCHEMA_VERSION,
    Catalog,
    ProviderSpec,
    save_catalog_bytes,
)


class _MissingDomainType:
    pass


AgentProfile = getattr(catalog_module, "AgentProfile", _MissingDomainType)
CredentialRef = getattr(catalog_module, "CredentialRef", _MissingDomainType)
ExecutionTarget = getattr(catalog_module, "ExecutionTarget", _MissingDomainType)
HostConfig = getattr(catalog_module, "HostConfig", _MissingDomainType)
TargetPool = getattr(catalog_module, "TargetPool", _MissingDomainType)


def schema2_catalog() -> dict[str, object]:
    return {
        "schema_version": 2,
        "concurrency": 8,
        "providers": [
            {
                "id": "vendor",
                "name": "Vendor",
                "protocol": "responses-compatible",
                "base_url": "https://api.example.test/v1",
                "auth_mode": "vault",
                "capabilities": ["text", "vision", "tool_calling"],
                "models_endpoint": "/models",
                "enabled": True,
            }
        ],
        "credentials": [
            {
                "id": "primary",
                "provider_id": "vendor",
                "vault_target": "multi-relay/vendor/primary",
                "enabled": True,
                "created_at": "2026-08-16T00:00:00Z",
                "label": "Primary",
            }
        ],
        "targets": [
            {
                "id": "vendor-primary",
                "provider_id": "vendor",
                "protocol": None,
                "model": "vendor-model",
                "credential_id": "primary",
                "capabilities": ["text", "tool_calling"],
                "context_window": 128000,
                "max_output_tokens": 16000,
                "reasoning_efforts": ["high", "max"],
                "trust": "standard",
                "host_compatibility": ["codex", "claude-code"],
                "enabled": True,
                "metadata": {"region": "test"},
            }
        ],
        "pools": [
            {
                "id": "general",
                "targets": ["vendor-primary"],
                "strategy": "sticky",
                "duration_seconds": None,
                "max_rate_limit_wait_seconds": 30,
                "cooldown": {
                    "quota_seconds": 86400,
                    "rate_limit_seconds": 60,
                    "auth_seconds": 3600,
                    "provider_seconds": 30,
                },
                "required_capabilities": ["text"],
                "host_compatibility": ["codex", "claude-code"],
                "enabled": True,
            }
        ],
        "agents": [
            {
                "name": "worker",
                "description": "Bounded implementation worker.",
                "developer_instructions": "Implement the assigned bounded task.",
                "pool_id": "general",
                "required_capabilities": ["text", "tool_calling"],
                "fallback_pool_id": None,
                "reasoning_effort": "high",
                "context_window": 64000,
                "trust": "standard",
                "priority": 20,
                "sandbox_mode": "workspace-write",
                "tools": ["shell", "apply_patch"],
                "mcp_servers": {},
                "skills": [],
                "hosts": ["codex", "claude-code"],
            }
        ],
        "hosts": {
            "codex": {"enabled": True, "scope": None, "default_pool": "general"},
            "claude-code": {
                "enabled": False,
                "scope": "user",
                "default_pool": "general",
            },
        },
    }


class CatalogSchema2Tests(unittest.TestCase):
    def test_minimal_schema2_catalog_builds_domain_types(self) -> None:
        self.assertEqual(CATALOG_SCHEMA_VERSION, 2)
        parsed = Catalog.from_dict(schema2_catalog())
        self.assertIsInstance(parsed.providers[0], ProviderSpec)
        self.assertIsInstance(parsed.credentials[0], CredentialRef)
        self.assertIsInstance(parsed.targets[0], ExecutionTarget)
        self.assertIsInstance(parsed.pools[0], TargetPool)
        self.assertIsInstance(parsed.agents[0], AgentProfile)
        self.assertIsInstance(parsed.hosts["codex"], HostConfig)

    def test_unknown_fields_are_rejected_at_every_schema_layer(self) -> None:
        paths = (
            ("catalog", lambda value: value.__setitem__("api_key", "not-a-secret")),
            ("provider", lambda value: value["providers"][0].__setitem__("extra", True)),
            ("credential", lambda value: value["credentials"][0].__setitem__("extra", True)),
            ("target", lambda value: value["targets"][0].__setitem__("extra", True)),
            ("pool", lambda value: value["pools"][0].__setitem__("extra", True)),
            ("agent", lambda value: value["agents"][0].__setitem__("extra", True)),
            ("host", lambda value: value["hosts"]["codex"].__setitem__("extra", True)),
        )
        for label, mutate in paths:
            with self.subTest(layer=label):
                payload = schema2_catalog()
                mutate(payload)
                with self.assertRaises(ManagerError):
                    Catalog.from_dict(payload)

    def test_duplicate_ids_and_references_are_rejected(self) -> None:
        mutations = []

        def duplicate_provider(value: dict[str, object]) -> None:
            value["providers"].append(copy.deepcopy(value["providers"][0]))

        def duplicate_target_in_pool(value: dict[str, object]) -> None:
            value["pools"][0]["targets"].append("vendor-primary")

        mutations.extend((duplicate_provider, duplicate_target_in_pool))
        for mutate in mutations:
            payload = schema2_catalog()
            mutate(payload)
            with self.assertRaises(ManagerError):
                Catalog.from_dict(payload)

    def test_credential_ids_are_scoped_to_their_provider(self) -> None:
        payload = schema2_catalog()
        second_provider = copy.deepcopy(payload["providers"][0])
        second_provider.update(
            {
                "id": "vendor-b",
                "name": "Vendor B",
                "base_url": "https://api-b.example.test/v1",
            }
        )
        second_credential = copy.deepcopy(payload["credentials"][0])
        second_credential.update(
            {
                "provider_id": "vendor-b",
                "vault_target": "multi-relay/vendor-b/primary",
            }
        )
        second_target = copy.deepcopy(payload["targets"][0])
        second_target.update(
            {
                "id": "vendor-b-primary",
                "provider_id": "vendor-b",
            }
        )
        payload["providers"].append(second_provider)
        payload["credentials"].append(second_credential)
        payload["targets"].append(second_target)

        parsed = Catalog.from_dict(payload)

        self.assertEqual(
            parsed.credential("primary", provider_id="vendor-b").provider_id,
            "vendor-b",
        )
        payload["credentials"].append(copy.deepcopy(second_credential))
        with self.assertRaises(ManagerError):
            Catalog.from_dict(payload)

    def test_dangling_references_are_rejected(self) -> None:
        changes = (
            ("credentials", "provider_id", "missing"),
            ("targets", "provider_id", "missing"),
            ("targets", "credential_id", "missing"),
            ("pools", "targets", ["missing"]),
            ("agents", "pool_id", "missing"),
            ("agents", "fallback_pool_id", "missing"),
            ("hosts", "default_pool", "missing"),
        )
        for collection, field, replacement in changes:
            with self.subTest(collection=collection, field=field):
                payload = schema2_catalog()
                if collection == "hosts":
                    payload["hosts"]["codex"][field] = replacement
                else:
                    payload[collection][0][field] = replacement
                with self.assertRaises(ManagerError):
                    Catalog.from_dict(payload)

    def test_strategy_duration_contract_is_strict(self) -> None:
        payload = schema2_catalog()
        payload["pools"][0]["duration_seconds"] = 60
        with self.assertRaises(ManagerError):
            Catalog.from_dict(payload)

        payload = schema2_catalog()
        payload["pools"][0]["strategy"] = "timed"
        payload["pools"][0]["duration_seconds"] = None
        with self.assertRaises(ManagerError):
            Catalog.from_dict(payload)

        payload["pools"][0]["duration_seconds"] = 60
        self.assertEqual(Catalog.from_dict(payload).pools[0].duration_seconds, 60)

        for invalid_duration in (0, -1):
            with self.subTest(duration=invalid_duration):
                payload = schema2_catalog()
                payload["pools"][0]["strategy"] = "timed"
                payload["pools"][0]["duration_seconds"] = invalid_duration
                with self.assertRaises(ManagerError):
                    Catalog.from_dict(payload)

    def test_protocol_capability_host_and_strategy_enums_are_strict(self) -> None:
        cases = (
            ("protocol", lambda value: value["providers"][0].__setitem__("protocol", "mystery")),
            (
                "capability",
                lambda value: value["targets"][0].__setitem__("capabilities", ["telepathy"]),
            ),
            (
                "host",
                lambda value: value["targets"][0].__setitem__("host_compatibility", ["other"]),
            ),
            (
                "protocol override",
                lambda value: value["targets"][0].__setitem__(
                    "protocol",
                    "anthropic-messages",
                ),
            ),
            ("strategy", lambda value: value["pools"][0].__setitem__("strategy", "random")),
        )
        for label, mutate in cases:
            with self.subTest(enum=label):
                payload = schema2_catalog()
                mutate(payload)
                with self.assertRaises(ManagerError):
                    Catalog.from_dict(payload)

    def test_secret_looking_metadata_values_are_rejected(self) -> None:
        payload = schema2_catalog()
        payload["targets"][0]["metadata"] = {"note": "Bearer sensitive-value"}

        with self.assertRaises(ManagerError) as raised:
            Catalog.from_dict(payload)

        self.assertEqual(raised.exception.code, "secret_not_allowed")
        self.assertNotIn("sensitive-value", str(raised.exception))

        for field_name in ("accessToken", "clientSecret", "authToken"):
            with self.subTest(field_name=field_name):
                payload = schema2_catalog()
                payload["targets"][0]["metadata"] = {
                    field_name: "sensitive-value"
                }
                with self.assertRaises(ManagerError) as field_raised:
                    Catalog.from_dict(payload)
                self.assertEqual(
                    field_raised.exception.code,
                    "secret_not_allowed",
                )

    def test_serialization_is_stable_secret_free_and_round_trips(self) -> None:
        parsed = Catalog.from_dict(schema2_catalog())

        first = save_catalog_bytes(parsed)
        second = save_catalog_bytes(Catalog.from_dict(json.loads(first)))

        self.assertEqual(first, second)
        self.assertNotIn(b"authorization", first.lower())
        self.assertNotIn(b"api_key", first.lower())
        self.assertEqual(
            json.loads(first)["targets"][0]["capabilities"],
            ["text", "tool_calling"],
        )

    def test_validated_nested_state_is_immutable(self) -> None:
        parsed = Catalog.from_dict(schema2_catalog())

        with self.assertRaises(TypeError):
            parsed.targets[0].metadata["region"] = "changed"
        with self.assertRaises(TypeError):
            parsed.agents[0].mcp_servers["other"] = {}
        with self.assertRaises(TypeError):
            parsed.hosts["codex"] = parsed.hosts["claude-code"]


if __name__ == "__main__":
    unittest.main()
