#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError  # noqa: E402
from multi_relay.catalog import (  # noqa: E402
    AgentSpec,
    Catalog,
    ProviderSpec,
    default_catalog,
    load_catalog,
    route_agent,
    save_catalog_bytes,
)


def provider(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "native",
        "name": "Native Codex",
        "protocol": "codex-native",
        "base_url": None,
        "auth": "codex",
        "capabilities": ["text", "vision", "audio", "tools", "web"],
        "context_window": None,
        "enabled": True,
    }
    value.update(overrides)
    if "protocol" in overrides and "auth" not in overrides:
        value["auth"] = (
            "codex" if value["protocol"] == "codex-native" else "vault"
        )
    return value


def agent(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": "reviewer",
        "description": "Native high-trust reviewer.",
        "provider": "native",
        "model": None,
        "reasoning_effort": None,
        "context_window": None,
        "capabilities": ["text", "tools"],
        "trust": "high",
        "priority": 20,
        "sandbox_mode": "read-only",
        "mcp_servers": {},
        "skills": [],
        "developer_instructions": "Review evidence and return control to the parent.",
    }
    value.update(overrides)
    return value


def catalog_payload(
    providers: list[dict[str, object]] | None = None,
    agents: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "concurrency": 8,
        "providers": providers if providers is not None else [provider()],
        "agents": agents if agents is not None else [agent()],
    }


