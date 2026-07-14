# 客户画像任务接口调用指南（前端）

本文面向前端开发，说明四个对外 HTTP 接口的调用方式、请求/响应字段、状态流转和轮询建议。

## 1. 接口总览

| 用途 | 方法 | 路径 |
|---|---|---|
| 健康检查 | `GET` | `/health` |
| 提交任务 | `POST` | `/v1/customer-profile/tasks` |
| 按手机号前缀查询任务列表 | `GET` | `/v1/customer-profile/tasks` |
| 查询单任务结果 | `GET` | `/v1/customer-profile/tasks/{customer_phonenumber}/result` |

> 后端为异步处理：提交后不会等待 Dify 完成，前端应轮询结果接口。
>
> 服务如何启动（本地 / Docker / Worker）见：**[部署与运行指南](deploy-and-run.md)**。

## 2. 通用约定

- Base URL：按环境配置，例如 `https://api.example.com`
- CORS：默认放开（`CORS_ALLOW_ORIGINS=*`），由服务端 `CORS_ALLOW_ORIGINS` 控制
- 手机号字段校验：仅允许数字和 `+`，不允许空格、`-` 等字符（前端建议在提交前先去空格）
- **任务类型**（`task_type`）：当前支持 `customer_profile` / `invitation_strategy`，所有接口均**可选**，缺省视为 `customer_profile`。
  - 两类任务输入/输出字段完全一致，区别仅在后端选择的 Dify 工作流不同
  - 同一手机号可以同时拥有两类任务，互不覆盖
- 任务业务状态：
  - `queued`：已入队
  - `processing`：处理中（Worker 已认领）
  - `succeeded`：成功
  - `failed`：失败
- 顶层错误响应统一结构：
  ```json
  {
    "code": "INVALID_ARGUMENT",
    "message": "invalid request",
    "details": { "...": "..." }
  }
  ```
  `details` 字段可能为 `null` 或 Pydantic 校验错误数组等附加信息。

---

## 3. 健康检查（GET `/health`）

- 方法：`GET`
- 路径：`/health`

成功响应（200）：

```json
{ "status": "ok", "db_status": "up" }
```

`db_status` 取值 `up` / `down`，对应后端到 MySQL 的 ping 结果。可用于前端环境探活。

---

## 4. 提交任务（POST `/v1/customer-profile/tasks`）

### 请求

- 方法：`POST`
- 路径：`/v1/customer-profile/tasks`
- Content-Type：**`multipart/form-data`**（注意：当前实现接收的是表单字段，不是 JSON Body）

表单字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `customer_phonenumber` | 是 | 手机号；仅数字和 `+`，长度 1–32 |
| `task_type` | 否 | 任务类型，取值 `customer_profile` / `invitation_strategy`。默认 `customer_profile`；非法值返回 400 |
| `work_env` | 否 | 工作流环境，取值 `test` / `production`。默认为 `test`；传 `develop` 会被规范化为 `test`；其它非法值返回 400 |
| `batch_id` | 否 | 客户端批次号，最长 64 字符；仅允许字母、数字、`-`、`_`；空字符串视为未传 |

> 当前 HTTP 接口**不再**接收 `wechat_homepage_image` / `wechat_moments_image` 字段。图片如有需求请按服务端约定的旁路方式提供（详见后端文档），本接口仅负责入队。

### 成功响应（202 Accepted）

```json
{
  “code”: “TASK_ACCEPTED”,
  “message”: “task accepted”,
  “customer_phonenumber”: “13129509529”,
  “status”: “queued”,
  “task_type”: “customer_profile”
}
```

> 同一 (`customer_phonenumber`, `task_type`) 组合重复提交会**复用**已有任务行（upsert 重新置为 `queued`），并不会新增记录，也不需要单独的”重试”接口；想重跑直接再提交一次即可。
>
> 不同 `task_type` 之间互不影响：同手机号可以同时存在 `customer_profile` 与 `invitation_strategy` 两条任务。

### 失败响应

| HTTP 码 | `code` | 含义 |
|---|---|---|
| 400 | `INVALID_ARGUMENT` | 手机号 / `task_type` / `work_env` / `batch_id` 格式非法，或缺少必填字段。`details` 为 Pydantic 校验错误数组 |
| 503 | `TASK_QUEUE_FULL` | 队列等待数已达 `TASK_QUEUE_MAX_WAITING`（队列两类任务共用计数）。`details` 形如 `{"max_waiting": 500, "queued_count": 500}` |
| 500 | `INTERNAL_ERROR` | 未预期的服务端错误 |

### cURL 示例（multipart 提交）

