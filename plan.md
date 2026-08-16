# Multi Relay 跨模型轮转与 Codex / Claude Code 双宿主改造计划

> **执行要求：** 实现本计划时使用 superpowers:executing-plans，严格按任务顺序执行；每个任务先写失败测试，再写最小实现，再运行局部与回归测试，再提交。
>
> **状态：** 待用户审阅。本文只定义一次性完整改造，不代表已经实现。
>
> **计划日期：** 2026-08-16
>
> **目标仓库：** https://github.com/Roblis0n/Codex-multi-relay
>
> **工作分支：** main。遵循用户要求，不创建额外 branch，不使用 git worktree。

---

## 0. 最终结果

把当前以 Codex 子代理和单一 Provider 桥接为中心的项目，改造成宿主中立的 **Multi Relay**：

1. 用户不再只配置一个 API Key，而是配置任意数量的执行目标。
2. 每个执行目标同时绑定 Provider、协议、模型、凭据、能力、上下文窗口和信任属性。
3. 用户可把多个不同 Provider、不同模型、不同凭据的执行目标排成轮转池。
4. 某个目标额度耗尽、认证失效、限流或暂时不可用时，按明确规则切换到下一个目标。
5. 用户可选择：
   - sticky：切换后一直使用新目标，直到再次失败或手动重置。
   - timed：切换后保持用户设定的时长，到期再尝试优先级更高的目标。
6. Codex 和 Claude Code 共用同一个目录、配置模型、凭据仓库、目标选择器和本地网关。
7. Codex 通过 Codex 宿主适配器使用目标池；父代理选择保持不变。
8. Claude Code 通过专用启动器和 Anthropic Messages 兼容入口使用目标池。
9. DeepSeek 只是默认可选 Provider，不再是产品身份和硬编码中心。
10. 项目对外统一命名为 **Multi Relay**，仓库 slug 使用 **multi-relay**；旧名称只保留在兼容迁移代码和迁移文档中。

完成后，用户面对的是一个功能完整的跨模型执行平面，而不是“多个 key 的简单轮询脚本”。

---

## 1. 主次关系与设计决策

### 1.1 第一优先级：执行正确性

轮转不能破坏一次代理执行的语义。

- 在上游尚未产生任何外部可见输出时，可以安全尝试下一个目标。
- 一旦已经输出文本、推理摘要、工具调用、文件写入请求或其他可见事件，当前请求必须固定到该目标。
- 固定后发生错误，只返回标准化终止错误，不把完整请求重放给另一模型。
- 这样避免重复工具调用、重复写文件、重复发送消息或两个模型给出互相矛盾的半截答案。

### 1.2 第二优先级：凭据安全

- 上游 API Key 只保存在操作系统凭据仓库。
- catalog、manifest、runtime state、日志、命令行参数、环境变量、测试快照中都不得出现上游 API Key。
- Codex 和 Claude Code 只接触本机网关的短期令牌。
- 本地网关只监听 loopback，拒绝非 loopback Host、跨源重定向和未认证请求。
- 不提供明文文件回退。

### 1.3 第三优先级：宿主中立

核心层不能导入 Codex TOML 逻辑，也不能依赖 Claude Code 环境变量。

- 核心层负责 catalog、凭据引用、能力过滤、轮转、状态机、协议适配和网关。
- Codex 适配器只负责生成和恢复 Codex 管理范围内的配置。
- Claude Code 适配器只负责生成 agent 文件、构建受控环境并启动 Claude Code。
- 两个宿主必须复用完全相同的目标池语义。

### 1.4 第四优先级：迁移与可恢复性

- 现有 schema 1 catalog 自动迁移到新 schema。
- 现有 DeepSeek 凭据原地引用或安全搬迁，不要求用户重新粘贴。
- 现有 Codex 配置的非托管字段保持原样。
- apply、setup、enable、disable、uninstall 必须保持原子性和可回滚性。
- 老入口 configure-multi-relay.cmd 可保留一段兼容期，但所有输出和文档使用新名称。

### 1.5 明确不做

- 不把 Claude Code 订阅 OAuth 凭据导出、复制或混入 API 目标池。
- 不把 Codex 原生订阅目标伪装成通用 HTTP Provider。
- 不在已经发生工具副作用后自动跨模型续写同一请求。
- 不根据猜测自动购买、充值或修改 Provider 账户。
- 不提供集中式云端密钥托管。
- 不在本次改造中制作 GUI；CLI 和清晰的 JSON 输出是完整管理入口。
- 不用产品“第一版、第二版”拆分交付；本文中的 schema 数字仅用于数据迁移。

---

## 2. 核心领域模型

### 2.1 Provider

Provider 表示上游服务，不再直接代表一个可执行代理。

字段：

- id：稳定、唯一、小写标识。
- name：展示名。
- protocol：responses-compatible、chat-completions-compatible、deepseek-chat、anthropic-messages 或 codex-native。
- base_url：上游 HTTPS 地址；只有 loopback 可使用 HTTP。
- auth_mode：vault、none 或 host-native。
- capabilities：Provider 层声明的最大能力集合。
- models_endpoint：可选模型发现路径及响应格式。
- enabled：是否允许新选择。

### 2.2 CredentialRef

CredentialRef 是秘密的非秘密引用。

字段：

- id：Provider 内唯一标识，例如 primary、backup-a。
- provider_id：所属 Provider。
- vault_target：操作系统凭据仓库定位符；catalog 只保存定位符，不保存内容。
- enabled：用户是否启用。
- created_at：创建时间。
- label：便于用户辨认的标签，不包含 key 尾号。

### 2.3 ExecutionTarget

**ExecutionTarget 是轮转的最小单位。** 切换 key 与切换模型统一成切换执行目标。

字段：

- id：稳定唯一标识。
- provider_id：上游 Provider。
- protocol：默认继承 Provider，只有经过验证时才允许覆盖。
- model：精确上游模型标识。
- credential_id：凭据引用；auth_mode 为 none 或 host-native 时可为空。
- capabilities：该具体模型已验证的能力集合。
- context_window：已验证的上下文窗口。
- max_output_tokens：可选输出上限。
- reasoning_efforts：可用推理档位集合。
- trust：standard 或 high。
- host_compatibility：codex、claude-code 的允许集合。
- enabled：是否进入候选集。
- metadata：非秘密说明，不参与选择。

目标示例：

~~~json
{
  "id": "deepseek-primary",
  "provider_id": "deepseek",
  "model": "deepseek-v4-pro",
  "credential_id": "primary",
  "capabilities": ["text", "tool_calling"],
  "context_window": 131072,
  "reasoning_efforts": ["high", "max"],
  "trust": "standard",
  "host_compatibility": ["codex", "claude-code"],
  "enabled": true
}
~~~

### 2.4 TargetPool

TargetPool 是用户定义的有序执行目标集合。

字段：

- id：稳定唯一标识。
- targets：按优先级排列的 target id，不允许重复。
- strategy：
  - sticky：故障切换后持续使用当前目标。
  - timed：在当前目标上保持 duration_seconds；到期后从首个目标重新探测。
- duration_seconds：timed 必填，sticky 禁止出现。
- max_rate_limit_wait_seconds：尊重 Retry-After 的最长等待时间。
- cooldown：
  - quota_seconds
  - rate_limit_seconds
  - auth_seconds
  - provider_seconds
- required_capabilities：池的最低能力约束。
- host_compatibility：允许使用该池的宿主集合。
- enabled：是否可路由。

### 2.5 AgentProfile

AgentProfile 表示宿主可见的角色，不再绑定单个 Provider。

字段：

- name、description、developer_instructions。
- pool_id：默认目标池。
- required_capabilities：角色最低能力。
- fallback_pool_id：可选的明确备用池，不自动跨 trust 边界。
- reasoning_effort：首选推理强度，目标不支持时按已验证交集降级或省略。
- context_window：角色所需下限。
- trust、priority、sandbox_mode。
- tools、mcp_servers、skills。
- hosts：要为哪些宿主生成配置。

### 2.6 SelectionRequest

一次选择至少包含：

- host：codex 或 claude-code。
- pool_id。
- required_capabilities。
- minimum_context_window。
- requested_reasoning_effort。
- high_risk。
- request_id。
- protocol_surface：responses 或 messages。

选择器先做能力过滤，再按池状态和用户顺序选择，不得先选目标再“假装”它具备缺失能力。

---

## 3. 总体架构

~~~mermaid
flowchart LR
    C["Codex host"] --> CA["Codex adapter"]
    H["Claude Code host"] --> HA["Claude Code launcher and adapter"]
    CA --> G["Loopback Multi Relay gateway"]
    HA --> G
    G --> S["Capability filter and target selector"]
    S --> R["Rotation state machine"]
    R --> P["Protocol adapters"]
    P --> O["OpenAI Responses upstream"]
    P --> X["OpenAI-compatible Chat upstream"]
    P --> D["DeepSeek upstream"]
    P --> A["Anthropic Messages upstream"]
    S --> V["OS credential vault"]
    R --> T["Secret-free runtime state"]
    M["Multi Relay CLI and manager"] --> CATALOG["Catalog and manifests"]
    M --> V
    M --> T
    CATALOG --> CA
    CATALOG --> HA
    CATALOG --> S
