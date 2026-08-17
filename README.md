**简体中文** | [English](./README_EN.md)

<p align="center">
  <img src="./assets/readme/hero.png" width="100%" alt="Multi Relay 在 Codex 与 Claude Code、target pool、本地网关和多 Provider 之间进行路由">
</p>

<h1 align="center">Multi Relay</h1>

<p align="center">让 Codex 与 Claude Code 通过一个本地网关，按能力在多模型、多 Provider、多凭据 target pool 中安全轮转。</p>

Multi Relay 把一次执行的四个关键事实放进 `ExecutionTarget`：Provider、协议、模型和凭据引用。Agent 只选择 pool；pool 再选择 target。这样切换模型时一定同步切换对应 key，不会把 A Provider 的凭据误发给 B Provider，也不会让一个模糊的“模型名”暗中改变认证边界。

## 支持矩阵

| 能力 | Codex | Claude Code |
| --- | --- | --- |
| 安装受管 agent | 用户级 `config.toml`、`AGENTS.md`、Agent TOML | 用户级或项目级 `.claude/agents/*.md` |
| 父请求 | 保持原 Codex 主模型；受管 HTTP target 走网关 | 必须由 launcher 启动，父请求与子 agent 都走网关 |
| 子 agent | 原生 Codex target 或网关 pool | 网关 pool |
| 协议 | Responses、Chat Completions、DeepSeek Chat、原生 Codex | Anthropic Messages，经网关适配其他协议 |
| 平台 | Windows、macOS、Linux | Windows、macOS、Linux；需已有 Claude Code |
| 安全停用/卸载 | 支持 | 支持 |

Claude Code 必须经 launcher 启动。launcher 只为该进程注入本地 loopback 地址和短期网关 token，因此 Claude Code 的父请求也能获得同一套轮转、冷却、凭据隔离和 committed 边界；它不会永久导出任何上游 API key。

## 5 分钟开始

要求：Python 3.11+；使用 Codex 时需 Codex，使用 Claude Code 时需 Claude Code。先安装 Skill：

```bash
npx skills add Roblis0n/multi-relay -g -y
```

在仓库目录检查状态并安装默认混合目录：

```bash
python scripts/multi_relay.py status --json
python scripts/multi_relay.py setup --preset hybrid --host all --json
python scripts/multi_relay.py test --host all --json
```

需要凭据时，CLI 会在本机显示掩码输入；不要把 key 写进聊天或命令。只使用原生 Codex target 时：

```bash
python scripts/multi_relay.py setup --preset native --host codex --json
```

启动 Claude Code：

```bash
python scripts/multi_relay.py host apply claude-code --json
python scripts/multi_relay.py launch claude-code --project . -- --help
```

`--` 后的参数原样交给 Claude Code。日常启动也必须使用 `multi-relay launch claude-code`，不要永久设置上游 key 或 Provider base URL。

## 一个跨 Provider target pool

以下示例 URL 与 model id 全是假值。三个 target 故意声明不同能力：DeepSeek target 支持文本与工具，Anthropic Messages target 只支持文本，OpenAI-compatible target 支持文本、工具与视觉。

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

一个 Provider 可以有多个凭据。再添加 `backup` credential 并创建第二个 target，就能让同一模型的两个 key 独立计数、冷却与禁用。

## sticky、timed 与错误切换

`sticky` 适合希望尽量保持模型行为一致的工作：当前 target 一直保留，直到手动轮转、被禁用，或在响应尚未 committed 时发生可切换错误。

```bash
python scripts/multi_relay.py pool strategy cross-provider sticky --json
python scripts/multi_relay.py pool rotate cross-provider --json
```

`timed` 适合定时分摊额度或成本：到期后，下一次选择会转到下一个合格 target。

```bash
python scripts/multi_relay.py pool strategy cross-provider timed --duration 30m --json
python scripts/multi_relay.py pool status cross-provider --json
```

只在开始可见输出前，以下错误允许切换：额度耗尽、限流、认证失败、模型不可用、Provider/传输不可用、协议响应错误。认证失败还会禁用对应 credential。请求无效、上下文超限、策略拒绝、用户取消和没有合格 target 不会换模型，因为重复同一请求不能安全解决它们。

一旦收到首个文本 delta、工具调用开始或其他可见内容，响应即 committed。之后若流中断，网关会终止并返回结构化错误，不会让另一个模型续写：另一个模型没有完全相同的隐藏状态、采样轨迹和工具上下文，拼接会制造一段无法证明来源的一致性假象。

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="Multi Relay 在响应 committed 前允许轮转，committed 后遇到错误则终止">
</p>

