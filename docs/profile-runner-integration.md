# 客户画像 Runner 与统一 Docker Compose 部署

业务 `customer-profile-api` 继续向原 backend 的 `/v1/customer-profile/tasks` 提交任务。新增 Runner Worker 与擎天／Dify Worker 连接同一 MySQL 队列，只处理客户画像；邀约保留原流程。

`profile-runner-worker` 已合入根目录的 `docker-compose.yml`，与 `api`、`worker`、`commercial-worker` 统一启动。画像生成服务本身仍是外部依赖，需要提前运行。

Runner 适配与各 Worker 的竞争领取复用现有 001–006 表结构。没有新增表、数据库迁移、批次收据、失败通知进程或回写补偿进程；提交、重复提交以及旧 Dify 的执行方式保持原行为。

## 并发领取配置

Runner 与旧工作流 Worker 从同一 MySQL 队列竞争领取符合自身任务类型和环境条件的任务。每个空闲线程直接领取一条任务，数据库行锁及 `SKIP LOCKED` 防止重复领取。通过各自并发数控制处理容量，无需配置百分比或等待另一组溢出。

```dotenv
WORKER_ROUTING_MODE=parallel
WORKER_CONCURRENCY=8
COMMERCIAL_WORKER_CONCURRENCY=1
PROFILE_RUNNER_CONCURRENCY=1
```

以上为默认并发示例，并不保证固定的处理份额；实际完成量还取决于工作流耗时和目标健康状态。Runner 仅领取生产环境中、有有效中国手机号和 1–50 字符业务批次的画像；邀约、测试环境以及不符合其入参要求的画像由支持这些任务的旧 Worker 处理。

旧 `PROFILE_ROUTING_*` 与 `PROFILE_RUNNER_TRAFFIC_PERCENT` 已停用，可从部署环境中删除；即使残留 `weighted` 或比例数值，也不会改变新版 Worker 的领取行为。旧版本 Worker 仍可能执行旧的分流规则，升级时需统一更新全部参与画像处理的 Worker。修改并发后需要重建容器；只执行 `restart` 不会加载新环境变量。

## 原有重试机制

Runner 使用原队列的手机号及业务批次，新任务提交到生成服务的 `customer_profile_pipeline`，并按原机制持久化 run_id、目标 ID 和 revision 后查询。已有 `customer_profile_entry` 运行仍按原 run_id 恢复查询。轮询超时或重启继续查询同一个 run_id，不增加生成次数；明确失败后的次数上限、退避、资源池切换和目标优先策略沿用原 `RETRY_*` 配置。竞争领取不更改旧任务已经固化的 retry policy。

Runner 使用独立目标 ID，默认 `profile-runner-production`，角色为 `local`。旧目标的 ID、revision 和资源池保持原值。旧 Dify 仍使用原调用及重试方式，没有新增 Dify 提交／轮询恢复改造。

不单独识别“画像已生成但回写失败”并补发 HTTP：新服务返回确定失败后，backend 按原工作流失败机制处理。业务回写仍由 CustomerProfile 工作流负责，部署时保持 `WRITEBACK_ENABLED=true`，并检查业务 API 地址与 M2M 密钥。提交结果未知和运行失联仍沿用原机制处理，不盲目重建远端运行。

## 新增 Worker 的配置

在部署用 `.env` 中添加以下字段，地址必须从新 Worker 容器内可达；示例地址应替换成实际地址。

```dotenv
PROFILE_RUNNER_BASE_URL_PRODUCTION=http://host.docker.internal:18000
PROFILE_RUNNER_REQUEST_TIMEOUT_MS=15000
PROFILE_RUNNER_POLL_INTERVAL_MS=2000
PROFILE_RUNNER_POLL_TIMEOUT_MS=2400000
PROFILE_RUNNER_TARGET_REVISION=v1
PROFILE_RUNNER_CONCURRENCY=1
PROFILE_RUNNER_TARGET_MODE=active
```

画像服务与 backend 在同一台 Docker Compose 宿主机时，使用上例的 `host.docker.internal:18000`；Runner 服务已配置 `host-gateway` 映射。部署在其他机器时，改用 Runner 容器可达的实际地址，不要使用 `127.0.0.1`。沿用原 `MYSQL_*`，确保连接同一个数据库且从容器内可访问。三个 Worker 共用根目录的 `.env`；其中的 `WORKFLOW_PLATFORM` 继续保持擎天或 Dify，Compose 只为 Runner 容器覆盖平台、画像任务范围及生产环境。

