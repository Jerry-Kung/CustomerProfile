# 擎天可靠重试方案（延后实施）

> 状态：仅设计留档，当前版本未实现、未启用。当前运行时仍是一次性执行：
> `queued -> processing -> succeeded | failed`。本文涉及的数据库字段、attempts
> 表、租约、run_id 恢复、自动重试、跨模型失败兜底、熔断和商用预算均不属于
> 当前商业 API 积压分流版本。

## 1. 背景与目标

系统有两个结构相同的擎天工作流：

- `local`：使用本地模型，作为默认执行通道。
- `commercial`：使用商用 API 模型，当前仅在队列积压时承担突发流量。

当前 Worker 从 MySQL 队列领取任务后，会在一次同步调用中完成擎天提交和轮询。任意请求、轮询或解析异常都会立即把主任务写成 `failed`，没有自动重试。擎天 `run_id` 也只存在于进程内，Worker 重启后无法恢复原运行。

未来重试版本的目标：

1. 正常情况下只使用本地模型。
2. 队列达到高水位时，商用模型分担积压。
3. 区分提交失败、轮询失败、工作流终止失败、输出失败和数据库失败。
4. 已获得 `run_id` 时只恢复轮询，不重复提交工作流。
5. 单次执行失败不立即把主任务标记为最终失败。
6. 支持同模型重试、跨模型兜底、退避、熔断和商用成本限制。
7. 防止多 Worker 重复领取、旧执行覆盖新提交、Worker 崩溃后重复调用模型。
8. 完整记录每次执行尝试，便于审计成功率、耗时和商用成本。

## 2. 当前问题

### 2.1 没有自动重试

当前状态机只有：

```text
queued -> processing -> succeeded | failed
```

`failed` 是吸收态。所有工作流异常都会直接调用 `mark_failed()`。

### 2.2 提交和轮询不可恢复

`QingTianClient.run_workflow()` 同时完成：

```text
submit -> 获得 run_id -> poll -> output
```

`run_id` 没有在获得后立即落库。进程退出后只能重新提交，存在重复执行和重复商用计费风险。

### 2.3 两个 Worker 会无条件竞争同一队列

现有 `claim_next_queued()` 对 Worker 身份没有限制。如果本地和商用 Worker 同时运行，任务会被随机分配，而不是只在积压时使用商用模型。

### 2.4 `updated_at` 不能安全判断 Worker 是否失活

工作流可能运行数十分钟，而任务领取后不会持续更新 `updated_at`。直接把“长时间 processing”重新入队，可能与仍在运行的 Worker 并发执行。

### 2.5 缺少执行代次和写入令牌

用户重新提交同一个任务后，旧 Worker 仍可能根据相同 `task_id` 写回结果，覆盖新一轮任务。

## 3. 总体架构

```text
API -> customer_profile_tasks
             |
             +-> local Worker
             |     - 始终可领取普通新任务
             |     - 处理 local 重试和 local run_id 恢复
             |
             +-> commercial Worker
                   - 高水位时领取普通新任务
                   - 始终可处理已明确分配给 commercial 的恢复/兜底任务
                   - 受并发、熔断和预算上限约束
```

两个 Worker 继续共享 MySQL，并使用 `FOR UPDATE SKIP LOCKED` 原子领取。

路由决策发生在领取任务时，不在 API 提交时提前决定，确保使用最新队列压力。

当前版本只落地共享队列的高低水位积压分流。本文其余能力都应在未来独立开发，
不得把其中的配置项当作当前可用功能。

## 4. 状态模型

主任务状态扩展为：

```text
queued
processing
retry_wait
succeeded
failed
```

状态流转：

```text
queued      -> processing
processing  -> succeeded
processing  -> retry_wait
retry_wait  -> processing
processing  -> failed
retry_wait  -> failed
```

语义：

- `queued`：第一次等待执行。
- `processing`：某个 Worker 持有有效租约，正在提交、轮询或写结果。
- `retry_wait`：任务尚未最终失败，等待恢复轮询或创建下一次工作流运行。
- `succeeded`：最终成功。
- `failed`：不可重试、重试路径耗尽、超过总生命周期或人工终止。

