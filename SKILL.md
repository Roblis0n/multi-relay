---
name: codex-multi-relay
description: Use when a user asks to configure, validate, route, test, repair, disable, enable, migrate, or uninstall Codex multi-provider child agents, including custom models, capability boundaries, provider credentials, or DeepSeek workers.
---

# Codex Multi Relay

为 Codex 配置可审计的多模型子代理目录。只在用户管理 Relay、选择子代理模型、诊断路由或处理迁移时使用；普通编码任务不重复运行本 Skill。

## 核心契约

- 不修改顶层 `model`、`model_provider` 或 `model_reasoning_effort`。
- 子代理由 `catalog.json` 独立指定 Provider、协议、模型、能力、信任级别、优先级、沙箱、MCP 和 Skill。
- 支持 `codex-native`、`responses-compatible`、`chat-completions-compatible`、`deepseek-chat` 四种协议。
- 默认 `hybrid` 目录把 `default`、`worker`、`explorer` 路由到 `deepseek-v4-pro`，把高信任 `reviewer` 留在原生 Codex；`native` 预设只安装原生 reviewer，不读取凭据，也不联网探测。
- 默认最多并发 8 个子线程。只并行两个及以上相互独立、边界明确的任务；顺序依赖、重叠写入和共享可变状态留给主代理。
- 派生子代理时必须显式设置目录中的 `agent_type`，并使用 `fork_turns="none"` 或正数局部上下文，不能用全量上下文继承代替模型路由。

## 主代理能力边界

派生前先列出任务所需能力，再调用 `route --capability ...`：

- 目录中的子代理必须声明全部所需能力，否则任务保留在主代理。
- `vision`、`audio`、`web` 默认属于主代理。只有子代理显式声明相应能力时才能路由；`web` 还必须具有真实可用的 MCP server。
- `high-risk` 工作必须路由给 `trust=high` 的子代理，并由主代理做最终验证；没有合格子代理时直接返回 `parent_required`。
- 不得静默换 Provider、换模型或降级能力。

每次 `spawn_agent`、`followup_task` 或 `send_message` 前，先输出与子代理消息逐字一致的可见交接块：

```text
[Relay task: <target>]
<exact complete child message>
[/Relay task: <target>]
```

新配置只写入 `[Relay task]`。适配层仍识别旧 `[DeepSeek task]`，仅用于升级兼容。Chat Completions Provider 通过本机 `127.0.0.1:42137` 转换为 Codex Responses；直连 Responses Provider 不经过转换层。

## 触发后的流程

1. 先运行 `status --json`，确认当前状态与所有权。
2. 首次安装选择 `setup --preset hybrid --json` 或 `setup --preset native --json`。
3. 需要自定义模型时，先添加 Provider，再添加或替换 Agent；每次变更都会验证整个目录并以事务方式写入。
4. 用 `route` 检查能力选择，用 `test --json` 做正式配置验收。
5. 最终报告状态、Provider、Agent、能力边界、并发上限与备份位置，不报告或复述凭据。

缺少 vault 凭据时，只允许管理器在本机显示掩码输入框。不要在聊天、配置文件、命令参数、日志或备份中接收和保存密钥。Windows 使用 Windows Credential Manager，macOS 使用 macOS Keychain；DeepSeek 的兼容目标仍为 `codex-deepseek-api-key`，其他 Provider 使用各自隔离的目标。

## 管理命令

统一入口为 `scripts/multi_relay.py`。Windows 可用 `py -3`，macOS 可用 `python3`：

```text
python3 <skill-dir>/scripts/multi_relay.py status --json
python3 <skill-dir>/scripts/multi_relay.py setup --preset hybrid --json
python3 <skill-dir>/scripts/multi_relay.py setup --preset native --json
python3 <skill-dir>/scripts/multi_relay.py catalog --json
python3 <skill-dir>/scripts/multi_relay.py apply --json
python3 <skill-dir>/scripts/multi_relay.py test --json
python3 <skill-dir>/scripts/multi_relay.py repair --json
python3 <skill-dir>/scripts/multi_relay.py disable --json
python3 <skill-dir>/scripts/multi_relay.py enable --json
python3 <skill-dir>/scripts/multi_relay.py uninstall --json
python3 <skill-dir>/scripts/multi_relay.py uninstall --remove-credential --json
python3 <skill-dir>/scripts/multi_relay.py provider list --json
python3 <skill-dir>/scripts/multi_relay.py agent list --json
python3 <skill-dir>/scripts/multi_relay.py route --capability text --json
```

添加直连 Responses Provider：

```text
python3 <skill-dir>/scripts/multi_relay.py provider add --id vendor --name Vendor --protocol responses-compatible --base-url https://api.vendor.example/v1 --auth vault --capability text --capability tools --context-window 128000 --json
```

添加 Chat Completions Provider 时将 `--protocol` 改为 `chat-completions-compatible`；DeepSeek 兼容端使用 `deepseek-chat`；原生 Codex 使用 `codex-native --auth codex` 且不填写 `--base-url`。

添加或替换子代理：

```text
python3 <skill-dir>/scripts/multi_relay.py agent set --name vendor-worker --description "Vendor implementation worker" --provider vendor --model vendor-model --reasoning-effort high --context-window 128000 --capability text --capability tools --sandbox-mode workspace-write --instructions "Implement only the assigned bounded task." --json
```

移除前先解除引用：

```text
python3 <skill-dir>/scripts/multi_relay.py agent remove vendor-worker --json
python3 <skill-dir>/scripts/multi_relay.py provider remove vendor --json
```

`provider remove` 会拒绝仍被 Agent 使用的 Provider。只有明确要求时才加 `--remove-credential`。

## 受管文件

- `$CODEX_HOME/codex-multi-relay/catalog.json`：无密钥的 Provider 与 Agent 目录。
- `$CODEX_HOME/codex-multi-relay/manifest.json`：所有权、哈希、状态和回滚信息。
- `$CODEX_HOME/agents/<agent>.toml`：按目录生成，例如 `default.toml`、`worker.toml`、`explorer.toml`、`reviewer.toml`。
- `$CODEX_HOME/config.toml`：只写受管 Provider 块与 `multi_agent_v2` 路由开关，不改变主模型三键。
- `$CODEX_HOME/AGENTS.md`：只写能力路由与可见交接规则。

旧 `$CODEX_HOME/codex-deepseek-relay`、`$CODEX_HOME/codex-deepseek-subagent` 以及旧 marker 只作为有 manifest 所有权证明时的迁移来源。没有证明时必须返回 `conflict`，不得接管相似的用户内容。

## 状态处理

- `ready`：目录、配置、角色与适用验收均通过。
- `disabled`：保留目录、Provider 和凭据；目录变更不得隐式启用，只有 `enable` 恢复角色与路由指令。
- `parent_required`：没有满足全部能力或信任边界的子代理，任务留给主代理。
- `not_configured`：尚未安装。
- `legacy` / `legacy_requires_setup`：先运行 `setup` 或 `repair` 完成受校验迁移，再执行其他生命周期操作。
- `credential_missing`：运行相应 setup/provider 命令，通过本机掩码输入保存。
- `model_unavailable`：停止，不猜测近似模型名。
- `compatibility_failed`：报告失败检查项；事务失败必须回滚。
- `conflict`：报告用户自有冲突文件；不要覆盖。
- `operation_in_progress`：稍后重试，不并发修改。

协议细节、边界、迁移与回滚规则见 [references/compatibility.md](references/compatibility.md)。
