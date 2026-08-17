[简体中文](./README.md) | **English**

<p align="center">
  <img src="./assets/readme/hero.png" width="100%" alt="Multi Relay routes between Codex, Claude Code, target pools, a local gateway, and multiple providers">
</p>

<h1 align="center">Multi Relay</h1>

<p align="center">Route Codex and Claude Code through one local gateway across multiple models, providers, and credential-backed target pools.</p>

Multi Relay puts four facts that must move together into an `ExecutionTarget`: provider, protocol, model, and credential reference. An agent selects a pool; the pool selects a target. A model switch therefore changes to the matching key at the same time, never sends provider A's credential to provider B, and never lets an ambiguous model name silently redefine the authentication boundary.

## Support matrix

| Capability | Codex | Claude Code |
| --- | --- | --- |
| Managed agent install | User `config.toml`, `AGENTS.md`, and Agent TOML | User or project `.claude/agents/*.md` |
| Parent request | Keeps the existing Codex parent model; managed HTTP targets use the gateway | Must start through the launcher; parent and child requests use the gateway |
| Child agent | Native Codex target or gateway pool | Gateway pool |
| Protocols | Responses, Chat Completions, DeepSeek Chat, native Codex | Anthropic Messages, with gateway adaptation to other protocols |
| Platforms | Windows, macOS, Linux | Windows, macOS, Linux; Claude Code must already exist |
| Safe disable/uninstall | Yes | Yes |

Claude Code must start through the launcher. The launcher injects only a loopback URL and a short-lived gateway token into that process, so the Claude Code parent request gets the same rotation, cooldown, credential isolation, and committed-response boundary as its agents. It never permanently exports an upstream API key.

## Five-minute start

Requirements: Python 3.11+; Codex for the Codex host and Claude Code for the Claude Code host. Install the Skill first:

```bash
npx skills add Roblis0n/multi-relay -g -y
```

From the repository, inspect state and install the default hybrid catalog:

```bash
python scripts/multi_relay.py status --json
python scripts/multi_relay.py setup --preset hybrid --host all --json
python scripts/multi_relay.py test --host all --json
```

When a credential is required, the CLI opens a local masked prompt. Never place a key in chat or a command. For native Codex targets only:

```bash
python scripts/multi_relay.py setup --preset native --host codex --json
```

Start Claude Code:

```bash
python scripts/multi_relay.py host apply claude-code --json
python scripts/multi_relay.py launch claude-code --project . -- --help
```

Arguments after `--` pass through to Claude Code. Daily use must also go through `multi-relay launch claude-code`; do not permanently set an upstream key or provider base URL.

## One cross-provider target pool

Every URL and model ID below is fake. The targets intentionally declare different capabilities: the DeepSeek target supports text and tools, the Anthropic Messages target supports text only, and the OpenAI-compatible target supports text, tools, and vision.

```bash
python scripts/multi_relay.py provider add --id deepseek-example --name "DeepSeek Example" --protocol deepseek-chat --base-url https://deepseek.example/v1 --auth vault --capability text --capability tool_calling --json
python scripts/multi_relay.py provider add --id anthropic-example --name "Anthropic Example" --protocol anthropic-messages --base-url https://anthropic.example/v1 --auth vault --capability text --json
python scripts/multi_relay.py provider add --id openai-example --name "OpenAI-compatible Example" --protocol responses-compatible --base-url https://responses.example/v1 --auth vault --capability text --capability tool_calling --capability vision --json

python scripts/multi_relay.py credential add --provider deepseek-example --id primary --label "DeepSeek primary" --json
python scripts/multi_relay.py credential add --provider anthropic-example --id primary --label "Anthropic primary" --json
python scripts/multi_relay.py credential add --provider openai-example --id primary --label "Responses primary" --json

python scripts/multi_relay.py target add --id deepseek-text-tools --provider deepseek-example --model reasoner-example --credential primary --capability text --capability tool_calling --host codex --host claude-code --json
python scripts/multi_relay.py target add --id anthropic-text --provider anthropic-example --model messages-example --credential primary --capability text --host codex --host claude-code --json
python scripts/multi_relay.py target add --id openai-vision-tools --provider openai-example --model responses-example --credential primary --capability text --capability tool_calling --capability vision --host codex --host claude-code --json

python scripts/multi_relay.py pool add --id cross-provider --target deepseek-text-tools --target anthropic-text --target openai-vision-tools --strategy sticky --capability text --host codex --host claude-code --json
python scripts/multi_relay.py agent set --name pooled-worker --description "Cross-provider worker" --pool cross-provider --capability text --host codex --host claude-code --sandbox-mode workspace-write --instructions "Complete only the assigned bounded task." --json
```

One provider can have multiple credentials. Add a `backup` credential and create a second target to give two keys for the same model independent counters, cooldowns, and enabled states.

## Sticky, timed, and failure switching

Use `sticky` when behavioral consistency matters: the current target stays selected until manual rotation, disablement, or a failover-eligible error before the response commits.

```bash
python scripts/multi_relay.py pool strategy cross-provider sticky --json
python scripts/multi_relay.py pool rotate cross-provider --json
```

Use `timed` to distribute quota or cost on a schedule: after expiry, the next selection moves to the next eligible target.

```bash
python scripts/multi_relay.py pool strategy cross-provider timed --duration 30m --json
python scripts/multi_relay.py pool status cross-provider --json
```