~~~

### 3.1 层级职责

#### A. 宿主中立核心

- catalog 解析、严格校验、迁移。
- Provider、CredentialRef、ExecutionTarget、TargetPool、AgentProfile。
- 能力匹配与目标选择。
- 错误分类、退避、冷却、sticky 或 timed 状态机。
- 规范化请求和流事件。
- 凭据仓库接口。
- loopback 网关生命周期。

#### B. Codex 适配器

- 保持父代理的 model、model_provider、model_reasoning_effort 不变。
- 为池生成本机网关 model provider。
- 为 AgentProfile 生成 Codex agent config 文件。
- 配置 multi-agent 所需字段。
- 保存管理前快照并原子恢复。
- 旧 Codex-only manifest 自动迁移。

#### C. Claude Code 适配器

- 生成 Claude Code 自定义 subagent Markdown 文件及 frontmatter。
- 把角色的 model 字段设为稳定的 relay alias，而不是写死上游模型。
- 专用 launcher 启动本地网关，再以受控环境启动 Claude Code。
- launcher 设置 ANTHROPIC_BASE_URL、本地令牌、默认 model alias。
- 用户已有环境变量和项目配置不落盘、不被永久改写。
- 退出 Claude Code 后关闭本次专用网关，或复用经过健康检查的用户级网关。

#### D. 协议适配层

- Codex 的 Responses 请求进入规范化请求模型。
- Claude Code 的 Anthropic Messages 请求进入同一规范化请求模型。
- 每个上游适配器负责请求翻译、流事件翻译、用量翻译和错误提取。
- 轮转状态机只处理规范化事件，不理解具体 Provider JSON。

### 3.2 宿主能力边界

| 能力 | Codex | Claude Code | 实现边界 |
|---|---:|---:|---|
| 自定义子代理角色 | 是 | 是 | 分别生成宿主原生配置 |
| 子代理自定义模型 | 是 | 是 | 角色绑定 pool alias |
| 跨 Provider 轮转 | 是 | 是 | 本地网关负责 |
| 多 API Key | 是 | 是 | OS vault + CredentialRef |
| 父代理保持原模型 | 是 | 否，使用 launcher 时父请求也经过网关 | 明确展示差异 |
| 订阅 OAuth 轮转 | 否 | 否 | 只支持 API-backed target |
| 视觉输入 | 取决于宿主请求和目标 | 取决于宿主请求和目标 | capability 过滤与图片块翻译 |
| 联网搜索 | 宿主工具或目标原生能力 | 宿主工具或目标原生能力 | 不把普通文本模型标成 web |
| 工具调用 | 是 | 是 | 规范化 tool schema 和事件 |

---

## 4. 请求、流和故障切换语义

### 4.1 请求生命周期

1. 宿主以本地短期令牌调用网关。
2. 网关验证 loopback 来源、Host、Content-Type、请求体大小和令牌。
3. 入站适配器构建 CanonicalRequest。
4. 从 model alias 或 agent profile 解析 pool。
5. 选择器过滤：
   - pool 和 target 已启用；
   - host 兼容；
   - capability 满足；
   - context window 满足；
   - high-risk 请求不跨 trust 限制；
   - 凭据存在且启用；
   - target 未在不可用冷却期。
6. 状态机选出当前候选。
7. 凭据只在发起上游请求前从 vault 读取，并只进入内存。
8. 上游适配器发起请求并把流翻译为 CanonicalEvent。
9. 出站适配器把 CanonicalEvent 翻译回宿主协议。
10. 更新 secret-free 状态与结构化指标。

### 4.2 首个可见事件边界

以下任一事件一旦写给宿主，本次请求即进入 committed 状态：

- text_delta。
- reasoning_delta 或 reasoning_summary_delta。
- tool_call_start、tool_call_delta、tool_call_complete。
- image 或其他内容块。
- 宿主可见的响应 metadata。

以下事件本身不提交请求：

- 内部连接建立。
- 尚未向宿主发送的 upstream headers。
- 内部重试记录。
- 零内容 keep-alive。

committed 前允许 failover；committed 后禁止自动重放。

### 4.3 标准错误分类

| 分类 | 典型信号 | 当前目标处理 | 是否切换 |
|---|---|---|---:|
| quota_exhausted | 402、明确余额或额度耗尽错误码 | 进入 quota 冷却 | 是，committed 前 |
| rate_limited | 429、明确速率限制 | 先按 Retry-After 等待，超阈值后冷却 | 是，committed 前 |
| auth_invalid | 401、403、invalid_api_key | 禁用该 CredentialRef，等待用户处理 | 是，committed 前 |
| model_unavailable | 404 model、model_not_found | 冷却具体 target | 是，committed 前 |
| provider_unavailable | 5xx、DNS、连接或超时 | 有界原目标重试后冷却 | 是，committed 前 |
| protocol_error | 无法解析的上游响应或流 | 冷却 target，记录脱敏诊断 | 是，committed 前 |
| request_invalid | 400 参数或 schema 错误 | 原样标准化返回 | 否 |
| context_exceeded | 上下文过长 | 返回所需与可用窗口 | 否，避免语义变化 |
| policy_blocked | 内容策略、安全拒绝 | 返回拒绝 | 否 |
| cancelled | 宿主断开或用户取消 | 取消上游请求 | 否 |
| no_eligible_target | 全部被过滤或冷却 | 返回每个目标的非秘密原因 | 否 |

错误分类顺序必须优先读取 Provider 稳定错误码，再看 HTTP 状态，最后才使用有限的消息模式；禁止用宽泛字符串匹配把普通错误误判为额度耗尽。

### 4.4 同一目标重试

- 网络连接建立失败：最多 1 次短重试，可配置但有硬上限。
- 502、503、504：使用带抖动的指数退避，默认最多 2 次。
- 429：
  - Retry-After 小于或等于 pool.max_rate_limit_wait_seconds 时等待同一目标。
  - 超过阈值或缺失时，将目标置入 rate-limit 冷却并尝试下一个。
- 401、403：不重试同一凭据。
- 400、413、422：不重试、不切换。
- 所有等待均响应宿主取消信号。

### 4.5 sticky 策略

- 初始选择 targets 中第一个健康候选。
- 发生可切换故障后选择后续目标。
- 选择成功即更新 active_target_id。
- 后续请求一直从 active_target_id 开始。
- 只有以下事件改变它：
  - 当前目标再次发生可切换故障；
  - 用户执行 pool reset；
  - 用户修改顺序并 apply；
  - 当前 target 被禁用或删除。

### 4.6 timed 策略

- 切换成功时记录 selected_at 和 hold_until。
- hold_until 前从当前目标开始选择。
- 到期后，新请求从池首位重新探测。
- 若首位仍处于冷却或探测失败，保持当前健康目标，并记录下次可探测时间。
- 已经执行中的请求不因计时到期而切换。
- duration_seconds 使用单调时钟计算进程内等待，持久化使用 UTC 时间戳；系统时钟异常时采取保守策略，不提前切换。

### 4.7 并发一致性

- 运行状态按 pool 分片。
- 状态文件更新采用短时文件锁、读改写、临时文件 fsync、原子替换。
- 同一 pool 同时发生多个失败时，只有一个请求推进 active target。
- 其他请求重新读取 generation，避免每个请求各跳一格。
- runtime state 包含 generation；catalog hash 改变时重新协调状态。
- 不在持锁期间执行网络请求或读取大文件。

---

## 5. 规范化协议模型

### 5.1 CanonicalRequest

至少包含：

- request_id。
- host。
- model_alias 和 pool_id。
- system blocks。
- developer blocks。
- conversation messages。
- content blocks：
  - text
  - image_url
  - image_base64
  - tool_result
- tools 和 input schema。
- tool_choice。
- max_output_tokens。
- temperature、top_p 等可安全透传参数。
- requested_reasoning_effort。
- stream。
- metadata 中允许的白名单字段。

未知字段不得静默丢弃：可安全忽略的字段记录为 warning；影响语义的字段返回 request_invalid。

### 5.2 CanonicalEvent

至少包含：

- response_started。
- content_block_started。
- text_delta。
- reasoning_summary_delta。
- tool_call_started。
- tool_call_arguments_delta。
- tool_call_completed。
- content_block_completed。
- usage。
- response_completed。
- error。

每个适配器都必须维持稳定的 block index、tool call id 和事件顺序。

### 5.3 工具调用映射

