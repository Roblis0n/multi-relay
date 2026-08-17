# 目录、执行目标与模型别名

本文档说明 Multi Relay 的领域模型、`catalog.json` 的结构与校验规则、模型别名规则，以及如何新增 Provider、多个凭据、执行目标、目标池和 Agent。轮转语义见 [rotation.md](rotation.md)，凭据与安全见 [security.md](security.md)，迁移与限制见 [compatibility.md](compatibility.md)。

## 1. 目录是什么

`catalog.json` 是 Multi Relay 唯一的无密钥配置来源。它把以下对象组织成一张图：

| 对象 | 含义 | 关键字段 |
| --- | --- | --- |
| `providers` | 上游服务 | `id`、`protocol`、`base_url`、`auth_mode`、`capabilities`、`enabled` |
| `credentials` | 凭据引用（不含秘密） | `id`、`provider_id`、`vault_target`、`enabled`、`label` |
| `targets` | 执行目标，轮转最小单位 | `provider_id`、`model`、`credential_id`、`capabilities`、`context_window`、`trust`、`host_compatibility` |
| `pools` | 有序目标池 | `targets`、`strategy`、`duration_seconds`、`cooldown`、`required_capabilities` |
| `agents` | 宿主可见角色 | `pool_id`、`required_capabilities`、`trust`、`sandbox_mode`、`hosts` |
| `hosts` | Codex 与 Claude Code 的安装配置 | `enabled`、`scope`、`default_pool` |

目录只保存引用和元数据。上游 API 凭据保存在操作系统凭据仓库，目录中的 `credentials` 条目只记录 `vault_target` 等非秘密信息；任何包含 secret-like 字段名或字段值的目录都会被拒绝。

Multi Relay 解决的问题是：用户不再只配置一个 API Key，而是把任意数量的 Provider、协议、模型和凭据组合成有优先级的执行目标；某个目标额度耗尽、认证失效、限流或暂时不可用时，按明确规则切换到池中的下一个目标。Codex 与 Claude Code 共用同一份目录、同一套目标选择语义和同一个本地网关。

## 2. ExecutionTarget 为什么同时绑定 Provider、模型和凭据

`ExecutionTarget` 是轮转的最小单位。它同时指向 `provider_id`、`model` 与 `credential_id`，原因如下：

- 同一个 Provider 可以有多个模型，同一个模型也可能通过不同 Provider 提供；只有把 Provider 与模型绑在一起，"切换"才有确定语义。
- 凭据属于 Provider 的某个命名引用（如 `primary`、`backup`），而不是全局。把凭据引用放进目标，切换目标就同时完成了"换模型 / 换 Provider / 换 key"，不需要单独维护 key 轮换逻辑。
- 能力、上下文窗口、思考强度、信任级别和宿主兼容性都随具体模型验证，因此也必须随目标声明。选择器不会先选一个目标再"假装"它具备缺失能力。

一个目标的最小形态（实际目录中的 JSON）：

```json
{
  "id": "deepseek-primary",
  "provider_id": "deepseek",
  "model": "deepseek-example-chat",
  "credential_id": "primary",
  "capabilities": ["text", "tool_calling"],
  "context_window": 128000,
  "reasoning_efforts": ["high", "max"],
  "trust": "standard",
  "host_compatibility": ["codex", "claude-code"],
  "enabled": true
}
```

`protocol` 默认继承 Provider，只有经过验证时才允许目标级覆盖；`credential_id` 在 Provider 为 `auth_mode=none` 或 `host-native` 时可为空。

## 3. 严格校验

目录按 schema 2 校验，规则包括：

- 顶层与每类对象都采用字段白名单，未知字段直接报错，不静默忽略。
- 所有 `id` 使用小写 ASCII 字母、数字、下划线或连字符。
- 所有引用必须存在：`targets` 引用存在的 Provider 与 CredentialRef，`pools` 引用存在的 target，`agents` 引用存在的 pool，`hosts.default_pool` 引用存在的 pool。
- 删除顺序有约束：Provider 删除前不得被 target 引用；CredentialRef 删除前不得被 target 引用；target 删除前不得被 pool 引用；pool 删除前不得被 Agent 或宿主引用。
- 同一个 pool 内不允许重复 target；`host_compatibility` 必须是已知宿主。
- pool 的 `required_capabilities` 必须是池内每个 target 能力的子集；pool 的宿主兼容性不得超出任一 target。
- secret-like 字段名或字段值触发拒绝，并提示改用操作系统凭据仓库导入。

