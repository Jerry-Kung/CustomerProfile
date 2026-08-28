# 部署快速上手（Docker Compose）

面向接手维护的同事，仅覆盖最基础的部署操作与必需环境变量。更完整的说明（本地开发、集成测试、常见问题）见 `docs/deploy-and-run.md`。

## 1. 项目是什么

一个"客户画像"任务后台，由 API 和可扩展 Worker 目标组成（同一份代码、同一个镜像）：

- **api**：FastAPI HTTP 服务，接收任务提交（手机号 + 可选微信截图），写入 MySQL。
- **worker**：本地目标 `local-default`（沿用旧本地工作流身份）。
- **commercial-worker**：商用目标 `commercial-default`（沿用旧商用工作流身份）。

MySQL 数据库即任务队列，没有额外的消息中间件。**MySQL 和 Dify/QingTian 都是外部依赖**，不在 compose 内，需要提前准备好并通过环境变量指向它们。

## 2. 前置条件

- 服务器已安装 Docker 与 Docker Compose（v2，`docker compose` 命令）。
- 一个可访问的 MySQL 实例（数据库需已创建，如 `customer_profile`；表结构由启动时的迁移自动建立）。
- Dify（或 QingTian）平台的访问地址与 API Key。

## 3. 部署步骤

```bash
# 1) 进入项目根目录，准备配置文件
cp .env.example .env
#    编辑 .env，按下文第 4 节填写必填项

# 2) 构建并启动（migrate 先跑数据库迁移，成功后 api 和两个 Worker 才启动）
docker compose up -d --build

# 3) 验证
docker compose ps                    # api / 两个 Worker 应为 running，migrate 为 exited (0)
curl http://127.0.0.1:18008/health   # 应返回健康状态
```

API 默认监听宿主机 **18008** 端口。如需改端口，在 `.env` 里设置 `HOST_API_PORT`（只改宿主机映射，容器内固定 18008）：

```bash
HOST_API_PORT=8080 docker compose up -d   # 或写进 .env
```

> 注意：`.env.example` 里 `API_HOST` / `API_PORT` 的注释提到 8000 端口，那只影响不用 Docker 的本地直跑；compose 部署时容器内端口被 `docker-compose.yml` 固定为 18008，无需关心这两个变量。

## 4. 环境变量（.env）

`.env` 放在项目根目录，被 `docker-compose.yml` 通过 `env_file` 注入 `migrate`、`api` 和两个 Worker 服务。

### 4.1 必填 — MySQL

| 变量 | 说明 |
|---|---|
| `MYSQL_HOST` | MySQL 地址 |
| `MYSQL_PORT` | 端口，默认 3306 |
| `MYSQL_USER` | 用户名 |
| `MYSQL_PASSWORD` | 密码 |
| `MYSQL_DATABASE` | 数据库名（需已存在，表由迁移自动创建） |

### 4.2 工作流平台选择

| 变量 | 说明 |
|---|---|
| `WORKFLOW_PLATFORM` | `dify`（默认）或 `qingtian`，决定 worker 调用哪个平台，也决定下面哪一组变量必填 |

### 4.3 必填 — Dify（当 `WORKFLOW_PLATFORM=dify`）

| 变量 | 说明 |
|---|---|
| `DIFY_BASE_URL` | Dify 平台根地址，如 `http://<host>` |
| `DIFY_RUN_URL` | 工作流执行地址，通常为 `<DIFY_BASE_URL>/v1/workflows/run` |
| `DIFY_API_KEY_TEST` | 客户画像工作流的测试环境 Key（不填时回退到旧变量 `DIFY_API_KEY`） |
| `DIFY_API_KEY_PRODUCTION` | 客户画像工作流的生产环境 Key（必填，无回退） |
| `DIFY_INVITATION_STRATEGY_API_KEY` | 邀约策略（invitation_strategy）任务类型的 Key |
| `DIFY_USER_ID` | 调用 Dify 时使用的用户标识，如 `klh` |

说明：任务提交时可指定 `work_env`（test / production），worker 据此选用对应的 API Key。

### 4.4 必填 — QingTian（当 `WORKFLOW_PLATFORM=qingtian`）

| 变量 | 说明 |
|---|---|
| `QINGTIAN_BASE_URL` | 需包含 `/api/proxy/api/v1` 前缀，如 `https://qingtian.dfmc.com.cn/api/proxy/api/v1` |
| `QINGTIAN_API_KEY_TEST` | 测试环境 Key |
| `QINGTIAN_API_KEY_PRODUCTION` | 生产环境 Key |
| `QINGTIAN_INVITATION_STRATEGY_API_KEY` | 邀约策略任务类型的 Key |
| `QINGTIAN_USER_ID` | 用户标识 |

