#!/usr/bin/env python3

from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
import hashlib
import json
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError, Paths, resolve_paths  # noqa: E402
from multi_relay.catalog import load_catalog  # noqa: E402
from multi_relay.compatibility import CompatibilityReport  # noqa: E402
from multi_relay.instructions import INSTRUCTIONS_BEGIN  # noqa: E402
from multi_relay.manager import RelayManager  # noqa: E402
from multi_relay.model_capabilities import ModelSelection  # noqa: E402


class FakeCredentialStore:
    def __init__(self, secret: str | None = "sk-test") -> None:
        self.secret = secret
        self.remove_calls = 0

    def exists(self) -> bool:
        return self.secret is not None

    def read(self) -> str | None:
        return self.secret

    def store(self, secret: str) -> None:
        self.secret = secret

    def remove(self) -> bool:
        self.remove_calls += 1
        existed = self.secret is not None
        self.secret = None
        return existed


def passing_report(selection: ModelSelection) -> CompatibilityReport:
    return CompatibilityReport(
        model=selection.resolved_model,
        effort=selection.reasoning_effort,
        provider_initialized=True,
        single_child_passed=True,
        fanout_passed=True,
        tools_passed=True,
        resume_passed=True,
        child_metadata_passed=True,
        parent_unchanged=True,
    )


def provider_smoke_report(selection: ModelSelection) -> CompatibilityReport:
    return CompatibilityReport(
        model=selection.resolved_model,
        effort=selection.reasoning_effort,
        provider_initialized=True,
        single_child_passed=None,
        fanout_passed=None,
        tools_passed=None,
        resume_passed=None,
        child_metadata_passed=None,
        parent_unchanged=None,
    )


class ManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection = ModelSelection(
            requested_model="deepseek-v4-pro",
            resolved_model="deepseek-v4-pro",
            reasoning_effort="xhigh",
            effort_source="empirical_codex_provider_probe",
        )

    def make_manager(
        self,
        home: Path,
        *,
        store: FakeCredentialStore | None = None,
        events: list[str] | None = None,
        report: CompatibilityReport | None = None,
        live_report: CompatibilityReport | None = None,
    ) -> RelayManager:
        credentials = store or FakeCredentialStore()
        timeline = events if events is not None else []

        def discover(secret: str) -> str:
            timeline.append("discover")
            return self.selection.resolved_model

        def resolve(model: str) -> ModelSelection:
            timeline.append("effort")
            return self.selection

        def gate(codex_bin: str, gate_home: Path, selection: ModelSelection) -> CompatibilityReport:
            timeline.append("gate")
            existing_install = any(
                (home / state / "manifest.json").exists()
                for state in (
                    "codex-multi-relay",
                    "codex-deepseek-relay",
                    "codex-deepseek-subagent",
                )
            )
            if not existing_install:
                self.assertFalse((home / "agents" / "default.toml").exists())
            return report or provider_smoke_report(selection)

        def live(codex_bin: str, live_home: Path, selection: ModelSelection) -> CompatibilityReport:
            timeline.append("live")
            self.assertTrue((home / "agents" / "default.toml").is_file())
            return live_report or passing_report(selection)

        return RelayManager(
            resolve_paths(str(home)),
            "codex.exe",
            credentials=credentials,
            model_discoverer=discover,
            selection_resolver=resolve,
            compatibility_gate=gate,
            live_acceptance=live,
            bridge_stopper=lambda: timeline.append("bridge_stop") or True,
        )

    def test_public_paths_include_the_secret_free_multi_relay_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = resolve_paths(directory)

        self.assertIsInstance(paths, Paths)
        self.assertEqual(paths.agents_dir, root / "agents")
        self.assertEqual(paths.instruction_file, root / "AGENTS.md")
        self.assertEqual(paths.state_dir, root / "codex-multi-relay")
        self.assertEqual(paths.manifest, paths.state_dir / "manifest.json")
        self.assertEqual(paths.catalog, paths.state_dir / "catalog.json")
        self.assertEqual(paths.relay_state_dir, root / "codex-deepseek-relay")
        self.assertEqual(paths.relay_manifest, paths.relay_state_dir / "manifest.json")
        self.assertEqual(paths.legacy_state_dir, root / "codex-deepseek-subagent")
        self.assertEqual(paths.legacy_manifest, paths.legacy_state_dir / "manifest.json")

    def test_current_install_in_legacy_state_is_read_and_adopted_on_disable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\n', encoding="utf-8"
            )
            manager = self.make_manager(home)
            manager.setup()
            paths = manager.paths
            paths.legacy_state_dir.mkdir(parents=True, exist_ok=True)
            paths.manifest.replace(paths.legacy_manifest)

            self.assertEqual(manager.status()["status"], "ready")
            result = manager.disable()

            self.assertEqual(result["status"], "disabled")
            self.assertTrue(paths.manifest.is_file())
            self.assertFalse(paths.legacy_manifest.exists())
            adopted = json.loads(paths.manifest.read_text(encoding="utf-8"))
            self.assertEqual(adopted["status"], "disabled")

    def test_failed_legacy_state_adoption_restores_the_old_manifest_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.toml"
            config.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
            manager = self.make_manager(home)
            manager.setup()
            paths = manager.paths
            paths.legacy_state_dir.mkdir(parents=True, exist_ok=True)
            legacy_manifest = paths.manifest.read_bytes()
            paths.manifest.replace(paths.legacy_manifest)
            config_before = config.read_bytes()
            agents_before = {
                path.name: path.read_bytes()
                for path in (home / "agents").glob("*.toml")
            }
            instructions_before = (home / "AGENTS.md").read_bytes()

            failing_manager = self.make_manager(
                home,
                live_report=provider_smoke_report(self.selection),
            )
            with self.assertRaises(ManagerError) as raised:
                failing_manager.setup()

            self.assertEqual(raised.exception.code, "compatibility_failed")
            self.assertFalse(paths.manifest.exists())
            self.assertEqual(paths.legacy_manifest.read_bytes(), legacy_manifest)
            self.assertEqual(config.read_bytes(), config_before)
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (home / "agents").glob("*.toml")
                },
                agents_before,
            )
            self.assertEqual((home / "AGENTS.md").read_bytes(), instructions_before)

    def test_lifecycle_changes_refuse_schema3_state_until_setup_migrates_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = resolve_paths(str(home))
            paths.legacy_state_dir.mkdir(parents=True)
            legacy_payload = b'{"schema_version":3,"status":"enabled","managed_files":{}}\n'
            paths.legacy_manifest.write_bytes(legacy_payload)
            manager = self.make_manager(home)

            for action in (manager.test, manager.disable, manager.enable, manager.uninstall):
                with self.subTest(action=action.__name__):
                    with self.assertRaises(ManagerError) as raised:
                        action()
                    self.assertEqual(raised.exception.code, "legacy_requires_setup")
                    self.assertEqual(paths.legacy_manifest.read_bytes(), legacy_payload)
                    self.assertFalse(paths.manifest.exists())

    def test_setup_gates_before_writes_then_installs_all_builtin_roles(self) -> None:
        events: list[str] = []
        self.selection = ModelSelection(
            requested_model="deepseek-v4-pro",
            resolved_model="deepseek-discovered-model",
            reasoning_effort="low",
            effort_source="empirical_codex_provider_probe",
        )
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            original = (
                'model = "gpt-5.6-sol"\n'
                'model_provider = "openai"\n'
                'model_reasoning_effort = "max"\n'
            )
            (home / "config.toml").write_text(original, encoding="utf-8")
            manager = self.make_manager(home, events=events)

            outcome = manager.setup()

            parsed = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(parsed["model"], "gpt-5.6-sol")
            self.assertEqual(parsed["model_provider"], "openai")
            self.assertEqual(parsed["model_reasoning_effort"], "max")
            self.assertEqual(parsed["agents"]["max_concurrent_threads_per_session"], 8)
            self.assertEqual(
                {path.name for path in (home / "agents").glob("*.toml")},
                {"default.toml", "worker.toml", "explorer.toml", "reviewer.toml"},
            )
            self.assertIn(
                INSTRUCTIONS_BEGIN,
                (home / "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(events, ["discover", "effort", "gate", "live"])
            self.assertEqual(outcome["status"], "ready")
            self.assertEqual(manager.status()["status"], "ready")
            manifest = json.loads(
                (home / "codex-multi-relay" / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["preinstall_compatibility"],
                {"provider_initialized": True},
            )
            self.assertTrue(all(manifest["compatibility"].values()))
            catalog = load_catalog(
                home / "codex-multi-relay" / "catalog.json"
            )
            self.assertEqual(
                {
                    item.model
                    for item in catalog.targets
                    if item.provider_id == "deepseek"
                },
                {"deepseek-discovered-model"},
            )
            self.assertEqual(
                {
                    item.reasoning_effort
                    for item in catalog.agents
                    if item.provider == "deepseek"
                },
                {"low"},
            )

    def test_status_rejects_a_v2_concurrency_cap_below_eight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config_path = home / "config.toml"
            config_path.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
            manager = self.make_manager(home)
            manager.setup()
            config = config_path.read_text(encoding="utf-8")
            config_path.write_text(
                config.replace(
                    'tool_namespace = "agents"\nmax_concurrent_threads_per_session = 8',
                    'tool_namespace = "agents"\nmax_concurrent_threads_per_session = 1',
                    1,
                ),
                encoding="utf-8",
            )

            status = manager.status()

        self.assertEqual(status["status"], "partial")
        self.assertIs(status["checks"]["v2_concurrency"], False)

    def test_failed_compatibility_gate_leaves_real_home_byte_identical(self) -> None:
        incomplete = CompatibilityReport(
            model="deepseek-v4-pro",
            effort="xhigh",
            provider_initialized=True,
            single_child_passed=True,
            fanout_passed=False,
            tools_passed=True,
            resume_passed=True,
            child_metadata_passed=True,
            parent_unchanged=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.toml"
            config.write_bytes(b'model = "gpt-5.6-sol"\n')
            before = config.read_bytes()
            manager = self.make_manager(home, report=incomplete)

            with self.assertRaises(ManagerError) as raised:
                manager.setup()

            self.assertEqual(raised.exception.code, "compatibility_failed")
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(list(home.rglob("*.toml")), [config])

    def test_incomplete_postinstall_report_rolls_back_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.toml"
            config.write_bytes(b'model = "gpt-5.6-sol"\n')
            before = config.read_bytes()
            manager = self.make_manager(
                home,
                live_report=provider_smoke_report(self.selection),
            )

            with self.assertRaises(ManagerError) as raised:
                manager.setup()

            self.assertEqual(raised.exception.code, "compatibility_failed")
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(list((home / "agents").glob("*.toml")), [])
            self.assertFalse((home / "AGENTS.md").exists())
            self.assertFalse(
                (home / "codex-multi-relay" / "manifest.json").exists()
            )

    def test_conflicting_user_owned_role_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\n', encoding="utf-8"
            )
            role = home / "agents" / "default.toml"
            role.parent.mkdir()
            role.write_bytes(b'user_owned = true\n')
            manager = self.make_manager(home)

            with self.assertRaises(ManagerError) as raised:
                manager.setup()

            self.assertEqual(raised.exception.code, "conflict")
            self.assertEqual(role.read_bytes(), b'user_owned = true\n')

    def test_disable_enable_and_uninstall_are_reversible_without_network(self) -> None:
        events: list[str] = []
        store = FakeCredentialStore()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            original = (
                'model = "gpt-5.6-sol"\n\n'
                '[features]\n'
                'multi_agent = false\n'
            )
            (home / "config.toml").write_text(original, encoding="utf-8")
            manager = self.make_manager(home, store=store, events=events)
            manager.setup()
            setup_events = list(events)

            disabled = manager.disable()
            self.assertEqual(disabled["status"], "disabled")
            self.assertEqual(list((home / "agents").glob("*.toml")), [])
            self.assertNotIn(
                INSTRUCTIONS_BEGIN,
                (
                    (home / "AGENTS.md").read_text(encoding="utf-8")
                    if (home / "AGENTS.md").is_file()
                    else ""
                ),
            )
            self.assertIn(
                "model_providers",
                tomllib.loads((home / "config.toml").read_text(encoding="utf-8")),
            )

            enabled = manager.enable()
            self.assertEqual(enabled["status"], "ready")
            self.assertEqual(events, setup_events)
            self.assertEqual(len(list((home / "agents").glob("*.toml"))), 4)

            removed = manager.uninstall()
            self.assertEqual(removed["status"], "uninstalled")
            parsed = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(parsed["model"], "gpt-5.6-sol")
            self.assertIs(parsed["features"]["multi_agent"], False)
            self.assertNotIn("model_providers", parsed)
            self.assertFalse((home / "codex-multi-relay" / "manifest.json").exists())
            self.assertEqual(store.remove_calls, 0)
            self.assertEqual(events[-1], "bridge_stop")

    def test_uninstall_removes_credential_only_when_explicit(self) -> None:
        store = FakeCredentialStore()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\n', encoding="utf-8"
            )
            manager = self.make_manager(home, store=store)
            manager.setup()

            manager.uninstall(remove_credential=True)

        self.assertEqual(store.remove_calls, 1)
        self.assertIsNone(store.secret)

    def test_uninstall_without_manifest_still_stops_an_orphaned_bridge(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            manager = self.make_manager(Path(directory), events=events)

            result = manager.uninstall()

        self.assertEqual(result["status"], "uninstalled")
        self.assertEqual(events, ["bridge_stop"])

    def test_missing_credential_stops_before_discovery_or_writes(self) -> None:
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "config.toml"
            config.write_bytes(b'model = "gpt-5.6-sol"\n')
            manager = self.make_manager(
                home,
                store=FakeCredentialStore(secret=None),
                events=events,
            )

            with self.assertRaises(ManagerError) as raised:
                manager.setup()

            self.assertEqual(raised.exception.code, "credential_missing")
            self.assertEqual(events, [])
            self.assertEqual(config.read_bytes(), b'model = "gpt-5.6-sol"\n')

    def test_owned_legacy_install_is_migrated_without_v1_or_catalog_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            alias_component = Path(directory) / "alias"
            alias_component.mkdir()
            home = alias_component / ".."
            catalog = home / "models-with-deepseek.json"
            legacy_agent = home / "agents" / "DeepSeek.toml"
            legacy_agent.parent.mkdir(parents=True)
            catalog_bytes = b'{"models":[{"slug":"gpt-5.6-sol","multi_agent_version":"v1"}]}\n'
            agent_bytes = b'model = "deepseek-v4-flash"\n'
            catalog.write_bytes(catalog_bytes)
            legacy_agent.write_bytes(agent_bytes)
            config = (
                'model = "gpt-5.6-sol"\n'
                'model_provider = "openai"\n'
                'model_reasoning_effort = "max"\n'
                f'model_catalog_json = {json.dumps(str(catalog))}\n\n'
                '# BEGIN CODEX-DEEPSEEK-SUBAGENT PROVIDER\n'
                '[model_providers.deepseek]\n'
                'name = "DeepSeek"\n'
                'base_url = "https://api.deepseek.com/"\n'
                'wire_api = "responses"\n'
                '# END CODEX-DEEPSEEK-SUBAGENT PROVIDER\n\n'
                '[features]\n'
                'multi_agent_v2 = false\n'
            )
            (home / "config.toml").write_text(config, encoding="utf-8")
            state = home / "codex-deepseek-subagent"
            state.mkdir()
            (state / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "managed_catalog_selection": True,
                        "previous_model_catalog_json": None,
                        "managed_multi_agent_v2": True,
                        "previous_multi_agent_v2": None,
                        "managed_provider_block": True,
                        "managed_agent_file": True,
                        "catalog_preexisted": False,
                        "agent_sha256": hashlib.sha256(agent_bytes).hexdigest(),
                        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            manager = self.make_manager(home)

            outcome = manager.setup()

            migrated_text = (home / "config.toml").read_text(encoding="utf-8")
            migrated = tomllib.loads(migrated_text)
            self.assertEqual(outcome["status"], "ready")
            self.assertNotIn("model_catalog_json", migrated)
            self.assertIs(migrated["features"]["multi_agent_v2"]["enabled"], True)
            self.assertIs(
                migrated["features"]["multi_agent_v2"]["hide_spawn_agent_metadata"],
                False,
            )
            self.assertEqual(
                migrated["features"]["multi_agent_v2"]["tool_namespace"],
                "agents",
            )
            self.assertNotIn("multi_agent_version", migrated_text)
            self.assertFalse(catalog.exists())
            self.assertFalse(legacy_agent.exists())
            self.assertEqual(
                {path.name for path in (home / "agents").glob("*.toml")},
                {"default.toml", "worker.toml", "explorer.toml", "reviewer.toml"},
            )
            new_manifest_path = home / "codex-multi-relay" / "manifest.json"
            new_manifest = json.loads(new_manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(new_manifest["schema_version"], 5)
            self.assertIs(new_manifest["legacy_migrated"], True)
            self.assertFalse((state / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