## 4. 模型别名

宿主侧只看到稳定的 relay alias，不写死上游模型，因此轮转后无需重写宿主配置：

| Alias | 解析方式 |
| --- | --- |
| `multi-relay-default` | 宿主的 `default_pool` |
| `multi-relay-<pool_id>` | 直接指定某个池 |
| `multi-relay-agent-<agent_name>` | 按 AgentProfile 解析池、能力、信任、上下文与思考强度 |

alias 不含当前上游 Provider 或模型名。`GET /v1/models` 列出可用 alias 及其能力，不泄露凭据。

## 5. 能力声明与验证

能力集合固定为 `text`、`vision`、`audio`、`tool_calling`、`server_web_search`：

- `tool_calling`：模型可以调用宿主暴露的工具。
- `server_web_search`：上游 Provider 自带搜索能力（例如 Responses web search）。普通模型即使能生成 URL，也不能声明该能力；只有经过显式配置和探测的目标才能声明。
- `vision`、`audio` 描述目标对图片或音频输入的验证支持；未声明的目标在收到对应内容时会在请求发出前被过滤。

声明不等于已验证。`target test` 验证认证、模型可用性、协议握手和已声明能力的基础契约；`provider test`、`provider discover-models --model <id>` 与 `credential test` 分别验证 Provider 与凭据。未验证的信息显示为 `unknown`，不会被伪装成 `supported`。

目录内的能力约束：池的 `required_capabilities` 必须被池内每个 target 满足；Agent 的 `required_capabilities` 必须能被其池内至少一个健康 target 满足，否则安装时返回 `no_eligible_target`。`high-risk` 工作要求 `trust=high`，且不会自动跨信任边界降级。

## 6. 常用管理流程

统一入口是 `python scripts\multi_relay.py <command>`（安装后也可直接使用 `multi-relay <command>`）。顺序是：先加 Provider，再加凭据引用，再加 target，再组成 pool，最后为 Agent 绑定 pool。所有变更都会先验证整个目录，再以事务方式写入。

### 6.1 新增 Provider

```powershell
python scripts\multi_relay.py provider add --id deepseek --name "DeepSeek demo" --protocol deepseek-chat --base-url https://api.deepseek.example/v1 --auth vault --capability text --capability tool_calling --context-window 128000 --json
```

`--protocol` 支持 `codex-native`、`responses-compatible`、`chat-completions-compatible`、`deepseek-chat`、`anthropic-messages`。`--base-url` 必须使用 HTTPS；只有 loopback 地址允许 HTTP。`codex-native` 不填 `base_url` 且使用 `--auth codex`。

### 6.2 添加多个凭据

`credential add` 和 `credential replace` 只在本地显示掩码输入框读取秘密，不提供任何 `--key` 参数，秘密不进入命令行、目录、日志或备份。同一 Provider 可以保存多个命名凭据，例如主用与备用：

```powershell
python scripts\multi_relay.py credential add --provider deepseek --id primary --label "Primary" --json
python scripts\multi_relay.py credential add --provider deepseek --id backup --label "Backup" --json
```

`credential list` 只显示 `provider`、`credential`、`label`、`enabled`、`present`，不显示 key 的前后缀或哈希。

### 6.3 新增 target

```powershell
python scripts\multi_relay.py target add --id deepseek-primary --provider deepseek --model deepseek-example-chat --credential primary --capability text --capability tool_calling --context-window 128000 --reasoning-effort high --reasoning-effort max --host codex --host claude-code --json

python scripts\multi_relay.py target add --id deepseek-backup --provider deepseek --model deepseek-example-chat --credential backup --capability text --capability tool_calling --context-window 128000 --host codex --host claude-code --json
```

第二个 target 与第一个只是凭据引用不同，因此额度耗尽时切换 target 就等于换 key。

### 6.4 组成 pool

```powershell
python scripts\multi_relay.py pool add --id general --target deepseek-primary --target deepseek-backup --strategy sticky --capability text --capability tool_calling --host codex --host claude-code --json
```

