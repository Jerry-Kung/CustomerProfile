# 02 · 架构与调用流

## 进程拓扑

```
┌────────────────────────────────┐         ┌──────────────────────────────┐
│  api (FastAPI / uvicorn)       │         │  worker (threading loop)     │
│  customer-profile-api          │         │  customer-profile-worker     │
│                                │         │                              │
│  POST  /v1/.../tasks (202)     │         │  loop {                      │
│  GET   /v1/.../tasks           │         │    claim queued row          │
│  GET   /v1/.../tasks/{p}/result│         │    call Dify (10-15 min)     │
│  GET   /health                 │         │    parse output JSON         │
└──────────────┬─────────────────┘         │    mark succeeded/failed     │
               │                           │  }                           │
               │ writes/reads              └──────────────┬───────────────┘
               │ customer_profile_tasks                   │ writes
               ▼                                          ▼
        ┌──────────────────────────────────────────────────┐
        │   MySQL  ── 业务库 + 任务队列（同一张表）       │
        └──────────────────────────────────────────────────┘
                                                          ▲
                                                          │ HTTP (workflow run / poll)
                                                          ▼
                                                  ┌──────────────────┐
                                                  │  Dify workflow   │
                                                  └──────────────────┘
```

**两个进程使用同一份代码、同一个 `bootstrap()`**（`src/app/bootstrap.py`），通过不同入口脚本启动：

| 入口（pyproject.toml `[project.scripts]`）| 内部入口函数 | 干什么 |
|---|---|---|
| `customer-profile-api` | `src.app.run_api:main` | uvicorn 启动 `src.app.asgi:app` |
| `customer-profile-worker` | `src.app.run_worker:main` | 构造 `WorkerRunner` 并 `run_forever()` |
| `customer-profile-migrate` | `src.app.run_migration:main` | 执行 `db/migrations/001_*.sql`（仅 001） |
| `customer-profile-check-contracts` | `src.contracts.validate_contracts:main` | 校验 OpenAPI/JSON Schema 基线 |

Docker Compose（`docker-compose.yml`）把这两个进程做成两个容器，**共享一个名为 `task_uploads` 的 volume** 用于 `TASK_UPLOAD_DIR`——即 API 写文件、Worker 读文件这条路径。容器内统一监听 **18008**，外部由 `HOST_API_PORT`（默认 18008）映射。

## 分层

| 层 | 目录 | 允许依赖 | 禁止 |
|---|---|---|---|
| API | `src/api/` | `domain/` | 直接用 MySQL/Dify；直接读 env |
| Application | `src/app/` | 所有层 | — |
| Domain | `src/domain/` | 纯 Python | 任何 I/O（DB、HTTP、文件） |
| Infrastructure | `src/infrastructure/` | `domain/` + 外部 SDK | 让 domain 知道自己的存在 |
| Worker | `src/worker/` | `domain/` + `infrastructure/` | — |
| Config | `src/config/` | — | — |
| Contracts | `src/contracts/` + `contracts/` | — | — |

**Domain 通过 `Protocol` 反转依赖**：`src/domain/task/repository.py:TaskRepository` 是一个 typing.Protocol，`src/infrastructure/db/task_repository.py:MySQLTaskRepository` 是它的唯一实现。`TaskService` 只持有 `Protocol`，不知道有 MySQL。

**领域错误不能逃出 API 层**：`TaskQueueFullError` 在 `src/api/routes_tasks.py:85-91` 被翻译成 `AppError(code=TASK_QUEUE_FULL, status_code=503)`，全局 `_app_error_handler` 序列化为 JSON。Dify 错误同理——Worker 把它们写到 `error_message`，结果接口读出来再翻译回 `code`。

## 提交任务的调用链（写路径）

```
FastAPI: POST /v1/customer-profile/tasks
  ├─ middleware: request_log_middleware  (缓存 raw_body 以便后续日志)
  ├─ Form 解析: customer_phonenumber / work_env / batch_id
  ├─ Pydantic 校验:
  │     SubmitTaskPhoneForm     (数字与 + 字符；最大 32)
  │     SubmitTaskWorkEnvForm   ({test, production}; develop→test)
  │     SubmitTaskBatchIdForm   ([A-Za-z0-9_-]{0,64})
  ├─ TaskService.submit_task(phone, work_env, batch_id)
  │     └─ MySQLTaskRepository.upsert_queued(...)
  │           ├─ GET_LOCK('cp_task_queue_admit', 10)     ← 进程间互斥
  │           ├─ START TRANSACTION
  │           ├─ SELECT ... FOR UPDATE                   ← 行锁
  │           ├─ if not 已经是 queued 且 queued 总数 >= TASK_QUEUE_MAX_WAITING:
  │           │      raise TaskQueueFullError
  │           ├─ INSERT ... ON DUPLICATE KEY UPDATE
  │           │      (清空 output/card_id/nickname/error_message; status='queued')
  │           ├─ COMMIT
  │           └─ SELECT 反查整行返回
  └─ 返回 202 {code, message, customer_phonenumber, status}
```

