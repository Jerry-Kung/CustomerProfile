# 将部分画像任务分流到 Runner Worker

业务 `customer-profile-api` 继续向原 backend 的 `/v1/customer-profile/tasks` 提交任务。新增 Runner Worker 与擎天／Dify Worker 连接同一 MySQL 队列，只处理客户画像；邀约保留原流程。

本次只增加 Runner 适配和画像首次分流，复用现有 001–006 表结构。没有新增表、数据库迁移、批次收据、失败通知进程或回写补偿进程；提交、重复提交以及旧 Dify 的执行方式保持原行为。

## 分流配置

所有旧画像 Worker 和新增 Runner Worker 应使用一致配置。业务 API 与 backend API 接口不变，backend API 可以保留现有镜像。先升级全部参与者，再启用 `weighted`；旧镜像不认识这套分配规则，不能只更新新增 Worker。

```dotenv
PROFILE_ROUTING_MODE=weighted
PROFILE_RUNNER_TRAFFIC_PERCENT=80
PROFILE_ROUTING_SPILLOVER_SECONDS=300
PROFILE_ROUTING_WORK_ENVS=production
```

`parallel` 是兼容默认值；`weighted` 对生产环境、有有效中国手机号和 1–50 字符批次的画像任务计算稳定分配桶。默认新服务首次分配占 80%，旧工作流组合计 20%，比例可以配置为 0–100。旧组内仍按原目标空闲能力领取；小样本、失败重试和等待超时接手会使实际完成比例偏离首次分配比例。

等待超过 300 秒后，另一组可接手尚未提交的画像任务。测试环境、邀约和不符合 Runner 入参要求的画像继续走旧 Worker。修改配置后需要重建容器；只执行 `restart` 不会加载新环境变量。

## 原有重试机制

Runner 调用生成服务的 `customer_profile_entry`，使用原队列的手机号及业务批次，并按原机制将 run_id、目标 ID 和 revision 持久化后查询。轮询超时或重启继续查询同一个 run_id，不增加生成次数；明确失败后的次数上限、退避、资源池切换和目标优先策略沿用原 `RETRY_*` 配置。首次分流比例不限制原有失败重试策略，也不更改旧任务已经固化的 retry policy。

Runner 使用独立目标 ID，默认 `profile-runner-production`，角色为 `local`。旧目标的 ID、revision 和资源池保持原值。旧 Dify 仍使用原调用及重试方式，没有新增 Dify 提交／轮询恢复改造。

不单独识别“画像已生成但回写失败”并补发 HTTP：新服务返回确定失败后，backend 按原工作流失败机制处理。业务回写仍由 CustomerProfile 工作流负责，部署时保持 `WRITEBACK_ENABLED=true`，并检查业务 API 地址与 M2M 密钥。提交结果未知和运行失联仍沿用原机制处理，不盲目重建远端运行。

## 新增 Worker 的配置

在部署用 `.env` 中添加以下字段，地址必须从新 Worker 容器内可达；示例地址应替换成实际地址。

```dotenv
PROFILE_RUNNER_BASE_URL_PRODUCTION=http://10.81.66.8:18000
PROFILE_RUNNER_REQUEST_TIMEOUT_MS=15000
PROFILE_RUNNER_POLL_INTERVAL_MS=2000
PROFILE_RUNNER_POLL_TIMEOUT_MS=2400000
PROFILE_RUNNER_TARGET_REVISION=v1
PROFILE_RUNNER_CONCURRENCY=1
PROFILE_RUNNER_TARGET_MODE=active
```

沿用原 `MYSQL_*`，确保连接同一个数据库且从 Runner 容器内可访问。各 Compose 项目默认网络独立，旧项目中的 MySQL 服务名不会自动在新项目解析；使用可达地址或显式共享网络。原 `.env` 的 `WORKFLOW_PLATFORM` 继续保持擎天或 Dify；新增 Compose 只为 Runner 容器覆盖平台、画像任务范围及生产环境。

生成服务目前串行执行画像，先保持 adapter 并发 1。增加 adapter 并发主要会增加生成服务内部排队量；不要直接增加共享 SQLite 的生成 Worker 副本。

## Docker Compose 发布

代码提交并推送后才能在服务器获取。先确认镜像适配服务器架构：原 Dockerfile 固定 ARM64，x86_64 环境需要调整首个 FROM 后构建验证。沿用原 backend 的 Compose 项目名、端口、卷、旧工作流密钥和目标身份。

1. 拉取新代码，添加配置，暂时保持所有组件 `PROFILE_ROUTING_MODE=parallel`。
2. 构建新镜像，更新全部旧画像 Worker，保留原有重试配置与身份；backend API 不需要更换镜像。沿用原迁移已完成 001–006 的数据库，本次没有新迁移要运行；服务更新使用 `--no-deps` 避免启动旧迁移容器。
3. 用独立 `compose.profile-runner.yml` 启动新增 Worker，不与原 Compose 文件叠加，不发布端口。
4. 所有画像 Worker 都更新后，短暂停止画像 Worker，统一切换 `weighted` 配置，再重建启动。新提交继续留在原队列；共享画像与邀约的 Worker 暂停期间，邀约也会短暂等待。
5. 验证原画像、邀约及新服务的生成回写。核对启动日志中的平台和分流配置，并查询任务表中的执行目标与状态；领取原因保存在重试尝试表中，不是当前日志输出字段。

