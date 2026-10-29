# 部署与运行

配置与灰度消费的权威说明见[统一 Worker 配置](worker-configuration.md)，首次部署见
[快速启动](deploy-quickstart.md)。历史 local/commercial 分流阈值不再控制新任务。

## 进程与入口

| 入口 | 职责 | 必需配置 |
|---|---|---|
| customer-profile-api | 提交、列表、查询、health/ready | MySQL；队列和 API 参数 |
| customer-profile-worker | 领取、执行、同目标重试/恢复、失败通知 | MySQL；所选适配器和声明任务/环境的凭据 |
| customer-profile-migrate | 顺序执行全部 SQL 迁移 | MySQL；迁移等待和日志参数 |
| customer-profile-check-contracts | 检查仓库接口契约基线 | 无外部服务 |

API/迁移不构造工作流客户端，不要求工作流密钥。API 的同步数据库提交在线程池运行。
本地 .env 从仓库根目录加载，系统环境优先；日志初始化也使用相同加载顺序。

## 本地运行

```bash
pip install -e '.[dev]'
customer-profile-migrate
customer-profile-api
# 另开终端，先配置统一 Worker 的平台、范围、目标和并发：
customer-profile-worker
```

默认 API_HOST=0.0.0.0、API_PORT=18008、CORS_ALLOW_ORIGINS=*。直接用 uvicorn 启动时，
监听端口由 uvicorn 的 --port 参数控制。CORS 用逗号分隔，可设置为业务域名列表。

## 数据库迁移

customer-profile-migrate 扫描 db/migrations/*.sql，按文件名排序；当前仍为既有 001–008。
本次统一 Worker 不新增迁移、不改表/字段，也不批量修复历史任务。已完成 008 的环境
可按[升级顺序](worker-configuration.md#重试与升级顺序)使用 Compose --no-deps 直接更新 API/Worker，跳过 migrate。

旧单目标重试丢失绑定时，在正常领取事务中读取现有 attempt，只有原目标可以接续；无法
证明归属时不允许其他目标接管。全新未执行任务仍可由任何能力匹配的 active Worker 竞争。
迁移以及业务用户需具备相应表的访问/建表权限。等待参数：MIGRATION_DB_WAIT_MAX_TRIES=30、
MIGRATION_DB_WAIT_INTERVAL_MS=2000。

## 外部服务与任务范围

擎天 URL 需要包含原平台 API 前缀；配置 QINGTIAN_USER_ID 和范围内所需 Key。
Runner URL 指向支持 /runs 的执行 API（宿主机端口可映射为 18000），不使用 8099 入站 API。
Runner 只支持 customer_profile；多种任务必须在适配器能力内声明，不能通过改任务类型绕过校验。

默认 Runner 仅消费 production。APP_ENV 是日志环境标识，不会把上游 work_env=test 改为 production。
需要测试 Runner 时增加独立目标，声明 WORKER_WORK_ENVS=test，并提供 PROFILE_RUNNER_BASE_URL_TEST。

PROFILE_API_BASE_URL/KEY 及其 TEST/PRODUCTION 覆盖项负责业务回调和邀请核验；生产可显式拆分
环境，默认不做地址/密钥隔离。最终失败 outbox 由各 Worker 竞争投递，有界租约保护；原有业务
execution_id 与回写语义见[邀约状态同步](invitation-delivery.md)。
CustomerProfile 高代码邀约的可选 Worker、独立入站密钥及升级步骤见[原生邀约服务接入](native-invitation-runner.md)。

## 接口与结果

三个业务接口：POST/GET /v1/customer-profile/tasks，以及 GET /v1/customer-profile/tasks/{phone}/result。
POST 是表单（urlencoded 或 multipart），不是 JSON body；入队返回 202，队列满返回 503。
当前上传图片未接通，不再初始化目录或共享上传卷。查询契约和执行标识见[前端指南](frontend-api-guide.md)。

共享输出解析器要求合法 JSON 对象，显示用 card_id/nickname 做尽力提取；丰富字段保持原样。
各平台适配器校验自己的响应信封和业务身份。输出 JSON Schema 描述这一宽松对象契约，
不额外限制模型业务字段；调整输出要求时需同时修改适配器、契约和测试。

## 检查与排障

```bash
scripts/compose.sh config -q
scripts/compose.sh logs --tail=100 api worker commercial-worker profile-runner-worker
```

/health 保留原响应，/ready 在 DB 不可用时返回 503。Worker 汇总按范围和目标观察；
worker_retry_lineage_missing 表示现有 attempts 无法证明旧重试的原目标，需核对历史与远端状态，不能清除远端 ID 重跑。
任务停留 queued 时先核对是否存在匹配类型和环境的 active 目标、Runner 业务身份是否有效，
再看目标熔断、容量和平台连接。排空目标仍会恢复自身已绑定运行，应保留其原 URL/凭据。