```bash
# customer_profile（默认）
curl -X POST "https://api.example.com/v1/customer-profile/tasks" \
  -F "customer_phonenumber=13129509529" \
  -F "work_env=test" \
  -F "batch_id=2026Q2-001"

# invitation_strategy
curl -X POST "https://api.example.com/v1/customer-profile/tasks" \
  -F "customer_phonenumber=13129509529" \
  -F "task_type=invitation_strategy" \
  -F "work_env=production"
```

---

## 5. 查询任务列表（GET `/v1/customer-profile/tasks`）

### 请求

- 方法：`GET`
- 路径：`/v1/customer-profile/tasks`
- Query：
  - `phone_prefix`（必填，长度 1–32，仅数字和 `+`）
  - `page`（可选，默认 `1`，最小 `1`）
  - `page_size`（可选，默认 `20`，范围 1–100）
  - `task_type`（可选，取值 `customer_profile` / `invitation_strategy`；缺省不按类型过滤，两类都返回）

示例：

```
GET /v1/customer-profile/tasks?phone_prefix=131&page=1&page_size=20
GET /v1/customer-profile/tasks?phone_prefix=131&task_type=invitation_strategy
```

### 成功响应（200）

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
      "updated_at": "2026-03-23T20:10:00",
      "task_type": "customer_profile"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

字段说明：

- `items[].card_id` / `nickname`：任务尚未成功时为 `null`；成功后会同步写回
- `items[].status`：`queued` / `processing` / `succeeded` / `failed`
- `items[].task_type`：`customer_profile` / `invitation_strategy`
- `total`：满足前缀过滤（及可选的 `task_type` 过滤）的总行数（不受分页限制）

用途建议：

- “任务历史”或“手机号前缀检索”列表页
- 展示状态标签：queued / processing / succeeded / failed

### 失败响应

- 400 `INVALID_ARGUMENT`：`phone_prefix` 缺失或字符非法、分页参数越界
- 500 `INTERNAL_ERROR`：服务异常

---

## 6. 查询单任务结果（GET `/v1/customer-profile/tasks/{customer_phonenumber}/result`）

### 请求

- 方法：`GET`
- 路径：`/v1/customer-profile/tasks/{customer_phonenumber}/result`
- Query：
  - `task_type`（可选，取值 `customer_profile` / `invitation_strategy`；默认 `customer_profile`）

示例：

```
GET /v1/customer-profile/tasks/13129509529/result
GET /v1/customer-profile/tasks/13129509529/result?task_type=invitation_strategy
```

### 响应分支

不同业务状态对应不同 HTTP 状态码，前端可按 HTTP 码直接分流。

#### 6.1 成功（200，`status=succeeded`）

```json
{
  "code": "TASK_SUCCEEDED",
  "message": "task succeeded",
  "customer_phonenumber": "13129509529",
  "status": "succeeded",
  "output": {
    "card_id": "C123",
    "nickname": "Alice"
  },
  "card_id": "C123",
  "nickname": "Alice",
  "updated_at": "2026-03-23T20:22:00",
  "task_type": "customer_profile"
}
```

- `output`：Dify 解析并落库后的完整对象（除 `card_id` / `nickname` 外可能包含 `confidence` / `summary` / `tags` 等扩展字段，详见 `contracts/customer-profile-output.schema.json`）
- 顶层 `card_id` / `nickname` 是冗余的快捷字段，与 `output.card_id` / `output.nickname` 一致

#### 6.2 处理中（202，`status=queued` 或 `processing`）

```json
{
  "code": "TASK_QUEUED",
  "message": "task is still running",
  "customer_phonenumber": "13129509529",
  "status": "queued",
  "updated_at": "2026-03-23T20:10:00",
  "task_type": "customer_profile"
}
```

或

```json
{
  "code": "TASK_PROCESSING",
  "message": "task is still running",
  "customer_phonenumber": "13129509529",
  "status": "processing",
  "updated_at": "2026-03-23T20:12:00",
  "task_type": "customer_profile"
}
```

#### 6.3 失败（422，`status=failed`）

```json
{
  "code": "DIFY_TIMEOUT",
  "message": "task failed",
  "customer_phonenumber": "13129509529",
  "status": "failed",
  "error_message": "DIFY_TIMEOUT: polling workflow result timed out",
  "updated_at": "2026-03-23T20:25:00",
  "task_type": "customer_profile"
}
```

`code` 当前可能的取值：

| `code` | 含义 |
|---|---|
| `DIFY_REQUEST_FAILED` | 调用 Dify HTTP 请求失败 |
| `DIFY_TIMEOUT` | 等待 Dify 输出超时 |
| `DIFY_OUTPUT_INVALID_JSON` | Dify 返回的 output 不是合法 JSON |
| `DIFY_OUTPUT_SCHEMA_MISMATCH` | Dify 返回的 output 不符合 `customer-profile-output.schema.json` |
| `WORKER_UNEXPECTED_ERROR` | Worker 意外异常（兜底） |
| `TASK_FAILED` | 其他失败原因（兜底，`error_message` 无可识别前缀时返回） |