在 backend 项目目录，新增 Worker 的命令逐条执行：

```bash
docker compose -p customer-profile-runner-adapter -f compose.profile-runner.yml build
docker compose -p customer-profile-runner-adapter -f compose.profile-runner.yml up -d
docker compose -p customer-profile-runner-adapter -f compose.profile-runner.yml ps
docker compose -p customer-profile-runner-adapter -f compose.profile-runner.yml logs --tail=100 profile-runner-worker
```

原项目按实际项目名与服务名更新全部旧画像 Worker：

```bash
docker compose -p "$CP_BACKEND_PROJECT" -f docker-compose.yml up -d --build --no-deps worker commercial-worker
```

配置调整后，先停止所有参与画像的 Worker，再分别用 `up -d --no-deps --force-recreate` 重建原项目的画像 Worker 和新增 Worker。保留原业务 API 的 `REMOTE_API_URL`，它继续指向 backend 任务接口。

## 构建时指定基础镜像和 pip 源

Dockerfile 默认使用 `python:3.12-slim`，可用构建参数 `PYTHON_BASE_IMAGE` 指定可访问的镜像仓库。该参数和 `PIP_INDEX_URL` 只用于镜像构建，不需要加入业务 `.env`。

如果 Docker Hub 不可达、腾讯加速域名解析失败，可先确认 DaoCloud 官方入口是否可访问：

```bash
curl -I --connect-timeout 5 --max-time 10 https://m.daocloud.io/v2/
```

带有 `docker-distribution-api-version: registry/2.0` 和 Bearer 认证头的 401 是正常认证响应，说明入口可访问；具体镜像能否拉取仍由后续构建确认。参考 [DaoCloud 官方使用说明](https://github.com/DaoCloud/public-image-mirror)。

代码更新后，在原 backend 项目目录构建旧 Worker：

```bash
docker compose -p "$CP_BACKEND_PROJECT" -f docker-compose.yml build \
  --build-arg PYTHON_BASE_IMAGE=m.daocloud.io/docker.io/library/python:3.12-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  worker commercial-worker
```

新增 Worker 使用相同构建参数：

```bash
docker compose -p customer-profile-runner-adapter -f compose.profile-runner.yml build \
  --build-arg PYTHON_BASE_IMAGE=m.daocloud.io/docker.io/library/python:3.12-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

该方式直接指定镜像地址，不需要修改服务器全局 Docker 配置或重启 Docker。基础镜像平台仍为原有 ARM64，沿用原服务器的架构适配方式。

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

1. 原 Worker 和新增 Runner 容器均为 running。Runner 启动日志应包含 `workflow_client_selected platform=runner`；所有画像 Worker 的 `config_loaded` 应显示 weighted、80 和 production，无数据库或画像服务连接错误。
2. 从原业务入口提交真实画像任务，至少验证一条 Runner 画像、一条旧工作流画像和一条邀约。确认任务成功，业务侧能看到实际画像结果；不要重复提交仍在执行的测试任务。
3. 按测试手机号检查执行目标、运行 ID、状态、重试次数及错误码：

```sql
SELECT customer_phonenumber, task_type, workflow_target_id, workflow_run_id,
       status, attempt_count, last_error_code
FROM customer_profile_tasks
WHERE customer_phonenumber IN ('TEST_PHONE_1', 'TEST_PHONE_2');
```

Runner 目标为 `profile-runner-production`。旧擎天沿用原目标 ID；旧 Dify 保留原执行方式，目标字段可能为空。80/20 是首次分配偏好，重试、300 秒超时接手和小样本会使当前完成分布偏离该比例。

## 配置回滚

先让 Runner 停止领取新任务，并让旧 Worker 恢复领取全部新任务。所有画像 Worker 使用的配置改为：

```dotenv
PROFILE_ROUTING_MODE=parallel
PROFILE_RUNNER_TARGET_MODE=draining
```

先重建 Runner，再更新全部旧画像 Worker；旧 Dify 在途任务应先完成：

```bash
docker compose -p customer-profile-runner-adapter -f compose.profile-runner.yml up -d --no-deps --force-recreate profile-runner-worker
docker compose -p "$CP_BACKEND_PROJECT" -f docker-compose.yml up -d --no-deps --force-recreate worker commercial-worker
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
docker compose -p customer-profile-runner-adapter -f compose.profile-runner.yml stop profile-runner-worker
```

只将比例改为 0 仍允许等待超时接手，停用新任务领取应使用 draining。配置回滚无需恢复数据库，也无需更新两个 API。

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
