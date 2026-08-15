#!/usr/bin/env python3
"""Exercise the installed Codex binary against the bridge without a real API key."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "scripts"
sys.path.insert(0, str(PACKAGE_ROOT))

from multi_relay import bridge as bridge_module  # noqa: E402
from multi_relay.bridge import _BridgeServer  # noqa: E402
from multi_relay.model_capabilities import ModelSelection  # noqa: E402
from multi_relay.native_test import run_native_acceptance, verify_native_evidence  # noqa: E402
from multi_relay.paths import resolve_paths  # noqa: E402
from multi_relay.roles import expected_agent_files  # noqa: E402


TOKEN = "CODEX_BRIDGE_RUNTIME_OK"
FANOUT_TOKEN = "CODEX_MULTI_RELAY_FANOUT_OK"
ACCEPTANCE_TOKEN = "CODEX_NATIVE_ACCEPTANCE_OK"
CHILD_TOKENS = {
    "default": "DEEPSEEK_FIXTURE_DEFAULT_OK",
    "worker": "DEEPSEEK_FIXTURE_WORKER_OK",
    "explorer": "DEEPSEEK_FIXTURE_EXPLORER_OK",
}
_CODEX_REQUESTS: list[dict[str, object]] = []


def _handoff(target: str, message: str) -> str:
    return (
        f"[DeepSeek task: {target}]\n"
        f"{message}\n"
        f"[/DeepSeek task: {target}]"
    )


_ORIGINAL_BUILD_CHAT_REQUEST = bridge_module.build_chat_request


def _capture_codex_request(
    body: dict[str, object],
    *,
    reasoning_secret: str | None = None,
):
    _CODEX_REQUESTS.append(body)
    return _ORIGINAL_BUILD_CHAT_REQUEST(body, reasoning_secret=reasoning_secret)


bridge_module.build_chat_request = _capture_codex_request


class _DeepSeekFixture(BaseHTTPRequestHandler):
    captured: list[dict[str, object]] = []
    authorizations: list[str | None] = []
    scenario = "tool"
    lock = threading.Lock()

    def log_message(self, format: str, *args: object) -> None:
        return

    @staticmethod
    def _tool_chunks(payload: dict[str, object]) -> list[dict[str, object]]:
        messages = payload.get("messages")
        has_tool_output = isinstance(messages, list) and any(
            isinstance(message, dict) and message.get("role") == "tool"
            for message in messages
        )
        if has_tool_output:
            return [{
                "id": "chat-runtime-fixture-2",
                "choices": [{
                    "delta": {"content": TOKEN},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                },
            }]
        return [
            {
                "id": "chat-runtime-fixture-1",
                "choices": [{
                    "delta": {"reasoning_content": "Use a harmless read-only tool."},
                    "finish_reason": None,
                }],
            },
            {
                "id": "chat-runtime-fixture-1",
                "choices": [{
                    "delta": {
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_runtime_fixture",
                            "function": {
                                "name": "shell_command",
                                "arguments": json.dumps({
                                    "command": "Write-Output CODEX_BRIDGE_TOOL_OK"
                                }),
                            },
                        }]
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                    "total_tokens": 20,
                },
            },
        ]

    @staticmethod
    def _message_text(message: object) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)

    @classmethod
    def _fanout_chunks(cls, payload: dict[str, object]) -> list[dict[str, object]]:
        messages = payload.get("messages")
        message_list = messages if isinstance(messages, list) else []
        user_text = "\n".join(
            cls._message_text(message)
            for message in message_list
            if isinstance(message, dict) and message.get("role") == "user"
        )
        system_text = "\n".join(
            cls._message_text(message)
            for message in message_list
            if isinstance(message, dict) and message.get("role") == "system"
        )

        history_names: list[str] = []
        for message in message_list:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tool_call in tool_calls:
                function = tool_call.get("function") if isinstance(tool_call, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                if isinstance(name, str):
                    history_names.append(name)

        for role, child_token in CHILD_TOKENS.items():
            if (
                f"Codex's {role} child agent" in system_text
                and child_token in user_text
            ):
                if role == "explorer" and "shell_command" not in history_names:
                    return [{
                        "id": "chat-child-explorer-tool",
                        "choices": [{
                            "delta": {
                                "reasoning_content": "Use the required harmless read tool.",
                                "tool_calls": [{
                                    "index": 0,
                                    "id": "call_explorer_fixture",
                                    "function": {
                                        "name": "shell_command",
                                        "arguments": json.dumps({
                                            "command": "Write-Output DEEPSEEK_EXPLORER_TOOL_OK"
                                        }),
                                    },
                                }],
                            },
                            "finish_reason": "tool_calls",
                        }],
                    }]
                return [{
                    "id": f"chat-child-{role}",
                    "choices": [{
                        "delta": {"content": child_token},
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 2,
                        "total_tokens": 7,
                    },
                }]

        if "agents__spawn_agent" not in history_names:
            calls = []
            for index, (role, child_token) in enumerate(CHILD_TOKENS.items()):
                calls.append({
                    "index": index,
                    "id": f"call_spawn_{role}",
                    "function": {
                        "name": "agents__spawn_agent",
                        "arguments": json.dumps({
                            "task_name": f"fixture_{role}",
                            "message": f"gAAAAABruntime-{role}-opaque==",
                            "agent_type": role,
                            "fork_turns": "none",
                        }),
                    },
                })
            return [{
                "id": "chat-parent-spawn",
                "choices": [{
                    "delta": {
                        "reasoning_content": "Fan out all three independent fixture tasks.",
                        "content": "\n".join(
                            _handoff(
                                f"fixture_{role}",
                                f"Reply exactly {child_token} and nothing else.",
                            )
                            for role, child_token in CHILD_TOKENS.items()
                        ),
                        "tool_calls": calls,
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            }]
        if not all(child_token in user_text for child_token in CHILD_TOKENS.values()):
            wait_index = history_names.count("agents__wait_agent")
            return [{
                "id": f"chat-parent-wait-{wait_index}",
                "choices": [{
                    "delta": {
                        "reasoning_content": "Wait for the spawned children.",
                        "tool_calls": [{
                            "index": 0,
                            "id": f"call_wait_children_{wait_index}",
                            "function": {
                                "name": "agents__wait_agent",
                                "arguments": json.dumps({"timeout_ms": 10000}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            }]
        if "agents__list_agents" not in history_names:
            return [{
                "id": "chat-parent-list",
                "choices": [{
                    "delta": {
                        "reasoning_content": "Confirm the fan-out tree.",
                        "tool_calls": [{
                            "index": 0,
                            "id": "call_list_children",
                            "function": {
                                "name": "agents__list_agents",
                                "arguments": "{}",
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            }]
        return [{
            "id": "chat-parent-finished",
            "choices": [{
                "delta": {"content": FANOUT_TOKEN},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 30, "completion_tokens": 2, "total_tokens": 32},
        }]

    @classmethod
    def _acceptance_chunks(cls, payload: dict[str, object]) -> list[dict[str, object]]:
        messages = payload.get("messages")
        message_list = messages if isinstance(messages, list) else []
        user_messages = [
            cls._message_text(message)
            for message in message_list
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        user_text = "\n".join(user_messages)
        routed_agent_text = "\n".join(
            text for text in user_messages if text.startswith("[Codex agent message:")
        )
        system_text = "\n".join(
            cls._message_text(message)
            for message in message_list
            if isinstance(message, dict) and message.get("role") == "system"
        )
        history_names: list[str] = []
        for message in message_list:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tool_call in tool_calls:
                function = tool_call.get("function") if isinstance(tool_call, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                if isinstance(name, str):
                    history_names.append(name)

        child_role = next(
            (
                role
                for role in ("default", "worker", "explorer")
                if f"Codex's {role} child agent" in system_text
            ),
            None,
        )
        if child_role:
            if "DEEPSEEK_RESUME_OK" in user_text:
                child_token = "DEEPSEEK_RESUME_OK"
            elif "DEEPSEEK_SINGLE_OK" in user_text:
                child_token = "DEEPSEEK_SINGLE_OK"
            else:
                child_token = {
                    "default": "DEEPSEEK_DEFAULT_OK",
                    "worker": "DEEPSEEK_WORKER_OK",
                    "explorer": "DEEPSEEK_EXPLORER_OK",
                }[child_role]
            if child_role == "explorer" and "shell_command" not in history_names:
                return [{
                    "id": "chat-acceptance-explorer-tool",
                    "choices": [{
                        "delta": {
                            "reasoning_content": "Use the required harmless read tool.",
                            "tool_calls": [{
                                "index": 0,
                                "id": "call_acceptance_explorer_tool",
                                "function": {
                                    "name": "shell_command",
                                    "arguments": json.dumps({
                                        "command": "Write-Output DEEPSEEK_ACCEPTANCE_TOOL_OK"
                                    }),
                                },
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                }]
            return [{
                "id": f"chat-acceptance-child-{child_role}",
                "choices": [{
                    "delta": {"content": child_token},
                    "finish_reason": "stop",
                }],
            }]

        spawn_count = history_names.count("agents__spawn_agent")
        wait_index = history_names.count("agents__wait_agent")

        def tool_chunk(
            call_id: str,
            name: str,
            arguments: dict[str, object],
            *,
            handoff_target: str | None = None,
            handoff_message: str | None = None,
        ) -> list[dict[str, object]]:
            delta: dict[str, object] = {
                "reasoning_content": "Continue the deterministic native acceptance.",
                "tool_calls": [{
                    "index": 0,
                    "id": call_id,
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }],
            }
            if handoff_target is not None and handoff_message is not None:
                delta["content"] = _handoff(handoff_target, handoff_message)
            return [{
                "id": f"chat-acceptance-{call_id}",
                "choices": [{
                    "delta": delta,
                    "finish_reason": "tool_calls",
                }],
            }]

        if spawn_count == 0:
            return tool_chunk(
                "call_acceptance_single",
                "agents__spawn_agent",
                {
                    "task_name": "acceptance_single",
                    "message": "gAAAAABacceptance-single-opaque==",
                    "agent_type": "default",
                    "fork_turns": "none",
                },
                handoff_target="acceptance_single",
                handoff_message="Reply exactly DEEPSEEK_SINGLE_OK and nothing else.",
            )
        if "DEEPSEEK_SINGLE_OK" not in routed_agent_text:
            return tool_chunk(
                f"call_acceptance_wait_{wait_index}",
                "agents__wait_agent",
                {"timeout_ms": 10000},
            )
        if spawn_count == 1:
            calls = []
            for index, (role, token) in enumerate(
                (
                    ("default", "DEEPSEEK_DEFAULT_OK"),
                    ("worker", "DEEPSEEK_WORKER_OK"),
                    ("explorer", "DEEPSEEK_EXPLORER_OK"),
                )
            ):
                calls.append({
                    "index": index,
                    "id": f"call_acceptance_{role}",
                    "function": {
                        "name": "agents__spawn_agent",
                        "arguments": json.dumps({
                            "task_name": f"acceptance_{role}",
                            "message": f"gAAAAABacceptance-{role}-opaque==",
                            "agent_type": role,
                            "fork_turns": "none",
                        }),
                    },
                })
            return [{
                "id": "chat-acceptance-fanout",
                "choices": [{
                    "delta": {
                        "reasoning_content": "Fan out the three independent role checks.",
                        "content": "\n".join(
                            _handoff(
                                f"acceptance_{role}",
                                f"Reply exactly {token} and nothing else.",
                            )
                            for role, token in (
                                ("default", "DEEPSEEK_DEFAULT_OK"),
                                ("worker", "DEEPSEEK_WORKER_OK"),
                                ("explorer", "DEEPSEEK_EXPLORER_OK"),
                            )
                        ),
                        "tool_calls": calls,
                    },
                    "finish_reason": "tool_calls",
                }],
            }]
        fanout_tokens = (
            "DEEPSEEK_DEFAULT_OK",
            "DEEPSEEK_WORKER_OK",
            "DEEPSEEK_EXPLORER_OK",
        )
        if not all(token in routed_agent_text for token in fanout_tokens):
            return tool_chunk(
                f"call_acceptance_wait_{wait_index}",
                "agents__wait_agent",
                {"timeout_ms": 10000},
            )
        if "agents__followup_task" not in history_names:
            return tool_chunk(
                "call_acceptance_followup",
                "agents__followup_task",
                {
                    "target": "/root/acceptance_single",
                    "message": "gAAAAABacceptance-followup-opaque==",
                },
                handoff_target="/root/acceptance_single",
                handoff_message="Reply exactly DEEPSEEK_RESUME_OK and nothing else.",
            )
        if "DEEPSEEK_RESUME_OK" not in routed_agent_text:
            return tool_chunk(
                f"call_acceptance_wait_{wait_index}",
                "agents__wait_agent",
                {"timeout_ms": 10000},
            )
        return [{
            "id": "chat-acceptance-finished",
            "choices": [{
                "delta": {"content": ACCEPTANCE_TOKEN},
                "finish_reason": "stop",
            }],
        }]

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        with self.lock:
            self.captured.append(payload)
            self.authorizations.append(self.headers.get("Authorization"))
        if self.scenario == "fanout":
            chunks = self._fanout_chunks(payload)
        elif self.scenario == "acceptance":
            chunks = self._acceptance_chunks(payload)
        else:
            chunks = self._tool_chunks(payload)
        body = (
            b"".join(
                b"data: "
                + json.dumps(chunk, separators=(",", ":")).encode("utf-8")
                + b"\n\n"
                for chunk in chunks
            )
            + b"data: [DONE]\n\n"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _config(bridge_port: int, helper: Path) -> str:
    return f'''model = "gpt-5.6-sol"
model_provider = "openai"
model_reasoning_effort = "max"

[model_providers.deepseek]
name = "DeepSeek fixture"
base_url = "http://127.0.0.1:{bridge_port}/v1"
wire_api = "responses"

[model_providers.deepseek.auth]
command = {json.dumps(sys.executable)}
args = [{json.dumps(str(helper))}]
timeout_ms = 5000

[features]
multi_agent = true

[features.multi_agent_v2]
enabled = true
hide_spawn_agent_metadata = false
tool_namespace = "agents"
max_concurrent_threads_per_session = 8

[agents]
enabled = true
max_concurrent_threads_per_session = 8
'''


def _run_codex(
    codex_bin: Path,
    home: Path,
    environment: dict[str, str],
    prompt: str,
    *,
    timeout: int,
    ephemeral: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [
            str(codex_bin),
            "--strict-config",
            "exec",
            "-m",
            "deepseek-v4-pro",
            "-c",
            'model_provider="deepseek"',
            "-c",
            'model_reasoning_effort="max"',
            "--skip-git-repo-check",
            "--json",
            "-s",
            "read-only",
            "-C",
            str(home),
            prompt,
        ]
    if ephemeral:
        command.insert(9, "--ephemeral")
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        timeout=timeout,
        check=False,
    )


def _parallel_spawn_batch_completed_before_wait(
    captured: list[dict[str, object]],
) -> bool:
    """Verify Codex executed one three-child spawn batch before its first wait."""

    for payload in captured:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            continue
        spawn_calls: list[tuple[int, str]] = []
        first_wait: int | None = None
        tool_outputs: dict[str, int] = {}
        for position, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "tool":
                call_id = message.get("tool_call_id")
                if isinstance(call_id, str):
                    tool_outputs[call_id] = position
                continue
            if message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            batch: list[tuple[int, str]] = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                call_id = tool_call.get("id")
                if name == "agents__spawn_agent" and isinstance(call_id, str):
                    batch.append((position, call_id))
                elif name == "agents__wait_agent" and first_wait is None:
                    first_wait = position
            if len(batch) == len(CHILD_TOKENS):
                spawn_calls = batch
        if len(spawn_calls) != len(CHILD_TOKENS) or first_wait is None:
            continue
        if all(
            spawn_position < tool_outputs.get(call_id, -1) < first_wait
            for spawn_position, call_id in spawn_calls
        ):
            return True
    return False


def _thread_store_snapshot(home: Path) -> list[dict[str, object]]:
    snapshots: list[dict[str, object]] = []
    for database in sorted(home.glob("state_*.sqlite")):
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute(
                "SELECT id, model_provider, model, reasoning_effort, agent_role, "
                "rollout_path, source, thread_source FROM threads ORDER BY created_at_ms"
            ).fetchall()
        for row in rows:
            record: dict[str, object] = {
                "id": row[0],
                "model_provider": row[1],
                "model": row[2],
                "reasoning_effort": row[3],
                "agent_role": row[4],
                "source": row[6],
                "thread_source": row[7],
                "rollout_events": [],
            }
            path = Path(str(row[5]))
            event_shapes: list[dict[str, object]] = []
            if path.is_file():
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = event.get("payload") if isinstance(event, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    shape: dict[str, object] = {"type": payload.get("type")}
                    for key in (
                        "id",
                        "thread_id",
                        "parent_thread_id",
                        "name",
                        "namespace",
                        "role",
                        "author",
                        "recipient",
                        "call_id",
                        "arguments",
                        "output",
                        "message",
                        "text",
                        "content",
                    ):
                        if key in payload:
                            shape[key] = payload.get(key)
                    source = payload.get("source")
                    if isinstance(source, dict):
                        shape["source"] = source
                    event_shapes.append(shape)
            record["rollout_events"] = event_shapes
            snapshots.append(record)
    return snapshots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--debug-thread-store", action="store_true")
    args = parser.parse_args(argv)
    codex_bin = Path(args.codex_bin).expanduser().resolve()
    if not codex_bin.is_file():
        print("codex bridge runtime: codex binary missing")
        return 2

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _DeepSeekFixture)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}/chat/completions"
    bridge = _BridgeServer(("127.0.0.1", 0), upstream_url)
    bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
    bridge_thread.start()
    original_codex_home = os.environ.get("CODEX_HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="codex-bridge-runtime-") as directory:
            home = Path(directory).resolve()
            os.environ["CODEX_HOME"] = str(home)
            helper = home / "fixture_credential.py"
            helper.write_text(
                "import sys\nsys.stdout.write(" + repr("sk-" + "runtime-fixture") + ")\n",
                encoding="utf-8",
                newline="\n",
            )
            (home / "config.toml").write_text(
                _config(bridge.server_address[1], helper),
                encoding="utf-8",
                newline="\n",
            )
            selection = ModelSelection(
                requested_model="deepseek-v4-pro",
                resolved_model="deepseek-v4-pro",
                reasoning_effort="max",
                effort_source="runtime_fixture",
            )
            for path, content in expected_agent_files(home / "agents", selection).items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            environment = dict(os.environ)
            environment["CODEX_HOME"] = str(home)
            completed = _run_codex(
                codex_bin,
                home,
                environment,
                f"Reply exactly {TOKEN} and nothing else.",
                timeout=60,
            )
            tool_captured = list(_DeepSeekFixture.captured)
            tool_authorizations = list(_DeepSeekFixture.authorizations)
            with _DeepSeekFixture.lock:
                _DeepSeekFixture.captured.clear()
                _DeepSeekFixture.authorizations.clear()
                _DeepSeekFixture.scenario = "fanout"
            fanout_completed = _run_codex(
                codex_bin,
                home,
                environment,
                "Run the deterministic three-role fan-out fixture.",
                timeout=120,
                ephemeral=False,
            )
            fanout_captured = list(_DeepSeekFixture.captured)
            fanout_authorizations = list(_DeepSeekFixture.authorizations)
            thread_store_snapshot = _thread_store_snapshot(home)
            with _DeepSeekFixture.lock:
                _DeepSeekFixture.captured.clear()
                _DeepSeekFixture.authorizations.clear()
                _DeepSeekFixture.scenario = "acceptance"
            deepseek_parent_config = _config(bridge.server_address[1], helper).replace(
                'model = "gpt-5.6-sol"\nmodel_provider = "openai"',
                'model = "deepseek-v4-pro"\nmodel_provider = "deepseek"',
                1,
            )
            (home / "config.toml").write_text(
                deepseek_parent_config,
                encoding="utf-8",
                newline="\n",
            )
            acceptance_evidence = run_native_acceptance(
                str(codex_bin),
                resolve_paths(str(home)),
                selection,
            )
            verify_native_evidence(
                acceptance_evidence,
                selection,
                {
                    "model_provider": "deepseek",
                    "model": "deepseek-v4-pro",
                    "reasoning_effort": "max",
                },
            )
            acceptance_authorizations = list(_DeepSeekFixture.authorizations)
    finally:
        if original_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = original_codex_home
        bridge.shutdown()
        bridge.server_close()
        upstream.shutdown()
        upstream.server_close()

    if completed.returncode != 0:
        print("codex bridge runtime: failed")
        if completed.stdout:
            print(completed.stdout[-8000:])
        if _CODEX_REQUESTS:
            print(json.dumps(_CODEX_REQUESTS[-1].get("tools"), ensure_ascii=False, indent=2))
        print(completed.stderr[-4000:])
        return 1
    if (
        TOKEN not in completed.stdout
        or "CODEX_BRIDGE_TOOL_OK" not in completed.stdout
        or len(tool_captured) < 2
    ):
        print("codex bridge runtime: response or upstream request missing")
        print(completed.stdout[-8000:])
        return 1
    payload = tool_captured[-1]
    if payload.get("model") != "deepseek-v4-pro":
        print("codex bridge runtime: wrong model")
        return 1
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        print("codex bridge runtime: tools were not translated")
        return 1
    messages = payload.get("messages")
    if not isinstance(messages, list):
        print("codex bridge runtime: translated messages missing")
        return 1
    assistants = [
        message
        for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    if not assistants or assistants[-1].get("reasoning_content") != "Use a harmless read-only tool.":
        print("codex bridge runtime: DeepSeek reasoning was not replayed")
        return 1
    summaries = [
        summary.get("text")
        for request_body in _CODEX_REQUESTS
        for item in (
            request_body.get("input")
            if isinstance(request_body.get("input"), list)
            else []
        )
        if isinstance(item, dict) and item.get("type") == "reasoning"
        for summary in (
            item.get("summary") if isinstance(item.get("summary"), list) else []
        )
        if isinstance(summary, dict)
        and summary.get("type") == "summary_text"
        and isinstance(summary.get("text"), str)
    ]
    if not summaries or any("Use a harmless read-only tool" in text for text in summaries):
        print("codex bridge runtime: safe reasoning summaries were not preserved")
        return 1
    if not any(
        isinstance(message, dict)
        and message.get("role") == "tool"
        and "CODEX_BRIDGE_TOOL_OK" in str(message.get("content"))
        for message in messages
    ):
        print("codex bridge runtime: Codex tool result was not returned upstream")
        return 1
    expected_authorization = "Bearer " + "sk-" + "runtime-fixture"
    if not tool_authorizations or any(
        value != expected_authorization for value in tool_authorizations
    ):
        print("codex bridge runtime: command-backed credential was not forwarded")
        return 1
    spawn = next(
        (
            tool.get("function", {})
            for tool in tools
            if isinstance(tool, dict)
            and isinstance(tool.get("function"), dict)
            and tool["function"].get("name") == "agents__spawn_agent"
        ),
        None,
    )
    properties = (
        spawn.get("parameters", {}).get("properties", {})
        if isinstance(spawn, dict)
        else {}
    )
    agent_type = properties.get("agent_type") if isinstance(properties, dict) else None
    role_description = agent_type.get("description") if isinstance(agent_type, dict) else None
    required_role_text = (
        "default:",
        "worker:",
        "explorer:",
        "deepseek-v4-pro",
        "reasoning effort is set to `max`",
    )
    if not isinstance(role_description, str) or any(
        fragment not in role_description for fragment in required_role_text
    ):
        print("codex bridge runtime: V2 agent_type roles were not exposed")
        print(json.dumps(spawn, ensure_ascii=False, indent=2))
        return 1

    if fanout_completed.returncode != 0 or FANOUT_TOKEN not in fanout_completed.stdout:
        print("codex bridge runtime: native V2 fan-out failed")
        print(fanout_completed.stdout[-12000:])
        if _CODEX_REQUESTS:
            print(json.dumps(_CODEX_REQUESTS[-1].get("input"), ensure_ascii=False, indent=2))
        print(fanout_completed.stderr[-4000:])
        return 1
    if not fanout_authorizations or any(
        value != expected_authorization for value in fanout_authorizations
    ):
        print("codex bridge runtime: fan-out credential forwarding failed")
        return 1
    if not acceptance_authorizations or any(
        value != expected_authorization for value in acceptance_authorizations
    ):
        print("codex bridge runtime: native acceptance credential forwarding failed")
        return 1
    if any(payload.get("model") != "deepseek-v4-pro" for payload in fanout_captured):
        print("codex bridge runtime: a fan-out request used the wrong model")
        return 1
    role_systems: set[str] = set()
    role_tasks: set[str] = set()
    for fanout_payload in fanout_captured:
        fanout_messages = fanout_payload.get("messages")
        if not isinstance(fanout_messages, list):
            continue
        rendered_messages = json.dumps(fanout_messages, ensure_ascii=False)
        payload_roles: set[str] = set()
        for message in fanout_messages:
            if not isinstance(message, dict) or message.get("role") != "system":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            for role in CHILD_TOKENS:
                if f"Codex's {role} child agent" in content:
                    payload_roles.add(role)
        role_systems.update(payload_roles)
        if payload_roles and "gAAAA" in rendered_messages:
            print("codex bridge runtime: encrypted child task reached DeepSeek")
            return 1
        for role in payload_roles:
            if CHILD_TOKENS[role] in rendered_messages:
                role_tasks.add(role)
    if role_systems != set(CHILD_TOKENS):
        print("codex bridge runtime: not every DeepSeek role executed")
        print(sorted(role_systems))
        for fanout_payload in fanout_captured:
            rendered_messages = json.dumps(
                fanout_payload.get("messages"), ensure_ascii=False
            )
            print(rendered_messages[-4000:])
        for request_body in _CODEX_REQUESTS:
            rendered_input = json.dumps(request_body.get("input"), ensure_ascii=False)
            if "encrypted_content" in rendered_input:
                print(rendered_input[-5000:])
        return 1
    if role_tasks != set(CHILD_TOKENS):
        print("codex bridge runtime: not every DeepSeek role received its plaintext task")
        print(sorted(role_tasks))
        return 1
    if not _parallel_spawn_batch_completed_before_wait(fanout_captured):
        print("codex bridge runtime: children were not spawned before the first wait")
        for fanout_payload in fanout_captured:
            rendered_messages = json.dumps(
                fanout_payload.get("messages"), ensure_ascii=False
            )
            if "agents__spawn_agent" in rendered_messages:
                print(rendered_messages[-8000:])
        return 1
    parent_rows = [row for row in thread_store_snapshot if row.get("agent_role") is None]
    child_rows = [row for row in thread_store_snapshot if isinstance(row.get("agent_role"), str)]
    child_roles = {str(row.get("agent_role")) for row in child_rows}
    if len(parent_rows) != 1 or len(child_rows) != 3 or child_roles != set(CHILD_TOKENS):
        print("codex bridge runtime: V2 thread-store topology is incomplete")
        print(json.dumps(thread_store_snapshot, ensure_ascii=False, indent=2))
        return 1
    parent_id = str(parent_rows[0].get("id"))
    for row in child_rows:
        role = str(row.get("agent_role"))
        source = str(row.get("source"))
        events = row.get("rollout_events")
        event_list = events if isinstance(events, list) else []
        final_messages = {
            event.get("message")
            for event in event_list
            if isinstance(event, dict)
            and event.get("type") == "agent_message"
            and isinstance(event.get("message"), str)
        }
        event_types = {
            event.get("type") for event in event_list if isinstance(event, dict)
        }
        if (
            parent_id not in source
            or CHILD_TOKENS[role] not in final_messages
            or "task_complete" not in event_types
        ):
            print("codex bridge runtime: a V2 child did not complete authoritatively")
            print(json.dumps(row, ensure_ascii=False, indent=2))
            return 1
        if role == "explorer" and not any(
            isinstance(event, dict)
            and event.get("type") == "function_call"
            and event.get("name") == "shell_command"
            for event in event_list
        ):
            print("codex bridge runtime: explorer did not execute its read tool")
            return 1
    print(
        "codex bridge runtime: ok "
        f"({len(tools)} translated tools, {len(CHILD_TOKENS)} native V2 fan-out children, "
        f"{len(acceptance_evidence.child_thread_ids)} lifecycle children)"
    )
    if args.debug_thread_store:
        print(json.dumps(thread_store_snapshot, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
