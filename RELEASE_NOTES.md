# Multi Relay release

Multi Relay is now a host-neutral relay for Codex and Claude Code. It routes canonical requests through ordered, credential-scoped targets and can switch providers or models before a response is committed.

Highlights:

- Responses, Chat Completions, DeepSeek Chat, and Anthropic Messages adapters.
- Sticky and timed target pools with quota, rate-limit, authentication, and availability cooldowns.
- Codex agent integration without changing the parent model.
- Claude Code user/project subagents and a process-scoped launcher for the local gateway.
- Multiple providers and multiple vault credentials per provider; secrets stay out of catalog, state, logs, and command arguments.
- Transactional migration from earlier state directories, markers, bridge identities, and credential targets.
- Offline fake-upstream coverage on Windows, macOS, and Linux with Python 3.11 and 3.12.

Existing installations can run `status`, then `repair`, to migrate managed state. Keep a backup when resolving any reported `state_conflict`; Multi Relay deliberately does not merge divergent state automatically.
