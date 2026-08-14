# Codex DeepSeek Fan-Out Design

## Status

Approved design basis from the conversation on 2026-08-14. This document records the exact behavior to be implemented before any live Codex configuration is changed.

## Goal

Keep the primary Codex session on the user's selected OpenAI model and reasoning level while routing Codex's built-in spawned agents through DeepSeek. Enable bounded parallel fan-out so the primary agent can delegate independent work to multiple DeepSeek children, wait for them, and synthesize the results.

The current primary settings are `gpt-5.6-sol` with `max` reasoning. They must remain unchanged.

## Requested Defaults

- Requested DeepSeek model identifier: `deepseek-v4-pro`. The user's `deepseel-v4-pro` spelling is treated as a typo.
- Built-in child roles routed to DeepSeek: `default`, `worker`, and `explorer`.
- Maximum open child threads per primary session: 8, excluding the primary thread.
- Child reasoning: the highest level that the selected DeepSeek model and the current Codex client both advertise and accept.
- Fan-out: automatic for two or more independent, bounded tasks; never for overlapping writes to the same files.
- Authentication: API key stored in the operating-system credential store, never in TOML, Markdown, command history, process arguments, logs, or the repository.
- Failure policy: report the DeepSeek failure to the primary agent. Do not silently substitute an OpenAI child model and do not downgrade the entire Codex multi-agent runtime.

## Non-Goals

- Do not change the primary `model`, `model_provider`, or `model_reasoning_effort`.
- Do not force `multi_agent_version = "v1"`.
- Do not disable the current multi-agent runtime.
- Do not replace the live Codex model catalog. An isolated temporary catalog may be generated only inside the disposable compatibility-test home; if the live client cannot use the supported custom-provider and custom-agent path without a catalog override, setup fails before installation.
- Do not claim to override arbitrary future plugin agents that explicitly pin their own model or provider. This design covers the built-in `default`, `worker`, and `explorer` roles and the managed fan-out policy.
- Do not provide unbounded concurrency. The default cap is 8 and remains configurable to accommodate provider rate limits.

## Architecture

The primary session remains the orchestrator:

1. `gpt-5.6-sol + max` receives the user's request.
2. A managed fan-out instruction determines whether the request contains at least two independent bounded tasks.
3. Codex spawns one or more built-in roles (`default`, `worker`, or `explorer`) with explicit `agent_type` and non-full-history `fork_turns`, so V2 selects the role instead of inheriting the Sol parent.
4. User-level custom-agent files with those same names override the built-in role configurations and select the DeepSeek provider and model.
5. A loopback-only adapter translates Codex Responses requests to DeepSeek Chat Completions and converts streaming text, reasoning, and tool calls back to Responses events.
6. DeepSeek children execute under the permission and sandbox policy of the parent turn.
7. The primary session waits for all requested children, verifies their evidence or changes, and produces the final result.

This separates orchestration quality from worker throughput: the OpenAI primary agent owns decomposition, conflict avoidance, validation, and final decisions; DeepSeek owns bounded delegated work.

## Managed Configuration

### User-level provider

The installer adds a marked, managed `[model_providers.deepseek]` block to the user-level Codex `config.toml`. Codex still sees a Responses provider, but its base URL is the loopback adapter at `http://127.0.0.1:42137/v1`. The adapter forwards translated Chat Completions requests to the official DeepSeek endpoint. Authentication remains command-backed and the adapter is started on demand by the credential helper.

The provider block does not set the top-level `model_provider`; therefore the primary session continues using OpenAI.

### Multi-agent settings

The installer ensures the following effective behavior while preserving and recording prior values:

- multi-agent collaboration is enabled;
- `features.multi_agent_v2.enabled` is true, spawn metadata remains visible, and the tool namespace is `agents`;
- `agents.enabled` is enabled;
- `agents.max_concurrent_threads_per_session` is 8 by default;
- no legacy `multi_agent_v2 = false` setting is introduced.