- OpenAI function tool 与 Anthropic tool 均映射为 canonical tool。
- JSON Schema 保留 properties、required、enum、items、additionalProperties。
- 不支持的 schema 关键字必须在请求前明确报错或通过已测试的降级规则处理。
- 并行工具调用只有在宿主与目标都支持时启用。
- tool_result 的错误标志必须双向保留。
- 任何 tool call 事件发给宿主后立即进入 committed 状态。

### 5.4 视觉输入映射

- 支持 HTTPS image URL 和合法 base64 image block。
- 限制单图大小、总图数和总请求体大小。
- MIME 白名单至少覆盖 JPEG、PNG、GIF、WebP；实际目标支持集合由 capability 描述。
- 不把图片写入临时明文文件。
- 目标缺少 vision 时，在发起上游请求前返回 no_eligible_target。

### 5.5 联网搜索边界

能力拆分为：

- tool_calling：模型可调用宿主暴露的搜索工具。
- server_web_search：上游 Provider 自带搜索工具或 Responses web search。

只有经过明确配置和探测的目标才能声明 server_web_search。普通模型即使可以生成 URL，也不能标为联网。

---

## 6. 配置与状态格式

### 6.1 catalog schema

新 catalog 的顶层：

~~~json
{
  "schema_version": 2,
  "concurrency": 8,
  "providers": [],
  "credentials": [],
  "targets": [],
  "pools": [],
  "agents": [],
  "hosts": {
    "codex": {
      "enabled": true
    },
    "claude-code": {
      "enabled": false,
      "scope": "user",
      "default_pool": "general"
    }
  }
}
~~~

严格规则：

- 顶层和每类对象拒绝未知字段。
- 所有 id 使用稳定的小写 ASCII 标识。
- 所有引用必须存在。
- Provider 删除前必须无 target 引用。
- CredentialRef 删除前必须无 target 引用。
- Target 删除前必须无 pool 引用。
- Pool 删除前必须无 agent 或 host 引用。
- 同一 pool 不允许重复 target。
- host_compatibility 必须是已知宿主。
- secret-looking 字段名和值触发拒绝并给出安全导入方法。

### 6.2 运行状态

runtime state 单独存储，不写回 catalog：

~~~json
{
  "schema_version": 1,
  "catalog_hash": "sha256:...",
  "generation": 12,
  "pools": {
    "general": {
      "active_target_id": "anthropic-backup",
      "selected_at": "2026-08-16T10:00:00Z",
      "hold_until": "2026-08-16T12:00:00Z",
      "targets": {
        "deepseek-primary": {
          "status": "cooldown",
          "reason": "quota_exhausted",
          "retry_at": "2026-08-17T00:00:00Z",
          "failure_count": 1
        }
      }
    }
  }
}
~~~

禁止字段：

- API Key、Authorization header。
- 请求正文、响应正文。
- 用户 prompt。
- 完整 Provider 错误正文。
- 图片、工具结果。

### 6.3 文件位置

产品级状态与宿主配置分离：

- Windows：LOCALAPPDATA 下的 multi-relay 目录。
- macOS：Library/Application Support/multi-relay。
- Linux：XDG_STATE_HOME/multi-relay；缺失时使用 ~/.local/state/multi-relay。
- Codex 管理文件仍位于 CODEX_HOME 的管理范围内。
- Claude Code user scope agent 位于用户 Claude 配置目录；project scope 位于项目 .claude/agents。

路径解析必须可注入测试根目录，测试不得写真实用户目录。

### 6.4 manifest

manifest 记录：

- manifest schema。
- catalog schema 与 hash。
- 安装的宿主。
- 每个宿主创建或修改的文件。
- 修改前快照或可恢复片段。
- 文件内容 hash 与 ownership markers。
- gateway launcher 版本。
- 兼容迁移来源。

uninstall 只删除 manifest 证明由本项目创建、且当前内容未被用户改写的文件。发生冲突时保留用户文件并报告。

### 6.5 凭据命名

- Windows Credential Manager target：multi-relay/provider-id/credential-id。
- macOS Keychain service：multi-relay；account：provider-id/credential-id。
- Linux Secret Service：application=multi-relay、provider=provider-id、credential=credential-id。
- 本地网关短期令牌使用独立 target：multi-relay/local-gateway/session。

旧 DeepSeek target codex-deepseek-api-key 只用于一次性迁移读取。迁移成功后先验证新引用可读，再删除旧凭据；若删除失败则报告但不泄露内容。

---

## 7. 公共 HTTP 接口

网关只绑定 127.0.0.1 和 ::1，不绑定 0.0.0.0。

### 7.1 健康与管理接口

- GET /health
  - 不需暴露上游信息。
  - 返回服务版本、catalog hash、支持的协议表面。
- GET /v1/models
  - 返回 relay model alias，不泄露凭据。
  - 可附带能力和上下文元数据。
- GET /_multi-relay/pools
  - 需要本地令牌。
  - 返回 pool 状态和脱敏故障原因。
- POST /_multi-relay/pools/{pool_id}/rotate
  - 手动切到下一个健康目标。
- POST /_multi-relay/pools/{pool_id}/reset
  - 清除 sticky 或 timed 选择，从第一优先级重新评估。
- POST /_shutdown
  - 需要一次性 shutdown token。
  - 只接受 loopback。

### 7.2 宿主协议接口

- POST /v1/responses：Codex Responses 表面。
- POST /v1/messages：Claude Code Anthropic Messages 表面。
- POST /v1/chat/completions：仅作为显式兼容入口，不作为核心内部协议。

旧 /providers/{provider_id} 路径保留受测兼容映射，内部立即解析成单目标临时 pool；文档不再推荐。

### 7.3 model alias

稳定 alias 规则：

- multi-relay-default：宿主默认 pool。
- multi-relay-{pool_id}：直接指定 pool。
- multi-relay-agent-{agent_name}：按 AgentProfile 解析 pool 与能力。

alias 不包含当前上游 Provider 或模型，因此轮转后宿主配置无需重写。

### 7.4 HTTP 安全

- Authorization 使用本地短期 Bearer token。
- 比较使用恒定时间。
- 限制 method、Content-Type、Content-Length、并发连接和 header 大小。
- 拒绝绝对 URI、代理形式请求和异常 Host。
- 上游只允许 catalog 中经过校验的 HTTPS origin。
- 带凭据的请求禁止跨 origin 重定向。
- 日志默认只含 request_id、target_id、错误分类、耗时、token usage。
- debug 模式仍不得记录 prompt、响应正文或 Authorization。

---

## 8. CLI 设计

统一入口：

~~~text
multi-relay <command>
python scripts/multi_relay.py <command>
~~~

为兼容现有用户，可在过渡期接受 codex-multi-relay 入口，但帮助文案和输出使用 Multi Relay。

### 8.1 全局命令

- multi-relay setup --host codex
- multi-relay setup --host claude-code
- multi-relay setup --host all
- multi-relay status
- multi-relay catalog
- multi-relay apply
- multi-relay repair
- multi-relay test --host codex
- multi-relay test --host claude-code
- multi-relay enable --host ...
- multi-relay disable --host ...
- multi-relay uninstall --host ... [--remove-credentials]

### 8.2 Provider 命令

- provider list
- provider add
- provider edit
- provider discover-models
- provider test
- provider enable
- provider disable
- provider remove

add 至少接收 id、name、protocol、base-url、auth-mode、capability。自定义 Provider 未显式声明协议时拒绝。

### 8.3 Credential 命令

- credential list
- credential add --provider ID --id ID
- credential replace --provider ID --id ID
- credential enable
- credential disable
- credential test
- credential remove

安全规则：

- 不提供 --key。
- add 和 replace 只从隐藏交互提示或标准输入的受控模式读取。
- JSON 输出只显示 present、enabled、provider、credential id。
- list 不显示 key 前后缀或 hash，避免可关联信息。

### 8.4 Target 命令

- target list
- target add
- target edit
- target test
- target enable
- target disable
- target remove

target test 必须验证认证、模型可用性、协议握手和已声明能力的基础契约；未验证的信息显示 unknown，不伪装成 supported。

### 8.5 Pool 命令

- pool list
- pool add
- pool edit
- pool order POOL TARGET...
- pool strategy POOL sticky
- pool strategy POOL timed --duration 2h
- pool rotate POOL
- pool reset POOL
- pool status POOL
- pool remove POOL

duration 支持明确单位 s、m、h、d，解析后存储整数秒；拒绝零、负数和超出硬上限的值。

### 8.6 Agent 与宿主命令

- agent list
- agent set
- agent remove
- host list
- host apply codex
- host apply claude-code
- host status ...
- launch claude-code [--pool ID] [--project PATH] [-- <claude args>]
- gateway start
- gateway status
- gateway stop

launch claude-code 不永久写入父 shell 环境；只给子进程构建新环境。

---

## 9. 计划中的源码布局

保留 scripts/multi_relay 包，拆分当前大文件，避免重写公共入口：