对外 API 可以把 `retry_wait` 映射为 HTTP 202 的 `processing`，同时返回 `TASK_RETRYING` 和 `next_retry_at`，避免破坏现有前端状态枚举。

## 5. 数据模型

### 5.1 主任务新增字段

`customer_profile_tasks` 新增：

| 字段 | 用途 |
|---|---|
| `generation` | 用户每次重新提交时递增，隔离旧执行 |
| `execution_backend` | 当前执行通道：`local` / `commercial` |
| `workflow_run_id` | 当前擎天运行 ID |
| `attempt_count` | 已创建的新工作流运行次数 |
| `next_retry_at` | 延迟重试可领取时间 |
| `last_error_code` | 最近错误码 |
| `last_error_stage` | `submit` / `poll` / `workflow` / `output` / `db` |
| `lease_expires_at` | Worker 租约到期时间 |
| `claim_token` | 本次领取 UUID，保护最终写入 |
| `queued_at` | 当前 generation 入队时间 |
| `started_at` | 当前尝试开始时间 |
| `finished_at` | 最终完成时间 |

保留原有 `error_message` 作为最近错误摘要。

### 5.2 执行尝试表

新增 `customer_profile_task_attempts`：

| 字段 | 用途 |
|---|---|
| `task_id` / `generation` / `attempt_no` | 唯一标识一次新工作流运行 |
| `execution_backend` | 本地或商用 |
| `route_reason` | 正常、本地重试、积压分流、跨模型兜底等 |
| `status` | `claimed` / `submitting` / `running` / `succeeded` / `failed` / `abandoned` |
| `workflow_run_id` | 擎天运行 ID |
| `failure_stage` | 失败阶段 |
| `error_code` / `error_message` | 失败原因 |
| `retryable` | 是否允许继续 |
| `queue_depth_when_claimed` | 领取时积压 |
| `oldest_queue_age_when_claimed` | 领取时最老等待时间 |
| `started_at` / `finished_at` | 尝试耗时 |

轮询恢复同一个 `run_id` 不新增 attempt；只有重新调用 `/run_app_workflow` 才新增 attempt。

## 6. 失败分类与处理

### 6.1 提交阶段

| 错误 | 策略 |
|---|---|
| DNS/连接失败 | 可重试，指数退避 |
| HTTP 429 | 可重试，优先使用 `Retry-After` |
| HTTP 5xx | 可重试，指数退避 |
| HTTP 401/403 | 不可重试，终止并报警 |
| HTTP 400/422 | 不可重试，终止 |
| 提交超时 | 状态记为 `unknown`；优先通过幂等键或业务执行 ID 查重 |

提交请求携带稳定的：

```text
execution_id = task-{task_id}-generation-{generation}-attempt-{attempt_no}
```

如果擎天不支持原生幂等，应把该值放入 `InputData`，并在工作流入口尽可能去重。

### 6.2 已获得 run_id 后轮询失败

网络错误、查询 5xx、Worker 重启或本地轮询超时，都只进入：

```text
retry_wait -> resume_existing_run
```

继续查询相同 `run_id`，不增加 `attempt_count`，不产生新的模型调用。

### 6.3 工作流明确终止

擎天返回 `failed`、`stopped`、`interrupted`、`timeout` 后，原运行不可继续。根据后端和错误码安排同模型重试或跨模型兜底。

### 6.4 输出失败

`card_id` 和 `nickname` 已改为可选，不参与成功判定。以下情况仍属于输出失败：

- `output` 为空。
- `output` 不是字符串。
- 字符串不是合法 JSON。
- JSON 根节点不是对象。

本地输出失败优先切换商用模型一次；商用输出失败优先回退本地模型，不对同一模型连续盲重试。

### 6.5 数据库写入失败

擎天已经成功时只重试结果持久化，不能重新提交工作流。由于 `run_id` 已落库，进程重启后可重新查询原成功结果并再次写库。

## 7. 默认重试路径