Compose 默认启动一条商用 Worker；擎天模式下还必须填写：

- `QINGTIAN_COMMERCIAL_API_KEY_TEST` / `QINGTIAN_COMMERCIAL_API_KEY_PRODUCTION`

邀请策略统一使用 `QINGTIAN_INVITATION_STRATEGY_API_KEY`，只由本地目标处理。

### 4.5 常用调优项（未来可能经常调整）

以下两项与业务负载直接相关，是最可能需要日常调整的配置，单独说明：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WORKER_CONCURRENCY` | 8 | worker 单进程并发执行的工作流数。调大可提高任务吞吐，但会增加对 Dify/QingTian 平台和 MySQL 的并发压力；必须 ≥ 1，否则启动报错。 |
| `WORKER_ROUTING_MODE` | parallel | `parallel` 让本地与商用立即共同消费；以后可改为 `local_preferred` 或 `local_only`。 |
| `COMMERCIAL_QUEUE_THRESHOLD` | 5 | `local_preferred` 下，排队数达到此值后商用 Worker 开始分流普通任务。 |
| `COMMERCIAL_SPILLOVER_DELAY_SECONDS` | 0 | `local_preferred` 下，最老任务达到该等待时间也会启用商用分流。 |
| `WORKER_TARGET_FAILURE_THRESHOLD` / `WORKER_TARGET_COOLDOWN_SECONDS` | 3 / 60 | 目标连续 HTTP/传输失败后，暂停领取未绑定任务的次数阈值和冷却时间。 |
| `TASK_QUEUE_MAX_WAITING` | 500 | 每种任务类型各自的最大等待（queued 状态）任务数。提交任务时同类型等待数达到该上限，API 返回"队列已满"错误；必须 ≥ 1，否则启动报错。 |

修改方法：编辑 `.env` 中对应的值，然后执行：

```bash
docker compose up -d
```

商用 Worker 已默认启用；无需再传 profile。启动前必须填写一套商用客户画像 Key；
否则对应容器会在擎天模式下因缺少必填 Key 而无法启动。

> 注意：`.env` 是在**容器创建时**注入的，`docker compose restart` 不会重新读取 `.env`，必须用 `docker compose up -d` 让 compose 重建受影响的容器，新值才生效。`WORKER_CONCURRENCY` 只影响 worker，`TASK_QUEUE_MAX_WAITING` 在 api 提交任务时生效，`up -d` 会自动重建这两个容器。

### 4.6 其他可选项（有合理默认值，一般不用改）

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST_API_PORT` | 18008 | 宿主机对外端口映射 |
| `APP_ENV` | local | 环境标识 |
| `LOG_LEVEL` | INFO | 日志级别 |
| `DIFY_IMAGE_MODE` | file_id | 图片传给 Dify 的方式，保持默认即可 |
| `DIFY_POLL_TIMEOUT_MS` | 2400000 | 等待 Dify 工作流完成的超时（毫秒） |
| `QINGTIAN_POLL_TIMEOUT_MS` | 2400000 | 等待 QingTian 完成的超时（毫秒） |

重试默认最多创建 3 个擎天新运行，同一 `runId` 最多恢复轮询 3 次、最长观察
7200 秒。代码会优先选择尚未尝试的目标；目标数量不足时自动复用健康目标，因此
这些参数不需要根据工作流数量计算或调整。

上传的图片保存在 compose 定义的共享卷 `task_uploads` 中，api 写入、worker 读取，已配置好，无需手动处理。

## 5. 数据库迁移

无需手动执行。`docker compose up` 时 `migrate` 服务自动按顺序执行 `db/migrations/*.sql`，全部为幂等脚本，可安全重复运行；`api` 和两个 Worker 会等它成功退出后再启动。若 `migrate` 失败，`docker compose logs migrate` 查看原因（通常是 MySQL 连不通或库不存在）。

## 6. 日常运维命令

```bash
docker compose logs -f api          # 看 API 日志
docker compose logs -f worker       # 看本地 Worker 日志
docker compose logs -f commercial-worker # 看商用 Worker 日志
docker compose restart worker       # 重启单个服务
docker compose down                 # 停止（保留上传文件卷）
docker compose up -d --build        # 更新代码后重新构建并启动
```

## 7. 验证服务正常

1. `curl http://127.0.0.1:18008/health` 返回正常。
2. 提交测试任务后，本地和商用两个 active 目标能原子竞争队列；一条任务只会被一个 Worker 领取。
3. 同一手机号重复提交会复用/重新排队原任务记录（这是设计行为，重新提交即重试，没有单独的重试接口）。