#### 6.4 不存在（404）

```json
{
  "code": "TASK_NOT_FOUND",
  "message": "task for this phone number does not exist",
  "task_type": "customer_profile"
}
```

#### 6.5 路径手机号格式非法 / `task_type` 非法（400）

```json
{
  "code": "INVALID_ARGUMENT",
  "message": "customer_phonenumber format is invalid"
}
```

---

## 7. 前端轮询策略（推荐）

推荐流程：

1. 调用提交接口，拿到 `customer_phonenumber`
2. 立即开始轮询结果接口
3. 轮询间隔建议 2~5 秒
4. 根据 HTTP 码分支：
   - `202` → 继续轮询
   - `200` → 任务成功，停止轮询
   - `422` → 任务失败，停止轮询并按 `code` 做文案分流
   - `404` → 任务不存在，停止轮询
5. 设置总轮询上限（例如 20 分钟）

注意：

- Dify 任务实际耗时可能 10~15 分钟，属于正常
- 前端 HTTP 连接不会一直挂起；是“短请求 + 轮询”模式

---

## 8. Axios 调用示例（multipart 提交 + 轮询）

```typescript
import axios from "axios";

const api = axios.create({
  baseURL: "https://api.example.com",
  timeout: 15000,
  // 关键：让 axios 不要对 4xx/5xx 抛错，方便我们按 HTTP 码分支处理结果接口
  validateStatus: () => true,
});

export async function submitProfileTask(payload: {
  customer_phonenumber: string;
  task_type?: “customer_profile” | “invitation_strategy”;
  work_env?: “test” | “production”;
  batch_id?: string;
}) {
  const form = new FormData();
  form.append(“customer_phonenumber”, payload.customer_phonenumber);
  if (payload.task_type) form.append(“task_type”, payload.task_type);
  if (payload.work_env) form.append(“work_env”, payload.work_env);
  if (payload.batch_id) form.append(“batch_id”, payload.batch_id);
  return api.post(“/v1/customer-profile/tasks”, form);
}

export async function getTaskResult(
  phone: string,
  taskType: “customer_profile” | “invitation_strategy” = “customer_profile”,
) {
  return api.get(
    `/v1/customer-profile/tasks/${encodeURIComponent(phone)}/result`,
    { params: { task_type: taskType } },
  );
}

export async function pollTaskResult(
  phone: string,
  taskType: “customer_profile” | “invitation_strategy” = “customer_profile”,
) {
  const startedAt = Date.now();
  const timeoutMs = 20 * 60 * 1000;

  while (true) {
    const res = await getTaskResult(phone, taskType);
    // 200=成功，422=失败，404=不存在 → 都属于”终态”
    if (res.status === 200 || res.status === 422 || res.status === 404) {
      return res;
    }
    // 202=仍在处理；其它 HTTP 码（如 500）按需要处理
    if (Date.now() - startedAt > timeoutMs) {
      throw new Error(“轮询超时，请稍后重试”);
    }
    await new Promise((r) => setTimeout(r, 3000));
  }
}
```

---

## 9. 联调检查清单

- `GET /health` 返回 `{"status":"ok","db_status":"up"}`
- `POST /v1/customer-profile/tasks`（multipart 表单）不传 `task_type` 默认入 `customer_profile`：返回 `202` + `code=TASK_ACCEPTED` + `task_type=customer_profile`
- `POST /v1/customer-profile/tasks` 带 `task_type=invitation_strategy` 返回 `202` + `task_type=invitation_strategy`
- 同一手机号同时提交 `customer_profile` 与 `invitation_strategy`，两类任务互不覆盖（数据库里有两条独立记录）
- 同一手机号 + 同一 `task_type` 再次提交不会报错，仍返回 `202`
- 同手机号 `GET .../result` 先返回 `202`（`TASK_QUEUED` 或 `TASK_PROCESSING`）
- 任务完成后 `GET .../result` 返回 `200`（`TASK_SUCCEEDED`）或 `422`（失败 + 对应错误码）
- `GET .../result?task_type=invitation_strategy` 与缺省值返回的是不同任务的结果
- 提交非法手机号 / 非法 `task_type` 返回 `400 INVALID_ARGUMENT`
- 队列满载返回 `503 TASK_QUEUE_FULL`（两类任务共用同一计数）
- 列表接口 `GET /v1/customer-profile/tasks?phone_prefix=...&task_type=...` 按类型过滤生效；缺省 `task_type` 时两类都返回
- 失败时前端按 `code` 做文案分流
- 页面刷新后可通过手机号 + `task_type` 再次拉取任务结果
