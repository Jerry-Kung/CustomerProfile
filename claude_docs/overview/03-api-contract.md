# 03 · API 契约

权威源：`contracts/customer-profile-api.openapi.yaml`（OpenAPI 3.0.3）。
实现：`src/api/routes_tasks.py` + `src/api/schemas.py`。
**当代码与 OpenAPI 冲突时以代码为准** —— 已知 OpenAPI 说 `application/json`，实际接口是 `multipart/form-data`（见下文）。

## 路由前缀

- API 路由都挂在 `/v1/customer-profile/tasks` 下（`src/api/routes_tasks.py:23`）。
- 健康检查：`GET /health` → `{status: ok, db_status: up|down}`，DB ping 来自 `MySQLClient.ping()`。

## POST /v1/customer-profile/tasks — 提交任务

**请求体**：`multipart/form-data`（不是 JSON，因为预留了文件上传）

| Field | Type | 必填 | 规则 |
|---|---|---|---|
| `customer_phonenumber` | string | ✅ | 仅 `[0-9+]`，长度 1–32 |
| `work_env` | string | – | `test` 或 `production`；缺省 / `develop` → `test` |
| `batch_id` | string | – | `[A-Za-z0-9_-]{0,64}`，空串视为 None |

**注意**：OpenAPI 里写了 `wechat_homepage_image / wechat_moments_image`，但当前 `submit_task` 函数签名不接收这两个字段。前端真的传了会被 FastAPI 忽略。要接通，得改 `routes_tasks.py` 的 `Form(...)` 列表和 `TaskUploadStore.save_optional()` 调用，并把得到的路径塞入 `Dify` `_resolve_image_inputs()`。

**响应**：

| HTTP | code | 何时 |
|---|---|---|
| 202 | `TASK_ACCEPTED` | 入队成功（含 upsert 覆盖已有终态行的情况） |
| 400 | `INVALID_ARGUMENT` | Pydantic 校验失败 |
| 503 | `TASK_QUEUE_FULL` | `queued` 行数已达 `TASK_QUEUE_MAX_WAITING` 且该手机号不是已 queued 状态 |

成功体：
```json
{
  "code": "TASK_ACCEPTED",
  "message": "task accepted",
  "customer_phonenumber": "13129509529",
  "status": "queued"
}
```

队列满体（`AppError.details` 带容量信息）：
```json
{
  "code": "TASK_QUEUE_FULL",
  "message": "task queue is full, please retry later",
  "details": { "max_waiting": 500, "queued_count": 500 }
}
```

## GET /v1/customer-profile/tasks — 列表（按手机号前缀）

**查询参数** (`TaskListQuery`)：

| Param | Type | 默认 | 规则 |
|---|---|---|---|
| `phone_prefix` | string | – | 必填；`[0-9+]`，1–32 |
| `page` | int | 1 | `>=1` |
| `page_size` | int | 20 | 1–100 |

SQL 用 `customer_phonenumber LIKE CONCAT(?, '%')` + `ORDER BY updated_at DESC LIMIT ? OFFSET ?`。
前缀走 `idx_customer_profile_tasks_phone_prefix` 索引（左前缀匹配生效）。

成功体：
```json
{
  "code": "OK",
  "message": "success",
  "items": [
    {
      "customer_phonenumber": "13129509529",
      "card_id": null,
      "nickname": null,
      "status": "processing",
      "updated_at": "2026-05-18T20:10:00"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

## GET /v1/customer-profile/tasks/{phone}/result — 查结果（前端轮询入口）

路径参数 `customer_phonenumber` 在处理函数里**手工校验**（不走 Pydantic）：必须为 `[0-9+]`。

按 `status` 分支返回不同 HTTP 码：

| 内部 status | HTTP | response.code |
|---|---|---|
| 行不存在 | 404 | `TASK_NOT_FOUND` |
| `queued` | 202 | `TASK_QUEUED` |
| `processing` | 202 | `TASK_PROCESSING` |
| `succeeded` | 200 | `TASK_SUCCEEDED` |
| `failed` | 422 | `<由 error_message 前缀解析>` |

成功体：
```json
{
  "code": "TASK_SUCCEEDED",
  "message": "task succeeded",
  "customer_phonenumber": "13129509529",
  "status": "succeeded",
  "output": { "card_id": "C123", "nickname": "Alice", "summary": "...", "tags": [...] },
  "card_id": "C123",
  "nickname": "Alice",
  "updated_at": "2026-05-18T20:22:00"
}
```

失败体（`code` 由 `_extract_error_code()` 从 `error_message` 起始字串匹配出）：
```json
{
  "code": "DIFY_TIMEOUT",
  "message": "task failed",
  "customer_phonenumber": "13129509529",
  "status": "failed",
  "error_message": "DIFY_TIMEOUT: polling workflow result timed out",
  "updated_at": "2026-05-18T20:25:00"
}
```

## 错误码全集

| code | 来自 | HTTP | 说明 |
|---|---|---|---|
| `INVALID_ARGUMENT` | FastAPI/Pydantic 校验失败 | 400 | `details` 带 Pydantic errors |
| `TASK_QUEUE_FULL` | 提交时 `TaskQueueFullError` | 503 | `details: {max_waiting, queued_count}` |
| `TASK_NOT_FOUND` | 结果接口未查到行 | 404 | — |
| `TASK_QUEUED` / `TASK_PROCESSING` | 结果接口（进行中） | 202 | — |
| `TASK_SUCCEEDED` | 结果接口（成功） | 200 | — |
| `TASK_FAILED` | 结果接口（失败，错误信息识别不出来） | 422 | 兜底 |
| `DIFY_REQUEST_FAILED` | Worker：HTTP 错误/网络错误 | 422 | `error_message` 含 `Bearer/Token/响应体片段` |
| `DIFY_TIMEOUT` | Worker：streaming/poll 超时 | 422 | — |
| `DIFY_OUTPUT_INVALID_JSON` | Worker：`output` 不是合法 JSON | 422 | — |
| `DIFY_OUTPUT_SCHEMA_MISMATCH` | Worker：缺 `card_id` 或 `nickname` | 422 | — |

OpenAPI 里还列了 `TASK_ALREADY_RUNNING` / `RATE_LIMITED` / `INTERNAL_ERROR`，**当前代码未触发它们**。

## CORS

`src/api/app.py:32-44`：默认 `CORS_ALLOW_ORIGINS=*`、所有方法/头放行。允许多源时用 `,` 分隔；只要包含 `*` 就走 wildcard 分支（`allow_credentials=False`）。

## 中间件链

仅有 `request_log_middleware`（`src/api/middlewares.py`），职责：
1. 提前 `await request.body()` 缓存到 `request.state.raw_body`，让后续 handler 可以再次读 body（FastAPI 的 form 解析会消费 body）；
2. 记录 `http_request method=... path=... status=... elapsed_ms=...` 日志。
