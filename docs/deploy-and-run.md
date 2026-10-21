# 部署与运行指南（MVP）

本文说明如何以**微服务方式**启动 Customer Profile 后端：HTTP API 与异步 Worker 分离部署，数据库、Dify/擎天与独立画像生成服务通过环境变量配置。

## 1. 前置条件

- Python **3.11+**（本地运行）或 **Docker / Docker Compose**（容器运行）
- 可访问的 **MySQL 8.x**，并已创建业务库（如 `customer_profile`）
- 可访问的 **Dify 或擎天**（由 `WORKFLOW_PLATFORM` 选择）
- 统一 Compose 还需要可访问的画像生成服务，配置 `PROFILE_RUNNER_BASE_URL_PRODUCTION`。
- 数据库迁移：**docker compose 启动时由独立的 `migrate` 容器自动执行**（见 §4 / §5）；本地裸跑可手动执行 `customer-profile-migrate`。

## 2. 环境变量

复制并编辑项目根目录下的 `.env`：

```bash
cp .env.example .env
```

启动 API / Worker / uvicorn 时，`load_config()` 会**自动读取项目根目录的 `.env`**（依赖 `python-dotenv`）。若已在操作系统里 `set` / `export` 了同名变量，则以系统环境为准（不覆盖）。

若未安装依赖，请先执行：`pip install -e .` 或 `pip install python-dotenv`。

必填项见 `.env.example`：`MYSQL_*`，以及所选平台对应的 `DIFY_*` 或
`QINGTIAN_*`；统一 Compose 还需填写 `PROFILE_RUNNER_BASE_URL_PRODUCTION`。可选：

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

### 2.2 可扩展工作流目标与消费模式

Worker 现在把“资源池”和“具体工作流目标”分开：

- `WORKER_ROLE=local|commercial`：表示资源池，用于成本与路由策略。
- `WORKFLOW_TARGET_ID`：表示一套稳定的工作流/AppKey，例如 `local-01`、
  `commercial-01`。同一 AppKey 的多个副本使用同一个 ID；不同 AppKey 必须使用不同
  ID。
- `WORKFLOW_TARGET_REVISION`：目标密钥/配置的非敏感版本，默认 `v1`。
- `WORKER_TARGET_MODE=active|draining|recovery_only`：`active` 可领取未绑定任务；
  另外两种模式只处理已经绑定到本目标的恢复任务。
- `WORKER_TASK_TYPES` / `WORKER_WORK_ENVS`：声明该目标能处理的任务类型和环境。

当前 Docker Compose 统一启动本地、商用和 Runner 三个目标，读取同一份 `.env`：

| 服务 | 目标 ID | 资源池 | 任务类型 |
|------|---------|--------|----------|
| `worker` | `local-default` | `local` | 新邀约；恢复已绑定的旧运行 |
| `commercial-worker` | `commercial-default` | `commercial` | 仅恢复已绑定旧运行（`recovery_only`） |
| `profile-runner-worker` | `profile-runner-production` | `local` | 全部新生产画像及其重试 |

默认 Compose 的 backend API 将新任务固定为 `single_target_v1`，画像失败后继续由 Runner 重试；不会切回旧画像流程。测试画像需要单独配置测试 Runner。已有旧任务保留原策略及目标身份。

商用目标保留原 TEST/PRODUCTION AppKey，用于已有运行恢复：

```env
QINGTIAN_COMMERCIAL_API_KEY_TEST=
QINGTIAN_COMMERCIAL_API_KEY_PRODUCTION=
```

沿用 `local-default` / `commercial-default` 是有意的：006 会把旧版 1+1
环境中尚未完成的运行绑定到这两个 ID，Compose 升级后可以直接恢复原 `runId`。
其他环境可继续增加带独立 AppKey 和唯一目标 ID 的 Worker，不需要修改数据库结构。

普通任务通过 MySQL `FOR UPDATE SKIP LOCKED` 原子领取，任意数量的 Worker
进程/线程都不会重复消费。以下资源池模式仅在显式启用旧画像消费者时使用；默认 Compose 的本地 Worker 只领新邀约，商用 Worker 固定为恢复模式：

```env
WORKER_ROUTING_MODE=parallel
COMMERCIAL_QUEUE_THRESHOLD=5
COMMERCIAL_SPILLOVER_DELAY_SECONDS=600
```

- `parallel`：所有 active 本地/商用目标立即共同消费，适合当前清理大量积压。
- `local_preferred`：本地立即消费；队列深度达到阈值，或最老任务等待时间达到
  `COMMERCIAL_SPILLOVER_DELAY_SECONDS` 后，商用目标参与新任务。