Before visible output starts, failover is allowed for exhausted quota, rate limits, invalid authentication, unavailable models, provider or transport unavailability, and protocol-response errors. Invalid authentication also disables that credential. Invalid requests, exceeded context, policy blocks, user cancellation, and no eligible target do not switch models because replaying the same request cannot safely repair them.

The response commits at the first text delta, tool-call start, or other visible content. A later stream failure terminates with a structured error; the gateway never asks another model to continue. Another model does not have the identical hidden state, sampling path, or tool context, so splicing its continuation would fabricate continuity that cannot be proven.

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="Multi Relay may rotate before a response commits and terminates errors after commitment">
</p>

## Architecture

<p align="center">
  <img src="./assets/readme/architecture.svg" width="100%" alt="Codex and Claude Code connect through a local Multi Relay gateway, target pool, and operating-system vault to multiple providers">
</p>

- Secret-free `catalog.json` defines providers, credential references, targets, pools, agents, and hosts.
- The loopback gateway listens only on `127.0.0.1` and presents unified Responses and Anthropic Messages host surfaces.
- Protocol adapters convert at the canonical request/event layer and do not read upstream secrets from the environment.
- The vault reads a target's credential reference only immediately before an upstream request.
- Capabilities such as `vision`, `audio`, `tool_calling`, and `server_web_search` must be satisfied by the provider, target, pool, and agent; a networked agent also needs a real MCP/tool configuration.

See [catalog model](references/catalog.md), [rotation and commit boundary](references/rotation.md), [Codex](references/codex.md), and [Claude Code](references/claude-code.md).

## CLI index

```text
status | catalog | setup | apply | repair | test
provider list|add|edit|discover-models|test|enable|disable|remove
credential list|add|replace|test|enable|disable|remove
target list|add|edit|test|enable|disable|remove
pool list|add|edit|order|strategy|rotate|reset|status|remove
agent list|set|remove
host list|apply|status
gateway start|status|stop
route | launch claude-code | disable | enable | uninstall
```

Every command supports the stable `--json` result envelope. Inspect exact parameters for an action:

```bash
python scripts/multi_relay.py --help
python scripts/multi_relay.py target add --help
python scripts/multi_relay.py pool add --help
python scripts/multi_relay.py launch claude-code --help
```

Lifecycle examples:

```bash
python scripts/multi_relay.py disable --host all --json
python scripts/multi_relay.py enable --host all --json
python scripts/multi_relay.py uninstall --host all --json
python scripts/multi_relay.py uninstall --host all --remove-credentials --json
```

A normal uninstall keeps vault credentials. Only explicit `--remove-credentials` deletes managed credentials.

## Security model and state paths

Upstream credentials live only in Windows Credential Manager, macOS Keychain, or Linux Secret Service. They are never written to the catalog, manifest, host configuration, Agent files, command arguments, logs, errors, backups, temporary files, or Git. The catalog contains only non-secret references such as `multi-relay/provider-id/credential-id`.

Product state no longer uses Codex Home as its root:

| Platform | Default state directory |
| --- | --- |
| Windows | `%LOCALAPPDATA%\multi-relay` |
| macOS | `~/Library/Application Support/multi-relay` |
| Linux | `${XDG_STATE_HOME:-~/.local/state}/multi-relay` |

Writes use locks, validation before mutation, same-directory atomic replacement, file hashes, and rollback-capable backups. See the [security model](references/security.md).

## Compatibility migration

New state wins. Old `$CODEX_HOME/codex-multi-relay`, `$CODEX_HOME/codex-deepseek-relay`, and `$CODEX_HOME/codex-deepseek-subagent` locations, former markers, and former credential targets are migration inputs only. Multi Relay copies, verifies, switches, and cleans them up only when a manifest, managed markers, and hashes prove ownership. Divergent old and new state returns `state_conflict`; it is never silently merged. Migration and repair are idempotent.

See [compatibility and migration](references/compatibility.md).

## Limits and troubleshooting

- Multi Relay cannot grant a capability that a model did not declare or that no real tool provides.
- It does not continue across models after commit; retry the complete request or ask the user how to proceed.
- Claude Code started without the launcher is outside Multi Relay management.
- A native Codex target is Codex-only; a Claude Code pool must contain an HTTP target.
- `no_eligible_target`: inspect host, capabilities, target/credential enabled state, and cooldowns.
- `state_conflict`: preserve both sides, compare manifests/catalogs, then select the trusted migration source.
- `gateway_port_conflict`: verify that no foreign or stale process owns `127.0.0.1:42137`.
- `credential_missing` / `auth_invalid`: use `credential replace` to enter it locally; never send it in chat.

## Development validation

The suite uses fake upstreams and needs no real provider credential:

```bash
python -m unittest discover -s scripts -p "test_*.py"
python -m compileall -q scripts
python scripts/check_runtime_contract.py
python scripts/check_public_contract.py
```

Real Codex/Claude Code smoke tests are optional local checks, not part of the offline unit suite. The project uses the [MIT License](./LICENSE) and is an independent community tool with no official affiliation or endorsement from OpenAI, Anthropic, DeepSeek, or any other provider. Read the [complete release notes](./RELEASE_NOTES.md) or visit the [GitHub repository](https://github.com/Roblis0n/multi-relay).
