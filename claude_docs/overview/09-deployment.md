# 09 · 部署与运行

详细 ops 手册：`docs/deploy-and-run.md`、`docs/docker-deploy-ubuntu24.md`。本节做"够用的概览"。

## 拓扑

- API 进程：一个就够；可平行扩容（DB 是状态源）。
- Worker 进程：可以多进程/多线程，靠 `SKIP LOCKED` 抢占。`WORKER_CONCURRENCY` 是单进程内的线程数。
- 外部依赖：MySQL 8.0+（必须 `SKIP LOCKED`、`JSON` 类型）；Dify；可选 TOS（当前未启用）。

## 本地启动

```bash
pip install -e ".[dev]"
cp .env.example .env   # 编辑 .env

# Shell A
customer-profile-api

# Shell B
customer-profile-worker
```

或 `uvicorn src.app.asgi:app --host 0.0.0.0 --port 8000` 启动 API。

## Docker Compose

`docker-compose.yml` 里:

- `api` 映射 `${HOST_API_PORT:-18008} → 容器:18008`
- `worker` 不暴露端口
- 共享 `task_uploads` 卷：`/var/lib/customer-profile/task-uploads`（在 `TASK_UPLOAD_DIR` 显式指定）

```bash
docker compose up -d --build           # 默认 18008 端口
HOST_API_PORT=8080 docker compose up -d  # 改端口
docker compose logs -f api
docker compose logs -f worker
```

**容器里 `MYSQL_HOST=127.0.0.1` 不可用**——指向容器自己。常见解：
- 配 `extra_hosts: ["host.docker.internal:host-gateway"]`，env 里写 `MYSQL_HOST=host.docker.internal`
- 或写宿主机局域网 IP（MySQL 须绑 `0.0.0.0`）
- 或写 `172.17.0.1`（默认 docker0 网关）

## 数据库迁移

```bash
# 仓库提供的两个迁移
mysql ... < db/migrations/001_create_customer_profile_tasks.sql
mysql ... < db/migrations/002_add_task_image_columns.sql

# 或用 entry-point（仅跑 001）：
customer-profile-migrate
```

**`002` 不在 `customer-profile-migrate` 范围**——当前 `run_migration.py` 只 cat `migration_sql_path`（来自 `ContractPaths.migration_sql_path`，硬编码为 001 文件路径）。增加迁移目前需要手动 `mysql <`。如果改这点，请同时修改 `src/contracts/registry.py:default_contract_paths` 或重写 `run_migration.py` 让它扫描整个目录。

## 健康检查

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","db_status":"up"}   # db_status=down 表示 MySQL ping 失败
```

容器编排（k8s/ECS）应该把 `/health` 当 readiness probe；DB 失联时返回 200 + `db_status=down`，不是 5xx —— **要做严格的 readiness 检查需要在外面包一层**。

## 上线前清单（来自 `docker-deploy-ubuntu24.md` §10）

- [ ] Docker / docker compose 可用
- [ ] `.env` 中 Dify / MySQL 对**容器内**可达（注意 `127.0.0.1` 陷阱）
- [ ] 已执行 001 / 002 迁移
- [ ] `HOST_API_PORT` 无冲突；UFW + 云安全组都放行
- [ ] `curl /health` 返回 `ok`
- [ ] 前端 Base URL = `http(s)://<主机>:<HOST_API_PORT>`（或反向代理域名）

## 反向代理（推荐生产）

`docker-deploy-ubuntu24.md` §8.2 建议在前端域名做反代到 `127.0.0.1:18008`，把跨域问题降级为同源问题。当前后端 CORS 默认放开（`*`），但不带 `Authorization` cookies——所以"反代 + 同域"还是更稳。

## 已知运维盲区

- **没有 stale processing 回收的 cron**。任务卡在 processing 不会自愈，要么人工 SQL 改回 `queued`，要么写个 cron 调 `TaskService.requeue_stale_processing(timeout_seconds=...)`。
- **没有指标/Prometheus**。靠日志看吞吐与失败率。
- **没有持久化 `dify_run_id`**。一旦 Worker 进程崩在 poll 阶段，没法从外部接着 poll——只能等下次重新提交。
- **没有 admin 接口**。"重新跑某行"必须靠重新提交，或手动 SQL。
