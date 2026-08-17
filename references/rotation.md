# 轮转、冷却与 committed 边界

本文档说明 Multi Relay 如何选择执行目标、`sticky` 与 `timed` 的差异、错误分类与冷却、以及"开始输出后不再自动跨模型续写"的 committed 边界。领域模型见 [catalog.md](catalog.md)，凭据安全见 [security.md](security.md)。

## 1. 选择流程

一次请求到达网关后，选择器按以下顺序过滤目标；任一条件不满足都会记录原因并从本次候选集中剔除：

1. 池与目标均已启用，Provider 已启用。
2. 请求宿主在池与目标的 `host_compatibility` 内。
3. 池的 `required_capabilities` 与请求要求的能力都是目标能力的子集。
4. 上下文 token 数不超过目标 `context_window`。
5. `high-risk` 请求不跨信任限制（要求 `trust=high`）。
6. 目标引用的凭据存在且启用（`auth_mode=none` 或 `host-native` 时跳过）。
7. 目标不在冷却期。

之后在过滤后的候选中按池的用户配置顺序取第一个。选择器先做能力过滤，再按池状态和顺序选择，绝不会先选目标再"假装"它具备缺失能力。

常见的拒绝原因包括 `pool_disabled`、`pool_host_incompatible`、`target_disabled`、`provider_disabled`、`host_incompatible`、`capability_missing`、`context_exceeded`、`trust_too_low`、`credential_disabled`、`credential_unavailable`、`cooldown` 与 `manually_skipped`。全部目标被过滤或冷却时返回 `no_eligible_target`，并附每个目标的非秘密原因，不静默换 Provider 或模型。

## 2. sticky 与 timed 如何选择

两种策略描述的是"切换成功后停留多久"：

### 2.1 sticky

- 初始选择 `targets` 中的第一个健康候选。
- 发生可切换故障后，选择后续健康目标；选择成功即把该目标记为 `active_target_id`。
- 后续请求始终从 `active_target_id` 开始，即使更高优先级的目标已经恢复。
- 只有以下事件改变当前目标：再次发生可切换故障、用户执行 `pool reset`、用户修改顺序并 apply、当前 target 被禁用或删除。

适合"切换后稳定最重要"的场景，例如不希望流量在多个模型之间来回抖动。

### 2.2 timed

- 切换成功时记录 `selected_at`，并保持 `duration_seconds`（`hold_until = selected_at + duration`）。
- 保持期内后续请求继续使用当前目标。
- 到期后重新从第一优先级目标探测；若首位健康则切回首位并重新计时。
- 若首位仍处于冷却期，则继续使用当前健康目标，并顺延到首位可重试的时间。
- `duration_seconds` 在 timed 池必填，sticky 池禁止出现；时长支持 `s`、`m`、`h`、`d` 单位，硬上限一年。

适合"优先用便宜的或首选的模型，故障时临时借道备用模型，到期自动回去"的场景。

修改策略与时长：

```powershell
python scripts\multi_relay.py pool strategy general sticky --json
python scripts\multi_relay.py pool strategy general timed --duration 2h --json
```

### 2.3 手动控制

- `pool rotate general`：立即切到下一个健康候选。
- `pool reset general`：清除 sticky/timed 选择与目标健康状态，从第一优先级重新评估。
- `pool status general`：查看当前目标、选择时间与冷却状态。

网关提供受本地令牌保护的 `GET /_multi-relay/pools` 供自动化读取池状态；复位必须走受管 CLI 的 `pool reset <pool>`，避免绕开状态锁与校验。

## 3. 冷却

发生可切换故障后，目标进入冷却，冷却时长按错误分类取值（pool 默认值）：

| 故障分类 | 冷却来源 | 默认时长 |
| --- | --- | --- |
| `quota_exhausted` | `cooldown.quota_seconds` | 86400 秒（24 小时） |
| `rate_limited` | `max(cooldown.rate_limit_seconds, Retry-After)` | 60 秒起 |
| `auth_invalid` | `cooldown.auth_seconds` | 3600 秒（1 小时） |
| 其余可切换故障 | `cooldown.provider_seconds` | 30 秒 |

`auth_invalid` 还会把该凭据引用标记为不可用：同一 Provider 下共享该凭据的其他 target 也会被过滤，等待用户替换或启用凭据。冷却只记录在运行时状态中，不写回 `catalog.json`。

## 4. 哪些错误会切换，哪些不会

分类顺序是：先读 Provider 稳定错误码，再看 HTTP 状态，最后才使用有限的消息模式。禁止用宽泛字符串匹配把普通错误误判为额度耗尽。