~~~text
scripts/
  multi_relay.py
  multi_relay/
    __init__.py
    cli.py
    errors.py
    paths.py
    catalog.py
    migration.py
    credentials.py
    credential_helper.py
    capabilities.py
    selection.py
    rotation.py
    state.py
    canonical.py
    gateway.py
    manager.py
    hosts/
      __init__.py
      codex.py
      claude_code.py
    protocols/
      __init__.py
      base.py
      responses.py
      chat_completions.py
      anthropic_messages.py
    compatibility.py
~~~

兼容策略：

- bridge.py 暂时保留为 gateway、旧类名和旧入口的薄包装；完成迁移后不再承载业务逻辑。
- toml_config.py 保留并由 hosts/codex.py 调用。
- relay_manager.py 保留旧导入路径，重新导出 manager.py 中的实现。
- provider_api.py 继续负责模型发现，但按 protocol dispatch。
- model_capabilities.py 的能力协商迁入 capabilities.py，旧模块保留导出。

新增测试建议：

~~~text
scripts/
  test_catalog_schema2.py
  test_catalog_migration2.py
  test_credentials_multi.py
  test_failure_classification.py
  test_rotation.py
  test_state.py
  test_canonical.py
  test_protocol_responses.py
  test_protocol_chat.py
  test_protocol_anthropic.py
  test_gateway.py
  test_host_codex.py
  test_host_claude_code.py
  test_launch_claude_code.py
  test_cli_targets.py
  test_cli_pools.py
  test_rebrand.py
  test_end_to_end_rotation.py
~~~

---

## 10. 详细实施任务

所有任务均采用 Red → Green → Refactor。每个测试先证明会以预期原因失败，禁止先写实现再补覆盖。

### Task 1：建立 schema 2 的领域模型和严格校验

**修改文件**

- scripts/multi_relay/catalog.py
- scripts/multi_relay/errors.py
- scripts/test_catalog_schema2.py
- scripts/test_catalog.py

**新增类型**

- CredentialRef
- ExecutionTarget
- TargetPool
- HostConfig
- 扩展后的 AgentSpec 或重命名后的 AgentProfile
- Catalog schema 2

**测试先行**

1. 写最小合法 schema 2 catalog 解析测试。
2. 分别为未知字段、重复 id、悬空引用、重复 target、非法 duration 写失败测试。
3. 写 protocol、capability、host、strategy 枚举严格校验测试。
4. 写 secret-looking 字段拒绝测试。
5. 写序列化后顺序稳定和 round-trip 测试。
6. 运行：

~~~powershell
python -m unittest scripts.test_catalog_schema2 -v
~~~

预期：新类型尚不存在，测试失败。

**实现**

1. 将 CATALOG_SCHEMA_VERSION 改为 2。
2. 保留不可变 dataclass 风格。
3. 每层使用字段白名单，不接受静默扩展。
4. 建立全 catalog 交叉引用校验。
5. capability 输出使用确定顺序，保证 diff 稳定。
6. 将 provider auth 与 credential 引用解耦。
7. Agent 从 provider/model 改为 pool_id，但为迁移器保留旧解析入口。

**验证**

~~~powershell
python -m unittest scripts.test_catalog_schema2 scripts.test_catalog -v
python -m compileall -q scripts
~~~

**提交**

~~~text
feat: add execution targets and pools to the catalog
~~~

### Task 2：实现 schema 1 到 schema 2 的无损迁移

**修改文件**

- scripts/multi_relay/migration.py
- scripts/multi_relay/catalog.py
- scripts/multi_relay/paths.py
- scripts/test_catalog_migration2.py
- scripts/test_multi_migration.py

**迁移规则**

- 每个旧 vault Provider 生成一个 credential primary。
- 每个旧 Agent 的 provider + model 组合生成确定性 target id。
- 每个旧 Agent 生成同名或稳定派生的单目标 pool。
- 相同 provider、model、credential、capabilities 的 target 去重。
- codex-native Agent 生成 host-native target，只允许 codex。
- 旧 Agent 的指令、sandbox、skills、MCP、priority、trust 原样保留。
- 旧 catalog 永远先备份，再以原子方式写 schema 2。

**测试先行**

1. 使用当前 default_catalog 固定 fixture。
2. 验证迁移后角色数量和行为等价。
3. 验证重复组合去重。
4. 验证 migration 重复运行幂等。
5. 验证中途写入失败时旧文件不变。
6. 验证旧 DeepSeek credential target 只产生引用，不读取秘密到文件。

**实现**

1. 编写 migrate_catalog_1_to_2 的纯函数。
2. 使用内容 hash 和 schema 判断是否需要迁移。
3. 在 manager 读 catalog 时调用迁移协调器。
4. manifest 记录来源 schema 和备份位置。
5. catalog 失败时输出可操作错误，不覆盖源文件。

**验证**

~~~powershell
python -m unittest scripts.test_catalog_migration2 scripts.test_multi_migration -v
~~~

**提交**

~~~text
feat: migrate existing catalogs to target pools
~~~

### Task 3：把单凭据仓库扩展为多 Provider、多凭据

**修改文件**

- scripts/multi_relay/credentials.py
- scripts/multi_relay/credential_helper.py
- scripts/multi_relay/paths.py
- scripts/test_credentials_multi.py
- scripts/test_credentials.py

**测试先行**

1. 同一 Provider 存储 primary 和 backup 两个独立 key。
2. 不同 Provider 相同 credential id 不冲突。
3. list 只显示元数据。
4. disabled credential 不可读取给执行路径。
5. remove 只删除精确目标。
6. Windows、macOS 模拟后端测试。
7. Linux Secret Service 命令不存在时明确失败且无明文回退。
8. 旧 DeepSeek target 搬迁成功、回滚和删除失败测试。
9. 日志与异常字符串不包含测试 secret。

**实现**

1. CredentialStore 工厂接收 provider_id、credential_id、protocol。
2. 统一 VaultLocator，不把 target 拼接散落在调用方。
3. 增加 LinuxSecretServiceCredentialStore。
4. prompt_and_store 支持命名凭据，但秘密仍只经隐藏输入。
5. credential helper 改为输出本地网关令牌；上游 key 只由网关进程读取。
6. 提供 legacy DeepSeek 凭据迁移事务。
7. 全路径加入 redaction guard。

**验证**

~~~powershell
python -m unittest scripts.test_credentials_multi scripts.test_credentials -v
python scripts/check_runtime_contract.py
~~~

**提交**

~~~text
feat: support multiple vaulted credentials per provider
~~~

### Task 4：实现统一错误分类器

**新增或修改文件**

- scripts/multi_relay/errors.py
- scripts/multi_relay/failure.py
- scripts/multi_relay/protocols/base.py
- scripts/test_failure_classification.py

**测试矩阵**

- DeepSeek 余额耗尽响应。
- OpenAI-compatible 429 加 Retry-After。
- Anthropic 429 和 rate_limit_error。
- 401、403。
- model_not_found。
- 500、502、503、504。
- DNS、连接拒绝、TLS、读取超时。
- 400 invalid_request。
- context_length_exceeded。
- content policy refusal。
- malformed JSON、超大错误体、未知 content type。
- Provider 错误正文含 API Key 时脱敏。

**实现**

1. 定义 FailureClass、RetryDirective、NormalizedFailure。
2. Provider adapter 提取稳定 code 和 retry metadata。
3. 核心分类器按 code → status → 有限 pattern 的顺序判断。
4. 错误体读取设置硬上限。
5. 生成用户可见 message 和内部 secret-free details。
6. 为 committed 状态附加 resumable=false。

**验证**

~~~powershell
python -m unittest scripts.test_failure_classification -v
~~~

**提交**

~~~text
feat: normalize provider failures for safe failover
~~~

### Task 5：实现 secret-free 状态和 sticky、timed 选择器

**新增或修改文件**

- scripts/multi_relay/state.py
- scripts/multi_relay/selection.py
- scripts/multi_relay/rotation.py
- scripts/multi_relay/paths.py
- scripts/test_state.py
- scripts/test_rotation.py

**测试先行**

1. sticky 首选、故障切换、持续保持、手动 reset。
2. timed 在 hold_until 前保持，到期从首位重试。
3. 首位仍冷却时保持当前健康目标。
4. capability、context、host、trust、credential availability 过滤。
5. 所有 target 不可用时返回完整但脱敏的原因列表。
6. quota、rate limit、auth、provider 分别使用不同冷却。
7. catalog hash 变化后删除无效状态，保留仍有效 target。
8. 两线程同时推进 generation 只切换一次。
9. 状态文件截断、JSON 损坏、未来 schema 的安全处理。
10. 原子替换失败不破坏上一份状态。
11. fake clock 覆盖 wall clock 回拨和前跳。

**实现**

1. RuntimeStateStore 提供 load、compare_and_swap、reset_pool。
2. TargetSelector 是纯选择逻辑，不做网络请求。
3. RotationController 管理失败、冷却、generation、策略时间。
4. 使用可注入 Clock 和 Random，测试不 sleep。
5. 状态写入前运行 secret scanner。
6. 锁范围只覆盖本地状态提交。

