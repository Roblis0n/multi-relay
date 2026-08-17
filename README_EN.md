[简体中文](./README.md) | English

<p align="center">
  <img src="./assets/readme/hero.png" width="100%" alt="Codex parent routes work to multiple child-agent models while retaining web, vision, audio, and high-risk boundaries">
</p>

<h1 align="center">Multi Relay</h1>

<p align="center">Choose the right provider and model for each Codex child agent while preserving explicit parent capability boundaries.</p>

A secret-free `catalog.json` now defines each provider and child agent independently: protocol, model, capabilities, priority, trust, sandbox, MCP servers, and skills. The default hybrid catalog routes `default`, `worker`, and `explorer` to the verified `deepseek-v4-pro` model while keeping a high-trust native Codex `reviewer`; the parent task stays on the user's original model.

## What you get

- Supports `codex-native`, `responses-compatible`, `chat-completions-compatible`, and `deepseek-chat` providers.
- Adds and removes providers, creates named agents, changes models, and resolves routes through one CLI.
- Routes against `text`, `tools`, `vision`, `audio`, `web`, and `high-risk` boundaries; when no complete match exists, it returns `parent_required`.
- Allows up to 8 concurrent children by default, only for genuinely independent work.
- Keeps `multi_agent_v2` and pins the actual child with explicit `agent_type` plus `fork_turns="none"`.
- Converts Chat Completions to Responses through a loopback-only service at `127.0.0.1:42137`; Responses-compatible providers connect directly.
- Emits an exact `[Relay task: <target>]` handoff before delegation, preventing protected host ciphertext from leaking to an external provider.
- Stores vault credentials only in Windows Credential Manager or macOS Keychain, isolated by provider.
- Parses, validates, and transactionally writes every catalog and configuration change, with rollback on failure.
- Allows catalog edits while disabled without silently re-enabling generated agents.

<p align="center">
  <img src="./assets/readme/architecture.svg" width="100%" alt="Architecture: Codex capability routing selects native Codex, Responses, or Chat Completions child agents">
</p>

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="Validation and rollback flow for a multi-model catalog, including parent boundaries for vision, audio, web, and high-risk work">
</p>

## Quick start

Requirements: Windows or macOS, Python 3.11+, and the Codex desktop runtime. Only catalogs using a vault-backed provider need that provider's API key; a native-only catalog needs no extra credential.

To install as a Skill:

```bash
npx skills add Roblis0n/multi-relay -g -y
```

You can also double-click `configure-multi-relay.cmd` in the project directory to install the default hybrid catalog.

Windows terminal:

```powershell
python scripts\multi_relay.py setup --preset hybrid
```

macOS:

```bash
python3 scripts/multi_relay.py setup --preset hybrid
```

For native Codex child agents only:

```bash
python3 scripts/multi_relay.py setup --preset native
```

`native` reads no provider credential and makes no provider network request. `hybrid` displays a locally masked prompt; never paste a key into chat. It returns `ready` only after model, compatibility, and native acceptance checks pass. A failed check restores the previous configuration.

## After installation

The manager writes `$CODEX_HOME/codex-multi-relay/catalog.json` and generates Agent TOML from it. The default `hybrid` catalog creates:

```text
$CODEX_HOME/agents/default.toml
$CODEX_HOME/agents/worker.toml
$CODEX_HOME/agents/explorer.toml
$CODEX_HOME/agents/reviewer.toml
```

Protocol mapping:

| Protocol | Path | Authentication |
| --- | --- | --- |
| `codex-native` | Native Codex provider | Codex login |
| `responses-compatible` | Direct provider Responses API | Vault or none |
| `chat-completions-compatible` | Local conversion to Chat Completions | Vault or none |
| `deepseek-chat` | DeepSeek adapter with reasoning continuation | Vault |

It also writes a removable capability-routing rule into `$CODEX_HOME/AGENTS.md` and guarantees:

