#!/usr/bin/env python3

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


PACKAGE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import ManagerError, resolve_paths  # noqa: E402
from multi_relay.model_capabilities import ModelSelection  # noqa: E402
from multi_relay.native_test import (  # noqa: E402
    NativeEvidence,
    _prompt,
    aggregate_host_checks,
    run_native_acceptance,
    verify_native_evidence,
)


def valid_selection() -> ModelSelection:
    return ModelSelection(
        requested_model="deepseek-v4-pro",
        resolved_model="deepseek-v4-pro",
        reasoning_effort="xhigh",
        effort_source="empirical_codex_provider_probe",
    )


def valid_evidence() -> NativeEvidence:
    child_ids = ("single-1", "fan-default", "fan-worker", "fan-explorer")
    return NativeEvidence(
        session_id="parent-1",
        child_thread_ids=child_ids,
        fanout_child_ids=child_ids[1:],
        role_names=("default", "default", "worker", "explorer"),
        tokens=(
            "DEEPSEEK_SINGLE_OK",
            "DEEPSEEK_DEFAULT_OK",
            "DEEPSEEK_WORKER_OK",
            "DEEPSEEK_EXPLORER_OK",
            "DEEPSEEK_RESUME_OK",
        ),
        metadata=tuple(
            {
                "model_provider": "deepseek",
                "model": "deepseek-v4-pro",
                "reasoning_effort": "xhigh",
                "agent_role": role,
            }
            for role in ("default", "default", "worker", "explorer")
        ),
        child_messages=(
            ("single-1", ("DEEPSEEK_SINGLE_OK", "DEEPSEEK_RESUME_OK")),
            ("fan-default", ("DEEPSEEK_DEFAULT_OK",)),
            ("fan-worker", ("DEEPSEEK_WORKER_OK",)),
            ("fan-explorer", ("DEEPSEEK_EXPLORER_OK",)),
        ),
        tool_child_ids=("fan-explorer",),
        resumed_child_ids=("single-1",),
        parent_metadata={
            "model_provider": "openai",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "max",
            "agent_role": None,
        },
    )


class NativeEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selection = valid_selection()
        self.parent = {
            "model_provider": "openai",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "max",
        }

    def test_host_check_aggregation_preserves_each_host_result(self) -> None:
        combined = aggregate_host_checks(
            {
                "codex": {"status": "ready"},
                "claude-code": {"status": "partial"},
            }
        )

        self.assertEqual(combined["status"], "partial")
        self.assertEqual(combined["details"]["hosts"]["codex"]["status"], "ready")

    def test_complete_evidence_passes(self) -> None:
        verify_native_evidence(valid_evidence(), self.selection, self.parent)

    def test_acceptance_prompt_forces_explicit_v2_roles_without_parent_inheritance(self) -> None:
        prompt = _prompt()

        self.assertIn('agent_type="default"', prompt)
        self.assertIn('agent_type="worker"', prompt)
        self.assertIn('agent_type="explorer"', prompt)
        self.assertIn('fork_turns="none"', prompt)
        self.assertIn("never fork_turns=all", prompt)
        self.assertIn("[DeepSeek task: <target>]", prompt)
        self.assertIn("exact complete message", prompt)
        self.assertIn("Before each spawn or follow-up", prompt)

    def test_native_runner_recovers_v2_children_when_stdout_hides_spawns(self) -> None:
        selection = valid_selection()

        def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
            self.assertEqual(kwargs.get("encoding"), "utf-8")
            self.assertEqual(kwargs.get("errors"), "replace")
            home = Path(kwargs["env"]["CODEX_HOME"])
            sessions = home / "sessions"
            sessions.mkdir()

            def write_rollout(name: str, payloads: list[dict[str, object]]) -> Path:
                path = sessions / f"{name}.jsonl"
                path.write_text(
                    "".join(json.dumps({"payload": payload}) + "\n" for payload in payloads),
                    encoding="utf-8",
                )
                return path

            parent_rollout = write_rollout(
                "parent",
                [
                    {"type": "function_call", "name": "spawn_agent", "call_id": "s0"},
                    {"type": "function_call_output", "call_id": "s0", "output": "single"},
                    {"type": "function_call", "name": "wait_agent", "call_id": "w0"},
                    {"type": "function_call_output", "call_id": "w0", "output": "done"},
                    {"type": "function_call", "name": "spawn_agent", "call_id": "s1"},
                    {"type": "function_call", "name": "spawn_agent", "call_id": "s2"},
                    {"type": "function_call", "name": "spawn_agent", "call_id": "s3"},
                    {"type": "function_call_output", "call_id": "s1", "output": "default"},
                    {"type": "function_call_output", "call_id": "s2", "output": "worker"},
                    {"type": "function_call_output", "call_id": "s3", "output": "explorer"},
                    {"type": "function_call", "name": "wait_agent", "call_id": "w1"},
                    {"type": "function_call_output", "call_id": "w1", "output": "done"},
                    {"type": "function_call", "name": "followup_task", "call_id": "f0"},
                ],
            )
            child_payloads = {
                "single-1": [
                    {"type": "task_started"},
                    {"type": "agent_message", "message": "DEEPSEEK_SINGLE_OK"},
                    {"type": "task_started"},
                    {"type": "agent_message", "message": "DEEPSEEK_RESUME_OK"},
                ],
                "fan-default": [
                    {"type": "task_started"},
                    {"type": "agent_message", "message": "DEEPSEEK_DEFAULT_OK"},
                ],
                "fan-worker": [
                    {"type": "task_started"},
                    {"type": "agent_message", "message": "DEEPSEEK_WORKER_OK"},
                ],
                "fan-explorer": [
                    {"type": "task_started"},
                    {"type": "function_call", "name": "shell_command"},
                    {"type": "agent_message", "message": "DEEPSEEK_EXPLORER_OK"},
                ],
            }
            child_rollouts = {
                child_id: write_rollout(child_id, payloads)
                for child_id, payloads in child_payloads.items()
            }
            source = lambda role: json.dumps({  # noqa: E731
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": "parent-1",
                        "agent_role": role,
                    }
                }
            })
            rows = [
                (
                    "parent-1",
                    str(parent_rollout),
                    "openai",
                    "gpt-5.6-sol",
                    "max",
                    None,
                    "exec",
                    1,
                ),
                *[
                    (
                        child_id,
                        str(child_rollouts[child_id]),
                        "deepseek",
                        "deepseek-v4-pro",
                        "xhigh",
                        role,
                        source(role),
                        index,
                    )
                    for index, (child_id, role) in enumerate(
                        (
                            ("fan-worker", "worker"),
                            ("single-1", "default"),
                            ("fan-explorer", "explorer"),
                            ("fan-default", "default"),
                        ),
                        start=2,
                    )
                ],
            ]
            with closing(sqlite3.connect(home / "state_v2.sqlite")) as connection:
                connection.execute(
                    "CREATE TABLE threads (id TEXT, rollout_path TEXT, model_provider TEXT, "
                    "model TEXT, reasoning_effort TEXT, agent_role TEXT, source TEXT, "
                    "created_at_ms INTEGER)"
                )
                connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
                connection.commit()
            stdout = json.dumps({"type": "thread.started", "thread_id": "parent-1"})
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        with tempfile.TemporaryDirectory() as directory:
            formal_home = Path(directory).resolve()
            (formal_home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\nmodel_provider = "openai"\n'
                'model_reasoning_effort = "max"\n',
                encoding="utf-8",
            )
            evidence = run_native_acceptance(
                "codex.exe",
                resolve_paths(str(formal_home)),
                selection,
                runner=runner,
            )

        verify_native_evidence(evidence, selection, self.parent)
        self.assertEqual(evidence.child_thread_ids, valid_evidence().child_thread_ids)

    def test_wrong_fanout_count_or_duplicate_child_is_rejected(self) -> None:
        evidence = valid_evidence()
        cases = (
            replace(evidence, fanout_child_ids=evidence.fanout_child_ids[:2]),
            replace(
                evidence,
                child_thread_ids=("single-1", "fan-default", "fan-worker", "fan-worker"),
            ),
        )
        for case in cases:
            with self.subTest(case=case.fanout_child_ids), self.assertRaises(ManagerError):
                verify_native_evidence(case, self.selection, self.parent)

    def test_wrong_child_route_model_effort_or_role_is_rejected(self) -> None:
        evidence = valid_evidence()
        broken_metadata = list(evidence.metadata)
        broken_metadata[2] = {**broken_metadata[2], "model_provider": "openai"}
        broken = replace(evidence, metadata=tuple(broken_metadata))

        with self.assertRaises(ManagerError) as raised:
            verify_native_evidence(broken, self.selection, self.parent)

        self.assertEqual(raised.exception.code, "native_route_mismatch")

    def test_missing_tool_resume_token_or_unchanged_parent_is_rejected(self) -> None:
        evidence = valid_evidence()
        cases = (
            replace(evidence, tool_child_ids=()),
            replace(evidence, resumed_child_ids=()),
            replace(
                evidence,
                child_messages=tuple(
                    item for item in evidence.child_messages if item[0] != "fan-worker"
                ),
            ),
            replace(evidence, parent_metadata={**evidence.parent_metadata, "model": "deepseek-v4-pro"}),
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ManagerError):
                verify_native_evidence(case, self.selection, self.parent)

    def test_native_runner_uses_formal_home_and_reads_authoritative_rollouts(self) -> None:
        selection = valid_selection()

        def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
            environment = kwargs["env"]
            home = Path(environment["CODEX_HOME"])
            self.assertEqual(home, formal_home)
            self.assertIn(str(formal_home), command)
            rollout = formal_home / "sessions" / "explorer.jsonl"
            rollout.parent.mkdir()
            rollout.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "name": "exec",
                            "status": "completed",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            database = formal_home / "state_native.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE threads (id TEXT, rollout_path TEXT, model_provider TEXT, "
                    "model TEXT, reasoning_effort TEXT, agent_role TEXT)"
                )
                connection.executemany(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("parent-1", "", "openai", "gpt-5.6-sol", "max", None),
                        ("single-1", "", "deepseek", "deepseek-v4-pro", "xhigh", "default"),
                        ("fan-default", "", "deepseek", "deepseek-v4-pro", "xhigh", "default"),
                        ("fan-worker", "", "deepseek", "deepseek-v4-pro", "xhigh", "worker"),
                        ("fan-explorer", str(rollout), "deepseek", "deepseek-v4-pro", "xhigh", "explorer"),
                    ],
                )
                connection.commit()
            events = [
                {"type": "thread.started", "thread_id": "parent-1"},
                {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent", "receiver_thread_ids": ["single-1"]}},
                {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait_agent", "agents_states": {"single-1": {"status": "completed", "message": "DEEPSEEK_SINGLE_OK"}}}},
                {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent", "receiver_thread_ids": ["fan-default"]}},
                {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent", "receiver_thread_ids": ["fan-worker"]}},
                {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent", "receiver_thread_ids": ["fan-explorer"]}},
                {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait_agent", "agents_states": {
                    "fan-default": {"status": "completed", "message": "DEEPSEEK_DEFAULT_OK"},
                    "fan-worker": {"status": "completed", "message": "DEEPSEEK_WORKER_OK"},
                    "fan-explorer": {"status": "completed", "message": "DEEPSEEK_EXPLORER_OK"},
                }}},
                {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "resume_agent", "receiver_thread_ids": ["single-1"]}},
                {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait_agent", "agents_states": {"single-1": {"status": "completed", "message": "DEEPSEEK_RESUME_OK"}}}},
            ]
            return SimpleNamespace(
                returncode=0,
                stdout="\n".join(json.dumps(event) for event in events),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            formal_home = Path(directory).resolve()
            (formal_home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\n'
                'model_provider = "openai"\n'
                'model_reasoning_effort = "max"\n',
                encoding="utf-8",
            )

            evidence = run_native_acceptance(
                "codex.exe",
                resolve_paths(str(formal_home)),
                selection,
                runner=runner,
            )

        verify_native_evidence(evidence, selection, self.parent)
        self.assertEqual(evidence.tool_child_ids, ("fan-explorer",))


if __name__ == "__main__":
    unittest.main()
