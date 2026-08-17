# 兼容、迁移、限制与排障

本文档说明 Multi Relay 的支持范围、从旧项目迁移的规则、能力边界、已知限制与常见故障。目录模型见 [catalog.md](catalog.md)，轮转语义见 [rotation.md](rotation.md)，凭据与安全见 [security.md](security.md)，两个宿主的生命周期见 [codex.md](codex.md) 与 [claude-code.md](claude-code.md)。

## 1. 支持矩阵

| 维度 | 支持范围 |
| --- | --- |
| 宿主 | Codex 桌面运行时；Claude Code CLI |
| 协议 | `codex-native`、`responses-compatible`、`chat-completions-compatible`、`deepseek-chat`、`anthropic-messages` |
| 平台 | Windows、macOS、Linux（凭据仓库分别对应 Windows Credential Manager、macOS Keychain、Linux Secret Service） |
| Python | 3.11+ |
| 入站协议面 | Responses（`POST /v1/responses`）与 Anthropic Messages（`POST /v1/messages`） |
| 兼容入口 | 旧 Provider 路径和旧管理端点仅作受测兼容映射，不作为核心内部协议 |

## 2. 宿主行为差异

| 能力 | Codex | Claude Code | 说明 |
| --- | --- | --- | --- |
| 自定义子代理角色 | 是 | 是 | 分别生成宿主原生配置 |
| 子代理自定义模型 | 是 | 是 | 角色绑定 relay alias |
| 跨 Provider 轮转 | 是 | 是 | 本地网关统一负责 |
| 多 API Key | 是 | 是 | OS vault + CredentialRef |
| 父代理保持原模型 | 是 | 否 | 使用 launcher 时 Claude Code 父请求也经过网关 |
| 订阅 OAuth 轮转 | 否 | 否 | 只支持 API-backed target |
| 视觉 / 音频 | 取决于请求与目标能力 | 取决于请求与目标能力 | 目标未声明时请求前过滤 |
| 联网搜索 | 宿主工具或目标原生能力 | 宿主工具或目标原生能力 | 不把普通文本模型标成联网 |
| 工具调用 | 是 | 是 | 统一 tool schema 与事件 |

Codex 侧父代理的 `model`、`model_provider`、`model_reasoning_effort` 保持不变；Claude Code 因为所有请求共用同一个 `ANTHROPIC_BASE_URL`，必须通过 `multi-relay launch claude-code` 启动，父请求经 `multi-relay-default`、子代理经 `multi-relay-agent-<name>` 进入同一网关。

## 3. 从旧项目迁移

### 3.1 状态目录

新安装的权威状态位于产品状态目录：

- Windows：`%LOCALAPPDATA%\multi-relay`
- macOS：`~/Library/Application Support/multi-relay`
- Linux：`$XDG_STATE_HOME/multi-relay`，缺失时用 `~/.local/state/multi-relay`

以下旧目录只作为迁移来源，读取优先级从新到旧：

- `$CODEX_HOME/codex-multi-relay`
- `$CODEX_HOME/codex-deepseek-relay`
- `$CODEX_HOME/codex-deepseek-subagent`

只有 manifest、受管标记与校验和共同证明所有权时才接管；没有证明的相似块或用户修改过的文件返回 `conflict`，不删除。规范状态与旧状态同时存在但 manifest 不一致时返回 `state_conflict`，需要用户先收敛两边再迁移。

### 3.2 旧受管块与标记