生成服务目前串行执行画像，先保持 adapter 并发 1。增加 adapter 并发主要会增加生成服务内部排队量；不要直接增加共享 SQLite 的生成 Worker 副本。

## Docker Compose 统一启动

在包含 `docker-compose.yml` 的 backend 项目根目录准备好 `.env` 后，一条命令统一构建并启动：

```bash
docker compose up -d --build
```

Compose 会先运行 `migrate`，迁移成功后启动 `api`、`worker`、`commercial-worker` 和 `profile-runner-worker`。已有数据库会重新检查幂等的 001–006 迁移，本次接入没有新增迁移。Runner 不发布端口，与旧 Worker 共用队列和日志目录。

线上沿用原 backend 的 Compose 项目名、端口、卷、旧工作流密钥和目标身份。若原部署显式使用了 `-p`，下面所有 backend 命令也要带相同的 `-p`，例如 `docker compose -p "$CP_BACKEND_PROJECT" up -d --build`。代码提交并推送后，服务器才能拉取到本次配置修改。

查看整组服务和 Worker 日志：

```bash
docker compose ps
docker compose logs --tail=100 worker commercial-worker profile-runner-worker
```

从旧的比例分流版本升级时，先等待旧 Dify 在途任务完成，再停止三个画像 Worker，统一构建并重建：

```bash
docker compose stop worker commercial-worker profile-runner-worker
# 编辑 .env，删除已停用的比例配置，按容量设置三个 Worker 的并发数
# 保留原目标 ID、revision 与重试配置
docker compose up -d --build --no-deps --force-recreate worker commercial-worker profile-runner-worker
```

新提交继续留在原队列；共享画像与邀约的 Worker 暂停期间，邀约也会短暂等待。保留业务 API 的 `REMOTE_API_URL`，它继续指向 backend 任务接口。核对启动日志中的平台、目标身份和并发数，领取原因保存在重试尝试表中。

## 从旧独立 Runner 项目迁移

如果此前已经使用 `customer-profile-runner-adapter` 独立 Compose 项目运行 Runner，需要一次性将它迁到原 backend 项目。若旧 Runner 使用独立环境文件，先把 Runner 配置合入 backend 的 `.env`，保留 `MYSQL_*`、目标 ID `profile-runner-production` 和目标 revision。

更新代码后，在 backend 目录使用统一配置定位并停止旧项目里的 Runner 容器：

```bash
docker compose -p customer-profile-runner-adapter -f docker-compose.yml stop profile-runner-worker
```

确认旧 Runner 已停止后，沿用原 backend 项目名执行上面的 `docker compose up -d --build`。持久化的运行 ID 仍由相同目标恢复；以后所有服务统一启停，不再使用独立 Runner Compose 文件。

## 构建时指定基础镜像和 pip 源

Dockerfile 默认使用 `python:3.12-slim`，可用构建参数 `PYTHON_BASE_IMAGE` 指定可访问的镜像仓库。该参数和 `PIP_INDEX_URL` 只用于镜像构建，不需要加入业务 `.env`。

如果 Docker Hub 不可达、腾讯加速域名解析失败，可先确认 DaoCloud 官方入口是否可访问：

```bash
curl -I --connect-timeout 5 --max-time 10 https://m.daocloud.io/v2/
```