The concurrency value is exposed as a setup option so the user can lower it if the provider rate limit requires it.

### Built-in role overrides

The installer manages three standalone files under the user-level Codex agents directory:

- `default.toml`
- `worker.toml`
- `explorer.toml`

Each file declares the matching built-in role name, `model_provider = "deepseek"`, the validated DeepSeek model identifier, the resolved maximum supported reasoning level, a focused description, and role-specific developer instructions.

Role responsibilities:

- `explorer`: read-heavy repository mapping, search, triage, log analysis, and evidence gathering.
- `worker`: isolated implementation or verification work with explicit file ownership.
- `default`: bounded general work when neither specialized role fits.

The role files must not grant broader permissions than the parent turn.

### Fan-out policy

A marked managed section is added to the applicable user-level Codex instruction file. It directs the primary agent to:

- fan out when at least two tasks are independent and parallel work materially reduces elapsed time;
- use one child per clearly bounded work item;
- run at most eight child threads at once;
- prefer `explorer` for read-heavy work and `worker` for isolated implementation;
- explicitly set `agent_type` and use `fork_turns="none"` or a positive partial-turn count for every managed spawn;
- never use full-history inheritance for a managed DeepSeek child;
- never assign concurrent writes to overlapping files or shared mutable state;
- wait for all requested children before consolidation;
- verify evidence, tests, and file changes before accepting results;
- keep trivial or tightly sequential tasks in the primary thread;
- respect any more specific user, project, or skill instruction.

The managed section is removable without replacing unrelated user instructions.

## Model and Reasoning Resolution

`deepseek-v4-pro` is a requested identifier, not an assumed valid identifier. Setup performs validation before changing live configuration:

1. Query or probe the provider using the supplied credential.
2. Confirm that the exact model identifier is accepted.
3. Inspect the model definition and current Codex client's accepted reasoning values.
4. Start with DeepSeek's requested maximum and select the deepest mutually supported value in descending order: `max`, `xhigh`, `high`, `medium`, `low`, `minimal`.
5. If the provider does not expose a compatible reasoning control but the model itself is a reasoning model, omit the Codex reasoning key and use the model's provider-side default. Report this explicitly in `status`.
6. If the model or protocol is unsupported, stop before installation and leave live Codex files unchanged.

No unsupported reasoning value is written merely to display the word "max".

## API Key Flow

The user never pastes the API key into this chat or edits it into `config.toml`.

The completed manager exposes a user-facing credential command and a masked interactive setup prompt. On Windows, the key is stored in Windows Credential Manager under the managed target `codex-deepseek-api-key`. The command-backed provider authentication helper prints the secret only to Codex over its stdout contract when Codex requests a token.

The final handoff will give the user one exact command to run. That command opens the masked prompt, stores the key, validates the requested model, performs the isolated compatibility test, and only then applies the live configuration transaction.

## Compatibility Gate

Before touching the live Codex home, setup creates an isolated temporary Codex home and tests the current native path:

- custom DeepSeek provider initialization;
- one DeepSeek child spawn;
- parallel spawn of at least three DeepSeek children;
- tool-call round trip;
- child completion and parent collection;
- follow-up or resume behavior;
- child metadata showing the DeepSeek provider, requested model, and resolved effort;
- primary metadata remaining on the user's OpenAI model and reasoning level.

The gate also proves that the current Codex binary can use command-backed provider authentication and that the local adapter can translate its actual tool schema, including V2 namespaces. DeepSeek reasoning emitted before tool calls is sealed into an opaque Responses reasoning item and replayed only in memory on the continuation request, as required by DeepSeek thinking mode.

If the current DeepSeek endpoint plus loopback adapter cannot satisfy Codex's Responses and collaboration contracts, setup fails cleanly. It must not automatically re-enable the original global v1/catalog workaround.

## Transaction and Rollback

All live writes occur under a file lock and one transaction:

