# 兼容性与安全边界

## 目录

- [支持范围](#支持范围)
- [正式配置](#正式配置)
- [模型与思考强度门禁](#模型与思考强度门禁)
- [原生验收证据](#原生验收证据)
- [凭据安全](#凭据安全)
- [事务与迁移](#事务与迁移)
- [已知边界](#已知边界)

## 支持范围

- Windows 或 macOS
- Python 3.11+
- 可执行的 Codex 桌面运行时
- DeepSeek 官方模型目录与 Chat Completions 接口
- 用户顶层配置中存在明确的主模型

请求模型固定为 `deepseek-v4-pro`，但正式安装值必须来自带凭据的在线模型目录。服务端没有该模型时返回 `model_unavailable`，不创建别名。

三个子角色同时声明 `model_context_window = 1000000`，对应 DeepSeek V4 Pro 官方的 100 万 token 上下文；该设置只存在于子角色文件，不影响 Sol 主模型。

## 正式配置

默认 Codex Home 为 `~/.codex`：

- 主配置：`$CODEX_HOME/config.toml`
- 子角色：`$CODEX_HOME/agents/default.toml`、`worker.toml`、`explorer.toml`
- 自动 fan-out 指令：`$CODEX_HOME/AGENTS.md` 中的受管块
- 状态、锁和备份：`$CODEX_HOME/codex-deepseek-relay/`

管理器只增加用户级 DeepSeek Provider、启用原生多代理、启用 agents，并把并发下限设为 8。用户已经设置高于 8 的值时保留更高值。新版多代理保持 `multi_agent_v2.enabled = true`，工具命名空间保持 `agents`。

Codex 自定义 Provider 使用 Responses 协议，而 DeepSeek V4 使用 Chat Completions。管理器因此把 Provider 指向本机 `http://127.0.0.1:42137/v1`：适配层仅绑定回环地址，将 Responses 消息、命名空间工具、并行工具调用和流式事件转换为 DeepSeek Chat Completions，再把结果转换回 Codex Responses。它按需启动，不在局域网端口监听。

每次创建受管子代理都必须显式给出 `agent_type`，并使用 `fork_turns="none"` 或正数局部上下文。省略角色或使用全量上下文会继承父代理模型，因此受管 fan-out 规则明确禁止这种派生方式。

当前 Codex 会在 `spawn_agent`、`followup_task` 和 `send_message` 到达本机工具层之前保护消息正文，父线程与子线程 rollout 都只保留 `gAAAA…`。自定义 Provider 没有该宿主保护内容的解密接口。受管规则因此要求父代理在每次调用前输出一个可见、目标明确的 `[DeepSeek task: <target>]` 交接块，内容与子任务消息完全一致。适配层从权威父子线程关系、目标和消息出现次序三方面匹配；缺块、歧义、目标不符或仍是密文时返回 `unresolved_agent_message`，不会把密文发送给 DeepSeek。

管理器不修改顶层 `model`、`model_provider`、`model_reasoning_effort`。主任务继续使用用户原来的 OpenAI 模型与登录方式。

正式配置不选中自定义模型目录。若当前 Codex 运行时只能依靠正式目录替换才能加载跨 Provider 子模型，门禁返回 `unsupported_live_catalog`，安装停止。

## 模型与思考强度门禁

写正式配置前依次完成：

1. 使用系统凭据库中的 Key 查询 DeepSeek 模型目录，精确确认 `deepseek-v4-pro`。
2. 在一次性 Codex Home 中从 DeepSeek 的最高档 `max` 开始，按 `max`、`xhigh`、`high`、`medium`、`low`、`minimal` 逐档实测。
3. 选择 Codex 与 Provider 共同接受的最高档位。
4. 如果所有显式档位都失败，再测试省略思考键的 Provider 默认模式。
5. 在隔离环境验证 Provider 初始化，不使用 DeepSeek 自身承担重型父编排。
6. 事务写入正式候选配置后，由用户实际 Codex 父模型验证一个子代理、三个并发子代理、工具调用、DeepSeek 安全步骤摘要和续接；任一项失败即回滚。

隔离环境不复制用户的认证文件、线程数据库或会话记录，只构造最小 Provider 探测配置。退出后删除整个临时目录。完整原生验收不在这里重复执行，避免让 DeepSeek 作为父编排器造成无意义的长时间推理；正式验收仍覆盖全部路由和生命周期证据。

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

系统凭据目标为 `codex-deepseek-api-key`：

- Windows：Windows Credential Manager
- macOS：Keychain

`setup` 在终端内本地显示掩码输入。Key 不进入聊天、TOML、JSON、Markdown、命令参数、临时文件、备份、日志或异常消息。Provider 通过固定 helper 命令按需从凭据库读取，helper 的标准输出只包含原始 Key。Codex 将它放入发往本机回环适配层的授权头，适配层只把该授权头转发给 DeepSeek 官方接口。

DeepSeek 思考模式在工具调用续接时要求回放先前的思考内容。适配层不会把明文思考写入磁盘或日志，而是用由当前 Key 派生的完整性保护密文放进 Codex 的不透明 reasoning item，并仅在下一次请求内存中解封后转发给 DeepSeek。

Codex UI 中可见的 reasoning summary 由适配层根据已经实际生成的工具调用构造，只描述“检查本地状态”“修改文件”“查询资料”等操作阶段。它不包含 DeepSeek 的原始私有思维链，也不会把私有推理伪装成可审计步骤。

普通卸载保留 Key。只有 `uninstall --remove-credential --json` 才删除系统凭据。

## 事务与迁移

所有修改共用一个进程锁。写入前生成候选 TOML/JSON 并完成解析，再为每个目标保存原始字节、权限和校验和。正式文件使用同目录临时文件原子替换，manifest 最后写入。

任意目标写入或正式验收失败时恢复事务前的精确文件状态。备份位于：

```text
$CODEX_HOME/codex-deepseek-relay/backups/<timestamp>-<operation>/
```

重复 setup 保留第一次安装前的受管字段值。disable、enable 和 uninstall 都使用同一事务机制。

检测到上一代单角色安装时，只在旧 manifest、受管标记和校验和共同证明所有权后迁移。用户修改过的旧角色或目录文件返回 `conflict`，不删除。

## 已知边界

- 没有真实 DeepSeek Key 时只能运行单元与模拟集成测试，不能声称模型或思考强度已在线确认。
- 当前桌面运行时必须暴露原生 collaboration tools 与线程元数据。
- 父代理必须遵循受管交接块规则；若其他更具体的指令阻止交接，DeepSeek 子任务会明确失败，不会降级为密文猜测。
- DeepSeek 子角色按纯文本能力使用。主代理负责视觉理解和最终综合。
- DeepSeek 不提供的 Codex 托管工具不会伪装成可用工具；当前关闭状态的托管 web search 会被忽略，若请求启用则门禁明确失败。
- OpenAI Provider 生成的不透明 compaction item 无法跨 Provider 解密；适配层遇到它会明确失败而不是丢弃上下文。100 万 token 子角色上下文会显著推迟这一边界。
- 自动 fan-out 只处理互相独立的任务；同一文件的并发写入、共享数据库写入或有依赖顺序的步骤不得并发。
