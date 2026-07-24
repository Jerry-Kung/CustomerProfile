# 部署与运行指南（MVP）

本文说明如何以**微服务方式**启动 Customer Profile 后端：HTTP API 与异步 Worker 分离部署，数据库与 Dify 使用外部环境变量配置。

## 1. 前置条件

- Python **3.11+**（本地运行）或 **Docker / Docker Compose**（容器运行）
- 可访问的 **MySQL 8.x**，并已创建业务库（如 `customer_profile`）
- 可访问的 **Dify**（Worker 消费任务时需要）
- 数据库迁移：**docker compose 启动时由独立的 `migrate` 容器自动执行**（见 §4 / §5）；本地裸跑可手动执行 `customer-profile-migrate`。

## 2. 环境变量

复制并编辑项目根目录下的 `.env`：

```bash
cp .env.example .env
```

启动 API / Worker / uvicorn 时，`load_config()` 会**自动读取项目根目录的 `.env`**（依赖 `python-dotenv`）。若已在操作系统里 `set` / `export` 了同名变量，则以系统环境为准（不覆盖）。

若未安装依赖，请先执行：`pip install -e .` 或 `pip install python-dotenv`。

必填项见 `.env.example`：`DIFY_*`、`MYSQL_*`。可选：

| 变量 | 说明 | 默认 |
|------|------|------|
| `API_HOST` | API 监听地址 | `0.0.0.0` |
| `API_PORT` | API 监听端口 | `8000` |
| `APP_ENV` | 环境标识 | `local` |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `HTTP_LOG_LEVEL` | httpx/httpcore 日志级别 | `WARNING` |
| `LOG_DIR` | Loguru 文件日志目录 | `logs` |
| `LOG_ROTATION` | 文件轮转策略 | `00:00` |
| `LOG_RETENTION` | 文件保留时间 | `30 days` |
| `WORKER_CONCURRENCY` | Worker 并发线程数 | `8` |
| `WORKFLOW_PLATFORM` | 工作流平台选择：`dify` 或 `qingtian` | `dify` |

> Dify API key 说明：
> - `DIFY_API_KEY_TEST` / `DIFY_API_KEY_PRODUCTION`：`customer_profile` 任务按 `work_env` 切换使用（旧的 `DIFY_API_KEY` 作为兜底）。
> - `DIFY_INVITATION_STRATEGY_API_KEY`：`invitation_strategy` 任务专用，不分 test/production（`work_env` 仍透传给 Dify，由工作流内部处理）。

### 2.1 工作流平台切换（Dify ↔ QingTian）

Worker 在调用工作流时通过 `WORKFLOW_PLATFORM` 决定走哪条适配器：

- `WORKFLOW_PLATFORM=dify`（**默认**）— 走原有 Dify 工作流，使用全部 `DIFY_*` 变量。QingTian 变量不读取。
- `WORKFLOW_PLATFORM=qingtian` — 走 DFMC 自研 QingTian 平台，必填项：

| 变量 | 说明 |
|------|------|
| `QINGTIAN_BASE_URL` | 包含 `/api/proxy/api/v1` 前缀，client 会附加 `/run_app_workflow` 与 `/query_run_app_process` |
| `QINGTIAN_API_KEY_TEST` | `customer_profile` 任务、`work_env=test` 时使用 |
| `QINGTIAN_API_KEY_PRODUCTION` | `customer_profile` 任务、`work_env=production` 时使用 |
| `QINGTIAN_INVITATION_STRATEGY_API_KEY` | `invitation_strategy` 任务专用 |
| `QINGTIAN_USER_ID` | 透传到 QingTian 请求的 `UserID` 字段 |
| `QINGTIAN_POLL_INTERVAL_MS` / `QINGTIAN_POLL_TIMEOUT_MS` | 轮询参数，默认与 Dify 一致（2000ms / 2400000ms） |

QingTian 的 API Key 选择规则与 Dify 完全一致：`task_type=invitation_strategy` 优先于 `work_env`。InputData 字段名也与 Dify 现有 inputs 一致（`phone_number / batch_id / work_env`）。

