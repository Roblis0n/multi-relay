# 兼容性与安全边界

## 目录

- [支持范围](#支持范围)
- [正式配置](#正式配置)
- [Provider 与能力门禁](#provider-与能力门禁)
- [原生验收证据](#原生验收证据)
- [凭据安全](#凭据安全)
- [事务与迁移](#事务与迁移)
- [已知边界](#已知边界)

## 支持范围

- Windows 或 macOS
- Python 3.11+
- 可执行的 Codex 桌面运行时
- 可选的原生 Codex、Responses 兼容、Chat Completions 兼容或 DeepSeek Provider
- 用户顶层配置中存在明确的主模型

目录支持 `codex-native`、`responses-compatible`、`chat-completions-compatible`、`deepseek-chat`。每个 Agent 独立声明 Provider、模型、能力、上下文、思考强度、信任级别、优先级、沙箱、MCP 和 Skill。Provider 或 Agent 标识按目录精确匹配，不跨 Provider 猜测同名模型。

默认 `hybrid` 目录仍在线验证 `deepseek-v4-pro`，并为 `default`、`worker`、`explorer` 配置 100 万 token 上下文；高信任 `reviewer` 使用原生 Codex。`native` 目录只包含 reviewer，不读取 DeepSeek 凭据，也不发起 Provider 网络请求。所有子代理设置都不影响主模型。

## 正式配置

默认 Codex Home 为 `~/.codex`：

- 主配置：`$CODEX_HOME/config.toml`
- Provider/Agent 目录：`$CODEX_HOME/codex-multi-relay/catalog.json`
- 子角色：`$CODEX_HOME/agents/<agent-name>.toml`
- 自动 fan-out 指令：`$CODEX_HOME/AGENTS.md` 中的受管块
- 状态、锁和备份：`$CODEX_HOME/codex-multi-relay/`

管理器只增加目录中启用的非原生 Provider、启用原生多代理和 agents，并把并发下限设为目录值（默认 8）。用户已有更高值时保留。`multi_agent_v2.enabled = true`，工具命名空间保持 `agents`。

`responses-compatible` 直接连接 Provider。`chat-completions-compatible` 与 `deepseek-chat` 指向本机 `http://127.0.0.1:42137/v1/providers/<provider>`：适配层仅绑定回环地址，将 Responses 消息、命名空间工具、并行工具调用和流式事件转换为 Chat Completions，再转换回 Codex Responses。`codex-native` 不生成自定义 Provider 块。

每次创建受管子代理都必须显式给出 `agent_type`，并使用 `fork_turns="none"` 或正数局部上下文。省略角色或使用全量上下文会继承父代理模型，因此受管 fan-out 规则明确禁止这种派生方式。

当前 Codex 会在 `spawn_agent`、`followup_task` 和 `send_message` 到达本机工具层之前保护消息正文。自定义 Provider 没有该宿主内容的解密接口。受管规则因此要求父代理在每次调用前输出可见、目标明确的 `[Relay task: <target>]` 交接块，内容与子任务消息完全一致。适配层从权威父子线程关系、目标和次序三方面匹配；缺块、歧义、目标不符或仍是密文时返回 `unresolved_agent_message`。旧 `[DeepSeek task]` 只为已安装配置的迁移兼容保留。

管理器不修改顶层 `model`、`model_provider`、`model_reasoning_effort`。主任务继续使用用户原来的 OpenAI 模型与登录方式。

正式配置不选中自定义模型目录。若当前 Codex 运行时只能依靠正式目录替换才能加载跨 Provider 子模型，门禁返回 `unsupported_live_catalog`，安装停止。

## Provider 与能力门禁

默认 DeepSeek Provider 在写正式配置前依次完成：

1. 使用系统凭据库中的 Key 查询 DeepSeek 模型目录，精确确认 `deepseek-v4-pro`。
2. 在一次性 Codex Home 中从 DeepSeek 的最高档 `max` 开始，按 `max`、`xhigh`、`high`、`medium`、`low`、`minimal` 逐档实测。
3. 选择 Codex 与 Provider 共同接受的最高档位。
4. 如果所有显式档位都失败，再测试省略思考键的 Provider 默认模式。
5. 在隔离环境验证 Provider 初始化，不使用 DeepSeek 自身承担重型父编排。
6. 事务写入正式候选配置后，由用户实际 Codex 父模型验证一个子代理、三个并发子代理、工具调用、DeepSeek 安全步骤摘要和续接；任一项失败即回滚。

隔离环境不复制用户的认证文件、线程数据库或会话记录，只构造最小 Provider 探测配置。退出后删除整个临时目录。完整原生验收不在这里重复执行，避免让 DeepSeek 作为父编排器造成无意义的长时间推理；正式验收仍覆盖全部路由和生命周期证据。

自定义目录另有硬边界：

- Agent 的能力必须是 Provider 能力的子集；上下文不得超过 Provider 上限。
- `vision`、`audio`、`web` 默认保留在主代理，只有 Agent 声明全部请求能力时才可路由。
- web Agent 必须包含真实的 MCP `url` 或 `command`，只写 `web` 标签不算可用。
- `high-risk` 要求 `trust=high`，主代理仍负责最终验证。
- 没有合格 Agent 时返回 `parent_required`，不静默改用其他 Provider、模型或能力。
- Provider URL 必须使用 HTTPS；仅 `localhost`、`127.0.0.1`、`::1` 可使用 HTTP。Provider 重定向不得改变 origin，Relay 上游重定向全部拒绝，防止 Authorization 外送。

## 原生验收证据

正式写入后，管理器在真实 Codex Home 中创建新的只读验收会话。成功必须同时具备：

- 一个 `default` 子线程返回 `DEEPSEEK_SINGLE_OK`；
- 三个新的 fan-out 子线程分别使用 `default`、`worker`、`explorer`；
- 三个子线程都完成并返回各自口令；
- 至少一个子线程 rollout 记录真实工具调用；
- 首个子线程收到后续任务并返回 `DEEPSEEK_RESUME_OK`；
- SQLite `threads` 元数据确认所有子线程的 Provider、模型、思考强度和角色；
- 父线程的 Provider、模型和思考强度与写入前一致。

口令与数据库/rollout 证据缺一不可。父代理转述、自述或单纯文本命中不能代替权威元数据。

当前 `multi_agent_v2` 的 `codex exec --json` 可能只显示等待事件，而不逐条显示 `spawn_agent`。验收器不会据此误判失败：它从 `threads.source` 的 `parent_thread_id` 还原真实父子关系，再从父线程 rollout 核对“一次三路 spawn、随后才 wait”，并从各子线程 rollout 核对最终口令、工具调用、续接任务和完成状态。

## 凭据安全

每个 `auth=vault` Provider 使用独立凭据目标。DeepSeek 为兼容旧安装继续使用 `codex-deepseek-api-key`；其他 Provider 使用 `codex-multi-relay-<provider>-api-key`：

- Windows：Windows Credential Manager
- macOS：Keychain

`setup` 或 `provider add` 在终端内本地显示掩码输入。Key 不进入聊天、TOML、JSON、Markdown、命令参数、临时文件、备份、日志或异常消息。Provider 通过固定 helper 命令按需从凭据库读取，helper 的标准输出只包含原始 Key。直连 Provider 由 Codex 使用该凭据；Chat Completions Provider 只由本机 Relay 转发到目录中精确配置的同 origin 上游。

DeepSeek 思考模式在工具调用续接时要求回放先前的思考内容。适配层不会把明文思考写入磁盘或日志，而是用由当前 Key 派生的完整性保护密文放进 Codex 的不透明 reasoning item，并仅在下一次请求内存中解封后转发给 DeepSeek。

Codex UI 中可见的 reasoning summary 由适配层根据已经实际生成的工具调用构造，只描述“检查本地状态”“修改文件”“查询资料”等操作阶段。它不包含 DeepSeek 的原始私有思维链，也不会把私有推理伪装成可审计步骤。

普通卸载和 Provider 删除都保留 Key。只有显式使用 `--remove-credential` 才删除对应系统凭据。

## 事务与迁移

所有修改共用一个进程锁。写入前生成候选 TOML/JSON 并完成解析，再为每个目标保存原始字节、权限和校验和。正式文件使用同目录临时文件原子替换，manifest 最后写入。

任意目标写入或正式验收失败时恢复事务前的精确文件状态。备份位于：

```text
$CODEX_HOME/codex-multi-relay/backups/<timestamp>-<operation>/
```

重复 setup 保留当前自定义目录和第一次安装前的受管字段值。disable、enable 和 uninstall 都使用同一事务机制。disabled 状态下的 `apply`、`repair` 和目录变更保持 disabled，不会重新生成 Agent 或路由指令。

读取状态的优先级为 `$CODEX_HOME/codex-multi-relay` → 旧 `$CODEX_HOME/codex-deepseek-relay` → 更早的 `$CODEX_HOME/codex-deepseek-subagent`。旧目录和 marker 仅作为迁移来源：只有 manifest、受管标记和校验和共同证明所有权时才接管；无 manifest 的相似块或用户修改过的文件返回 `conflict`，不删除。

## 已知边界

- 没有真实 DeepSeek Key 时只能运行单元与模拟集成测试，不能声称模型或思考强度已在线确认。
- 当前桌面运行时必须暴露原生 collaboration tools 与线程元数据。
- 父代理必须遵循受管交接块规则；若其他更具体的指令阻止交接，自定义 Provider 子任务会明确失败，不会降级为密文猜测。
- 默认 DeepSeek 角色只声明 `text` 与 `tools`；主代理或合格的原生 reviewer 处理视觉、音频与最终综合。
- Provider 不具备的 Codex 托管工具不会伪装成可用工具；web 路由必须有真实 MCP server。
- OpenAI Provider 生成的不透明 compaction item 无法跨 Provider 解密；适配层遇到它会明确失败而不是丢弃上下文。100 万 token 子角色上下文会显著推迟这一边界。
- 自动 fan-out 只处理互相独立的任务；同一文件的并发写入、共享数据库写入或有依赖顺序的步骤不得并发。