**验证**

~~~powershell
python -m unittest scripts.test_state scripts.test_rotation -v
~~~

**提交**

~~~text
feat: add sticky and timed target rotation
~~~

### Task 6：建立 CanonicalRequest 和 CanonicalEvent

**新增或修改文件**

- scripts/multi_relay/canonical.py
- scripts/multi_relay/capabilities.py
- scripts/multi_relay/model_capabilities.py
- scripts/test_canonical.py

**测试先行**

1. 文本、多轮对话、system、developer 内容 round-trip。
2. OpenAI function tool 和 Anthropic tool 映射等价。
3. tool result 的 call id、错误标志和内容块顺序保留。
4. HTTPS 图片与 base64 图片校验。
5. 非法 MIME、超大图片、过多图片和错误 base64 拒绝。
6. 不支持的 JSON Schema 关键字给出确定错误。
7. reasoning effort 取宿主请求与 target 支持集合交集。
8. 未知但可忽略参数产生 warning；语义相关未知参数失败。
9. CanonicalEvent 的 block index 和 tool id 稳定。
10. 首个可见事件准确设置 committed。

**实现**

1. 定义不可变 CanonicalRequest、CanonicalMessage、CanonicalContentBlock。
2. 定义 CanonicalTool 和受支持的 JSON Schema 子集。
3. 定义 CanonicalEvent 及 EventKind。
4. 增加 RequestCommitTracker。
5. 把现有 model_capabilities 的 effort 选择接入 target 能力。
6. 所有转换器只依赖 canonical 类型，不互相导入。

**验证**

~~~powershell
python -m unittest scripts.test_canonical scripts.test_roles -v
~~~

**提交**

~~~text
feat: define a host-neutral relay protocol
~~~

### Task 7：抽取现有 Responses 与 Chat 适配器

**修改文件**

- scripts/multi_relay/bridge.py
- scripts/multi_relay/protocols/__init__.py
- scripts/multi_relay/protocols/base.py
- scripts/multi_relay/protocols/responses.py
- scripts/multi_relay/protocols/chat_completions.py
- scripts/test_protocol_responses.py
- scripts/test_protocol_chat.py
- scripts/test_bridge.py

**目的**

当前 bridge.py 已有 Responses 请求构造、Chat 流翻译和 HTTP server。此任务先做可验证的等价抽取，不同时改变轮转行为，降低大文件继续膨胀的风险。

**测试先行**

1. 把现有请求与 SSE fixture 固定为 golden tests。
2. Responses 入站 → canonical → Responses 上游 round-trip。
3. Responses 入站 → canonical → Chat Completions 上游。
4. Chat SSE 文本、reasoning、tool calls、usage 和 finish reason。
5. UTF-8 被分段、CRLF、空 data、[DONE]、超长事件。
6. 非流响应与流响应等价。
7. 宿主断开后停止上游读取。
8. 老 bridge 公共函数仍能导入并产生相同结果。

**实现**

1. 建立 ProtocolAdapter 抽象：
   - build_request
   - parse_response
   - iter_events
   - classify_error_metadata
   - discover_models
2. 将 build_chat_request 移至 chat_completions.py。
3. 将 ChatStreamTranslator 移至适配器。
4. 将 Responses 入站解析与出站渲染移至 responses.py。
5. bridge.py 改为兼容 re-export 和旧 server 包装。
6. 保留旧错误码和测试契约，除非新统一错误有明确迁移映射。

**验证**

~~~powershell
python -m unittest scripts.test_protocol_responses scripts.test_protocol_chat scripts.test_bridge -v
~~~

**提交**

~~~text
refactor: extract responses and chat protocol adapters
~~~

### Task 8：实现 Anthropic Messages 双向适配器

**新增或修改文件**

- scripts/multi_relay/protocols/anthropic_messages.py
- scripts/multi_relay/protocols/base.py
- scripts/multi_relay/canonical.py
- scripts/test_protocol_anthropic.py

**入站范围**

- POST /v1/messages。
- x-api-key 或 Authorization 本地令牌形式。
- anthropic-version、anthropic-beta 的受控处理。
- system 字符串或内容块。
- user、assistant 消息。
- text、image、tool_use、tool_result。
- tools、tool_choice、max_tokens、temperature、top_p、stream。

**出站范围**

- canonical → Anthropic Messages upstream。
- canonical → OpenAI Responses 或 Chat upstream 的能力保真转换。
- Anthropic message_start、content_block_start、delta、stop、message_delta、message_stop。
- input_json_delta 增量拼接与验证。
- usage input_tokens、output_tokens、cache token 字段的可选映射。

**测试先行**

1. Claude Code 最小非流 Messages 请求。
2. Claude Code 流式文本 fixture。
3. tool_use 和 tool_result 完整往返。
4. 多工具与交错 content blocks。
5. base64 图片映射。
6. Anthropic extended thinking 或未知 beta 字段的明确支持边界。
7. stop_reason：end_turn、tool_use、max_tokens、stop_sequence。
8. Anthropic error envelope 到 FailureClass。
9. OpenAI tool call 映回 Anthropic input_json_delta。
10. 在首个 content_block 事件后禁止 failover。
11. 不支持字段不得静默改变语义。

**实现**

1. AnthropicInboundAdapter：Messages → canonical。
2. AnthropicUpstreamAdapter：canonical → Messages。
3. AnthropicOutboundRenderer：canonical events → Messages SSE。
4. 映射 stop reason、usage 和 request id。
5. 对 beta header 使用显式白名单；未知 beta 返回清晰错误。
6. 对 thinking block 采用 capability 检查，不把私有推理内容伪装为普通文本。

**验证**

~~~powershell
python -m unittest scripts.test_protocol_anthropic -v
~~~

**提交**

~~~text
feat: add anthropic messages protocol support
~~~

### Task 9：把 bridge 升级为统一 loopback gateway

**新增或修改文件**

- scripts/multi_relay/gateway.py
- scripts/multi_relay/bridge.py
- scripts/multi_relay/credential_helper.py
- scripts/multi_relay/selection.py
- scripts/multi_relay/rotation.py
- scripts/test_gateway.py
- scripts/test_end_to_end_rotation.py
- scripts/test_bridge.py

**测试先行：HTTP 合约**

1. /health、/v1/models、/v1/responses、/v1/messages。
2. 缺失、错误和过期本地令牌均返回 401。
3. 非 loopback Host、绝对 URI、错误 Content-Type、超大 body 被拒绝。
4. shutdown token 与请求 token 分离。
5. legacy /providers/{id} 仍可使用。
6. 并发请求不阻塞健康检查。

**测试先行：轮转合约**

1. target A 在首字节前返回 quota，自动使用 target B。
2. target A 429 且 Retry-After 在阈值内，等待后仍用 A。
3. Retry-After 超阈值，切到 B。
4. A 发出 text delta 后连接断开，不调用 B。
5. A 发出 tool call start 后连接断开，不调用 B。
6. A 401 禁用 credential，下一请求不再选 A。
7. sticky 切到 B 后，新请求从 B 开始。
8. timed 到期后重新探测 A。
9. 所有候选失败时返回按尝试顺序排列的脱敏摘要。
10. 宿主取消时，当前上游请求被关闭，状态不误判为 Provider 故障。

**实现**

1. GatewayApplication 组合 catalog、vault、selector、rotation 和 adapters。
2. 请求级状态：
   - selected
   - attempting
   - committed
   - completed
   - failed
   - cancelled
3. attempt loop 只在 committed=false 时推进候选。
4. 上游凭据按 attempt 延迟读取，失败后立即丢弃引用。
5. Responses 和 Messages 使用同一 attempt loop。
6. 网关启动时轮换本地短期令牌并写 vault。
7. state file 只写 pid、port、catalog hash、generation、token target，不写 token。
8. 进程探活同时校验 pid、端口和 nonce，避免连接到陈旧进程。
9. graceful shutdown 等待已有请求到上限，再强制取消。
10. bridge.py 的 ensure_bridge 委托给 GatewayController。

**验证**

~~~powershell
python -m unittest scripts.test_gateway scripts.test_end_to_end_rotation scripts.test_bridge -v
python scripts/check_codex_bridge_runtime.py
python scripts/check_runtime_contract.py
~~~

**提交**

~~~text
feat: route both host protocols through the relay gateway
~~~

### Task 10：完成 Codex 宿主适配器

**新增或修改文件**

- scripts/multi_relay/hosts/__init__.py
- scripts/multi_relay/hosts/codex.py
- scripts/multi_relay/toml_config.py
- scripts/multi_relay/instructions.py
- scripts/multi_relay/roles.py
- scripts/multi_relay/paths.py
- scripts/test_host_codex.py
- scripts/test_toml_config.py
- scripts/test_instructions.py
- scripts/test_roles.py