启动时 `config_loaded` 日志会输出 `workflow_platform` 字段，便于核对当前生效平台；同时 bootstrap 会打印 `workflow_client_selected platform=...`。失败任务在 `tasks.error_message` 中保留平台前缀（`DIFY_*` 或 `QINGTIAN_*`），可据此排查归属平台。

回滚到 Dify：将 `.env` 中 `WORKFLOW_PLATFORM` 改回 `dify`（或删除该行）并重启 API + Worker。QingTian 变量空着不会影响 Dify 路径。

### 2.2 擎天双工作流积压分流

擎天模式支持两个使用同一 MySQL 队列的 Worker：

- 本地模型 Worker：`WORKER_ROLE=local`，始终领取普通新任务。
- 商用模型 Worker：`WORKER_ROLE=commercial`，排队数达到阈值时参与领取普通任务。

Docker Compose 会默认启动这两个 Worker。擎天模式下，必须先在 `.env` 填写商用
客户画像工作流的 TEST/PRODUCTION 两个 `QINGTIAN_COMMERCIAL_API_KEY_*`；邀请策略
统一使用 `QINGTIAN_INVITATION_STRATEGY_API_KEY`。商用 Worker 不会回退使用本地客户
画像工作流的 Key。商用 Worker 在排队数达到阈值时领取普通任务；它还会无视该阈值
领取明确分配给 `commercial` 的 `retry_wait`，并且只恢复由商用 AppKey 创建的
`runId`。

唯一的分流参数：

```env
COMMERCIAL_QUEUE_THRESHOLD=5
```

当 `queued >= 5` 时商用 Worker 参与普通任务分流，低于 5 时停止领取普通任务。
商用 Worker 没有额外启停开关，Compose 默认启动。两个 Worker 仍通过数据库的
`FOR UPDATE SKIP LOCKED` 互斥领取同一队列中的任务。

### 2.3 擎天 runId 恢复与重试（纯后端能力）

擎天 Worker 默认使用“持久化已有 `runId` 后恢复轮询”。这不修改擎天工作流、
变量或接口：本服务仍调用原有 `/run_app_workflow` 与
`/query_run_app_process`，只是会在收到 `runId` 后先写入 MySQL。

擎天模式默认启用。必须先确认 `005_add_qingtian_retry_fields.sql` 已成功执行，再将
API、本地 Worker 和商用 Worker 一起升级。可调参数只保留轮询恢复边界：

```env
RETRY_MAX_POLL_RECOVERIES=3
RETRY_MAX_RUN_LIFETIME_SECONDS=7200
```

重试退避（30/120/600 秒）、任务租约（180 秒）、租约心跳（30 秒）和过期租约
扫描周期（30 秒）属于内部一致性参数，已固定在代码中，不再通过环境变量配置。

`execution_backend` 会和 `runId` 一起落库：本地 Worker 只领取 `local` 恢复任务，
商用 Worker 只领取 `commercial` 恢复任务，因此不会用一个 AppKey 查询另一个
AppKey 创建的运行。本地第 1 次明确失败后仍走本地；第 2 次明确失败后，任务会固定
转给商用 Worker 执行最后 1 次。新运行次数固定为 3 次（本地 2 次、
商用 1 次），不提供环境变量覆盖。

- 已持久化 `runId` 的轮询网络错误、轮询超时和 Worker 重启会恢复查询同一个运行，
  不会重复提交工作流。
- 对同一个 `runId`，首次轮询之外最多再安排 `RETRY_MAX_POLL_RECOVERIES` 次恢复轮询，
  且从 `runId` 落库起最多观察 `RETRY_MAX_RUN_LIFETIME_SECONDS` 秒。默认是“首次轮询
  + 最多 3 次恢复轮询”、总计 2 小时（以先达到的边界为准）；达到任一边界会标记
  `QINGTIAN_RUN_STALLED` 并保留原 `runId`，不会自动创建新的擎天运行。