每个任务最多创建 3 次工作流运行：

```text
MAX_WORKFLOW_ATTEMPTS=3
MAX_LOCAL_ATTEMPTS=2
MAX_COMMERCIAL_ATTEMPTS=1
```

初始走本地：

```text
local #1
  -> 可重试基础设施失败：local #2
  -> 仍失败或输出失败：commercial #1
  -> succeeded / failed
```

因积压初始走商用：

```text
commercial #1
  -> 失败：local #1
  -> 可重试：local #2
  -> succeeded / failed
```

同一个 `run_id` 的轮询恢复不消耗以上次数。

退避：

```text
30 秒、2 分钟、10 分钟，附加 ±20% jitter
```

## 8. 动态分流

### 8.1 高低水位

初始参数：

```text
BURST_QUEUE_HIGH_WATERMARK=10
BURST_QUEUE_LOW_WATERMARK=3
BURST_OLDEST_WAIT_SECONDS=600
BURST_OLDEST_RECOVER_SECONDS=120
```

进入突发状态：

```text
queue_depth >= 10
OR oldest_queued_age >= 10 分钟
```

退出突发状态：

```text
queue_depth <= 3
AND oldest_queued_age <= 2 分钟
```

本地 Worker 始终领取普通新任务。商用 Worker 只有在突发状态下领取普通新任务，但明确分配给商用的恢复和兜底任务不受高水位限制。

### 8.2 领取优先级

每个后端按以下顺序领取：

1. 已有对应后端 `run_id` 的到期恢复任务。
2. 已到 `next_retry_at`、明确分配到该后端的重试任务。
3. 普通新任务；商用后端需满足突发条件。

领取和写入 `claim_token`、租约、执行来源必须在同一事务完成。

## 9. 租约和崩溃恢复

初始参数：

```text
WORKER_LEASE_SECONDS=180
WORKER_LEASE_HEARTBEAT_SECONDS=30
```

Worker 每次发起轮询请求前刷新租约；180 秒默认值长于擎天客户端 120 秒读取超时，避免请求尚未返回时被恢复器误判为 Worker 失联。恢复器只处理：

```text
status = processing
AND lease_expires_at < NOW()
```

恢复策略：

- 有 `workflow_run_id`：进入 `retry_wait`，继续轮询原运行。
- 无 `workflow_run_id` 且确认提交未成功：安排新提交。
- 无 `workflow_run_id` 且提交结果未知：先按幂等执行 ID 查重；无法查重时进入受限重试并记录重复风险。
- 目标 Worker 未启动或商用预算耗尽时，监护器仍会把超过任务总生命周期的
  `retry_wait` 任务终止为 `TASK_LIFETIME_EXCEEDED`，不会无限等待。

旧 Worker 写回时必须匹配：

```text
task_id + generation + claim_token + status=processing
```

匹配失败说明结果已过期，禁止覆盖新任务。

## 10. 熔断与运行模式

目标后端运行模式：

| 模式 | 行为 |
|---|---|
| `NORMAL` | 本地处理普通新任务，商用仅处理已分配任务 |
| `BURST` | 本地和商用共同处理普通新任务 |
| `LOCAL_DEGRADED` | 商用临时接管新任务 |
| `COMMERCIAL_DEGRADED` | 仅本地接收新任务 |
| `BOTH_DEGRADED` | 暂停创建新工作流运行，任务留队 |

熔断初始规则：

```text
5 分钟内连续 5 次基础设施失败
-> 熔断 10 分钟
-> 半开只放 1 个探测任务
```

熔断只统计连接、5xx、429 过载和平台超时，不统计业务结果、数据库错误或代码校验错误。

建议第一版熔断器使用 Worker 进程内状态，因此同一角色固定为单 Pod，通过
`WORKER_CONCURRENCY` 调整吞吐。attempts 计数和商用小时/日预算保存在
MySQL，可跨进程共享。后续若同一角色需要多 Pod 水平扩容，再把熔断窗口、
打开时间和半开探测锁迁入 MySQL。

## 11. 商用成本保护

