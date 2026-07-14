# 10 · 运营与故障定位备忘

## 日志格式

`src/config/logging_utils.get_logger()`：

```
%(asctime)s %(levelname)s %(name)s %(message)s
2026-05-18T20:10:00 INFO worker_executor worker_dify_invoke_begin task_id=42 work_env=test
```

所有 logger 共用 stdout 流，没有 JSON 化、没有 trace id 自动注入。`x-request-id` 头由调用方传，被 `submit_task` 显式记录但不会自动透传到 Worker（因为 Worker 不知道是哪个 HTTP 请求触发的）。

## 关键日志锚点（grep 用）

| 文案前缀 | 含义 | 位置 |
|---|---|---|
| `config_loaded` | 启动时打印脱敏 config | `logging_utils.log_config_summary` |
| `http_request` | 每个 HTTP 请求一行 | `middlewares.request_log_middleware` |
| `submit_task_request_received` | 包含请求 body 预览（4000 字符截断） | `routes_tasks` |
| `submit_task_invalid_request` | Pydantic 校验失败的请求详情 | `app.py` |
| `task_submitted` / `task_claimed` / `task_listed` / `task_fetched` | TaskService 的业务事件 | `domain/task/service.py` |
| `worker_started` / `worker_tick` / `worker_shutdown_requested` | Worker 主循环 | `worker/runner.py` |
| `worker_dify_invoke_begin` | 开始调 Dify | `worker/executor.py` |
| `worker_task_succeeded` / `worker_task_failed` | 任务终态 | `worker/executor.py` |
| `dify_run_workflow_start` / `dify_workflow_stream_post` / `dify_inputs_ready` | Dify 协议层 | `infrastructure/dify/client.py` |
| `task_requeued_stale_processing` | 卡死回收（手动触发时） | `domain/task/service.py` |

## "我提交了任务但前端总是 202" 排错路径

1. 看 API 日志，确认 `submit_task_request_body` 拿到正确手机号 → 接口侧正常。
2. 看 Worker 日志：
   - 完全没有 `worker_started`：Worker 进程没起来。
   - 一直 `DEBUG worker_tick status=idle`：Worker 跑着但没抢到任务 → MySQL 那行可能没插进去 / 走到了另一个 DB / status 不是 `queued`。
   - 看到 `worker_dify_invoke_begin` 但卡很久 → Dify 在跑（正常 10–15 min），看是否最终 succeeded/failed。
3. 直接查 DB：`SELECT id, status, updated_at, error_message FROM customer_profile_tasks WHERE customer_phonenumber=?`。

## "task_id=X 卡在 processing" 怎么处理

不会自愈。两种修复：

```sql
-- 手工放回去重跑
UPDATE customer_profile_tasks
SET status='queued', updated_at=CURRENT_TIMESTAMP, error_message='manual_requeue'
WHERE id = ?;
```

```python
# 或者临时跑一下保留的回收函数
from src.app.bootstrap import bootstrap
_, _, task_service, *_ = bootstrap()
n = task_service.requeue_stale_processing(timeout_seconds=1800, limit=100)
print(n)
```

## 队列已满 (503 TASK_QUEUE_FULL)

`AppError.details.queued_count >= max_waiting`。
- 短期：把 `TASK_QUEUE_MAX_WAITING` 调大重启 API。
- 中期：Worker 跑不动 → 看 Dify 是否慢/失败，加 `WORKER_CONCURRENCY`。
- **不要**直接 `DELETE` queued 行——会丢用户提交。优先看消费速度。

## 看 Dify 调用是不是慢

`grep "dify_workflow_stream_post\|worker_dify_invoke_begin\|worker_task_succeeded" logs`。每个任务的 begin/succeeded 时间差就是 wall clock 上的 Dify 用时。

## 修改 schema / 契约时

修改顺序很重要：

1. **改 `contracts/*.{yaml,json}`** —— 这是 source of truth。
2. **跑 `customer-profile-check-contracts`** 看基线断言（`validate_contract_baseline`）还过不过。
3. **改 `src/api/schemas.py`、`routes_tasks.py`、`output_parser.py`** 使行为对齐。
4. **写迁移 SQL 加列**（必要时），先在本地跑一次。
5. **`MySQLTaskRepository._has_*_column()` 已经为可选列做了运行时探测**——继续保留这种"老库也能跑"的兼容方式，除非你明确决定要 drop 旧 schema 的支持。

## 性能要点（给后续优化的参考）

- 没有连接池。每次 Repository 方法都 connect / close 一次。MySQL connector 自带的 keepalive 不在用。如果跑到 50+ tps，考虑加 connection pool。
- 没有缓存。`/v1/.../tasks/{phone}/result` 是热点接口，每次都打 DB。前端 polling 间隔 ≥ 2s + DB 命中 unique index，**目前不是瓶颈**。
- Worker 单进程多线程，受 GIL 影响。但本进程的瓶颈是 Dify 阻塞 I/O，所以多线程是合理的——不要换成多进程，因为那样要管 connection、文件 lock、信号处理。
- `output` 列是 `JSON`，`SELECT` 都是整列返回——如果未来 output 变大且热点查询不需要它，考虑拆个 detail 表或在 `list_by_prefix` 里不 `SELECT output`。

## 安全提醒

- **没有鉴权**。如果暴露到公网，请在 nginx/网关层加 IP 白名单或 Bearer token。
- **`error_message` 会暴露 Dify 响应体片段**（最长 800 字符，`DifyClient` 的 HTTPStatusError 分支）。如果 Dify 错误体里有敏感信息，会落进 DB 并被结果接口回吐给前端。审计期间需要注意。
- **`CORS_ALLOW_ORIGINS=*`** 仅适合开发；生产应该收紧。
- **Dify API key 在日志里被脱敏**（前 4 字符 + 长度），不会泄露。但 `dify_full_request_payload` INFO 日志会打整个 payload（含 phone、batch_id），如果手机号本身是敏感信息，**生产建议把 `LOG_LEVEL` 调到 WARNING** 或改这行日志。