class CatalogValidationTests(unittest.TestCase):
    def test_all_four_provider_protocols_are_supported(self) -> None:
        providers = [
            provider(id="native", protocol="codex-native", base_url=None),
            provider(
                id="responses",
                protocol="responses-compatible",
                base_url="https://responses.example.test/v1",
                capabilities=["text", "vision", "audio", "tools", "web"],
            ),
            provider(
                id="chat",
                protocol="chat-completions-compatible",
                base_url="https://chat.example.test/v1",
                capabilities=["text", "tools"],
            ),
            provider(
                id="deepseek",
                protocol="deepseek-chat",
                base_url="https://api.deepseek.com/v1",
                capabilities=["text", "tools"],
            ),
        ]
        agents = [agent(name="reviewer", provider="native")]

        parsed = Catalog.from_dict(catalog_payload(providers, agents))

        self.assertEqual(
            {item.protocol for item in parsed.providers},
            {
                "codex-native",
                "responses-compatible",
                "chat-completions-compatible",
                "deepseek-chat",
            },
        )

    def test_unknown_fields_are_rejected_by_strict_schema(self) -> None:
        payload = catalog_payload()
        payload["surprise"] = True

        with self.assertRaises(ManagerError) as raised:
            Catalog.from_dict(payload)

        self.assertEqual(raised.exception.code, "catalog_invalid")

    def test_non_loopback_http_upstream_is_rejected(self) -> None:
        payload = catalog_payload(
            [
                provider(
                    id="chat",
                    protocol="chat-completions-compatible",
                    base_url="http://chat.example.test/v1",
                    capabilities=["text", "tools"],
                )
            ],
            [agent(provider="chat", model="example-model")],
        )

        with self.assertRaises(ManagerError) as raised:
            Catalog.from_dict(payload)

        self.assertEqual(raised.exception.code, "unsafe_provider_url")

    def test_loopback_http_upstream_is_allowed(self) -> None:
        parsed = Catalog.from_dict(
            catalog_payload(
                [
                    provider(
                        id="local",
                        protocol="chat-completions-compatible",
                        base_url="http://127.0.0.1:8080/v1",
                        capabilities=["text", "tools"],
                    )
                ],
                [agent(provider="local", model="local-model")],
            )
        )

        self.assertEqual(parsed.providers[0].id, "local")

    def test_url_credentials_query_and_fragment_are_rejected(self) -> None:
        for url in (
            "https://user:pass@example.test/v1",
            "https://example.test/v1?key=secret",
            "https://example.test/v1#fragment",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ManagerError) as raised:
                    ProviderSpec.from_dict(
                        provider(
                            id="chat",
                            protocol="chat-completions-compatible",
                            base_url=url,
                            capabilities=["text", "tools"],
                        )
                    )
                self.assertEqual(raised.exception.code, "unsafe_provider_url")

    def test_malformed_ipv6_and_port_are_safe_domain_errors(self) -> None:
        for url in ("https://[::1/v1", "https://example.test:not-a-port/v1"):
            with self.subTest(url=url):
                with self.assertRaises(ManagerError) as raised:
                    ProviderSpec.from_dict(
                        provider(
                            id="chat",
                            protocol="chat-completions-compatible",
                            base_url=url,
                            capabilities=["text", "tools"],
                        )
                    )
                self.assertEqual(raised.exception.code, "unsafe_provider_url")

    def test_boolean_schema_version_is_rejected(self) -> None:
        payload = catalog_payload()
        payload["schema_version"] = True

        with self.assertRaises(ManagerError) as raised:
            Catalog.from_dict(payload)

        self.assertEqual(raised.exception.code, "unsupported_catalog_schema")

    def test_duplicate_identifiers_are_case_insensitive(self) -> None:
        payload = catalog_payload(
            [provider(id="native"), provider(id="NATIVE")],
            [agent(provider="native")],
        )

        with self.assertRaises(ManagerError) as raised:
            Catalog.from_dict(payload)

        self.assertEqual(raised.exception.code, "duplicate_provider")

    def test_agent_must_reference_an_existing_provider(self) -> None:
        with self.assertRaises(ManagerError) as raised:
            Catalog.from_dict(catalog_payload(agents=[agent(provider="missing")]))

        self.assertEqual(raised.exception.code, "unknown_provider")

    def test_agent_capabilities_must_be_offered_by_provider(self) -> None:
        payload = catalog_payload(
            [provider(capabilities=["text", "tools"])],
            [agent(capabilities=["text", "tools", "vision"])],
        )

        with self.assertRaises(ManagerError) as raised:
            Catalog.from_dict(payload)

        self.assertEqual(raised.exception.code, "capability_unsupported")

    def test_chat_protocols_cannot_claim_media_capabilities(self) -> None:
        for protocol in ("chat-completions-compatible", "deepseek-chat"):
            with self.subTest(protocol=protocol):
                with self.assertRaises(ManagerError) as raised:
                    ProviderSpec.from_dict(
                        provider(
                            id="chat",
                            protocol=protocol,
                            base_url="https://chat.example.test/v1",
                            capabilities=["text", "tools", "vision"],
                        )
                    )
                self.assertEqual(raised.exception.code, "capability_unsupported")

    def test_provider_auth_modes_match_protocol_boundaries(self) -> None:
        invalid = [
            provider(id="native", protocol="codex-native", base_url=None, auth="vault"),
            provider(
                id="chat",
                protocol="chat-completions-compatible",
                base_url="https://chat.example.test/v1",
                auth="codex",
                capabilities=["text", "tools"],
            ),
            provider(
                id="deepseek",
                protocol="deepseek-chat",
                base_url="https://api.deepseek.com/v1",
                auth="none",
                capabilities=["text", "tools"],
            ),
        ]
        for entry in invalid:
            with self.subTest(entry=entry):
                with self.assertRaises(ManagerError) as raised:
                    ProviderSpec.from_dict(entry)
                self.assertEqual(raised.exception.code, "catalog_invalid")

    def test_web_agent_requires_a_concrete_mcp_server(self) -> None:
        for servers in ({}, {"docs": {}}, {"docs": {"url": ""}}):
            with self.subTest(servers=servers):
                with self.assertRaises(ManagerError) as raised:
                    Catalog.from_dict(
                        catalog_payload(
                            agents=[
                                agent(
                                    capabilities=["text", "tools", "web"],
                                    mcp_servers=servers,
                                )
                            ]
                        )
                    )
                self.assertEqual(raised.exception.code, "web_requires_mcp")

        parsed = Catalog.from_dict(
            catalog_payload(
                agents=[
                    agent(
                        capabilities=["text", "tools", "web"],
                        mcp_servers={
                            "docs": {"url": "https://developers.openai.com/mcp"}
                        },
                    )
                ]
            )
        )
        self.assertIn("web", parsed.agents[0].capabilities)

    def test_json_round_trip_is_deterministic_and_secret_free(self) -> None:
        catalog = default_catalog()

        first = save_catalog_bytes(catalog)
        second = save_catalog_bytes(load_catalog(first))

        self.assertEqual(first, second)
        self.assertEqual(json.loads(first), catalog.to_dict())
        self.assertNotIn(b"api_key", first.lower())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_bytes(first)
            self.assertEqual(load_catalog(path), catalog)


