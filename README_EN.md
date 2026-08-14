[简体中文](./README.md) | English

<p align="center">
  <img src="./assets/readme/hero.png" width="100%" alt="Codex parent tasks fan out natively through a local Relay into eight DeepSeek subagents">
</p>

<h1 align="center">Codex DeepSeek Relay</h1>

<p align="center">Route Codex's native subagents to DeepSeek while keeping task handoffs and execution trails auditable.</p>

A local loopback adapter converts Codex Responses into DeepSeek Chat Completions, routing all three built-in roles — `default`, `worker`, and `explorer` — to the online-verified `deepseek-v4-pro` model, with 8-way concurrent fan-out enabled by default. The main task keeps running on the original OpenAI model with maximum reasoning effort.

## What you get

- Two or more independent tasks can be dispatched in parallel like fan-out subagents.
- All three built-in subagent roles use DeepSeek.
- DeepSeek reasoning effort starts from its highest tier `max` and is measured across `max → xhigh → high → medium → low → minimal`.
- All three subagent roles declare DeepSeek V4 Pro's official 1-million-token context window, avoiding unknown-model fallback values that are too small.
- Keeps the newer `multi_agent_v2`, selects DeepSeek roles through explicit `agent_type`, and keeps subagents from inheriting Sol.
- The local loopback adapter converts Codex Responses into DeepSeek Chat Completions, supporting namespaced tools, parallel tool calls, and reasoning-mode continuation.
- The parent agent shows a complete, structured task handoff before calling DeepSeek, and the adapter matches it exactly against the child-thread target, so host ciphertext is never mistakenly sent to DeepSeek.
- Child-thread UIs show a safe step summary generated from real tool calls; the model's raw private chain-of-thought is never fabricated or exposed directly.
- The Key is stored only in Windows Credential Manager or macOS Keychain.
- Providers are verified in isolation before installation, and after installation a real Codex parent model performs full native acceptance; any failure rolls back automatically.
- It can be disabled, enabled, or fully uninstalled at any time.

<p align="center">
  <img src="./assets/readme/architecture.svg" width="100%" alt="Architecture: Codex parent tasks pass through a protected, visible handoff into a local Relay, which fans out natively into eight DeepSeek subagents across the default, worker, and explorer roles">
</p>

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="Validation and rollback flow for DeepSeek subagent installation: credential write, model probe, transactional install, and native acceptance, with automatic rollback on any failure">
</p>

## Quick start

Requirements: Windows or macOS, Python 3.11+, the Codex desktop runtime, and a valid DeepSeek API Key.

To install as a Skill:

```bash
npx skills add Roblis0n/codex-deepseek-relay -g -y
```

You can also double-click `configure-deepseek-subagents.cmd` directly in the project directory. It locates a working Python on its own and shows clear prompts for entering the Key.

Windows terminal:

```powershell
python codex-deepseek-subagent\scripts\codex_deepseek.py setup
```

macOS:

```bash
python3 codex-deepseek-subagent/scripts/codex_deepseek.py setup
```

The command shows a locally masked input field in the current terminal. Do not send the Key to ChatGPT; once entered, it is saved to the system credential target `codex-deepseek-api-key`.

`ready` is returned only after all of these pass: the model actually exists, the highest compatible reasoning tier, isolated provider probing, a formal single-agent run, three-way concurrency, tool calls, continuation, and thread metadata. Full subagent acceptance runs exactly once and uses the real Codex parent model; on failure, the transaction restores the previous configuration. If the server does not currently offer `deepseek-v4-pro`, the program returns `model_unavailable` and the official Codex configuration stays unchanged.

## After installation

The manager adds one user-level DeepSeek provider. Codex connects to the local `http://127.0.0.1:42137/v1`; the adapter listens only on the loopback address and converts Responses into DeepSeek Chat Completions. It then creates:

```text
$CODEX_HOME/agents/default.toml
$CODEX_HOME/agents/worker.toml
$CODEX_HOME/agents/explorer.toml
```