**目标配置**

- 只生成一个或少量稳定 relay provider，而不是每个真实 Provider 都直接暴露给 Codex。
- provider.base_url 指向本机 gateway。
- wire_api 使用 Responses。
- auth command 只返回本地短期令牌。
- AgentProfile 的 model 指向 multi-relay-agent-{name} alias。
- 原父代理 model、model_provider、model_reasoning_effort 完全不变。

**测试先行**

1. 原配置为空、已有 features、已有 agents、已有自定义 providers。
2. 父代理三个关键字段逐字保持。
3. ownership block 之外的 TOML 字节保持。
4. 重复 apply 幂等。
5. agent 配置生成 pool alias、指令、sandbox、skills、MCP。
6. Codex-only native Agent 仍可直接使用 codex-native target。
7. disabled host 不留活动 provider。
8. uninstall 恢复被管理字段，不删除用户后改过的文件。
9. 旧 marker 能识别并升级为 Multi Relay marker。
10. auth helper 无上游 key 泄漏。

**实现**

1. CodexHostAdapter 实现 plan、apply、status、disable、enable、uninstall。
2. 把管理范围和快照逻辑封装在 host adapter。
3. build_provider_blocks 改为基于 pool alias 的 gateway provider。
4. agent config_file 按 AgentProfile 生成。
5. AGENTS.md fan-out 块使用新产品名，旧块可识别、可替换、可恢复。
6. Codex 原生 target 不通过通用 HTTP 转换器；选择时仅允许 codex。

**验证**

~~~powershell
python -m unittest scripts.test_host_codex scripts.test_toml_config scripts.test_instructions scripts.test_roles -v
~~~

**提交**

~~~text
feat: adapt codex agents to multi-relay pools
~~~

### Task 11：完成 Claude Code 宿主适配器和启动器

**新增或修改文件**

- scripts/multi_relay/hosts/claude_code.py
- scripts/multi_relay/paths.py
- scripts/multi_relay/manager.py
- scripts/multi_relay/cli.py
- scripts/test_host_claude_code.py
- scripts/test_launch_claude_code.py

**设计边界**

Claude Code 的 ANTHROPIC_BASE_URL 对被启动进程生效，因此 launcher 模式下父代理和子代理请求都会先到本地网关。网关再按 model alias 解析目标池。此模式使用用户配置的 API-backed targets，不导出 Claude 订阅 OAuth。

**生成的 subagent 文件**

- user scope：Claude Code 用户 agents 目录。
- project scope：项目 .claude/agents。
- YAML frontmatter 至少包含 name、description、model、tools。
- model 使用 multi-relay-agent-{name}。
- Markdown body 使用 AgentProfile 的 developer_instructions。
- 文件头含 ownership marker 和 catalog hash。

**测试先行**

1. AgentProfile 正确渲染成 Claude subagent Markdown。
2. YAML frontmatter 的引号、换行和特殊字符安全。
3. user 与 project scope 路径准确。
4. project path 必须存在且明确传入，拒绝写任意父目录。
5. 用户同名未托管文件不覆盖。
6. 托管文件被用户修改后 uninstall 不删除。
7. launcher 仅给子进程设置环境，不修改当前进程环境。
8. 保留用户无关环境变量，覆盖危险的上游 auth 变量。
9. 设置 ANTHROPIC_BASE_URL、本地 auth token、默认 model alias。
10. claude 可执行文件不存在时给出明确错误。
11. claude 退出码和 Ctrl+C 正确透传。
12. 网关启动失败时不启动 Claude。
13. Claude 退出后专用网关按策略关闭。
14. 命令尾部参数使用数组传递，不经过 shell 拼接。

**实现**

1. ClaudeCodeHostAdapter 实现 plan、apply、status、disable、enable、uninstall。
2. 实现受控 frontmatter renderer，不依赖不确定的 YAML 库。
3. 实现 find_claude_code，支持显式路径和 PATH。
4. 实现 build_claude_environment：
   - 复制父环境；
   - 设置 loopback base URL；
   - 设置短期本地 token；
   - 设置默认 pool alias；
   - 清除会绕过网关的上游 key 变量；
   - 不把环境落盘。
5. 使用 subprocess 参数数组启动，不使用 shell=true。
6. launcher 输出启动摘要，但不显示 token、上游地址中的秘密或完整环境。
7. manager manifest 记录 scope 与托管文件。

**验证**

~~~powershell
python -m unittest scripts.test_host_claude_code scripts.test_launch_claude_code -v
~~~

在具有 Claude Code 的真实环境追加人工 smoke test：

~~~text
multi-relay launch claude-code --pool general -- --version
multi-relay launch claude-code --pool general
~~~

**提交**

~~~text
feat: add claude code host support
~~~

### Task 12：重构 manager 与 CLI，暴露完整管理能力

**修改文件**

- scripts/multi_relay/manager.py
- scripts/multi_relay/relay_manager.py
- scripts/multi_relay/cli.py
- scripts/multi_relay/native_test.py
- scripts/multi_relay/transaction.py
- scripts/test_manager.py
- scripts/test_cli.py
- scripts/test_cli_targets.py
- scripts/test_cli_pools.py
- scripts/test_native_test.py
- scripts/test_transaction.py
- configure-multi-relay.cmd

**测试先行**

1. parser 覆盖第 8 节列出的所有命令。
2. 每个可变操作同时覆盖成功、验证失败、事务回滚。
3. duration 的 s、m、h、d 和边界值。
4. credential add 不接受 --key。
5. JSON 输出 schema 稳定且无 secret。
6. provider → credential → target → pool → agent 的完整创建顺序。
7. 有引用时删除对象失败并列出引用者。
8. pool order 的重复、缺失、disabled target。
9. setup --host all 任何宿主失败时整体回滚。
10. status 在 gateway 未启动、陈旧 pid、catalog 损坏、host 漂移时仍为只读。
11. repair 只修复托管差异。
12. 旧 relay_manager 导入和现有命令仍可工作。

**实现**

1. RelayManager 改为编排器，具体宿主写入交给 HostAdapter。
2. 对每个变更先生成 ChangePlan，再由 Transaction 应用。
3. setup、apply、repair 实现双宿主单事务：
   - 校验 catalog；
   - 校验凭据引用；
   - 预渲染所有文件；
   - 检查冲突；
   - 写临时文件；
   - 提交；
   - 更新 manifest；
   - 失败则逆序恢复。
4. 为 provider、credential、target、pool、agent 增加 CRUD。
5. JSON 输出统一：
   - status
   - changed
   - warnings
   - details
   - next_actions
6. native_test 分宿主检查，并提供 all 聚合。
7. configure-multi-relay.cmd 只作为安全入口转发到新 CLI。

**验证**

~~~powershell
python -m unittest scripts.test_manager scripts.test_cli scripts.test_cli_targets scripts.test_cli_pools scripts.test_native_test scripts.test_transaction -v
~~~

**提交**

~~~text
feat: expose target pools through the multi-relay cli
~~~

### Task 13：完成公共名称改造与兼容迁移

**修改文件**

- scripts/multi_relay/__init__.py
- scripts/multi_relay/paths.py
- scripts/multi_relay/compatibility.py
- scripts/multi_relay/migration.py
- scripts/multi_relay/credentials.py
- scripts/multi_relay/cli.py
- scripts/multi_relay.py
- configure-multi-relay.cmd
- agents/openai.yaml
- SKILL.md
- scripts/test_rebrand.py
- scripts/test_compatibility.py
- scripts/test_relay_public_layout.py
- scripts/test_skill_contract.py
- scripts/test_windows_launcher.py

**正式命名**

- 产品展示名：Multi Relay。
- 仓库名：multi-relay。
- Python package：multi_relay。
- CLI：multi-relay。
- 状态目录：multi-relay。
- ownership marker：MULTI-RELAY。
- HTTP 内部管理前缀：/_multi-relay。

**仅兼容代码允许出现的旧名称**

- codex-deepseek-relay。
- codex-deepseek-subagent。
- codex-multi-relay。
- 旧 Credential Manager target。
- 旧 TOML 和 AGENTS marker。

**测试先行**

1. 面向用户的 README、help、status、错误、manifest 新写入不含旧品牌。
2. 旧目录发现顺序明确，新目录优先。
3. 旧 manifest、catalog、marker、credential 可迁移。
4. 新旧状态同时存在时不静默合并，按 hash 和 manifest 判定并报告。
5. 迁移幂等。
6. uninstall 能识别迁移前创建的托管内容。
7. Python 旧导入 shim 仍可用。
8. 兼容代码之外搜索不到 DeepSeek 硬编码产品身份。

**实现**

