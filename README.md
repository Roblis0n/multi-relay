**简体中文** | [English](./README_EN.md)

<p align="center">
  <img src="./assets/readme/hero.png" width="100%" alt="Codex 父任务按能力路由到多模型子代理，联网、视觉、音频和高风险任务保留在主代理">
</p>

<h1 align="center">Codex Multi Relay</h1>

<p align="center">为每个 Codex 子代理选择合适的 Provider 与模型，同时给主代理保留明确的能力边界。</p>

项目现在以无密钥的 `catalog.json` 管理 Provider 和 Agent。每个子代理可以独立设置协议、模型、能力、优先级、信任级别、沙箱、MCP 和 Skill。默认混合目录让 `default`、`worker`、`explorer` 使用经验证的 `deepseek-v4-pro`，让高信任 `reviewer` 使用原生 Codex；主任务继续使用用户原来的模型。

## 能得到什么

- 支持 `codex-native`、`responses-compatible`、`chat-completions-compatible`、`deepseek-chat`；
- 可用 CLI 添加或删除 Provider、创建任意命名的 Agent、替换模型并查询最终路由；
- 根据 `text`、`tools`、`vision`、`audio`、`web` 与 `high-risk` 边界选择子代理；没有完整能力匹配时返回 `parent_required`；
- 默认最多 8 路并发，只 fan-out 相互独立的工作项；
- 保持 `multi_agent_v2`，通过显式 `agent_type` 和 `fork_turns="none"` 固定实际子代理；
- Chat Completions 协议通过仅监听 `127.0.0.1:42137` 的本机 Relay 转换为 Responses；Responses 兼容端直接连接；
- 每次派生前显示完整的 `[Relay task: <target>]` 交接，适配层按目标精确匹配，宿主密文不会被误发给外部 Provider；
- vault 凭据只保存在 Windows Credential Manager 或 macOS Keychain，且按 Provider 隔离；
- 所有目录和配置变更都解析、校验并事务写入，失败自动回滚；
- disable 后仍可编辑目录，但不会隐式重新启用；只有 enable 恢复角色和路由规则。

<p align="center">
  <img src="./assets/readme/architecture.svg" width="100%" alt="架构图：Codex 父任务经能力路由选择原生 Codex、Responses 或 Chat Completions 子代理">
</p>

<p align="center">
  <img src="./assets/readme/workflow.svg" width="100%" alt="多模型目录的验证与回滚流程，以及视觉、音频、联网和高风险任务的主代理边界">
</p>

## 快速开始

要求：Windows 或 macOS、Python 3.11+、Codex 桌面运行时。只有使用 vault Provider 的目录才需要对应 API Key；纯原生目录不需要额外凭据。

如果作为 Skill 安装：

```bash
npx skills add Roblis0n/codex-multi-relay -g -y
```

也可以在项目目录双击 `configure-multi-relay.cmd`，安装默认混合目录。

Windows 终端方式：

```powershell
python scripts\multi_relay.py setup --preset hybrid
```

macOS：

```bash
python3 scripts/multi_relay.py setup --preset hybrid
```

只想使用 Codex 原生子代理时运行：

```bash
python3 scripts/multi_relay.py setup --preset native
```

`native` 不读取凭据，也不联网。`hybrid` 会在当前终端显示本地掩码输入框；Key 不要发到聊天窗口。只有模型、兼容性和正式验收全部通过才返回 `ready`，否则事务恢复原配置。

## 安装结果

管理器写入 `$CODEX_HOME/codex-multi-relay/catalog.json`，再按目录生成 Agent TOML。默认 `hybrid` 会创建：

```text
$CODEX_HOME/agents/default.toml
$CODEX_HOME/agents/worker.toml
$CODEX_HOME/agents/explorer.toml
$CODEX_HOME/agents/reviewer.toml
```

协议映射如下：

| 协议 | 路径 | 认证 |
| --- | --- | --- |
| `codex-native` | Codex 原生 Provider | Codex 登录态 |
| `responses-compatible` | 直连 Provider Responses API | vault 或无认证 |
| `chat-completions-compatible` | 本机 Relay 转换 Chat Completions | vault 或无认证 |
| `deepseek-chat` | 本机 Relay 的 DeepSeek 适配与思考续接 | vault |

同时在 `$CODEX_HOME/AGENTS.md` 写入可移除的能力路由规则，并保证：

