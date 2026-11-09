# 统一 Worker 配置与灰度迁移

当前默认主线使用 `qingtian`、画像 `runner`，另提供可选的原生邀约 `invitation_runner`。
同一镜像可启动任意数量的 Worker 组，
每组声明具体工作流目标、任务范围、环境、每进程并发及运行模式。商用是一个擎天目标，
不再有专属队列门槛、权重或跨平台优先级。`dify` 仅保留为历史部署兼容适配器。

## 消费模型

所有组竞争 MySQL 任务表，使用 `FOR UPDATE SKIP LOCKED` 原子领取：

| 运行时字段 | 含义 |
|---|---|
| `WORKFLOW_PLATFORM` | qingtian / runner / invitation_runner，选择 HTTP 适配器 |
| `WORKER_TASK_TYPES` | customer_profile / invitation_strategy 的非空逗号列表 |
| `WORKER_WORK_ENVS` | test / production 的非空逗号列表 |
| `WORKER_CONCURRENCY` | 每个进程的执行槽位数，默认 1，必须 >= 1 |
| `WORKFLOW_TARGET_ID` | 稳定的工作流/凭据身份；不同目标必须不同 |
| `WORKFLOW_TARGET_REVISION` | 非敏感凭据/配置版本；恢复时必须与原运行匹配 |
| `WORKER_TARGET_MODE` | active 接新任务并恢复；draining 或兼容别名 recovery_only 只恢复本目标已绑定任务 |

同一工作流、同一组凭据的多个副本使用相同 ID/revision，可以接续恢复。不同凭据、
不同端点或不同工作流使用独立 ID。更换凭据时保留旧目标恢复在途运行，新凭据使用新版本。
类型和环境过滤决定新任务/未绑定任务的准入；已绑定的运行继续由原目标恢复，即使该组已
缩小消费范围。排空结束前应保留原环境 URL、密钥、ID/revision。

总容量 = 副本数 × 每进程并发；实际流量占比随耗时和可用容量变化，不保证固定比例。
Runner 当前只支持画像，错误配置 invitation_strategy 会在启动时拒绝。生产画像需要有效
中国手机号及 1–50 字符 batch_id；API 的历史通用提交契约仍允许更宽的字段范围。
`invitation_runner` 只支持邀约，要求有效手机号、1–50 字符批次和 execution_id；领取 SQL
会跳过缺执行标识的历史邀约，保留给旧目标处理。原生邀约的终态不能通过重复 POST 重跑。

## 配置来源只维护一条链路

- 本地进程：系统环境变量优先，其次根目录 .env，再其次代码默认值。
- Compose：.env 用于变量插值，也通过 env_file 注入容器；服务 environment 显式映射优先。
- K8s：使用部署环境变量/ConfigMap/Secret，容器不会自动读取本地 .env。
- .env.example 是参考模板，不参与运行。不要把生产凭据提交到源码。

使用统一入口可以避免 --env-file 只影响插值、容器却继续读取旧 .env：

```bash
scripts/compose.sh --env-file /absolute/path/to/env config -q
scripts/compose.sh --env-file /absolute/path/to/env up -d --build
```

省略 --env-file 时使用根目录 .env。普通 `docker compose` 也支持原有根目录 .env 用法。

## 默认 Compose 的三个组与可选邀约组

| 服务 | 平台 | 声明任务范围 | 环境 | 并发 | 模式 | 目标 |
|---|---|---|---|---:|---|---|
| worker | qingtian | 画像 | test,production | 1 | draining | local-default |
| commercial-worker | qingtian | 画像 | test,production | 1 | draining | commercial-default |
| profile-runner-worker | runner | 画像 | production | 5 | active | profile-runner-production |
| invitation-runner-worker（需启用 profile） | invitation_runner | 邀约 | test | 5 | active | invitation-runner-test |