It also writes a removable fan-out rule into `$CODEX_HOME/AGENTS.md` and guarantees:

- The top-level main model, main provider, and main reasoning effort stay unchanged.
- The concurrency floor is 8, and a higher value the user already has is preserved.
- Every subagent explicitly uses `agent_type` and `fork_turns="none"` (or a positive partial-turn count for local context), so it can never accidentally inherit the main model.
- Before every spawn, follow-up, or send, the parent emits a `[DeepSeek task: <target>]` handoff block matched one-to-one to its target; the adapter strictly rejects calls without a matching handoff.
- The official model directory is never replaced.
- The newer multi-agent mode is never disabled.
- A failed subagent never silently falls back to the OpenAI model.

## Day-to-day use

Once configured, there is no need to run setup again. Just give Codex a normal task:

```text
Investigate these four independent modules in parallel, then produce a combined conclusion.
```

The managed rule fans out only when tasks are genuinely independent; shared state, writes to the same file, and sequential dependencies remain serial in the main agent.

Codex's OpenAI parent model turns the task body into protected `gAAAA…` content before `spawn_agent` is visible locally, and custom providers have no official decryption interface. The managed rule therefore first writes the same complete task into the parent task commentary as a visible handoff block, then invokes the native subagent tools. The adapter accepts handoffs only when the target and order match exactly; when none matches, it returns an error instead of letting DeepSeek guess the task from ciphertext.

DeepSeek's raw reasoning content is used only for tool continuation inside the same child thread and is stored as integrity-protected ciphertext. The "Thinking/Steps" shown in the UI is a safe summary the adapter generates from tool calls that were actually issued — for example, "checks local state and runs validation" — not a verbatim transcript of the model's private chain-of-thought.

## Management commands

The examples below use Windows; on macOS replace `python` with `python3` and use `/` path separators:

```powershell
python codex-deepseek-subagent\scripts\codex_deepseek.py status --json
python codex-deepseek-subagent\scripts\codex_deepseek.py setup --json
python codex-deepseek-subagent\scripts\codex_deepseek.py test --json
python codex-deepseek-subagent\scripts\codex_deepseek.py repair --json
python codex-deepseek-subagent\scripts\codex_deepseek.py disable --json
python codex-deepseek-subagent\scripts\codex_deepseek.py enable --json
python codex-deepseek-subagent\scripts\codex_deepseek.py uninstall --json
python codex-deepseek-subagent\scripts\codex_deepseek.py uninstall --remove-credential --json
```

- A plain uninstall keeps the Key.
- Only uninstall with `--remove-credential` deletes the system credential.
- `repair` is equivalent to re-running setup with the full verification.
- If Codex auto-discovery fails, use `CODEX_DESKTOP_BIN` to point to the desktop runtime.

## Safety and rollback

The manager uses process locks, parse-before-write, atomic replacement within the same directory, and per-file checksums. Backups live at:

```text
$CODEX_HOME/codex-deepseek-subagent/backups/
```

The Key never enters configuration, command arguments, temporary files, backups, logs, exceptions, or Git. Legacy single-role installations are migrated automatically only when the manifest, managed markers, and checksums jointly prove ownership.

See [Compatibility and safety boundaries](codex-deepseek-subagent/references/compatibility.md) and [Skill execution rules](codex-deepseek-subagent/SKILL.md) for more details.

## Development validation

```bash
python -m unittest discover -s scripts -p "test_*.py" -v
python -m compileall -q codex-deepseek-subagent/scripts scripts
python scripts/check_runtime_contract.py
python scripts/check_codex_bridge_runtime.py --codex-bin <path-to-codex>
```

## Brand note

This project is an independent community tool with no affiliation, partnership, or official endorsement relationship with OpenAI or DeepSeek. ChatGPT, OpenAI, DeepSeek, and their logos belong to their respective owners.

## License

[MIT](./LICENSE)