`sticky` 禁止带 `--duration`；`timed` 必须带 `--duration`（单位 `s`、`m`、`h`、`d`，上限一年）。`--max-rate-limit-wait` 是尊重 `Retry-After` 的最长等待秒数（默认 30）。冷却时长使用 pool 默认值：quota 24 小时、rate limit 60 秒、认证 1 小时、Provider 30 秒。

### 6.5 新增 Agent

```powershell
python scripts\multi_relay.py agent set --name implementer --description "Bounded implementation worker" --pool general --capability text --capability tool_calling --trust standard --sandbox-mode workspace-write --host codex --host claude-code --instructions "Implement only the assigned bounded task." --json
```

Agent 的 `--sandbox-mode` 取值 `read-only`、`workspace-write`、`danger-full-access`。`--mcp-json` 接收 MCP server 定义；声明 `server_web_search` 的目标必须由 Provider 原生支持，声明 `web` 的旧式 Agent 必须配置真实的 MCP `url` 或 `command`，只写标签不算可用。

## 7. 跨 Provider 池完整示例

以下示例全部使用示例域名与假模型 ID，不含真实凭据。三个 target 的能力刻意不同，以演示选择器的过滤结果：

```powershell
# 1) DeepSeek：文本 + 工具调用，文本上下文 128000
python scripts\multi_relay.py provider add --id deepseek --name "DeepSeek demo" --protocol deepseek-chat --base-url https://api.deepseek.example/v1 --auth vault --capability text --capability tool_calling --context-window 128000 --json
python scripts\multi_relay.py credential add --provider deepseek --id primary --json
python scripts\multi_relay.py target add --id deepseek-primary --provider deepseek --model deepseek-example-chat --credential primary --capability text --capability tool_calling --context-window 128000 --host codex --host claude-code --json

# 2) Anthropic Messages：文本 + 工具调用 + 视觉，上下文 200000
python scripts\multi_relay.py provider add --id anthropic --name "Anthropic demo" --protocol anthropic-messages --base-url https://api.anthropic.example --auth vault --capability text --capability tool_calling --capability vision --context-window 200000 --json
python scripts\multi_relay.py credential add --provider anthropic --id primary --json
python scripts\multi_relay.py target add --id anthropic-backup --provider anthropic --model anthropic-example-claude --credential primary --capability text --capability tool_calling --capability vision --context-window 200000 --host codex --host claude-code --json

# 3) OpenAI 兼容：文本 + 工具调用 + 服务端联网搜索，上下文 256000
python scripts\multi_relay.py provider add --id openai-alike --name "OpenAI-compatible demo" --protocol responses-compatible --base-url https://api.openai.example/v1 --auth vault --capability text --capability tool_calling --capability server_web_search --context-window 256000 --json
python scripts\multi_relay.py credential add --provider openai-alike --id primary --json
python scripts\multi_relay.py target add --id openai-web --provider openai-alike --model openai-example-web --credential primary --capability text --capability tool_calling --capability server_web_search --context-window 256000 --host codex --host claude-code --json
```

组成 sticky 池并绑定 Agent：

```powershell
python scripts\multi_relay.py pool add --id general --target deepseek-primary --target anthropic-backup --target openai-web --strategy sticky --capability text --capability tool_calling --host codex --host claude-code --json
python scripts\multi_relay.py agent set --name implementer --description "Bounded implementation worker" --pool general --capability text --capability tool_calling --trust standard --sandbox-mode workspace-write --host codex --host claude-code --instructions "Implement only the assigned bounded task." --json
python scripts\multi_relay.py host apply codex --json
```

此时池按顺序优先选择 `deepseek-primary`；它发生可切换故障后 sticky 保持 `anthropic-backup`。能力差异意味着：`vision` 请求只匹配 `anthropic-backup`，`server_web_search` 请求只匹配 `openai-web`，两者都不具备的能力会在请求发出前返回 `no_eligible_target`，而不是静默降级。

若希望到期自动回到第一优先级，把策略改为 timed：

```powershell
python scripts\multi_relay.py pool strategy general timed --duration 2h --json
```

timed 池在切换后保持当前目标 2 小时，到期重新探测 `deepseek-primary`；首位仍冷却时继续使用当前健康目标。两种策略的详细行为见 [rotation.md](rotation.md)。