- 擎天明确返回工作流终态失败、或成功响应的输出不是合法 JSON 对象时，才在上限内
  创建新的运行。
- 提交请求已发出但没有收到 `runId` 时，系统不会盲目重提；它会最终标记
  `QINGTIAN_SUBMIT_UNKNOWN`，以避免同一任务在擎天执行两次。
- `retry_wait` 是内部状态；现有结果接口仍返回 HTTP 202 和 `processing`，前端无需
  为该状态增加分支。

Worker 在无 `queued` 任务时，默认 **不再** 以 INFO 每 2 秒打印 `worker_tick`（避免刷屏）。需要观察轮询心跳时，可将 `LOG_LEVEL` 设为 `DEBUG`。

## 3. 本地运行（开发 / 联调）

### 3.1 安装依赖

```bash
cd customer_profile_backend
pip install -e ".[dev]"
```

### 3.2 启动 API

```bash
# 方式一：入口脚本（读取 API_HOST / API_PORT）
set API_HOST=0.0.0.0
set API_PORT=8000
customer-profile-api
```

Linux / macOS：

```bash
export API_HOST=0.0.0.0
export API_PORT=8000
customer-profile-api
```

### 3.3 启动 Worker（处理队列、调用 Dify）

另开终端，相同 `.env`：

```bash
customer-profile-worker
```

### 3.4 使用 uvicorn 直接启动（与容器一致）

```bash
uvicorn src.app.asgi:app --host 0.0.0.0 --port 8000
```

健康检查：`GET http://127.0.0.1:8000/health`

## 4. Docker Compose（推荐 MVP 部署）

**Ubuntu 24.04 上从零安装 Docker、改端口、UFW 放通、前端 Base URL**：见 **[docker-deploy-ubuntu24.md](docker-deploy-ubuntu24.md)**。

在项目根目录：

```bash
# 准备 .env（含 MYSQL_ / DIFY_ 等）
docker compose build
docker compose up -d
```

- **api**：对外暴露 HTTP，默认映射本机 `8000` → 容器内 `8000`。
- **worker**：本地 Worker，后台轮询 MySQL 队列并调用工作流。
- **commercial-worker**：商用 Worker，Compose 默认启动；排队数达到阈值时领取普通任务，并处理明确分配给商用的兜底/恢复任务。
- **migrate**：一次性容器，启动时扫 `db/migrations/*.sql` 并按文件名顺序全部执行（所有 SQL 已实现幂等，可放心重复执行）。`api` 和两个 Worker 通过 `depends_on: service_completed_successfully` 等它退出成功后再启动；任一迁移失败都会阻塞业务进程启动。MySQL 尚未就绪时 `migrate` 会自动重试（次数与间隔由 `MIGRATION_DB_WAIT_MAX_TRIES` / `MIGRATION_DB_WAIT_INTERVAL_MS` 控制，默认 30 次 × 2s）。

修改本机映射端口（例如改为 8080）：

```bash
# Windows CMD
set HOST_API_PORT=8080
docker compose up -d
```

```bash
# Linux / macOS
export HOST_API_PORT=8080
docker compose up -d
```

查看日志：

```bash
docker compose logs -f api
docker compose logs -f worker
docker compose logs -f commercial-worker
```

同一份 Loguru 日志也会按服务写入宿主机 `./logs/`。文件名包含组件、Pod 名（非 K8s 环境为 `local`）和日期；stderr 与文件使用相同格式。

停止：

```bash
docker compose down
```

## 5. 数据库迁移

**Docker Compose 部署下无需手动操作**：`docker compose up` 会先跑独立的 `migrate` 容器，扫描 `db/migrations/*.sql` 并按文件名顺序全部应用，成功退出后 `api` 和两个 Worker 才会启动。所有迁移文件都使用 `CREATE TABLE IF NOT EXISTS` 或 `information_schema` 检查实现幂等，每次启动重复执行都是安全的。

如需单独触发（例如只想做迁移、不起业务进程）：

```bash
docker compose run --rm migrate
```

**本地裸跑**（已配好 `.env` 中的 `MYSQL_*`）：