- The top-level main model, main provider, and main reasoning effort stay unchanged.
- The concurrency floor is 8, and a higher value the user already has is preserved.
- Every subagent explicitly uses `agent_type` and `fork_turns="none"` (or a positive partial-turn count for local context).
- Before every spawn, follow-up, or send, the parent emits a `[Relay task: <target>]` handoff block matched one-to-one to its target; the adapter strictly rejects calls without one.
- The official model directory is never replaced.
- The newer multi-agent mode is never disabled.
- Missing capabilities or trust keep work in the parent; no route silently changes provider or model.

## Day-to-day use

Once configured, there is no need to run setup again. Just give Codex a normal task:

```text
Investigate these four independent modules in parallel, then produce a combined conclusion.
```

The managed rule checks capabilities before it fans out; shared state, writes to the same file, and sequential dependencies remain serial in the main agent.

Codex protects native child-agent messages before the local tool layer, and custom providers cannot decrypt that host ciphertext. The managed rule therefore writes the same complete task into parent commentary as a visible `[Relay task]` block before invoking the native child tool. The adapter requires an exact target and order match; otherwise it fails instead of asking an external provider to guess the task.

`vision`, `audio`, and `web` remain parent capabilities by default. A child must declare every requested capability, a web child must include a real MCP server, and `high-risk` work requires `trust=high` plus final parent verification.

## Management commands

The examples below use Windows; on macOS replace `python` with `python3` and use `/` path separators:

```powershell
python scripts\multi_relay.py status --json
python scripts\multi_relay.py setup --preset hybrid --json
python scripts\multi_relay.py setup --preset native --json
python scripts\multi_relay.py catalog --json
python scripts\multi_relay.py apply --json
python scripts\multi_relay.py provider list --json
python scripts\multi_relay.py provider add --id vendor --name Vendor --protocol responses-compatible --base-url https://api.vendor.example/v1 --auth vault --capability text --capability tools --context-window 128000 --json
python scripts\multi_relay.py provider remove vendor --json
python scripts\multi_relay.py agent list --json
python scripts\multi_relay.py agent set --name vendor-worker --description "Vendor worker" --provider vendor --model vendor-model --capability text --capability tools --instructions "Implement the assigned bounded task." --json
python scripts\multi_relay.py agent remove vendor-worker --json
python scripts\multi_relay.py route --capability text --capability tools --json
python scripts\multi_relay.py test --json
python scripts\multi_relay.py repair --json
python scripts\multi_relay.py disable --json
python scripts\multi_relay.py enable --json
python scripts\multi_relay.py uninstall --json
python scripts\multi_relay.py uninstall --remove-credential --json
```

- A plain uninstall keeps the Key.
- Only uninstall with `--remove-credential` deletes the system credential.
- `provider remove` refuses a provider still referenced by an agent.
- `repair` preserves the current catalog; repeated setup does not reset custom providers or agents.
- Catalog changes while disabled stay disabled until `enable` regenerates agents and routing instructions.
- If Codex auto-discovery fails, use `CODEX_DESKTOP_BIN` to point to the desktop runtime.

## Safety and rollback

The manager uses process locks, parse-before-write, atomic replacement within the same directory, and per-file checksums. Backups live at:

```text
$CODEX_HOME/codex-multi-relay/backups/
```

Keys never enter configuration, command arguments, temporary files, backups, logs, exceptions, or Git. The old `$CODEX_HOME/codex-deepseek-relay` and `$CODEX_HOME/codex-deepseek-subagent` locations are migration sources only. Without a legacy manifest, managed markers, and matching checksums proving ownership, migration returns `conflict` rather than adopting user content.

See [Compatibility and safety boundaries](references/compatibility.md) and [Skill execution rules](SKILL.md) for more details.

## Development validation

```bash
python -m unittest discover -s scripts -p "test_*.py" -v
python -m compileall -q scripts
python scripts/check_runtime_contract.py
python scripts/check_codex_bridge_runtime.py --codex-bin <path-to-codex>
```

## Brand note

This project is an independent community tool with no affiliation, partnership, or official endorsement relationship with OpenAI, DeepSeek, or any other provider. Related names and marks belong to their respective owners.

## License

[MIT](./LICENSE)