两个擎天目标默认只恢复已绑定的存量任务，不接新任务。保留其原凭据、目标 ID 和 revision，
普通组仍能恢复此前绑定给它的旧邀约，即使声明的新任务范围已缩小。原生组分别提供 5 个
后端槽位；CustomerProfile 单进程队列 worker 同时配置 `WORKER_CONCURRENCY=10`，避免
任务只是在远端串行排队。执行服务仍共用既有模型请求并发和限流。

当前 UAT 文件让原生画像和邀约都覆盖 `test,production`，每组的两个环境共用 5 个槽位。
画像需同时填写 `PROFILE_RUNNER_BASE_URL_TEST / _PRODUCTION`；两个标签在本次部署都
指向原有执行服务。保持目标 `profile-runner-production / v1`，不因扩展环境而改变存量归属。

每个组独立设置 TASK_TYPES、WORK_ENVS、CONCURRENCY、TARGET_MODE、TARGET_ID、TARGET_REVISION。
模板中的前缀分别是 `QINGTIAN_WORKER_*`、`COMMERCIAL_WORKER_*`、`PROFILE_RUNNER_*`；这些由
Compose 映射到上述统一运行时字段，Python 不猜测服务名或消费职责。

可选邀约组使用 `INVITATION_RUNNER_*` 前缀，默认不启动。通过 `--profile invitation-runner`
启用时，只校验所消费环境的独立入站 URL/Key，并要求业务回调已启用。完整接入、终态语义及
配置示例见[原生邀约服务接入](native-invitation-runner.md)。它复用现有 008 结构，不增加 SQL 迁移。

例如分离邀约：设置 QINGTIAN_WORKER_TASK_TYPES=invitation_strategy，再参考额外目标示例增加
一个只消费画像的擎天组。这样邀请与画像分别调并发，不需要新增 Python 执行逻辑。

扩展同一目标的副本（避免设置固定 container_name 或发布 Worker 端口）：

```bash
scripts/compose.sh up -d --scale commercial-worker=3 --scale profile-runner-worker=2
```

增加不同凭据的目标，参考 deploy/examples/additional-worker.compose.yaml，设置其中要求的
QINGTIAN_B_API_KEY_PRODUCTION，并保持 QINGTIAN_B 的目标 ID 独立：

```bash
scripts/compose.sh -f docker-compose.yml -f deploy/examples/additional-worker.compose.yaml config -q
scripts/compose.sh -f docker-compose.yml -f deploy/examples/additional-worker.compose.yaml up -d
```

## 现有线上 env 的迁移

| 原变量 | 新行为/建议 |
|---|---|
| WORKER_CONCURRENCY | 仅作为 QINGTIAN_WORKER_CONCURRENCY 的兼容后备；现在明确设置每组并发 |
| INVITATION_WORKER_CONCURRENCY | 不再隐式覆盖 Worker；用邀请组自己的 CONCURRENCY |
| LOCAL_WORKER_TARGET_MODE / LOCAL_WORKFLOW_TARGET_REVISION | 作为 QINGTIAN_WORKER 对应新字段的兼容后备 |
| COMMERCIAL_1_WORKER_TARGET_MODE / COMMERCIAL_1_WORKFLOW_TARGET_REVISION | 作为 COMMERCIAL_WORKER 对应新字段的兼容后备 |
| WORKER_ROUTING_MODE / COMMERCIAL_QUEUE_THRESHOLD / COMMERCIAL_SPILLOVER_DELAY_SECONDS | 已退出运行时准入，旧值忽略；用目标模式和容量控制 |
| PROFILE_ROUTING_* / PROFILE_RUNNER_TRAFFIC_PERCENT | 已退出运行时，无需恢复 |
| DIFY_IMAGE_MODE / TASK_UPLOAD_* / TOS_* | 当前业务未接通，不再初始化上传目录或对象存储配置，也不挂载上传卷 |

用户现有 env 中 COMMERCIAL_1_WORKER_TARGET_MODE=draining 会继续保留排空行为。
如果要让该目标参与新画像，显式设置 COMMERCIAL_WORKER_TARGET_MODE=active；新字段优先。
WORKER_CONCURRENCY=1 现在确实成为普通擎天组的后备值，不再被隐藏的邀请默认并发 4 覆盖。
重复声明同一变量仍是后值生效，建议每个变量保留一处。模板不包含生产凭据。