带有 `docker-distribution-api-version: registry/2.0` 和 Bearer 认证头的 401 是正常认证响应，说明入口可访问；具体镜像能否拉取仍由后续构建确认。参考 [DaoCloud 官方使用说明](https://github.com/DaoCloud/public-image-mirror)。

Dockerfile 按构建目标平台选取基础镜像；服务器为 AMD64 时应构建 AMD64 镜像。在 backend 项目目录统一构建全部服务：

```bash
docker compose build \
  --build-arg PYTHON_BASE_IMAGE=m.daocloud.io/docker.io/library/python:3.12-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

启动 Runner 前，用一次性容器验证配置的画像服务地址；输出应为 `200`，且不会领取画像任务：

```bash
docker compose run --rm --no-deps --pull never profile-runner-worker \
  python -c 'import os, urllib.request; url = os.environ["PROFILE_RUNNER_BASE_URL_PRODUCTION"].rstrip("/") + "/health"; print(urllib.request.urlopen(url, timeout=10).status)'
```

构建成功后，启动时沿用已经生成的镜像，避免再次使用默认构建参数：

```bash
docker compose up -d --no-build --force-recreate
```

该方式直接指定镜像地址，不需要修改服务器全局 Docker 配置或重启 Docker。构建完成后可用 `docker image inspect --format '{{.Os}}/{{.Architecture}}' 镜像名` 核对平台；本次 AMD64 服务器应输出 `linux/amd64`。

## 发布前保留回滚版本

备份部署用 `.env`，记录升级前 Git 提交号，并给运行中的旧 Worker 镜像打标签。以下按 `worker` 和 `commercial-worker` 示范；线上更多旧画像 Worker 应一并备份、升级和回滚。重建旧 Dify Worker 前，等待其正在执行的任务完成。

```bash
git rev-parse HEAD
docker compose -p "$CP_BACKEND_PROJECT" -f docker-compose.yml images
```

将下方镜像 ID 替换为上述输出中对应服务的旧镜像 ID，在构建新镜像前执行：

```bash
docker image tag OLD_WORKER_IMAGE_ID customer-profile-worker:pre-runner
docker image tag OLD_COMMERCIAL_IMAGE_ID customer-profile-commercial-worker:pre-runner
```

## 检验

1. 原 Worker 和新增 Runner 容器均为 running。Runner 启动日志应包含 `workflow_client_selected platform=runner`；各 Worker 的 `config_loaded` 应显示预期的 concurrency、目标身份和任务范围，无数据库或画像服务连接错误。
2. 从原业务入口提交真实画像任务，至少验证一条 Runner 画像、一条旧工作流画像和一条邀约。确认任务成功，业务侧能看到实际画像结果；不要重复提交仍在执行的测试任务。
3. 按测试手机号检查执行目标、运行 ID、状态、重试次数及错误码：

```sql
SELECT customer_phonenumber, task_type, workflow_target_id, workflow_run_id,
       status, attempt_count, last_error_code
FROM customer_profile_tasks
WHERE customer_phonenumber IN ('TEST_PHONE_1', 'TEST_PHONE_2');
```

Runner 目标为 `profile-runner-production`。旧擎天沿用原目标 ID；旧 Dify 保留原执行方式，目标字段可能为空。任何可处理画像任务的空闲 Worker 都可立即领取，实际完成量无需符合固定比例。

## 配置回滚

先让 Runner 停止领取新任务，旧 Worker 继续通过空闲并发领取新任务。修改 Runner 配置：

```dotenv
PROFILE_RUNNER_TARGET_MODE=draining
```

旧 Dify 在途任务完成后，在同一项目中统一重建三个 Worker：

```bash
docker compose up -d --no-deps --force-recreate worker commercial-worker profile-runner-worker
```

确认 Runner 绑定的未完成任务为 0；出现 SUBMIT_UNKNOWN、RUN_STALLED 或 REQUIRES_RECONCILIATION 的任务先核对远端结果，不能直接换平台重建运行。

```sql
SELECT COUNT(*) AS unfinished
FROM customer_profile_tasks
WHERE workflow_target_id = 'profile-runner-production'
  AND status IN ('queued', 'processing', 'retry_wait');
```

处理完已有运行后停止新增 Worker：

```bash
docker compose stop profile-runner-worker
```

停用 Runner 新任务领取使用 `draining`，它仍会恢复同一目标绑定的已有运行。配置回滚无需恢复数据库，也无需更新两个 API。

## 旧镜像回滚

如果还需要撤回 Worker 代码，先完成上述配置回滚并停止排空后的 Runner。创建 `compose.rollback.yml`：

```yaml
services:
  worker:
    image: customer-profile-worker:pre-runner
  commercial-worker:
    image: customer-profile-commercial-worker:pre-runner
```

使用发布前保存的镜像重建旧 Worker，不重新构建镜像：

```bash
docker compose -p "$CP_BACKEND_PROJECT" -f docker-compose.yml -f compose.rollback.yml up -d --no-deps --no-build --pull never --force-recreate worker commercial-worker
```

沿用原目标 ID、revision、端口及卷。Runner 排空并停止后，可恢复原 `.env` 中的旧平台和重试配置；没有数据库结构需要回滚。
