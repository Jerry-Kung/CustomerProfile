# ADR 0001：任务队列用 SQLite 表实现，不引入 Redis / Celery

- 状态：已采纳（V0.5.2）
- 相关：差异 W26、W27、W28；`docs/architecture.md` §3

## 背景

`Dify迁移任务说明.md` §4 要求：任务**先持久化入队再返回 run_id**，且 API 与执行 worker
分离部署（「部署 = Docker Compose，含 API、单个执行 worker、数据库」）。V0.5.1 之前的
实现是内存里的 `asyncio.Task`（`Scheduler._run_tasks`），进程退出任务即消失，不满足该要求。

需要回答的是：**用什么承载这个队列。**

## 备选方案

| 方案 | 结论 |
|---|---|
| Redis + Celery / RQ | **否决。** 规划 §5 约束 7 明确禁止「未实际需要的重型框架」。为一个单 worker、单机部署、日均任务量个位数的场景引入 broker，要额外承担进程、持久化配置、序列化兼容与监控面 |
| Airflow / Temporal | **否决。** 同上，且它们解决的是跨系统编排，本项目的编排已在工作流定义层解决 |
| 新建独立 `run_queue` 表 | **否决。** 会让「一次运行」有两个事实来源（队列表 + 运行表），需要跨表同步状态；同步失败的表现是「队列里有、运行列表里没有」这类最难查的不一致 |
| **复用 `workflow_runs` 表 + 条件 UPDATE 领取** | **采纳** |

## 采纳方案与理由

**队列复用 `workflow_runs` 表**：入队即写一行 `status='queued'`，worker 领取即转
`running`。`RunStatus.QUEUED` 这个枚举值自 V0.2 就存在却从未被写入——它本来就是为队列
预留的位置，只是当时没有消费者。

**原子领取用单条语句**：

```sql
UPDATE workflow_runs SET status='running', claimed_at_ms=?, claimed_by=?,
       lease_expires_at_ms=?
WHERE run_id = (SELECT run_id FROM workflow_runs
                WHERE status='queued' AND parent_run_id IS NULL
                ORDER BY created_at_ms LIMIT 1)
  AND status='queued'
RETURNING run_id, ...
```

SQLite 的写事务是串行的，因此两个 worker 同时执行时，只有一个能把某行从 `queued` 改成
`running`；另一个的 `WHERE` 不再命中，`RETURNING` 无行返回。

**为什么不用「先 SELECT 再 UPDATE」**：两句之间存在窗口，窗口内两个 worker 会读到同一行
并都认为自己领到了——同一个任务被跑两遍，对本项目意味着重复的真实模型调用与重复的外部
副作用。这不是理论风险：多 worker 是运维手工起第二个进程就能触发的场景。

`RETURNING` 需要 SQLite ≥ 3.35（本机实测 3.50.4）。若部署环境低于该版本，需退回
「先 UPDATE 再 SELECT by claimed_by」的等价写法。

## 代价与后续

- **留痕表兼任队列**：队列字段（`queued_at_ms` / `claimed_at_ms` / `claimed_by` /
  `lease_expires_at_ms`）全部可空，不影响既有查询。这是复用带来的直接代价，接受它，
  因为「单一事实来源」的收益更大。
- **轮询而非推送**：worker 按 `WORKER_POLL_INTERVAL_MS`（缺省 1s）轮询。跨进程通知需要
  额外机制，而一次运行实测约 1282s，秒级轮询的空转开销可忽略。
- **子运行不入队**：子运行由父运行的 `tool` 节点直接执行（`SubWorkflowRunner`），
  通过 `parent_run_id IS NULL` 把领取限定在主运行上。否则主入口一次扇出十几路，会与父运行
  抢队列名额并自锁。
- **过期租约不自动重领**（差异 W28）：重领会把跑到一半的运行整条重跑，而这需要幂等保证。
  在幂等回写落地前，`requeue_expired_leases` 只作为显式动作存在。

## 若日后任务量增长

第一步是给队列加优先级（`ORDER BY` 已有位置可插），而不是换基础设施。真正需要 broker 的
信号是：需要跨机器调度、需要精确的延迟重试、或 SQLite 成为写瓶颈——目前都不是。
