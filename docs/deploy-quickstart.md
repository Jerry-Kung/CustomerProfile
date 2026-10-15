# 部署快速上手（Docker Compose）

根目录 `docker-compose.yml` 统一管理迁移、API、本地 Worker、商用 Worker 和 Runner Worker。MySQL、Dify／擎天、独立画像生成服务需要事先准备；详细配置见[部署运行指南](deploy-and-run.md)，Runner 升级和回滚见 [Runner 接入与部署](profile-runner-integration.md)。

## 1. 准备配置

服务器安装 Docker 和 Compose v2，在项目根目录执行：

```bash
cp .env.example .env
# 编辑 .env，填写实际地址与密钥
```

必须配置以下项目：

| 配置 | 要求 |
|---|---|
| `MYSQL_*` | 可访问的 MySQL 8.x，数据库已创建；表由迁移建立 |
| `WORKFLOW_PLATFORM` | 共享配置保持 `dify` 或 `qingtian`；Runner 服务单独覆盖为 `runner` |
| `DIFY_*` 或 `QINGTIAN_*` | 对应平台的地址、测试／生产画像 Key、邀约 Key 和用户 ID，完整字段见部署运行指南 §2 |
| `QINGTIAN_COMMERCIAL_API_KEY_TEST` / `QINGTIAN_COMMERCIAL_API_KEY_PRODUCTION` | 使用擎天时，商用 Worker 的独立画像 Key |
| `PROFILE_RUNNER_BASE_URL_PRODUCTION` | 从 Runner 容器内可访问的画像生成服务地址 |

生成服务在同一宿主机时，可使用 `http://host.docker.internal:18000`；Runner 服务已配置 `host-gateway`。远端服务填写实际地址。容器里的 `127.0.0.1` 指向该容器自身。

## 2. 统一启动与更新

首次启动，以及后续更新 backend 代码后，均执行：

```bash
docker compose up -d --build
```

先由 `migrate` 按文件名顺序执行全部幂等迁移，成功后启动 API 和三个 Worker。无需再启动独立的 Runner Compose 项目。

```bash
docker compose ps -a
curl -fsS http://127.0.0.1:18008/health
docker compose logs --tail=100 worker commercial-worker profile-runner-worker
```

API 和三个 Worker 应运行，`migrate` 应以 0 退出，健康检查中的数据库状态应为 `up`。宿主机 API 端口默认 18008，可在 `.env` 设置 `HOST_API_PORT=8080` 后重新执行启动命令。

## 3. 日常调整

| 变量 | 默认值 | 作用 |
|---|---|---|
| `WORKER_CONCURRENCY` | 8 | 旧 Worker 单进程并发 |
| `PROFILE_RUNNER_CONCURRENCY` | 1 | Runner 适配 Worker 并发 |
| `WORKER_ROUTING_MODE` | parallel | 旧本地／商用目标的普通任务领取策略 |
| `PROFILE_ROUTING_MODE` | parallel | 画像首次分流；全部画像 Worker 升级后可切换为 weighted |
| `PROFILE_RUNNER_TRAFFIC_PERCENT` | 80 | weighted 下 Runner 首次分配百分比 |
| `PROFILE_ROUTING_SPILLOVER_SECONDS` | 300 | 等待超时后允许另一组接手 |
| `PROFILE_ROUTING_WORK_ENVS` | production | 应用画像比例分流的环境 |
| `TASK_QUEUE_MAX_WAITING` | 500 | 各任务类型 queued 数量上限 |

仅修改 `.env` 后执行 `docker compose up -d`，让 Compose 重建受影响的容器并加载配置。`restart` 不会重新注入环境变量。首次启用比例分流时，按 [Runner 升级步骤](profile-runner-integration.md#docker-compose-统一启动)统一更新全部画像 Worker。

默认首次分配为 Runner 80%、旧工作流合计 20%；失败重试和等待超时接手可能改变最终完成比例。邀约继续走旧工作流。

## 4. 日常命令

```bash
docker compose logs -f api
docker compose logs -f profile-runner-worker
docker compose logs migrate
docker compose down
```

上传文件由 `task_uploads` 共享卷保存。数据库升级无需逐个执行 SQL；本地运行时使用 `customer-profile-migrate`。提交和查询辅助工具见[运维与调试脚本](../scripts/README.md)。