| 分类 | 典型信号 | 当前目标处理 | committed 前是否切换 |
| --- | --- | --- | --- |
| `quota_exhausted` | 402、余额或额度耗尽错误码 | 进入 quota 冷却 | 是 |
| `rate_limited` | 429、明确限流 | 先按 Retry-After 等待，超阈值后冷却 | 是 |
| `auth_invalid` | 401、403、`invalid_api_key` | 禁用该凭据引用，等待用户处理 | 是 |
| `model_unavailable` | 404 model、`model_not_found` | 冷却具体 target | 是 |
| `provider_unavailable` | 5xx、DNS、连接或超时 | 有界重试后冷却 | 是 |
| `protocol_error` | 无法解析的响应或流 | 冷却 target，记录脱敏诊断 | 是 |
| `request_invalid` | 400 参数或 schema 错误 | 原样标准化返回 | 否 |
| `context_exceeded` | 上下文过长 | 返回所需与可用窗口 | 否 |
| `policy_blocked` | 内容策略或安全拒绝 | 返回拒绝 | 否 |
| `cancelled` | 宿主断开或用户取消 | 取消上游请求 | 否 |
| `no_eligible_target` | 全部被过滤或冷却 | 返回每个目标的非秘密原因 | 否 |

`request_invalid`、`context_exceeded`、`policy_blocked` 不切换，是因为换一个模型不会修复请求本身，而且可能改变语义。

## 5. 同一目标重试

在切换到下一个目标之前，部分故障会先在同一目标上有界重试：

- 网络连接建立失败：最多 1 次短重试，可配置但有硬上限。
- 502、503、504：带抖动的指数退避，默认最多 2 次。
- 429 且 `Retry-After` 小于等于 `pool.max_rate_limit_wait_seconds`（默认 30 秒）：等待后重试同一目标。
- 429 超过等待阈值或没有 `Retry-After`：目标进入 rate-limit 冷却并尝试下一个目标。
- 401、403：不重试同一凭据。
- 400、413、422：不重试、不切换。
- 所有等待都响应宿主的取消信号。

## 6. committed 边界

### 6.1 什么算"已开始输出"

以下任一规范化事件写入宿主时，本次请求进入 committed 状态：

- `content_block_started`（含 reasoning summary 内容块）。
- `text_delta`。
- `tool_call_started`、`tool_call_arguments_delta`。

以下事件本身不提交请求：

- 内部连接建立。
- 尚未发给宿主的 upstream headers。
- 内部重试记录。
- 零内容 keep-alive。

非流式请求在完整响应到达并校验通过后一次性提交；流式请求在首个可见事件前会把事件缓冲在内存中，首个提交事件一出现就按顺序刷给宿主。

### 6.2 为什么 committed 后不能自动跨模型续写

一次代理执行一旦产生外部可见输出，重放给另一个模型会破坏执行语义：

- 工具调用可能已经让宿主执行了副作用；换模型重放会重复写文件、重复发送消息或重复调用外部服务。
- 前一个模型已经给出的半截文本与后一个模型的续写没有一致性，可能产生互相矛盾的答案。
- 请求中的上下文无法保证被第二个模型完整、等价地重建。

因此 committed 前允许 failover，committed 后禁止自动重放。committed 之后的上游失败只返回标准化终止错误（`code` 与脱敏 `message`），`resumable` 与重试、failover 指令都被清除；用户可以在宿主中修正后重新发起一次完整的新请求。

## 7. 运行时状态

选择、保持期与冷却都记录在独立的 `runtime-state.json`（产品状态目录下），不写回目录：

- 顶层含 `catalog_hash` 与单调递增的 `generation`，更新使用比较并交换（CAS），避免并发覆盖。
- 每个池记录 `active_target_id`、`selected_at`、`hold_until` 与各 target 的 `status`、`reason`、`retry_at`、`failure_count`。
- 目录内容变化后，无效的旧状态会被 reconcile 丢弃，不会把过期冷却套用到新目标。

状态文件禁止出现 API Key、Authorization header、请求正文、响应正文、用户 prompt、完整 Provider 错误正文、图片或工具结果。

## 8. 排障

- 没有发生切换：先看 `pool status general` 的拒绝原因；常见是目标仍在冷却期，或请求要求的能力只有部分目标具备。
- timed 到期没有切回首位：首位通常仍在冷却；`retry_at` 显示下次可探测时间。
- 已开始输出后上游断线：这是 committed 之后的终止错误，属预期行为，不会静默切模型；重新发起请求即可。
- 怀疑状态损坏：`pool reset general` 清除池级选择与健康状态后重新评估。