1. 集中定义 PRODUCT_NAME、CLI_NAME、STATE_DIR_NAME、MARKERS。
2. paths.py 返回产品级路径和两个宿主路径，不再把 Codex home 当产品根目录。
3. CompatibilityReport 显示发现的旧资产及动作。
4. 迁移采取 copy/verify/switch/cleanup，任何一步失败保留旧数据。
5. 所有 User-Agent 改为 multi-relay/产品版本。
6. 删除默认逻辑中的 REQUESTED_MODEL = deepseek-v4-pro 硬编码；默认 preset 可引用 catalog seed。
7. 保留 DeepSeek Provider preset，但与其他 Provider 同级。

**验证**

~~~powershell
python -m unittest scripts.test_rebrand scripts.test_compatibility scripts.test_relay_public_layout scripts.test_skill_contract scripts.test_windows_launcher -v
~~~

再运行定向搜索，只允许兼容白名单命中：

~~~powershell
Get-ChildItem -Recurse -File | Select-String -Pattern "codex-deepseek|codex-multi-relay|DeepSeek child fan-out"
~~~

**提交**

~~~text
refactor: rebrand the project as multi relay
~~~

### Task 14：重写中英文文档与视觉资产

**修改文件**

- README.md
- README_EN.md
- SKILL.md
- references/compatibility.md
- references/catalog.md
- references/rotation.md
- references/codex.md
- references/claude-code.md
- references/security.md
- assets/readme/architecture.svg
- assets/readme/workflow.svg
- assets/readme/hero.svg
- assets/readme/hero.png
- assets/readme/social-preview.svg
- assets/readme/social-preview.png
- assets/readme/relay-mark.svg
- scripts/build_readme_assets.py
- scripts/readme_visuals.py
- scripts/test_readme_visuals.py
- scripts/test_skill_contract.py

**文档必须回答**

1. Multi Relay 解决什么问题。
2. ExecutionTarget 为什么同时绑定模型和 key。
3. sticky 与 timed 如何选择。
4. 哪些错误会切换，哪些不会。
5. 为什么开始输出后不能自动跨模型续写。
6. 如何为 Codex 安装、测试、禁用、卸载。
7. 如何为 Claude Code 安装、启动、测试、禁用、卸载。
8. 为什么 Claude Code launcher 下父请求也经过网关。
9. 如何新增 Provider、多个 key、target、pool、agent。
10. 视觉、工具调用和联网能力如何声明与验证。
11. 凭据保存在哪里，哪些地方绝不会保存 secret。
12. 如何从旧项目迁移。
13. 限制和故障排查。

**README 推荐顺序**

1. 一句话价值。
2. Codex 和 Claude Code 支持矩阵。
3. 5 分钟快速开始。
4. target pool 示例。
5. 轮转行为示例。
6. 架构图。
7. CLI 索引。
8. 安全模型。
9. 兼容迁移。
10. 限制、测试、许可证。

**文档示例要求**

- 所有示例使用假 Provider URL 和假 model id，不能出现形似真实 key 的字符串。
- 同时给 sticky 与 timed 示例。
- 至少给一个跨 Provider 池：
  - DeepSeek target。
  - Anthropic Messages target。
  - OpenAI-compatible target。
- 示例明确标注 target 的能力差异。
- Claude Code 示例必须通过 multi-relay launch claude-code，不指导用户永久导出上游 API Key。

**视觉更新**

- 品牌不再突出 Codex 或 DeepSeek。
- 架构图同时出现 Codex、Claude Code、target pool、vault、gateway。
- workflow 图展示 committed 前切换和 committed 后终止。
- 继续使用源码 SVG 和可重复生成的 PNG。
- 更新 social preview 与 alt text。

**测试**

1. README 中英文关键命令一致。
2. 所有引用文件存在。
3. SVG 可解析，PNG 尺寸正确。
4. 文档禁止旧品牌和 secret-like 示例，兼容章节除外。
5. SKILL.md 元数据和实际 CLI 一致。

**验证**

~~~powershell
python scripts/build_readme_assets.py
python -m unittest scripts.test_readme_visuals scripts.test_skill_contract -v
~~~

**提交**

~~~text
docs: document multi-provider rotation for both hosts
~~~

### Task 15：扩展跨平台 CI 与端到端测试

**修改文件**

- .github/workflows/test.yml
- scripts/test_end_to_end_rotation.py
- scripts/test_gateway.py
- scripts/test_host_codex.py
- scripts/test_host_claude_code.py
- scripts/check_runtime_contract.py
- scripts/check_codex_bridge_runtime.py
- 新增 scripts/check_public_contract.py

**CI 矩阵**

- windows-latest，Python 3.11 和 3.12。
- macos-latest，Python 3.11 和 3.12。
- ubuntu-latest，Python 3.11 和 3.12。

若执行时间过长，可让 3.11 运行全平台完整测试，3.12 运行 compile、核心单测和 contract；但同一次改造中必须至少有三平台完整覆盖记录。

**Fake upstreams**

使用本地受控 HTTP server，不访问付费 API：

- Responses fake。
- Chat Completions fake。
- Anthropic Messages fake。
- 支持脚本化响应序列：
  - quota 后成功。
  - 429 Retry-After 后成功。
  - 5xx 后成功。
  - 首个 delta 后断开。
  - tool call 后断开。
  - malformed stream。
  - slow response 和取消。

**端到端场景**

1. Codex Responses 入站，DeepSeek Chat target 成功。
2. Codex Responses 入站，A quota，Anthropic target B 成功。
3. Claude Messages 入站，Anthropic target 成功。
4. Claude Messages 入站，A 429，OpenAI-compatible target B 成功。
5. sticky 后续请求保持 B。
6. timed fake clock 到期恢复 A。
7. vision 请求过滤 text-only target。
8. tool request 在 tool-capable target 上转换成功。
9. 已 committed 后断线不访问备用 target。
10. 日志、状态、JSON 输出通过 secret scanner。

**contract checks**

- 运行时不得导入测试专用模块。
- 核心不得导入 hosts.codex 或 hosts.claude_code。
- host adapter 不得读取上游 secret。
- catalog 和 manifest JSON 不得含 secret-like keys。
- 公共 CLI 帮助与文档命令一致。
- 所有兼容 shim 有明确测试。
- package compileall。

**验证**

~~~powershell
python -m unittest discover -s scripts -p "test_*.py" -v
python -m compileall -q scripts
python scripts/check_runtime_contract.py
python scripts/check_codex_bridge_runtime.py
python scripts/check_public_contract.py
~~~

**提交**

~~~text
test: cover cross-model rotation on all supported hosts
~~~

### Task 16：完整验收、发布和 GitHub 更名

**修改文件**

- 必要的版本或元数据文件。
- README.md 与 README_EN.md 中最终仓库链接。
- GitHub 仓库名称与 description。
- 可选 release notes。

**验收前置**

1. git status 只含本计划范围内改动。
2. 全部自动测试在本地通过。
3. 三平台 GitHub Actions 通过。
4. 无 secret、临时文件、__pycache__、真实用户路径进入提交。
5. 文档命令按全新目录人工走通。
6. 旧安装迁移与卸载人工走通。
7. Codex 真实 smoke test。
8. Claude Code 真实 smoke test；没有可执行文件或 API 账户时，必须明确记录未执行，不能伪称通过。

**人工验收清单**

#### Codex

- setup --host codex 不改变父模型。
- 自定义 agent 能按 pool 路由。
- target A 失败后使用 B。
- status 显示 active target 和冷却原因。
- disable、enable、uninstall 正确。

#### Claude Code

- setup --host claude-code 生成受管 subagents。
- launcher 能启动 Claude Code。
- 父请求与 subagent 请求均能到网关。
- 不向父 shell 写永久环境变量。
- 退出码、Ctrl+C、网关清理正确。
- uninstall 不删除用户自建 agent。

#### 凭据

- 至少两个 Provider 和三个 credential。
- replace、disable、remove 精确生效。
- API Key 不出现在 process argv、catalog、manifest、state、日志。

#### 轮转

- quota、rate limit、auth、provider unavailable 行为符合表格。
- sticky 无限保持直到失败或 reset。
- timed 按用户设置时长保持。
- committed 后绝不自动重放。
- capability 不兼容时不会选择错误模型。

**发布动作**

1. 更新仓库名为 multi-relay。
2. 确认旧 GitHub URL 自动重定向。
3. 更新 origin 到新 URL。
4. 提交最终链接修正。
5. 在 main 上执行最终测试。
6. 推送 main。
7. 等待 GitHub Actions 完成。
8. 若 Actions 失败，修复后重新完整验证并推送；不以“本地通过”代替远端通过。
9. 创建单次完整 release notes，不使用产品分阶段标签。

**最终验证**

~~~powershell
git diff --check
python -m unittest discover -s scripts -p "test_*.py" -v
python -m compileall -q scripts
python scripts/check_runtime_contract.py
python scripts/check_codex_bridge_runtime.py
python scripts/check_public_contract.py
git status --short
~~~

**提交**

~~~text
chore: release multi relay
~~~

---

