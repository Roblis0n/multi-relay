# Codex 宿主指南

本文档说明如何为 Codex 安装、测试、禁用、启用与卸载 Multi Relay，以及安装后哪些文件由管理器维护、请求如何经本地网关路由。目录与别名见 [catalog.md](catalog.md)，Claude Code 宿主见 [claude-code.md](claude-code.md)。

## 1. 不变式

Codex 适配器只管理自己创建的内容，绝不修改以下顶层配置：

- `model`
- `model_provider`
- `model_reasoning_effort`

父代理继续使用用户原来的 Codex 模型与登录方式，轮转只作用于通过 relay alias 路由的子代理。管理器写入的受管块包括：

- `features.multi_agent_v2.enabled = true`，工具命名空间保持 `agents`，并发下限取目录值（默认 8），用户已有更高值时保留。
- 为每个 API-backed 池生成一个 `[model_providers.multi-relay]` Provider，`base_url` 指向仅监听 loopback 的本地网关 `http://127.0.0.1:42137/v1`，`wire_api = "responses"`。该 Provider 的 `auth` 是一个本地 helper 命令：Codex 向网关出示短期本地令牌，而不是上游 API Key。
- 按目录生成 `$CODEX_HOME/agents/<name>.toml`；非原生 Agent 的 `model` 写为稳定的 `multi-relay-agent-<name>` alias 且 `model_provider = "multi-relay"`，因此池轮转后无需重写 Agent 配置。全原生 Agent 不写 model/model_provider，继续走宿主登录态。

## 2. 安装

前置条件：Windows、macOS 或 Linux，Python 3.11+、可执行的 Codex 运行时，`$CODEX_HOME/config.toml` 存在（默认 `~/.codex`）。仅使用 vault Provider 的目录才需要 API Key；纯原生目录不需要凭据，也不联网。

默认混合预设：

```powershell
python scripts\multi_relay.py setup --preset hybrid --json
```

- `hybrid` 把 `default`、`worker`、`explorer` 路由到经验证的 DeepSeek target，把高信任 `reviewer` 留在原生 Codex；首次安装会在当前终端显示本地掩码输入框，Key 不要发到聊天窗口。
- `native` 只安装原生 reviewer，不读取凭据，也不发起 Provider 网络请求：

```powershell
python scripts\multi_relay.py setup --preset native --json
```

只安装或重新安装 Codex 宿主（不影响 Claude Code 宿主配置）：

```powershell
python scripts\multi_relay.py setup --preset hybrid --host codex --json
```

自动发现 Codex 失败时用 `CODEX_DESKTOP_BIN` 或 `--codex-bin` 指定桌面运行时。安装会先解析并校验整个目录，再把受管文件事务写入；失败自动回滚。

## 3. 测试与验证

```powershell
python scripts\multi_relay.py test --host codex --json
```

验证范围包括：Provider 初始化、单个子代理、三路并发 fan-out、真实工具调用、续接、子线程 Provider/模型/思考强度元数据，以及父线程 Provider/模型/思考强度与写入前一致。原生验收需要真实的 Codex 运行时与配置的凭据；没有真实凭据时只能运行单元与模拟集成测试，不能声称模型或能力已在线确认。

`status --json` 报告 `ready`、`partial`、`disabled`、`legacy` 或 `not_configured`，并给出各 Provider 凭据是否存在与受管文件是否漂移。

## 4. 日常使用

安装完成后无需重复运行 setup。直接给 Codex 正常任务；目录中的受管规则会先做能力路由，再对相互独立、边界明确的工作项 fan-out（默认最多 8 路）：

- 每次 `spawn_agent`、`followup_task` 或 `send_message` 都显式设置目录中的 `agent_type`，并使用 `fork_turns="none"` 或正数局部上下文；不能用全量上下文继承代替模型路由。
- 调用原生子代理工具前，先把同一份完整任务以 `[Relay task: <target>]` 可见交接块写到父任务评论区，内容与子任务消息完全一致；适配层只接受目标与顺序精确匹配的交接，缺块、歧义、目标不符或仍是密文时返回 `unresolved_agent_message`。
- 重叠写文件、共享数据库写入或顺序依赖的工作不得并发，留在父代理串行处理。

也可以直接查询目录路由结果：

```powershell
python scripts\multi_relay.py route --capability text --capability tools --json
python scripts\multi_relay.py route --capability vision --high-risk --json
```

`route` 接受旧能力名 `tools`、`web`；目录 schema 2 内部使用 `tool_calling`、`server_web_search`。

## 5. 禁用与启用

```powershell
python scripts\multi_relay.py disable --host codex --json
python scripts\multi_relay.py enable --host codex --json
```

禁用会移除生成的 Agent 与受管指令块，保留目录、Provider、凭据与备份；禁用期间可以继续编辑目录，但不会隐式重新启用，只有 `enable` 重新生成角色与路由指令。`enable` 在恢复前重新校验目录与文件所有权。

## 6. 卸载

```powershell
python scripts\multi_relay.py uninstall --host codex --json
```

卸载只删除 manifest 证明由本工具创建、且当前内容未被用户改写的文件；用户修改过的文件会保留并报告 `conflict`。普通卸载保留所有 API Key。只有显式指定：

```powershell
python scripts\multi_relay.py uninstall --host codex --remove-credentials --json
```

才从操作系统凭据仓库删除目录中引用的 vault 凭据。

## 7. 受管文件

- 产品状态目录（Windows `%LOCALAPPDATA%\multi-relay`、macOS `~/Library/Application Support/multi-relay`、Linux `${XDG_STATE_HOME:-~/.local/state}/multi-relay`）：`catalog.json`、`manifest.json`、`runtime-state.json`、`gateway-state.json` 与备份。
- `$CODEX_HOME/config.toml`：只写 `[model_providers.multi-relay]` 与 `multi_agent_v2` 受管块，不动主模型三键。
- `$CODEX_HOME/agents/<name>.toml`：按目录生成。
- `$CODEX_HOME/AGENTS.md`：只写 `<!-- BEGIN MULTI-RELAY -->` 与 `<!-- END MULTI-RELAY -->` 之间的受管能力路由块。

所有写入都使用进程锁、解析后写入、同目录原子替换与逐文件校验和；备份位于产品状态目录的 `backups/` 下。

## 8. 故障排查

- `codex_not_found`：设置 `CODEX_DESKTOP_BIN` 或 `--codex-bin` 指向桌面运行时。
- `config_missing`：`$CODEX_HOME/config.toml` 不存在；先启动一次 Codex 生成配置。
- `conflict`：受管文件或目录中存在没有所有权证明的内容；保留用户文件，不要覆盖。
- `legacy` / `legacy_requires_setup`：先运行 `setup` 或 `repair` 完成受校验迁移，再做其他生命周期操作。
- `parent_changed`：候选配置意外改变了父模型；事务会拒绝并回滚。
- `unsupported_live_catalog`：当前 Codex 运行时只能靠替换正式模型目录加载自定义 Provider，安装停止。
- 子任务失败且提示 `unresolved_agent_message`：检查是否缺少或写错了 `[Relay task: <target>]` 交接块。