- 旧 `# BEGIN CODEX-MULTI-RELAY PROVIDERS` / `# BEGIN CODEX-DEEPSEEK-FANOUT` 系列 Provider 与角色块，只有在旧 manifest 证明所有权时才被移除并恢复原值，否则 `conflict`。
- 旧 `<!-- BEGIN CODEX-MULTI-RELAY -->` 与 `<!-- BEGIN CODEX-DEEPSEEK-FANOUT -->` 指令块在迁移、禁用与卸载时移除；新指令块使用 `<!-- BEGIN MULTI-RELAY -->` 与 `<!-- END MULTI-RELAY -->`。
- 旧 `[DeepSeek task]` 交接标记只为已安装配置的升级兼容保留识别；新写入一律使用 `[Relay task: <target>]`，并且每次 spawn、follow-up 或 send 前都必须输出与子任务消息完全一致的可见交接块，同时显式设置目录中的 `agent_type` 与 `fork_turns="none"`（或正数局部上下文）。

### 3.3 目录 schema 迁移

- schema 1 目录自动无损迁移到 schema 2：每个旧 vault Provider 生成 `primary` 凭据引用，每个旧 Agent 的 provider + model 组合生成确定性 target id，每个旧 Agent 生成同名或稳定派生的单目标 pool，相同组合去重，Agent 的指令、sandbox、skills、MCP、priority、trust 原样保留。
- 迁移前先备份原文件，再以原子方式写 schema 2；重复运行幂等，写入失败时旧文件不变。
- 旧 `codex-native` Agent 生成 host-native target，只允许 codex 宿主。
- 更早的 schema 4 manifest 会按已选模型、思考强度与并发转换为 schema 2 目录。

### 3.4 旧凭据

旧 DeepSeek 凭据目标 `codex-deepseek-api-key` 只用于一次性迁移读取：先验证新的规范引用可读，再删除旧目标；删除失败时保留并报告 `cleanup_pending`，不泄露内容。规范命名见 [security.md](security.md)。

### 3.5 兼容入口与旧别名

- 旧路径 `POST /providers/{provider_id}/responses` 保留受测兼容映射，内部立即解析为单目标临时 pool；文档不再推荐。
- Windows 批处理入口 `configure-multi-relay.cmd` 可保留一段兼容期，但所有输出与文档使用新名称。
- 旧 Python 类名 `FanoutManager` 保留为 `RelayManager` 的兼容别名；`bridge.py`、`toml_config.py` 等旧模块继续作为薄包装被新实现调用。

产品对外统一命名为 **Multi Relay**，仓库 slug 为 `multi-relay`；旧名称只保留在上述迁移代码与本文档中。

## 4. 能力边界

- 能力集合为 `text`、`vision`、`audio`、`tool_calling`、`server_web_search`。`route` 命令与受管 AGENTS.md 块仍接受旧名称 `tools`、`web`（兼容映射）。
- `vision`、`audio` 与联网搜索默认属于主代理；只有子代理显式声明全部所需能力才有资格路由。
- 联网搜索能力区分 `server_web_search`（Provider 原生）与宿主工具；声明 `web` 的旧式 Agent 必须配置真实可用的 MCP `url` 或 `command`，只写标签不算可用。
- `high-risk` 工作要求 `trust=high`，并始终由主代理做最终验证；没有合格目标时返回 `parent_required`，不静默换 Provider、换模型或降级能力。
- 视觉输入只接受 HTTPS 图片 URL 或合法 base64 图片块，限制单图大小、总图数与总请求体大小，不把图片写入临时明文文件；目标缺少 `vision` 时在发起请求前返回 `no_eligible_target`。
- 工具 schema 保留 `properties`、`required`、`enum`、`items`、`additionalProperties`；不支持的 schema 关键字在请求前明确报错或走已测试的降级规则。并行工具调用只在宿主与目标都支持时启用，`tool_result` 的错误标志双向保留。
- 未知请求字段不静默丢弃：可安全忽略的记 warning，影响语义的返回 `request_invalid`。

## 5. 事务与回滚

所有配置修改共用进程锁；写入前生成候选内容并完成解析，再为每个目标保存原始字节、权限与校验和。正式文件用同目录临时文件原子替换，manifest 最后写入；任一目标写入或验收失败时恢复事务前的精确文件状态。备份位于产品状态目录的 `backups/<timestamp>-<operation>/`。