1. Resolve and validate exact target paths.
2. Back up every file that may be touched.
3. Record pre-existing values, content hashes, created files, and ownership in a manifest.
4. Generate all new content in memory and parse it before writing.
5. Write atomically.
6. Run post-install native validation.
7. Restore the complete pre-install state if any step fails.

Commands:

- `status`: report primary model, child provider/model/effort, concurrency, credential presence, and compatibility state without exposing secrets.
- `setup`: masked key input, isolated validation, transactional install, and native acceptance test.
- `test`: repeat native single-child, fan-out, tool, and resume checks.
- `disable`: temporarily remove the managed role overrides and fan-out policy while retaining the provider and credential.
- `enable`: reapply validated managed role overrides and policy.
- `uninstall`: restore prior settings and files while retaining the credential unless explicitly requested otherwise.
- `uninstall --remove-credential`: restore prior settings and also remove the managed credential.

Uninstall removes or restores only managed content. If a managed file has been edited since installation, uninstall stops with a conflict report rather than overwriting the user's changes.

## Code Structure

The existing monolithic manager remains the CLI entry point but delegates to focused standard-library modules:

- configuration parsing and managed-block edits;
- provider and credential handling;
- custom-agent generation and fan-out policy generation;
- loopback Responses-to-Chat protocol adaptation and streaming tool-call translation;
- model/effort capability resolution;
- isolated compatibility and native acceptance tests;
- transaction, backup, manifest, and rollback handling.

The command remains usable through the existing Skill workflow, but internals become independently testable. No unrelated visual assets or repository branding are redesigned.

## Verification

### Unit tests

- preserve every unrelated user config key and instruction section;
- keep the primary model, provider, and reasoning unchanged;
- generate all three built-in role overrides with the selected DeepSeek settings;
- configure concurrency 8 and allow a validated lower override;
- resolve only supported reasoning values;
- reject an invalid model identifier before live writes;
- keep credentials out of files, arguments, logs, JSON output, and exceptions;
- perform exact rollback and conflict-safe uninstall;
- ensure repeated setup is idempotent and refreshes provider/model capability data.

### Integration tests

- run the manager against temporary Codex homes;
- exercise a local compatible Responses test server for deterministic protocol and tool-call cases;
- execute the bundled desktop Codex binary against the isolated configuration;
- verify child thread metadata and parent metadata independently;
- exercise three-way fan-out, waiting, result collection, interruption, and resume.

### Live acceptance with the user's key

- validate `deepseek-v4-pro` exactly;
- spawn at least three DeepSeek child threads concurrently;
- complete one read-heavy task and one isolated write/test task;
- verify all child metadata uses DeepSeek and the maximum supported effort;
- verify the primary thread remains `gpt-5.6-sol + max`;
- measure wall-clock duration and retries for the fan-out run;
- run disable, enable, and rollback smoke tests without losing user configuration.

## Acceptance Criteria

The work is complete only when all of the following are true:

- the primary model and effort are unchanged;
- built-in `default`, `worker`, and `explorer` children use the validated DeepSeek model;
- at least three independent children can run concurrently and return results to the primary;
- the default concurrency cap is 8;
- the maximum supported DeepSeek reasoning mode is active and reported accurately;
- automatic fan-out follows the conflict-avoidance policy;
- no global v1 downgrade, multi-agent-v2 disable, or stale parent catalog rewrite is present;
- the API key is stored only in the OS credential store;
- failed setup leaves the live Codex home byte-for-byte equivalent to its pre-install managed state;
- disable and uninstall restore native Codex child behavior safely;
- unit, integration, and live acceptance tests pass.

## Known Boundary

The exact `deepseek-v4-pro` model and its highest supported reasoning value cannot be truthfully confirmed until the user's API credential is supplied to the provider validation step. The implementation therefore prepares and tests the complete workflow but does not activate an unverified model or write a placeholder secret.

Hosted Codex tools that DeepSeek cannot execute are rejected instead of being silently emulated; text and local Codex function/custom tools are the supported child-agent path.