- `local_only`：商用目标不领取普通 `queued` 任务。已经绑定的 `runId` 和明确的重试
  仍会继续处理，避免切换策略时破坏进行中的任务。

商用队列闸门只限制普通新任务，不会阻断已经进入 `retry_wait` 的兜底重试。以后本地
资源恢复后，只需将模式切到 `local_preferred` 并设置合适的等待时间；不需要修改消费
代码或数据库结构。

每个 Worker 进程还有一个目标级轻量熔断器：

```env
WORKER_TARGET_FAILURE_THRESHOLD=3
WORKER_TARGET_COOLDOWN_SECONDS=60
```

连续 3 次目标级 HTTP/传输失败后，该目标暂停领取普通任务和未绑定重试 60 秒，让健康
目标接管；已经绑定的 `runId` 仍只能由原目标恢复，不会跨 AppKey 轮询。成功访问目标
后失败计数立即清零。熔断状态和剩余时间会进入 Worker 周期汇总日志。

### 2.3 目标绑定、runId 恢复与有界重试

擎天 Worker 收到 `runId` 后会先把它和 `WORKFLOW_TARGET_ID` /
`WORKFLOW_TARGET_REVISION` 一起持久化，再开始轮询。一个已存在的 `runId` 只能由
完全相同的目标 ID 和 revision 恢复，绝不会拿另一条商用工作流的 AppKey 去查询。

需要先完成 `005_add_qingtian_retry_fields.sql` 和
`006_add_workflow_target_routing.sql`，再统一升级 API 与所有 Worker。默认策略为
`throughput_first_v1`，新运行总上限 3 次；同一 `runId` 默认最多恢复轮询 3 次、
最长观察 7200 秒。普通部署无需在 `.env` 重复配置这些默认值，也不需要根据目标数量
调整它们。

API 会把 `RETRY_POLICY_VERSION` 固化到每一代任务上。修改策略只影响之后新提交或重新
提交的任务；已有任务继续按原策略完成，避免发布过程中语义漂移。

三类重试策略：

- `throughput_first_v1`：新运行总数最多
  3 次；明确失败后释放目标绑定，尚未尝试过且能力匹配的健康目标先竞争下一次。
  如果没有新目标，短暂优先窗口结束后允许已尝试的健康目标复用，直到达到总次数上限。
  因此 1+0、1+1、1+3 等拓扑都不会仅因目标数量不足而永久停在 `retry_wait`。
- `local_first_v1`：最多本地 2 次、商用 1 次，适合以后本地资源为主的运行方式。
  Helm 的旧版 1+1 兼容模式默认使用此策略。
- `invitation_strategy` 当前只有一条兼容工作流，会自动使用
  `single_target_v1`：在同一个本地目标上有界重试，仍受总运行次数上限约束，不会被
  错误路由到客户画像商用工作流。

轮询恢复和创建新运行是两套独立上限：

- 已持久化 `runId` 的网络错误、轮询超时和 Worker 重启只恢复同一个运行，不增加
  `attempt_count`。
- 同一个 `runId` 在首次轮询之外，最多再安排
  `RETRY_MAX_POLL_RECOVERIES` 次恢复轮询；默认是首次 1 次 + 恢复最多 3 次。
- 从 `runId` 落库起最多观察 `RETRY_MAX_RUN_LIFETIME_SECONDS` 秒，默认 2 小时。
  次数或时长任一先到即标记 `QINGTIAN_RUN_STALLED`，不会无限轮询，也不会因此创建
  新运行。
- 只有擎天明确返回终态失败，或返回结果明确不可用时，才会在新运行次数上限内重试。
- 提交请求结果不确定、没有安全拿到 `runId` 时标记 `QINGTIAN_SUBMIT_UNKNOWN`，
  不会盲目重提造成重复画像。
- 基础退避为 30/120/600 秒，并加入 ±20% 抖动避免大量失败任务同时重试；
  `Retry-After` 只会把等待时间拉长。租约 180 秒、心跳 30 秒、过期租约扫描 30 秒，
  属于内部一致性参数。领取时已经创建完整租约，首次续租会等待一个心跳周期；
  MySQL 返回零变更行时还会校验 generation/claim token，避免把同秒续租误判为丢失。
- `retry_wait` 是内部状态；现有结果接口仍返回 HTTP 202 和 `processing`。