重复 setup 保留当前自定义目录与首次安装前的受管字段值。disable、enable、uninstall 使用同一事务机制；disabled 状态下的 apply、repair 与目录变更保持 disabled，不会隐式重新生成角色或路由指令。卸载只删除 manifest 证明由本工具创建、且未被改写的文件；冲突文件保留并报告。

## 6. 已知限制

- 不把 Claude Code 订阅 OAuth 凭据导出、复制或混入 API 目标池；不把 Codex 原生订阅目标伪装成通用 HTTP Provider。
- 已经产生工具副作用或可见输出后，不自动跨模型续写同一请求；committed 后的失败返回标准化终止错误。
- 不根据猜测自动购买、充值或修改 Provider 账户。
- 不提供集中式云端密钥托管，也没有 GUI；CLI 与 JSON 输出是完整管理入口。
- 不提供明文凭据文件回退；Linux 需要可用的 Secret Service。
- 网关只监听 loopback，请求体上限 1 MiB；timed 时长硬上限一年。
- 没有真实凭据时只能运行单元与模拟集成测试，不能声称模型或能力已在线确认。
- 当前 Codex 运行时若只能靠替换正式模型目录加载自定义 Provider，安装返回 `unsupported_live_catalog` 并停止。
- OpenAI Provider 产生的不透明 compaction item 无法跨 Provider 解密，遇到时明确失败而不是丢弃上下文。

## 7. 故障排查

| 状态或错误 | 含义 | 处理 |
| --- | --- | --- |
| `not_configured` | 尚未安装 | 运行 `setup` |
| `ready` | 目录、配置、角色与适用验收均通过 | 无需处理 |
| `partial` | 受管文件漂移或某项检查未过 | 运行 `repair` 或 `test --host` 查看检查项 |
| `disabled` | 已禁用但保留目录与凭据 | 需要时运行 `enable` |
| `legacy` / `legacy_requires_setup` | 检测到旧安装 | 先 `setup` 或 `repair` 完成受校验迁移 |
| `future` / `unsupported_manifest_schema` | 由更新版本创建 | 升级 Multi Relay 后再操作 |
| `state_conflict` | 规范状态与旧状态并存且不一致 | 收敛旧目录后重试迁移 |
| `conflict` | 文件或块没有所有权证明 | 保留用户内容，不要覆盖 |
| `credential_missing` | vault 中缺少凭据 | 用 `credential add` 或 setup 在本机掩码输入 |
| `model_unavailable` | 模型不存在 | 停止，不猜测近似模型名 |
| `parent_required` | 没有满足能力或信任边界的子代理 | 任务留在主代理 |
| `no_eligible_target` | 目标全部被过滤或冷却 | 查看 `pool status` 的拒绝原因与 `retry_at` |
| `local_token_expired` | 网关本地令牌过期 | 重新启动网关 |
| `operation_in_progress` | 另一个管理器操作持锁 | 稍后重试，不并发修改 |
| `codex_not_found` | 找不到 Codex 运行时 | 设置 `CODEX_DESKTOP_BIN` 或 `--codex-bin` |
| `claude_code_not_found` | 找不到 Claude Code | 安装后用 `--claude-bin` 或 `CLAUDE_CODE_BIN` |
| `project_required` / `project_not_found` | project scope 缺项目路径 | 显式传 `--project` |
| `unsafe_provider_url` | Provider URL 不安全 | 改用 HTTPS（loopback 例外） |
| `unknown_pool` / `unknown_target` | 引用悬空 | 校验目录并修复引用 |
| `compatibility_failed` | 正式验收未通过 | 查看 `test` 的检查项；事务已回滚 |

网关排障：`gateway start`、`gateway status`、`gateway stop` 管理本地网关；`pool status <id>` 与 `pool reset <id>` 检查或复位轮转状态；`catalog --json` 查看当前生效目录。所有回滚备份位于产品状态目录的 `backups/` 下。