QINGTIAN_API_KEY_TEST/PRODUCTION 只在该组声明消费对应环境画像时必填；邀约组只要求
QINGTIAN_INVITATION_STRATEGY_API_KEY、平台 URL 和 UserID。缩小任务范围时仍需保留恢复存量所需的密钥。

PROFILE_FAILURE_CALLBACK_ENABLED=true 配合 PROFILE_API_BASE_URL、PROFILE_API_KEY 用于最终失败
通知以及邀约回写确认，不要作为无效画像配置删除。业务 API 地址使用 M2M 根地址，代码会
拼接 /api/v1/remote 路径。Runner 地址是支持 POST /runs、GET /runs/{id} 的执行服务根地址。

## 重试与升级顺序

新任务默认 single_target_v1，首个领取目标在新运行重试时仍保持绑定。网络轮询错误恢复
同一个 run_id；明确终态失败可在相同目标创建下一运行，默认最多 3 次。旧
throughput_first_v1/local_first_v1 任务继续采用其已持久化的策略，不能凭资源池推断原目标。

本次不新增迁移、不改表/字段，也不批量回填历史数据；复用 main 已有的 001–008 结构。
旧单目标重试如果丢了绑定，领取 SQL 会只读查询同一 generation 最后一次已确认失败且
可重试的 attempt，只允许其中记录的目标 ID/revision/pool 领取。这个判断也适用于 draining，
不会要求原目标重新接收新任务；只有正常领取的那条任务会更新状态和绑定。
无法证明归属的任务继续留在 retry_wait，并提示 worker_retry_lineage_missing，需核对
attempts/远端状态，不能任意指向 Runner 或从头重跑。

1. 确认当前环境已使用 main 的 001–008 库结构，保存已部署镜像和配置。
2. 暂停旧消费者的新领取，保留在途运行的目标身份和凭据；统一替换旧消费者。
3. 已有 008 环境更新时跳过迁移服务，不需要执行额外 SQL：

   ```bash
   scripts/compose.sh up -d --build --no-deps api worker commercial-worker profile-runner-worker
   ```

4. 原生画像与邀约组 active；擎天两组保持 draining，使用原 ID/revision 恢复存量。
5. 核对后端各 5 个槽位和执行服务总并发 10，观察成功、耗时、业务回写及模型限流。

首次建库仍使用已有 migrate 服务执行 001–008；本次功能不增加其 SQL 文件。回滚时恢复
上一个已验证的镜像/配置并保留原数据库和尝试历史，不需要本次功能的数据库回滚。

完全迁移后，让旧画像组进入 draining，直到该目标的 processing/retry_wait 及失败通知均完成
再停止。邀请若仍走擎天，应继续保留邀请目标、客户端和公共平台配置。

## 运行观测与验证

/health 保留既有 HTTP 200 响应体；/ready 在数据库不可用时返回 503，供 readiness 使用。
Worker 汇总包含消费范围内的未绑定队列和本目标全部已绑定任务，缩小范围不会隐藏待排空任务。
排队年龄使用 queued_at，处理年龄使用远端运行开始
时间/started_at，租约心跳不重置耗时。恢复扫描由独立维护循环触发，不依赖执行槽空闲。
Compose 副本用各自容器 hostname 区分日志文件，避免多个进程轮转同一文件。

```bash
pytest tests/unit -q
# 以下仅连接明确指定的临时本地 MySQL，自动创建并删除随机测试库：
RUN_WORKER_MYSQL_INTEGRATION=1 WORKER_TEST_MYSQL_PORT=3307 pytest -m worker_mysql_integration -q
```

集成测试只接受 loopback 地址，使用 WORKER_TEST_MYSQL_HOST/PORT/USER/PASSWORD，
不会使用业务 MYSQL_* 或读取生产 env；需要测试账户拥有创建/删除临时测试库的权限。