```bash
customer-profile-migrate
```

可调参数：

| 变量 | 说明 | 默认 |
|------|------|------|
| `MIGRATION_DB_WAIT_MAX_TRIES` | MySQL 不可达时的最大重试次数 | `30` |
| `MIGRATION_DB_WAIT_INTERVAL_MS` | 重试间隔（毫秒） | `2000` |

> ⚠️ `003_add_task_type.sql` 会先 `DROP` 旧的 `uk_customer_profile_tasks_phone` 唯一键，再加上新的 `uk_customer_profile_tasks_phone_type (customer_phonenumber, task_type)`。MySQL DDL 不支持事务，期间存在短暂窗口可能允许同一手机号重复入库。**对在线升级建议在低峰期、API 短暂下线后执行**；首次全新部署不受此影响。
>
> 若代码已上线但 `003` 因某种原因未执行成功，新版 API 会在 `bootstrap` 日志里输出 `task_type_column_missing` 警告，且 `invitation_strategy` 提交会被错误地按 `customer_profile` 处理。
>
> ⚠️ `004_add_work_env_and_batch_id.sql` 补齐了 `work_env` / `batch_id` 两列。在该迁移执行前，repository 在读取时会回退到字面量 `'test'`、写入时直接丢弃这两个字段，**导致所有 `work_env=production` 的提交都被静默打到测试 Dify 工作流**。新部署或 docker compose 启动会自动应用；已经存在的环境需要确认 `customer_profile_tasks` 表里有这两列，缺失则手动执行一次 `customer-profile-migrate`。
>
> ⚠️ `005_add_qingtian_retry_fields.sql` 增加 `retry_wait` 状态、`workflow_run_id`、同一
> `runId` 的恢复计数/起始时间、租约、generation 和 attempts 审计表。必须先执行迁移，再部署能够识别这些字段的 API/Worker；
> 该迁移还增加执行通道和尝试路由原因。新版擎天 Worker 默认使用这些字段，因此必须
> 严格按“先迁移、后升级两个 Worker”的顺序发布。如需回滚，先让已有 `retry_wait`
> 任务处理完，或按运维流程人工终止；不要删除 `workflow_run_id`
> 或把任务直接改回 `queued`。

## 6. 对外接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查（含 DB ping） |
| POST | `/v1/customer-profile/tasks` | 提交任务（异步） |
| GET | `/v1/customer-profile/tasks` | 按手机号前缀列表 |
| GET | `/v1/customer-profile/tasks/{phone}/result` | 查询单任务结果 |

详细请求体与轮询方式见 `docs/frontend-api-guide.md`。

## 7. 启动后 API 集成测试（curl）

1. 按上文启动 API（Worker 可选；仅测提交/列表/进行中结果时可不启 Worker）。
2. 设置环境变量并运行 pytest：

**Windows CMD：**

```cmd
set RUN_API_HTTP_E2E=1
set API_E2E_BASE_URL=http://127.0.0.1:8000
pytest tests\integration\test_api_http_curl_e2e.py -v -s
```

**Linux / macOS：**

```bash
export RUN_API_HTTP_E2E=1
export API_E2E_BASE_URL=http://127.0.0.1:8000
pytest tests/integration/test_api_http_curl_e2e.py -v -s
```

也可使用 shell 脚本（需已安装 `curl`）：

```bash
chmod +x scripts/api_e2e_curl.sh
API_E2E_BASE_URL=http://127.0.0.1:8000 ./scripts/api_e2e_curl.sh
```

## 8. 常见问题

- **API 启动报缺环境变量**：检查 `.env` 是否与启动命令在同一工作目录，或已 `export` / `set` 全部必填项。
- **Worker 不消费**：确认 MySQL 连通、任务状态为 `queued`、Worker 进程在跑。
- **容器内访问本机文件路径**：提交任务里的 `wechat_*_image` 若为本地路径，须是 **Worker 容器内可读路径**；演示环境建议使用 URL 或与 Worker 挂载相同卷。