要点：

- `GET_LOCK` + `count_queued()` 是为了在并发提交时**防止队列超过 `TASK_QUEUE_MAX_WAITING`**——但**已经是 `queued` 的同一手机号免计入**，所以即使队列满了，老用户也能 re-trigger。
- `ON DUPLICATE KEY UPDATE` 是 upsert 的物理实现。Schema 上 `customer_phonenumber` 有 unique key。

## Worker 消费的调用链（读+写路径）

```
WorkerRunner._loop()   (concurrency 个线程并行跑)
  └─ WorkerExecutor.execute_once()
        ├─ TaskService.claim_next_task()
        │   └─ MySQLTaskRepository.claim_next_queued()
        │         ├─ START TRANSACTION
        │         ├─ SELECT id FROM ... WHERE status='queued'
        │         │   ORDER BY updated_at ASC, id ASC
        │         │   LIMIT 1 FOR UPDATE SKIP LOCKED      ← 关键
        │         ├─ UPDATE SET status='processing'
        │         └─ COMMIT, 返回 TaskRecord(status='processing')
        ├─ 若 idle 返回 "idle"  (DEBUG 级日志, 不刷屏)
        ├─ DifyClient.run_workflow(...)
        │     ├─ 尝试 streaming 模式 POST run_url → 取 workflow_run_id
        │     ├─ 失败时退化为 blocking 模式 POST → 取 workflow_run_id
        │     └─ GET base_url/v1/workflows/run/{run_id} 轮询，直到
        │        status ∈ {succeeded, failed, stopped}
        ├─ extract_output_string(response)   ← 在 envelope 的多种位置找
        ├─ DifyOutputParser.parse(raw)
        │     ├─ json.loads  → DifyOutputInvalidJson
        │     ├─ normalize_profile_output_dict(把 customer.nickname 提升到根)
        │     ├─ 必填 card_id / nickname  → DifyOutputSchemaMismatch
        │     └─ 返回 ParsedOutput(output, card_id, nickname)
        └─ repository.mark_succeeded(...) / mark_failed(...)
```

要点：

- **`SKIP LOCKED` 让多线程/多进程并发抢占同一队列零冲突**。`WORKER_CONCURRENCY > 1` 即在同一个 worker 进程里跑多个线程；在多机部署时同一张表也能直接共享。
- **Dify 调用并非纯 streaming**：先尝试 SSE，拿不到 `workflow_run_id` 再 fallback 到 blocking 模式 POST。然后**统一靠轮询** `GET /v1/workflows/run/{id}` 拿终态，因为 blocking POST 可能要等十几分钟才返回。
- **错误处理**：`DifyTimeout / DifyRequestFailed / DifyOutputInvalidJson / DifyOutputSchemaMismatch` 都写入 `error_message`（格式 `"<CODE>: <detail>"`，最长 800 字符），状态置 `failed`；其它 `DifyError` 子类一并按 failed 处理。

## 查询路径（结果接口）

```
GET /v1/.../tasks/{phone}/result
  ├─ 路径变量手工校验 (digits + '+')
  ├─ TaskService.get_task_by_phone(phone)
  │     └─ SELECT ... FROM customer_profile_tasks WHERE customer_phonenumber=?
  └─ 按 status 分支:
        queued / processing → 202 {code: TASK_QUEUED|TASK_PROCESSING, ...}
        failed              → 422 {code: <从 error_message 还原的错误码>, error_message, ...}
        succeeded           → 200 {code: TASK_SUCCEEDED, output, card_id, nickname, ...}
        None                → 404 {code: TASK_NOT_FOUND}
```

`_extract_error_code()` (`src/api/routes_tasks.py:180`) 把 `error_message` 的前缀匹配回稳定错误码集合 `{DIFY_REQUEST_FAILED, DIFY_TIMEOUT, DIFY_OUTPUT_INVALID_JSON, DIFY_OUTPUT_SCHEMA_MISMATCH}`，识别不出就回 `TASK_FAILED`。
