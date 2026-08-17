---
name: multi-relay
description: Use when a user asks to configure, validate, route, rotate, test, repair, disable, enable, migrate, or uninstall Multi Relay multi-provider credentials, targets, pools, or agents for Codex and Claude Code.
---

# Multi Relay

管理 Codex 与 Claude Code 的多 Provider 路由。Multi Relay 的正式身份与命令均为 `multi-relay`；DeepSeek 只是内置 preset 中的一个普通 Provider。

## 工作原则

- 先运行 `status --json`，再决定 setup、repair、apply 或只读检查。
- 不修改 Codex 顶层 `model`、`model_provider`、`model_reasoning_effort`。
- 不永久修改 Claude Code 的上游 URL 或 key；只通过 `launch claude-code` 启动。
- 不在聊天、命令参数、配置、catalog、manifest、日志、错误、备份、临时文件或 Git 中接收、复述、保存 secret。
- 需要凭据时，只允许 CLI 在本机掩码输入并写入操作系统 vault。
- 不把未声明的能力当作可用能力；`vision`、`audio`、`tool_calling`、`server_web_search` 必须由整条路由满足。
- 配置变更必须走 RelayManager 的事务路径；不要手工拼接 host 配置或 Agent 文件。
- 用户只要求检查时，不执行 setup、apply、launch、disable、uninstall 或真实 Provider 探测。

## 路由模型

目录层级固定为：

```text
Provider → CredentialRef → ExecutionTarget → TargetPool → Agent → Host
```

`ExecutionTarget` 同时绑定 Provider、协议、model id、credential id、能力、上下文、信任级别和宿主兼容性。模型与 key 必须在同一个 target 中移动，避免跨 Provider 误用凭据。Agent 只引用 pool，不直接猜测 Provider。

`sticky` 保留当前 target，适合行为稳定；`timed` 在时限到期后的下一次选择轮转，适合额度或成本分摊。只有 quota、rate limit、auth、model unavailable、provider/transport unavailable、protocol error 在 response committed 前允许 failover。请求无效、上下文超限、策略拒绝、取消和无合格 target 不允许切换。committed 后终止，不跨模型续写。

## 先检查

```bash
python scripts/multi_relay.py status --json
python scripts/multi_relay.py catalog --json
python scripts/multi_relay.py host list --json
python scripts/multi_relay.py gateway status --json
```

读取 `status` 的 `status`、`changed`、`warnings`、`details`、`next_actions`。结构化错误按 `error.code` 处理，不解析自然语言。

## 安装与宿主生命周期

默认混合 preset：

```bash
python scripts/multi_relay.py setup --preset hybrid --host all --json
python scripts/multi_relay.py test --host all --json
```

只使用 Codex 原生 target：

```bash
python scripts/multi_relay.py setup --preset native --host codex --json
```

Codex：

```bash
python scripts/multi_relay.py host apply codex --json
python scripts/multi_relay.py host status codex --json
python scripts/multi_relay.py disable --host codex --json
python scripts/multi_relay.py enable --host codex --json
python scripts/multi_relay.py uninstall --host codex --json
```

Claude Code：

```bash
python scripts/multi_relay.py host apply claude-code --json
python scripts/multi_relay.py host status claude-code --json
python scripts/multi_relay.py launch claude-code --project . --
python scripts/multi_relay.py test --host claude-code --json
python scripts/multi_relay.py disable --host claude-code --json
python scripts/multi_relay.py enable --host claude-code --json
python scripts/multi_relay.py uninstall --host claude-code --json
```

Claude Code 父请求和子 agent 都必须经 launcher 的临时本地 gateway 环境。不要教用户永久 export 上游 API key、`ANTHROPIC_BASE_URL` 或其他 Provider 地址。

## CRUD 与轮转

Provider：

