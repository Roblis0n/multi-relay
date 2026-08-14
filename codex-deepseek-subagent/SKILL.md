---
name: codex-deepseek-subagent
description: Use when a user asks to configure, validate, test, repair, disable, enable, or uninstall DeepSeek as Codex native subagents, or when DeepSeek child fan-out, custom-provider routing, model availability, reasoning effort, credential-vault storage, or legacy single-agent migration is involved.
---

# Codex DeepSeek Fan-out

只维护 Codex 与 DeepSeek 的子代理配置。日常编码、探索和评审任务不重复运行本 Skill。

## 核心契约

- 保持顶层主模型、主 Provider 和主模型思考强度不变。
- 将 Codex 内置 `default`、`worker`、`explorer` 子角色统一路由到在线验证过的 `deepseek-v4-pro`。
- 三个角色统一声明 `model_context_window = 1000000`，与 DeepSeek V4 Pro 官方上下文长度一致。
- 逐档实测 Codex 与 Provider 共同支持的最高思考强度；没有共同配置项时省略该键并报告 Provider 默认值。
- 默认允许 8 个并发子线程。仅对两个及以上独立、边界明确的工作项 fan-out；重叠写入、共享可变状态和顺序任务留给主代理。
- 保持 `multi_agent_v2` 开启；每次派生子代理都显式选择 `agent_type`（`default`、`worker` 或 `explorer`）并使用 `fork_turns="none"` 或正数局部上下文，禁止因全量继承而回到 Sol 主模型。
- 每次 `spawn_agent`、`followup_task` 或 `send_message` 前必须输出受管 `[DeepSeek task: <target>]` 可见交接块；适配层只接受精确匹配的明文交接，绝不把宿主 `gAAAA…` 密文转发给 DeepSeek。
- Codex 只向本机 `127.0.0.1:42137` 发送 Responses；受管适配层再转换成 DeepSeek Chat Completions。它不监听局域网，也不替换正式模型目录、不关闭新版多代理、不静默回退为 OpenAI 子模型。
- DeepSeek 作为纯文本子模型使用；由主代理先把视觉材料整理成文字事实。

## 触发后的流程

1. 先运行 `status --json`。
2. 首次配置或重新验证时运行 `setup --json`；修复请求运行 `repair --json`。
3. 缺少凭据时，让管理器在用户本机显示掩码输入框。不要让用户在聊天中发送 Key，也不要把 Key 放进命令参数。
4. 安装前在不复制用户认证或会话数据的隔离目录验证 Provider。随后事务写入正式配置，并由用户实际 Codex 父模型运行一次原生单代理、三路 fan-out、工具、思考内容续接和子线程续接验收；任一项失败即回滚。
5. 最终只报告状态、模型、实际思考强度、三个角色、并发上限和备份位置。

## 管理命令

入口为 `scripts/codex_deepseek.py`。Windows 使用 `py -3`，macOS 使用 `python3`：

```text
python3 <skill-dir>/scripts/codex_deepseek.py status --json
python3 <skill-dir>/scripts/codex_deepseek.py setup --json
python3 <skill-dir>/scripts/codex_deepseek.py test --json
python3 <skill-dir>/scripts/codex_deepseek.py repair --json
python3 <skill-dir>/scripts/codex_deepseek.py disable --json
python3 <skill-dir>/scripts/codex_deepseek.py enable --json
python3 <skill-dir>/scripts/codex_deepseek.py uninstall --json
python3 <skill-dir>/scripts/codex_deepseek.py uninstall --remove-credential --json
```

- `status`：只读检查，不提示凭据，不写文件。
- `setup` / `repair`：本地掩码收集凭据，验证模型与兼容性，事务安装并验收。
- `test`：使用正式配置做原生验收。
- `disable`：移除三个角色文件与 fan-out 指令，保留 Provider 和凭据。
- `enable`：无需联网，恢复上次已验证的三个角色与 fan-out 指令。
- `uninstall`：恢复受管配置并保留凭据；只有明确要求时才删除凭据。

角色文件必须是：

```text
$CODEX_HOME/agents/default.toml
$CODEX_HOME/agents/worker.toml
$CODEX_HOME/agents/explorer.toml
```

凭据目标必须是 Windows Credential Manager 或 macOS Keychain 中的 `codex-deepseek-api-key`。

## 状态处理

- `ready`：模型、隔离 Provider 门禁、正式原生验收和静态配置均通过。
- `not_configured`：尚未安装。
- `credential_missing`：引导用户直接运行 `setup`，由本机掩码输入框收集。
- `model_unavailable`：明确说明 `deepseek-v4-pro` 当前不可用；不安装近似名称或占位模型。
- `compatibility_failed`：报告失败检查项；正式写入前失败则不改配置，写入后失败则回滚。
- `conflict`：报告用户自有冲突文件；不要覆盖。
- `disabled`：保留 Provider 与凭据，可运行 `enable` 恢复。
- `operation_in_progress`：稍后重试，不并发修改。

详细文件边界、验收证据和回滚规则见 [references/compatibility.md](references/compatibility.md)。