`throughput_first_v1` 的目标去重是软优先，不是会阻塞任务的硬限制：到期重试先给新
目标 5 秒领取窗口，没有新目标时旧目标自动兜底。总尝试次数仍由数据库原子限制为 3，
不会无限重试。旧目标更换 AppKey 时仍不要直接复用 ID/revision：先以
`recovery_only` 保留旧目标恢复存量 `runId`，新 AppKey 使用新的目标 ID。

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
# 准备 .env（含 MYSQL_ / DIFY_ 或 QINGTIAN_，以及 PROFILE_RUNNER_BASE_URL_PRODUCTION）
docker compose up -d --build
```

- **api**：对外暴露 HTTP，默认映射本机 `18008` → 容器内 `18008`。
- **worker**：仅领取新邀约，仍可恢复已绑定到本地目标的旧远程运行。
- **commercial-worker**：固定 `recovery_only`，仅恢复已绑定的旧商用运行。
- **profile-runner-worker**：独立画像生成服务的适配 Worker，只处理生产环境客户画像，默认并发 1；与其他服务统一启动。
- **migrate**：一次性容器，启动时扫 `db/migrations/*.sql` 并按文件名顺序全部执行（所有 SQL 已实现幂等，可放心重复执行）。`api` 和三个 Worker 通过 `depends_on: service_completed_successfully` 等它退出成功后再启动；任一迁移失败都会阻塞业务进程启动。MySQL 尚未就绪时 `migrate` 会自动重试（次数与间隔由 `MIGRATION_DB_WAIT_MAX_TRIES` / `MIGRATION_DB_WAIT_INTERVAL_MS` 控制，默认 30 次 × 2s）。

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
docker compose logs -f profile-runner-worker
```

同一份 Loguru 日志也会按服务写入宿主机 `./logs/`。文件名包含组件、Pod 名（非 K8s 环境为 `local`）和日期；stderr 与文件使用相同格式。

停止：

```bash
docker compose down
```

## 5. 数据库迁移

**Docker Compose 部署下无需手动操作**：`docker compose up` 会先跑独立的 `migrate` 容器，扫描 `db/migrations/*.sql` 并按文件名顺序全部应用，成功退出后 `api` 和三个 Worker 才会启动。所有迁移文件都使用 `CREATE TABLE IF NOT EXISTS` 或 `information_schema` 检查实现幂等，每次启动重复执行都是安全的。

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
> 严格按“先迁移、后升级所有 Worker”的顺序发布。如需回滚，先让已有 `retry_wait`
> 任务处理完，或按运维流程人工终止；不要删除 `workflow_run_id`
> 或把任务直接改回 `queued`。
>
> ⚠️ `006_add_workflow_target_routing.sql` 增加 `execution_pool`、
> `workflow_target_id`、`workflow_target_revision`、`retry_policy_version` 及 attempts
> 审计字段和索引。代码的目标感知领取 SQL 会直接读取这些字段，因此发布顺序必须是：
> **先完成 006，再启动新版 API 和全部目标 Worker**。迁移会把升级时仍在
> `processing` / `retry_wait` 的旧任务绑定到 `local-default` 或
> `commercial-default`，旧版 1+1 Worker 的默认目标 ID 与之匹配；普通 `queued`
> 任务保持未绑定。若升级时直接中断旧 Worker，新版擎天 Worker 会在原轮询超时加一次
> 扫描间隔后，自动把“无租约、无 claim token、无新旧 runId、零尝试”的遗留
> `processing` 任务作为新 generation 重新入队；已有 runId 的任务不会走该路径。

`007_add_profile_failure_outbox.sql` 新建最终失败通知表，可重复执行；先迁移后启用 `PROFILE_FAILURE_CALLBACK_ENABLED=true`。通知配置和状态规则见 [Runner 接入与部署](profile-runner-integration.md#最终失败通知)。

`008_add_invitation_execution_delivery.sql` 给任务表增加 `execution_id`，给现有失败通知表增加 `task_type` 和 `execution_id`，均先检查字段再添加，可重复执行。必须先迁移，再升级 API 和全部 Worker。启用 customer-profile-api 的邀约执行标识之前，还需更新青天成功回调；步骤见 [邀约状态同步](invitation-delivery.md)。

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


## 新增画像 Runner Worker

`profile-runner-worker` 已合入根目录 `docker-compose.yml`，使用 `docker compose up -d --build` 与其他服务统一启动。任务分工、单目标重试及旧独立项目迁移见 [Runner 接入与部署](profile-runner-integration.md)。执行复用 001–006 表结构与重试机制，最终失败通知通过 007 增加 outbox。