```bash
python scripts/multi_relay.py provider list --json
python scripts/multi_relay.py provider add --id provider-example --name "Provider Example" --protocol responses-compatible --base-url https://provider.example/v1 --auth vault --capability text --json
python scripts/multi_relay.py provider edit provider-example --base-url https://next.provider.example/v1 --json
python scripts/multi_relay.py provider test provider-example --json
python scripts/multi_relay.py provider remove provider-example --json
```

Credential 只在本地掩码输入：

```bash
python scripts/multi_relay.py credential list --json
python scripts/multi_relay.py credential add --provider provider-example --id primary --label "Primary" --json
python scripts/multi_relay.py credential add --provider provider-example --id backup --label "Backup" --json
python scripts/multi_relay.py credential replace --provider provider-example --id primary --json
python scripts/multi_relay.py credential test --provider provider-example --id primary --json
python scripts/multi_relay.py credential remove --provider provider-example --id backup --json
```

Target、pool、agent：

```bash
python scripts/multi_relay.py target add --id target-example --provider provider-example --model model-example --credential primary --capability text --host codex --host claude-code --json
python scripts/multi_relay.py target test target-example --json
python scripts/multi_relay.py pool add --id pool-example --target target-example --strategy sticky --capability text --host codex --host claude-code --json
python scripts/multi_relay.py pool strategy pool-example timed --duration 30m --json
python scripts/multi_relay.py pool rotate pool-example --json
python scripts/multi_relay.py pool reset pool-example --json
python scripts/multi_relay.py pool status pool-example --json
python scripts/multi_relay.py agent set --name worker-example --description "Bounded worker" --pool pool-example --capability text --host codex --host claude-code --sandbox-mode workspace-write --instructions "Complete only the assigned bounded task." --json
python scripts/multi_relay.py route --capability text --json
```

删除前先解除引用：Agent → pool → target → credential/provider。CLI 遇到仍被引用的对象必须返回冲突，不强制级联删除。

## 修复、停用与卸载

```bash
python scripts/multi_relay.py apply --json
python scripts/multi_relay.py repair --json
python scripts/multi_relay.py disable --host all --json
python scripts/multi_relay.py enable --host all --json
python scripts/multi_relay.py uninstall --host all --json
python scripts/multi_relay.py uninstall --host all --remove-credentials --json
```

普通 uninstall 保留 vault 凭据；只有用户明确要求删除凭据时才使用 `--remove-credentials`。disable 保留 catalog 和 manifest，enable 重新生成受管 host 文件。

## 状态、安全与兼容

Canonical 状态目录：

- Windows：`%LOCALAPPDATA%\multi-relay`
- macOS：`~/Library/Application Support/multi-relay`
- Linux：`${XDG_STATE_HOME:-~/.local/state}/multi-relay`

Windows 使用 Windows Credential Manager，macOS 使用 macOS Keychain，Linux 使用 Secret Service。catalog 只保存 `multi-relay/provider/credential` 引用。

旧 `$CODEX_HOME/codex-multi-relay`、`$CODEX_HOME/codex-deepseek-relay`、`$CODEX_HOME/codex-deepseek-subagent`、旧 marker、旧 credential target 和 `FanoutManager` import 仅为兼容迁移。缺少 manifest/哈希所有权证明或新旧状态分歧时，返回 `conflict` / `state_conflict`，不接管、不合并、不删除。

## 结果判定

- `ready`：目录、宿主与必需检查通过。
- `disabled`：受管 host 文件已停用，catalog 仍保留。
- `uninstalled`：受管配置已移除；凭据是否保留取决于显式参数。
- `no_eligible_target`：核对 host、能力、enabled、credential 和 cooldown。
- `credential_missing` / `auth_invalid`：只在本机执行 `credential add` 或 `replace`。
- `state_conflict`：保留两边状态并要求明确可信来源，不做静默修复。
- `gateway_port_conflict`：不要终止未知进程；先确认 loopback 端口所有者。

完成后报告实际执行的命令、`status`、变更对象和离线验证结果。未运行真实 Codex、Claude Code 或 Provider smoke 时明确写“未运行”，不能把单元测试写成真实宿主验证。
