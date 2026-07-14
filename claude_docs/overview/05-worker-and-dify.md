# 05 · Worker 与 Dify 集成

## Worker 主循环

文件：`src/worker/runner.py`、`src/worker/executor.py`。

```python
WorkerRunner(executor, interval_seconds, concurrency=N)
└── 启动 N 个 daemon=False 的线程，每个线程独立跑：
    while not stop_event.is_set():
        executor.execute_once()       # 抢一个任务、跑完、写回
        stop_event.wait(interval_seconds)
```

- **并发**：`WORKER_CONCURRENCY`（默认 5）个线程共用同一个 `MySQLClient`，每次操作都新开/关连接，无连接池。
- **节流**：`WORKER_POLL_INTERVAL_MS / 1000.0` 秒（默认 2s）。
- **空跑日志**：`status == "idle"` 走 DEBUG，否则 INFO。线上把 `LOG_LEVEL=INFO` 就不会有 `worker_tick status=idle` 刷屏。
- **优雅停止**：`SIGINT/KeyboardInterrupt` → `stop_event.set()` → 每个线程 join 最长 30s。
- **失败不重试**：Worker 把异常写到 `error_message` 后**不再回滚状态**——失败行直接置 `failed`，靠用户重新 submit 触发新一轮。

## 处理一个任务（`WorkerExecutor.execute_once()`）

```
claim_next_task()  →  TaskRecord | None
  None ⇒ return "idle"

try:
    response = DifyClient.run_workflow(phone, work_env, batch_id)
    raw     = extract_output_string(response)
    parsed  = DifyOutputParser.parse(raw)   # {card_id, nickname, ...}
    mark_succeeded(id, output=parsed.output, card_id=..., nickname=...)
    return "succeeded"
except DifyTimeout | DifyRequestFailed | DifyOutputInvalidJson | DifyOutputSchemaMismatch as exc:
    mark_failed(id, error_message=f"{exc.code}: {exc}")
    return "failed"
except DifyError as exc:                    # 任何其他 DifyError 子类
    mark_failed(id, error_message=...)
    return "failed"
```

**重要：错误必须是 `DifyError` 子类才会被吞**。其它异常（如 DB 异常）会冒出来杀死那一轮 `execute_once()`，但**任务行已被置 processing**——没有自动回滚，会卡在 processing 直到有人手动调 `requeue_stale_processing()`。这是一个目前已知的薄弱点。

## DifyClient — 调用协议

文件：`src/infrastructure/dify/client.py`。

### 一次工作流调用包含三个阶段

```
1. 启动工作流 (取得 workflow_run_id)
    ├─ 先 SSE: POST run_url + response_mode=streaming
    │   └─ httpx.Client().stream(...).iter_lines() → 找包含 workflow_run_id 的 data: 帧
    ├─ 拿不到 → fallback: POST + response_mode=blocking
    │   └─ JSON body 里找 workflow_run_id / run_id / task_id
    └─ 还拿不到 → 兼容老 workflow: 把 phone_number 复制成 customer_phonenumber 再 blocking 一次

2. 轮询结果
    └─ GET {base_url}/v1/workflows/run/{workflow_run_id}
       间隔 = DIFY_POLL_INTERVAL_MS（默认 2s）
       上限 = DIFY_POLL_TIMEOUT_MS（默认 40 分钟）
       直到 status ∈ {succeeded, failed, stopped} 或超时

3. 解析 envelope
    └─ extract_output_string(response):
       在 response.output / response.data.output / response.data.outputs.output /
       response.outputs.output 里找第一个非空字符串。
```

### 鉴权与多环境

- 每次提交带 `work_env`，进 Dify 时由 `_pick_api_key(work_env)` 选 `DIFY_API_KEY_TEST` 或 `DIFY_API_KEY_PRODUCTION`。
- 兼容旧 env：如果只设了 `DIFY_API_KEY`，会被同时映射成 `DIFY_API_KEY_TEST`（`settings.py:119-120`）。

### 输入 payload

```python
inputs = {
  "phone_number": phone,
  "customer_phonenumber": phone,   # 兼容两种命名的 workflow
  "batch_id": batch_id or "",
}
```

`_resolve_image_inputs()` 当前**只是恒等映射**——即图片字段没接上 Dify。

### 超时矩阵

| 阶段 | 超时项 | 默认 |
|---|---|---|
| SSE 首字节 | `DIFY_STREAM_INIT_TIMEOUT_MS` | 30 s |
| Blocking start POST | 由 `DIFY_POLL_TIMEOUT_MS` 驱动 | ≤ 40 min |
| Poll GET | 单次 read 120 s | — |
| 上传文件 read/write | `min(600s, max(120s, poll_timeout))` | — |
| TCP connect / write | 20 s / 120 s | 硬编码 |

`httpx.Client(timeout=None)`——客户端不设默认超时，每个调用都显式传 `httpx.Timeout(...)`。改超时**不要**碰 Client，去改每次调用上的 `httpx.Timeout`。

## DifyOutputParser — 输出契约

文件：`src/infrastructure/dify/output_parser.py`。

权威 schema：`contracts/customer-profile-output.schema.json`。
当前只用 schema 的 `required` 字段（`{card_id, nickname}`）做 minimal 验证 —— **没有走完整 jsonschema 校验**。

流程：

1. `json.loads(output_raw)` 失败 → `DifyOutputInvalidJson`
2. 不是 dict → `DifyOutputSchemaMismatch("output root must be object")`
3. `normalize_profile_output_dict()`：如果根级 `nickname` 缺失/为空但 `customer.nickname` 有值，**把它提升到根**。这是为了兼容某些 workflow 把 nickname 嵌在 `customer` 子对象里的情况——必须保留这一行为，否则解析会失败。
4. 检查 `card_id` / `nickname` 是非空字符串。

通过后返回 `ParsedOutput(output=parsed_dict, card_id, nickname)`；`output` 包含**全部字段**（含 `summary / tags / confidence` 等），将整段写入 `output` JSON 列。

## 错误码 → `error_message` 写库

`WorkerExecutor` 写库格式：

```
"<exc.code>: <str(exc)>"
```

其中 `exc.code` 是 `DifyError.code` 类属性，分别为：
- `DIFY_REQUEST_FAILED`
- `DIFY_TIMEOUT`
- `DIFY_OUTPUT_INVALID_JSON`
- `DIFY_OUTPUT_SCHEMA_MISMATCH`

读出来由 `routes_tasks._extract_error_code()` 做**前缀字符串匹配**还原成 `code`。**新增错误码必须在两端同时更新**——否则前端拿到的就是兜底的 `TASK_FAILED`。