## 11. 测试总矩阵

### 11.1 Catalog

- 合法 schema。
- 每一个必填字段缺失。
- 每一个未知字段。
- 非法 id、URL、协议、能力、时长、上下文。
- 所有引用关系。
- stable serialization。
- schema 1 迁移和幂等。
- secret 字段拒绝。

### 11.2 Credentials

- Windows Credential Manager。
- macOS Keychain。
- Linux Secret Service。
- 多 Provider、多凭据。
- add、replace、enable、disable、remove。
- 遗留凭据迁移。
- 不可用后端。
- Unicode label。
- secret redaction。

### 11.3 Selection 与 rotation

- 排序。
- capability。
- host。
- context。
- trust。
- credential availability。
- 每种 FailureClass。
- sticky。
- timed。
- cooldown。
- Retry-After。
- reset、manual rotate。
- concurrent generation。
- catalog change reconciliation。

### 11.4 Protocols

- Responses 入站与出站。
- Chat Completions 上游。
- DeepSeek Chat 差异。
- Anthropic Messages 入站与出站。
- 流和非流。
- 文本、图像、工具、tool result、usage。
- UTF-8 和分段边界。
- malformed、oversized、timeout、cancel。
- committed boundary。

### 11.5 Gateway

- loopback。
- auth。
- health。
- model aliases。
- legacy route。
- lifecycle。
- stale process。
- graceful shutdown。
- 并发。
- secret-free logging。

### 11.6 Codex host

- TOML ownership。
- parent unchanged。
- provider block。
- agent files。
- AGENTS block。
- apply、repair、disable、enable、uninstall。
- legacy migration。

### 11.7 Claude Code host

- user/project scope。
- frontmatter。
- model alias。
- launcher environment。
- executable discovery。
- argument forwarding。
- signal and exit code。
- gateway lifecycle。
- ownership and uninstall。

### 11.8 CLI

- help。
- human output。
- JSON output。
- every CRUD path。
- dry validation。
- rollback。
- error exit codes。
- no secret argv。

### 11.9 Documentation and package

- link existence。
- command parity。
- visual render。
- public naming。
- compatibility whitelist。
- no cache or build artifacts。

---

## 12. 完成定义

只有同时满足以下条件，改造才算完成：

- [ ] 用户能配置两个以上 Provider。
- [ ] 同一 Provider 能配置多个独立 credential。
- [ ] target 同时绑定 Provider、model 和 credential。
- [ ] pool 能跨 Provider、跨模型排序。
- [ ] sticky 与 timed 均可配置、可观察、可重置。
- [ ] 额度耗尽可在 committed 前自动切换。
- [ ] 普通限流遵守 Retry-After 与用户阈值。
- [ ] 认证失败精确禁用 credential，不误伤 Provider 的其他 key。
- [ ] committed 后绝不自动重放。
- [ ] vision、tool_calling、server_web_search 按能力过滤。
- [ ] Codex 父模型不被修改。
- [ ] Codex agent 能使用 pool alias。
- [ ] Claude Code 能通过 launcher 使用 /v1/messages。
- [ ] Claude subagent 使用同一 catalog 的 AgentProfile。
- [ ] 上游 key 只存在于 OS vault 和短时进程内存。
- [ ] catalog、manifest、state、日志、argv 无上游 key。
- [ ] schema 1 与旧目录能自动迁移。
- [ ] uninstall 可恢复两个宿主的受管改动。
- [ ] 所有公共名称为 Multi Relay 或 multi-relay。
- [ ] Windows、macOS、Linux 自动测试通过。
- [ ] 本地完整测试和 GitHub Actions 均通过。
- [ ] main 已推送，工作目录干净。

任意一项未满足，都不得对用户声称“已全部完成”。

---

## 13. 关键风险与处理

### 13.1 不同模型不是完全可替换

风险：上下文、工具 schema、视觉、推理参数不同。

处理：

- target 显式声明能力。
- 请求前过滤。
- 只对已测试字段转换。
- 无安全等价映射时失败，不静默删字段。

### 13.2 流式请求中途失败

风险：跨模型重放导致重复工具副作用或拼接错误。

处理：

- RequestCommitTracker。
- 首个可见事件前才允许 failover。
- committed 后标准化终止。

### 13.3 Claude Code endpoint 是进程级

风险：无法只给某个 subagent 改 endpoint。

处理：

- 官方支持的 launcher 环境方式。
- 父与子都经过 gateway。
- model alias 决定 pool。
- 文档明确此行为。

### 13.4 API 错误格式不统一

风险：误判额度、错误轮转。

处理：

- Provider 稳定 code 优先。
- HTTP status 次之。
- 有限 pattern 最后。
- 可配置但受验证的 Provider error mapping。
- 未知错误默认保守，不无限轮转。

### 13.5 并发请求同时推进轮转

风险：跳过多个健康 target 或状态损坏。

处理：

- generation compare-and-swap。
- pool 粒度短锁。
- 网络 I/O 不持锁。
- 状态原子替换。

### 13.6 本地网关令牌被其他本机进程读取

风险：本机同用户进程可尝试访问 gateway。

处理：

- 高熵短期 token。
- OS vault。
- 每次 gateway start 轮换。
- loopback 和 Host 校验。
- 最小状态文件权限。
- shutdown token 分离。

### 13.7 用户配置冲突

风险：覆盖已有 Codex TOML 或 Claude agent。

处理：

- ownership marker。
- 写前 snapshot/hash。
- 预检冲突。
- 原子事务。
- uninstall 遇用户修改则保留。

### 13.8 产品更名造成旧用户断裂

风险：路径、凭据、marker、命令变化。

处理：

- 兼容读取。
- copy/verify/switch。
- 旧入口 shim。
- 定向兼容测试。
- GitHub 旧 URL 重定向验证。

---

## 14. 执行顺序和检查点

任务顺序具有依赖关系，不并行写同一核心文件：

1. Task 1–2：数据模型与迁移。
2. Task 3–5：凭据、错误、状态和选择。
3. Task 6–8：规范模型与协议。
4. Task 9：统一网关与端到端轮转。
5. Task 10–11：Codex 和 Claude Code 宿主。
6. Task 12：manager 与 CLI。
7. Task 13–14：正式更名与文档。
8. Task 15：完整 CI。
9. Task 16：验收、GitHub 更名、推送 main。

每个检查点都要：

- 运行该任务列出的局部测试。
- 运行受影响的旧回归测试。
- 检查 git diff --check。
- 检查工作区没有秘密和缓存。
- 单独提交，提交信息使用本文建议或同义准确描述。
- 若出现测试失败，先按 systematic-debugging 查根因，不绕过测试。

禁止做法：

- 不创建 worktree。
- 不创建临时 feature branch。
- 不一次提交所有实现而失去可回滚点。
- 不跳过旧测试。
- 不用 mock 掩盖核心轮转状态机。
- 不用 live 付费 API 代替 deterministic fake upstream。
- 不在失败测试不明确时继续堆实现。

---

## 15. 官方宿主依据

实现前和发布前各复核一次官方文档，以防宿主配置发生变化：

- Codex configuration reference：
  https://developers.openai.com/codex/config-reference
- Claude Code subagents：
  https://code.claude.com/docs/en/sub-agents
- Claude Code features overview：
  https://code.claude.com/docs/en/features-overview
- Claude Code settings：
  https://code.claude.com/docs/en/configuration
- Claude Code LLM gateway：
  https://code.claude.com/docs/en/llm-gateway
- Claude Code model configuration：
  https://code.claude.com/docs/en/model-config
- Claude Code environment variables：
  https://code.claude.com/docs/en/env-vars
- Claude Code authentication：
  https://code.claude.com/docs/en/team

若官方接口与本文不同：

1. 先写一个能复现新行为的契约测试。
2. 更新本文对应条目和兼容说明。
3. 只修改宿主适配层；宿主中立核心语义不随宿主配置格式漂移。

---

## 16. 实现交接

开始实现前：

- [ ] 用户已审阅并认可本文的架构和边界。
- [ ] 基线完整测试已重新运行并记录数量。
- [ ] git status 已确认，不覆盖用户未提交改动。
- [ ] main 与 origin/main 的关系已确认。
- [ ] 不创建 branch 或 worktree。

实现中：

- [ ] 每个 Task 遵循测试先行。
- [ ] 每个 Task 独立提交。
- [ ] 每次修改核心 schema 后立即运行迁移回归。
- [ ] 每次修改协议后立即运行 committed boundary 回归。
- [ ] 每次修改凭据路径后立即运行 secret scanner。

实现结束：

- [ ] 逐条核对第 12 节完成定义。
- [ ] 运行 Task 16 的最终验证。
- [ ] 审阅完整 diff。
- [ ] 推送 main。
- [ ] 等待并确认 GitHub Actions。
- [ ] 向用户报告实际完成内容、测试证据、commit、远端状态和仍存在的真实限制。