class RoutingTests(unittest.TestCase):
    def test_route_filters_capabilities_then_sorts_priority_and_name(self) -> None:
        parsed = Catalog.from_dict(
            catalog_payload(
                agents=[
                    agent(name="zeta", priority=10),
                    agent(name="alpha", priority=10),
                    agent(name="cheap", priority=50),
                    agent(
                        name="vision",
                        priority=1,
                        capabilities=["text", "tools", "vision"],
                    ),
                ]
            )
        )

        self.assertEqual(route_agent(parsed, {"text", "tools"}, False).name, "vision")
        self.assertEqual(route_agent(parsed, {"vision"}, False).name, "vision")

        without_vision = Catalog.from_dict(
            catalog_payload(
                agents=[agent(name="zeta", priority=10), agent(name="alpha", priority=10)]
            )
        )
        self.assertEqual(
            route_agent(without_vision, {"text", "tools"}, False).name,
            "alpha",
        )

    def test_high_risk_route_requires_high_trust(self) -> None:
        parsed = Catalog.from_dict(
            catalog_payload(
                agents=[
                    agent(name="standard", trust="standard", priority=1),
                    agent(name="trusted", trust="high", priority=50),
                ]
            )
        )

        self.assertEqual(route_agent(parsed, {"text"}, high_risk=True).name, "trusted")

    def test_no_match_returns_parent_required_without_fallback(self) -> None:
        parsed = Catalog.from_dict(
            catalog_payload(
                [provider(capabilities=["text", "tools"])],
                [agent(capabilities=["text", "tools"], trust="standard")],
            )
        )

        self.assertIsNone(route_agent(parsed, {"vision"}, high_risk=False))
        self.assertIsNone(route_agent(parsed, {"text"}, high_risk=True))
        self.assertIsNone(route_agent(parsed, {"hosted-search"}, high_risk=False))

    def test_catalog_can_describe_a_narrow_media_only_agent(self) -> None:
        parsed = Catalog.from_dict(
            catalog_payload(
                [provider(capabilities=["vision"])],
                [agent(capabilities=["vision"])],
            )
        )

        self.assertEqual(route_agent(parsed, {"vision"}, False).name, "reviewer")

    def test_disabled_provider_is_never_selected(self) -> None:
        parsed = Catalog.from_dict(
            catalog_payload(
                [provider(enabled=False)],
                [agent()],
            )
        )

        self.assertIsNone(route_agent(parsed, {"text"}, high_risk=False))


class DefaultCatalogTests(unittest.TestCase):
    def test_hybrid_default_has_deepseek_roles_and_native_reviewer(self) -> None:
        parsed = default_catalog()

        self.assertEqual(
            {item.name for item in parsed.agents},
            {"default", "worker", "explorer", "reviewer"},
        )
        reviewer = next(item for item in parsed.agents if item.name == "reviewer")
        self.assertEqual(reviewer.provider, "codex")
        self.assertEqual(reviewer.trust, "high")
        self.assertEqual(reviewer.sandbox_mode, "read-only")
        for name in ("default", "worker", "explorer"):
            child = next(item for item in parsed.agents if item.name == name)
            self.assertEqual(child.provider, "deepseek")
            self.assertEqual(child.model, "deepseek-v4-pro")

    def test_native_preset_does_not_include_deepseek(self) -> None:
        parsed = default_catalog("native")

        self.assertEqual([item.id for item in parsed.providers], ["codex"])
        self.assertEqual([item.name for item in parsed.agents], ["reviewer"])


if __name__ == "__main__":
    unittest.main()
