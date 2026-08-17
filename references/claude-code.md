# Claude Code 宿主指南

本文档说明如何为 Claude Code 安装、启动、测试、禁用与卸载 Multi Relay，以及为什么通过 launcher 启动时父请求也经过本地网关。目录与别名见 [catalog.md](catalog.md)，Codex 宿主见 [codex.md](codex.md)。

## 1. 概览

Claude Code 适配器为目录中的每个角色生成一个自定义 subagent Markdown 文件，并在 frontmatter 中把 `model` 写成稳定的 relay alias `multi-relay-agent-<name>`，而不是写死上游模型。两个作用域：

- `user`：用户级 `~/.claude/agents/<name>.md`（默认）。
- `project`：项目级 `<project>/.claude/agents/<name>.md`，需要显式 `--project` 且目录存在。

所有请求通过本机网关的 Anthropic Messages 兼容入口 `POST /v1/messages` 翻译到统一请求模型，再由同一套目标池与轮转状态机转发到上游。

## 2. 安装

前置条件与 Codex 一致（`$CODEX_HOME` 默认 `~/.codex`）。产品状态使用平台级 `multi-relay` 状态目录，Codex Home 只定位 Codex 宿主文件：

```powershell
python scripts\multi_relay.py setup --preset hybrid --host claude-code --json
```

用户级 subagent 直接写 `~/.claude/agents/`；项目级写法：

```powershell
python scripts\multi_relay.py setup --preset hybrid --host claude-code --project C:\path\to\project --json
```

`--host all` 同时安装两个宿主。只有 vault Provider 才需要凭据，凭据同样只在本地掩码输入框中接收。

## 3. 启动

必须通过 launcher 启动，不指导用户永久导出上游 API Key：

```powershell
python scripts\multi_relay.py launch claude-code --pool general --project C:\path\to\project -- --help
```

launcher 的完整生命周期：

1. 启动或复用通过健康检查的用户级本地网关（只绑定 `127.0.0.1`）。
2. 从操作系统凭据仓库读取网关短期令牌。
3. 只为子进程构建受控环境：
   - `ANTHROPIC_BASE_URL=http://127.0.0.1:42137`
   - `ANTHROPIC_AUTH_TOKEN=<本地短期令牌>`
   - `ANTHROPIC_MODEL=multi-relay-default`（或 `multi-relay-<pool>`）
   - 删除父环境中的 `ANTHROPIC_API_KEY`、`CLAUDE_CODE_OAUTH_TOKEN` 以及 Bedrock、Vertex、Foundry 开关，防止绕过网关。
4. 以该环境启动 Claude Code，并把 `--` 之后的参数原样传给宿主。
5. 退出 Claude Code 后关闭本次专用网关；`--keep-gateway` 可保留网关供下次复用。

这些变量只存在于子进程环境，不落盘、不写回父 shell，也不改写项目配置。可执行文件用 `--claude-bin` 或环境变量 `CLAUDE_CODE_BIN` 指定，否则从 `PATH` 查找 `claude`。

## 4. 为什么 launcher 下父请求也经过网关

Codex 可以只为子代理配置自定义 Provider，父代理保持用户原来的模型；Claude Code 不同：它的所有模型请求（包括顶层会话与 subagent）都走同一个 `ANTHROPIC_BASE_URL`。

launcher 因此把 `ANTHROPIC_BASE_URL` 指向本地网关，并把默认模型设为 relay alias。结果是父请求与子代理请求都进入同一个网关：父请求经 `multi-relay-default` 解析到宿主的 `default_pool`，subagent 请求经 `multi-relay-agent-<name>` 解析到各自的池与能力约束，两者共享同一份轮转状态。launcher 同时移除子环境中的上游密钥变量，保证网关是唯一出口，也保证上游 API Key 不被永久导出或复制到 shell 与项目配置中。

## 5. 测试

```powershell
python scripts\multi_relay.py test --host claude-code --json
python scripts\multi_relay.py host status claude-code --json
```

Claude Code 的 `test` 校验宿主安装状态（`enabled`、受管文件无漂移）；上游模型与协议行为由 `target test`、`provider test` 验证。`test --host all` 依次检查两个宿主。

## 6. 禁用、启用与卸载

```powershell
python scripts\multi_relay.py disable --host claude-code --project C:\path\to\project --json
python scripts\multi_relay.py enable --host claude-code --project C:\path\to\project --json
python scripts\multi_relay.py uninstall --host claude-code --project C:\path\to\project --json
```

- 禁用保留目录、凭据与备份，只移除受管的 subagent 文件；用户修改过的文件保留并报告 `conflict`。
- 卸载只删除 manifest 证明由本工具创建、且未被改写的文件。
- 普通卸载保留凭据；只有显式加 `--remove-credentials` 才删除目录引用的 vault 凭据。

## 7. 能力边界

- `vision`、`audio` 与 `server_web_search` 是否可用由目标的已验证能力决定；目标不支持时，请求在发出前返回 `no_eligible_target`。
- 工具调用经统一规范模型映射：OpenAI function tool 与 Anthropic tool 双向转换，`tool_result` 的错误标志保留。
- Anthropic 的 `anthropic-version` 与 `anthropic-beta` 头采用显式白名单处理；未知 beta 字段返回清晰错误，不静默丢弃。
- extended thinking 等私有推理内容按能力检查，不会被伪装成普通可审计文本。
- 订阅 OAuth 凭据不导出、不复制、不混入 API 目标池；launcher 只使用目录中的 API-backed target。

## 8. 故障排查

- `claude_code_not_found`：安装 Claude Code，或用 `--claude-bin` / `CLAUDE_CODE_BIN` 指定可执行文件。
- `gateway_token_missing`：本地网关令牌不在凭据仓库中，先运行 `gateway start` 再启动。
- `host status` 显示 `not_configured` 或 `partial`：重新运行 `setup --host claude-code` 或 `repair`。
- `conflict`：目标 subagent 路径存在未被证明所有权的文件，保留用户内容。
- 希望连续多次启动省去网关重启：加 `--keep-gateway`，结束后用 `gateway stop` 或 `gateway status` 管理。