```text
COMMERCIAL_MAX_CONCURRENCY=1（初期）
COMMERCIAL_MAX_ATTEMPTS_PER_TASK=1
COMMERCIAL_MAX_CALLS_PER_HOUR=<配置>
COMMERCIAL_MAX_CALLS_PER_DAY=<配置>
COMMERCIAL_FALLBACK_ENABLED=true
```

预算只限制创建新的商用运行；继续轮询已有商用 `run_id` 不受限制。
该开关的代码默认值为 `false`；只有确认商用 Worker 已部署后才设为 `true`。

达到预算后：

- 不创建新商用运行。
- 普通新任务由本地处理。
- 已分配的商用兜底任务保持 `retry_wait`，预算恢复后再执行；超过总生命周期
  则终止为 `TASK_LIFETIME_EXCEEDED`。
- 记录告警和预算拒绝原因。

## 12. API 语义

重试等待对外保持 HTTP 202：

```json
{
  "code": "TASK_RETRYING",
  "status": "processing",
  "attempt_count": 2,
  "next_retry_at": "2026-07-17T10:30:00+08:00"
}
```

最终失败才返回 HTTP 422：

```json
{
  "code": "TASK_FAILED",
  "status": "failed",
  "attempt_count": 3,
  "error_code": "QINGTIAN_WORKFLOW_FAILED"
}
```

执行后端可以只在内部管理接口和日志中展示，避免对业务前端暴露模型供应方式。

## 13. 可观测性

至少记录：

```text
task_id
generation
attempt_no
execution_backend
route_reason
workflow_run_id
failure_stage
error_code
retryable
queue_depth_when_claimed
oldest_queue_age_when_claimed
workflow_elapsed_ms
next_retry_at
```

核心指标：

- 首次成功率。
- 最终成功率。
- 平均尝试次数。
- 同 `run_id` 恢复次数。
- 本地/商用成功率和 P50/P95 耗时。
- 队列深度和最老等待时间。
- 商用每小时/每天新运行次数。
- 熔断次数。
- 最终失败原因分布。
- 可能重复提交的 `unknown` 尝试数。

## 14. 初始推荐参数

```text
本地并发：5
商用并发：1

积压启动：queued >= 10 或最老等待 >= 10 分钟
积压停止：queued <= 3 且最老等待 <= 2 分钟

单任务最大新运行：3
本地最大新运行：2
商用最大新运行：1

重试退避：30 秒、2 分钟、10 分钟，±20% jitter
租约：180 秒
租约刷新：30 秒

熔断：5 分钟内连续 5 次基础设施失败
熔断时间：10 分钟
半开探测：1 个
任务总生命周期：2 小时
```

## 15. TDD 实施顺序

1. 先写状态机、配置和迁移契约测试，再增加字段和 attempts 表。
2. 先写擎天 submit/poll 分离测试，再重构客户端。
3. 先写 run_id 持久化、租约、claim token 和恢复测试，再实现仓储事务。
4. 先写失败分类、退避和最大次数测试，再实现 RetryPolicy。
5. 先写本地常驻、商用高低水位、明确重试绕过水位测试，再实现 RoutingPolicy。
6. 先写跨模型路径、熔断和预算测试，再接入 WorkerExecutor。
7. 更新 API、Deployment 和运维文档。
8. 运行单元测试、契约测试、迁移测试和 MySQL 并发集成测试。

## 16. 上线顺序

1. 先部署向后兼容的数据库迁移。
2. 部署支持新字段但关闭自动重试和商用突发的代码。
3. 开启 run_id 持久化与同 run_id 恢复。
4. 开启本地模型同后端重试。
5. 以商用并发 1 开启积压分流。
6. 开启跨模型兜底。
7. 开启熔断和预算硬限制。

未来版本紧急回滚可关闭：

```text
RETRY_ENABLED=false
BURST_ENABLED=false
COMMERCIAL_FALLBACK_ENABLED=false
```

已有 `run_id` 的轮询恢复仍应保留，不能因为关闭新调用而放弃正在运行的任务。