## 架构

<p align="center">
  <img src="./assets/readme/architecture.svg" width="100%" alt="Codex 与 Claude Code 通过本地 Multi Relay gateway、target pool 和系统 vault 连接多个 Provider">
</p>

- `catalog.json` 无 secret，定义 Provider、credential 引用、target、pool、agent 与 host。
- loopback gateway 只监听 `127.0.0.1`，统一 Responses 与 Anthropic Messages 宿主入口。
- 协议 adapter 在 canonical request/event 层转换，不读取环境中的上游 secret。
- vault 在真正发起 upstream 请求前，按 target 的 credential 引用读取一次凭据。
- `vision`、`audio`、`tool_calling`、`server_web_search` 等能力必须由 Provider、target、pool 与 agent 同时满足；联网 agent 还要配置实际 MCP/tool。

详见 [目录模型](references/catalog.md)、[轮转与 committed 边界](references/rotation.md)、[Codex](references/codex.md) 和 [Claude Code](references/claude-code.md)。

## CLI 索引

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

所有命令支持稳定的 `--json` 结果信封。查看某个动作的精确参数：

```bash
python scripts/multi_relay.py --help
python scripts/multi_relay.py target add --help
python scripts/multi_relay.py pool add --help
python scripts/multi_relay.py launch claude-code --help
```

生命周期示例：

```bash
python scripts/multi_relay.py disable --host all --json
python scripts/multi_relay.py enable --host all --json
python scripts/multi_relay.py uninstall --host all --json
python scripts/multi_relay.py uninstall --host all --remove-credentials --json
```

普通卸载保留 vault 凭据；只有显式 `--remove-credentials` 才删除受管 credential。

## 安全模型与状态位置

上游凭据只保存在 Windows Credential Manager、macOS Keychain 或 Linux Secret Service。它们绝不会写入 catalog、manifest、host 配置、Agent 文件、命令参数、日志、错误、备份、临时文件或 Git。catalog 中只有形如 `multi-relay/provider-id/credential-id` 的非 secret 引用。

产品状态不再以 Codex Home 作为根目录：

| 平台 | 默认状态目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\multi-relay` |
| macOS | `~/Library/Application Support/multi-relay` |
| Linux | `${XDG_STATE_HOME:-~/.local/state}/multi-relay` |

写入采用锁、预校验、同目录原子替换、文件哈希与可回滚备份。更多内容见 [安全模型](references/security.md)。

## 兼容迁移

新状态优先；旧 `$CODEX_HOME/codex-multi-relay`、`$CODEX_HOME/codex-deepseek-relay`、`$CODEX_HOME/codex-deepseek-subagent`，旧 marker 和旧 credential target 仅作为迁移输入。只有 manifest、受管 marker 与哈希能证明所有权时才会复制、验证、切换并清理；新旧状态不一致时返回 `state_conflict`，绝不静默合并。迁移和修复可重复运行。

详见 [兼容与迁移](references/compatibility.md)。

## 限制与排障

- Multi Relay 不会让模型获得它没有声明或没有实际工具支持的能力。
- committed 后不跨模型续写；应用应重试整个请求或让用户决定下一步。
- Claude Code 不经 launcher 时不受 Multi Relay 管理。
- native Codex target 只兼容 Codex；Claude Code pool 必须包含 HTTP target。
- `no_eligible_target`：检查 host、能力、target/credential enabled 状态和冷却时间。
- `state_conflict`：保留两边文件，比较 manifest/catalog，确认来源后再迁移。
- `gateway_port_conflict`：确认 `127.0.0.1:42137` 没有外部或过期进程。
- `credential_missing` / `auth_invalid`：用 `credential replace` 在本机重新录入，不要在聊天中发送凭据。

## 开发验证

测试全部使用 fake upstream，不需要真实 Provider 凭据：

```bash
python -m unittest discover -s scripts -p "test_*.py"
python -m compileall -q scripts
python scripts/check_runtime_contract.py
python scripts/check_public_contract.py
```

真实 Codex/Claude Code smoke test 是可选的本机验证，不属于离线单元测试。项目采用 [MIT License](./LICENSE)，是独立社区工具，与 OpenAI、Anthropic、DeepSeek 或其他 Provider 不存在官方隶属或背书关系。查看 [完整发布说明](./RELEASE_NOTES.md) 或访问 [GitHub 仓库](https://github.com/Roblis0n/multi-relay)。
