# 凭据与安全模型

本文档说明上游 API Key 与本地网关令牌保存在哪里、哪些位置绝不保存秘密、网关的 HTTP 边界，以及凭据迁移时的安全规则。兼容迁移细节见 [compatibility.md](compatibility.md)。

## 1. 上游凭据保存在哪里

上游 API Key 只保存在操作系统凭据仓库，通过 `multi-relay/<provider-id>/<credential-id>` 命名隔离：

| 平台 | 后端 | 定位方式 |
| --- | --- | --- |
| Windows | Windows Credential Manager | target：`multi-relay/<provider-id>/<credential-id>` |
| macOS | Keychain | service：`multi-relay`；account：`<provider-id>/<credential-id>` |
| Linux | Secret Service | application=`multi-relay`、provider=`<provider-id>`、credential=`<credential-id>` |

同一 Provider 可以保存多个命名凭据（如 `primary`、`backup`），不同 Provider 的同名凭据互不冲突。目录里的 `credentials` 条目只保存引用元数据（`vault_target`、`enabled`、`label` 等），不含秘密本身。Linux 上 Secret Service 不可用时明确失败，不提供明文文件回退。

## 2. 本地网关令牌

Codex 与 Claude Code 只接触本机网关的短期令牌，不接触上游 Key：

- 网关请求令牌存放在独立的 vault 槽位 `multi-relay/local-gateway/session`，生命周期 12 小时。
- 网关同时持有独立的 shutdown token，两个 token 必须不同。
- 宿主侧通过本地 helper 命令取得令牌；上游 Key 只由网关进程从 vault 读取。

## 3. 哪些位置绝不保存秘密

以下位置禁止出现 API Key、Authorization header 或任何 secret-like 内容，并且有运行时校验与测试守护：

- `catalog.json`、manifest、运行时状态与网关状态。
- 日志、异常消息、命令行参数与标准错误输出。
- 上游 secret 环境变量、备份文件与测试快照。Claude Code launcher 只在子进程环境中放置短期本地网关 token。
- 请求正文、响应正文、用户 prompt、图片与工具结果。

secret-like 字段名或字段值会让目录校验直接拒绝，并提示改用凭据仓库导入。`credential list` 只显示 `provider`、`credential`、`label`、`enabled`、`present`，不显示 key 的前后缀或哈希，避免可关联信息。

## 4. 凭据输入与读取

- `credential add` 与 `credential replace` 只从本地掩码输入框接收秘密；没有 `--key` 参数，也不接受聊天窗口传入的 Key。
- 执行路径只在发起上游请求前从 vault 读取凭据，且只进入内存；请求结束后立即释放。
- `auth_invalid` 会禁用对应凭据引用，跨池共享同一凭据的目标一并过滤，等待用户替换或重新启用。
- Provider 错误正文可能回显密钥时，诊断信息经过已知秘密的最终脱敏后才可输出；错误体读取有 1 MiB 硬上限。

## 5. 网关 HTTP 边界

本地网关只监听 `127.0.0.1`（不回显 `0.0.0.0`），并实施：

- 只接受 loopback 客户端，Host 头必须是 loopback 形式。
- 拒绝绝对 URI 与代理形式请求、异常 Host、非 `application/json` 的 Content-Type 和 chunked 请求体。
- 请求体上限 1 MiB，头部上限 64 KiB。
- 令牌比较使用恒定时间；请求令牌与 shutdown token 分离，`/_multi-relay/shutdown` 只接受 shutdown token 且只允许 loopback；旧 `/_shutdown` 仅作兼容入口。
- 并发受目录 `concurrency` 限制；停机时先停止接新请求，宽限后取消在途请求。
- 日志默认只含 `request_id`、target id、错误分类、耗时与 token 用量；debug 模式也不记录 prompt、响应正文或 Authorization。

## 6. 上游安全

- Provider `base_url` 必须使用 HTTPS；只有 `localhost`、`127.0.0.1`、`::1` 允许 HTTP。
- 带凭据的请求禁止跨 origin 重定向，防止 Authorization 被转发到其他域名。
- 上游只允许目录中经过校验的 origin；错误分类优先读 Provider 稳定错误码，再读 HTTP 状态，最后才使用有限模式匹配。

## 7. 凭据迁移规则

从旧安装迁移时：

- 旧凭据目标只用于一次性迁移读取；迁移成功后先验证新引用可读，再删除旧凭据。
- 删除旧凭据失败时保留并报告 `cleanup_pending`，不泄露内容。
- 目标位置已存在不同凭据时返回冲突，不回滚已经验证成功的新引用，也不覆盖旧值。
- 迁移事务失败时保留原始引用，用户凭据不会被破坏。

## 8. 检查清单

- 不在聊天、配置、命令参数、日志或备份中接收和保存任何上游 Key。
- 首次安装只在本机掩码输入框中输入凭据。
- 卸载默认保留凭据，只有 `--remove-credentials` 才删除 vault 中的值。
- 上游仅使用 HTTPS（loopback 除外），不启用跨 origin 重定向。
- 网关保持 loopback 监听，本地令牌轮换后重新启动网关生效。