- 顶层主模型、主 Provider、主思考强度不变；
- 并发下限为 8，用户已有更高值时保留；
- 每个子代理显式使用 `agent_type` 与 `fork_turns="none"`（或正数局部上下文）；
- 每次 spawn、follow-up 或 send 前先输出与目标一一对应的 `[Relay task: <target>]` 交接块；缺少交接时适配层严格拒绝；
- 不替换正式模型目录；
- 不关闭新版多代理；
- 不满足能力或信任边界时留在主代理，不静默换 Provider 或模型。

## 日常使用

配置成功后，无需重复运行 setup。直接给 Codex 正常任务即可：

```text
并行调查这四个互相独立的模块，最后给出综合结论。
```

受管规则会先检查能力再 fan-out；共享状态、同一文件写入和顺序依赖任务仍由主代理串行处理。

Codex 会在本机工具层之前保护原生子代理消息，自定义 Provider 无法解开宿主密文。受管规则因此先把同一份完整任务以 `[Relay task]` 可见交接块写到父任务评论区，再调用原生子代理工具。适配层只接受目标和顺序均精确匹配的交接；找不到时返回错误，不让外部 Provider 根据密文猜任务。

`vision`、`audio` 和 `web` 默认留给主代理。子代理只有显式声明全部能力才有资格；web 代理还必须带真实 MCP server。`high-risk` 请求需要 `trust=high`，并始终由主代理最终验证。

## 管理命令

以下以 Windows 为例；macOS 把 `python` 换成 `python3`，路径分隔符换成 `/`：

```powershell
python scripts\multi_relay.py status --json
python scripts\multi_relay.py setup --preset hybrid --json
python scripts\multi_relay.py setup --preset native --json
python scripts\multi_relay.py catalog --json
python scripts\multi_relay.py apply --json
python scripts\multi_relay.py provider list --json
python scripts\multi_relay.py provider add --id vendor --name Vendor --protocol responses-compatible --base-url https://api.vendor.example/v1 --auth vault --capability text --capability tools --context-window 128000 --json
python scripts\multi_relay.py provider remove vendor --json
python scripts\multi_relay.py agent list --json
python scripts\multi_relay.py agent set --name vendor-worker --description "Vendor worker" --provider vendor --model vendor-model --capability text --capability tools --instructions "Implement the assigned bounded task." --json
python scripts\multi_relay.py agent remove vendor-worker --json
python scripts\multi_relay.py route --capability text --capability tools --json
python scripts\multi_relay.py test --json
python scripts\multi_relay.py repair --json
python scripts\multi_relay.py disable --json
python scripts\multi_relay.py enable --json
python scripts\multi_relay.py uninstall --json
python scripts\multi_relay.py uninstall --remove-credential --json
```

- 普通 uninstall 保留 Key。
- 只有带 `--remove-credential` 的 uninstall 才删除系统凭据。
- `provider remove` 会拒绝删除仍被 Agent 引用的 Provider。
- `repair` 保留当前目录；重复 setup 也不会把自定义目录重置为默认值。
- disabled 状态下可以更新目录，但只有 `enable` 会重新生成角色和路由指令。
- 自动发现 Codex 失败时，用 `CODEX_DESKTOP_BIN` 指定桌面运行时。

## 安全与回滚

管理器使用进程锁、解析后写入、同目录原子替换和逐文件校验和。备份位于：

```text
$CODEX_HOME/codex-multi-relay/backups/
```

Key 不进入配置、命令参数、临时文件、备份、日志、异常或 Git。旧 `$CODEX_HOME/codex-deepseek-relay` 和 `$CODEX_HOME/codex-deepseek-subagent` 状态只作为迁移来源；旧 manifest、受管 marker 与校验和无法证明所有权时返回 `conflict`，不会接管用户内容。

更多细节见 [兼容性与安全边界](references/compatibility.md) 和 [Skill 执行规则](SKILL.md)。

## 开发验证

```bash
python -m unittest discover -s scripts -p "test_*.py" -v
python -m compileall -q scripts
python scripts/check_runtime_contract.py
python scripts/check_codex_bridge_runtime.py --codex-bin <path-to-codex>
```

## 品牌说明

本项目是独立社区工具，与 OpenAI、DeepSeek 或其他 Provider 不存在隶属、合作或官方背书关系。相关名称与标志归各自权利人所有。

## License

[MIT](./LICENSE)
