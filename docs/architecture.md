# 当前架构

本说明对应当前工作区的实现。完整配置与发布步骤见[部署运行指南](deploy-and-run.md)，接口字段见[前端接口指南](frontend-api-guide.md)。

## 进程与依赖

API 和 Worker 使用同一份 Python 包，通过 MySQL 任务表协作。MySQL 同时保存业务状态和待处理队列，没有额外消息中间件。

- API：校验提交、保存可选图片、调用领域服务入队，并提供列表和结果查询。
- Worker：领取符合任务类型、环境和执行目标要求的任务，调用远端工作流，再写入结果。
- migrate：扫描 `db/migrations/*.sql`，在共享连接上按文件名顺序执行全部迁移。

根目录 `docker-compose.yml` 统一管理 `migrate`、`api`、`worker`、`commercial-worker` 和 `profile-runner-worker`，API 和三个 Worker 都依赖迁移成功退出。画像生成服务由 `PROFILE_RUNNER_BASE_URL_PRODUCTION` 指向外部服务。上传图片通过 `task_uploads` 共享卷提供给 API 和旧图片处理路径；日志写入宿主机 `logs/`。

## 分层

| 目录 | 职责 |
|---|---|
| `src/api/` | FastAPI、请求响应模型、HTTP 错误映射、中间件 |
| `src/domain/task/` | 任务模型、仓储协议、任务服务、状态机和重试策略 |
| `src/infrastructure/db/` | MySQL 连接、任务仓储和迁移执行 |
| `src/infrastructure/dify/` | Dify HTTP 适配 |
| `src/infrastructure/qingtian/` | 擎天提交、查询与错误分类 |
| `src/infrastructure/runner/` | 独立画像生成服务的提交、查询与输出适配 |
| `src/infrastructure/workflow/` | 公共工作流协议与输出解析 |
| `src/worker/` | 执行编排、线程运行器、资源池路由和目标健康管理 |
| `src/config/` | 冻结配置、环境变量加载和日志 |
| `src/contracts/` | OpenAPI、输出 JSON Schema 和迁移路径的加载校验 |

API 通过领域服务访问任务，不直接依赖 MySQL 或某个平台 HTTP 客户端。TOS 适配代码目前没有在 bootstrap 中接入主流程。

## 任务身份与状态

业务唯一键由 `customer_phonenumber` 和 `task_type` 组成。同一手机号的画像与邀约可以共存；重复提交同一业务键会重新排队已有记录并开始新的执行代次。

当前状态转换定义在 `src/domain/task/state_machine.py`：

```text
queued -> processing -> succeeded | failed
                   -> retry_wait -> processing | failed
```

`succeeded` 和 `failed` 是执行状态机的终态；重新提交由独立的入队路径处理。`retry_wait` 是内部等待状态，对结果查询仍映射为 HTTP 202 和 `processing`。

`TASK_QUEUE_MAX_WAITING` 按任务类型分别限制 queued 深度。领取使用事务和 `FOR UPDATE SKIP LOCKED`，支持多个 Worker 线程或进程并发消费。

## 工作流与恢复

`bootstrap()` 根据 `WORKFLOW_PLATFORM` 选择 Dify、QingTian 或 Runner 客户端，并通过统一输出解析器将结果对齐到 `contracts/customer-profile-output.schema.json`。共享 `.env` 选择旧 Worker 的 Dify 或擎天平台；Compose 为 Runner 服务单独覆盖为 `runner`。

擎天和 Runner 客户端提供可恢复的提交 / 查询路径。Worker 通过 generation、claim token 和租约保护回写；远端运行 ID 持久化后，只由相同目标 ID 和 revision 恢复查询。提交结果不明时不会盲目重新生成运行。

Runner 使用独立目标 `profile-runner-production`，通过 `customer_profile_entry` 提交原任务的手机号和批次，只处理生产环境客户画像。邀约继续由原工作流处理。Runner 的并发默认 1，生成服务自行排队执行。

旧 Dify 路径保持自身调用方式。具体目标模式、重试上限、资源池切换和升级顺序以[部署运行指南](deploy-and-run.md)为准。

## 画像首次分流

`PROFILE_ROUTING_MODE=parallel` 是兼容默认值。全部画像 Worker 升级后，可统一启用 `weighted`：对配置环境中具有有效中国手机号及 1–50 字符批次的画像任务，按手机号、批次和环境计算稳定分配桶。`PROFILE_RUNNER_TRAFFIC_PERCENT` 默认 80，表示 Runner 首次分配占比；旧工作流组合计 20%。

默认等待 300 秒后另一组可接手未提交任务；失败重试仍沿用原目标选择和次数限制，因此最终完成比例可能偏离首次分配比例。测试环境、邀约和不符合 Runner 入参要求的任务继续走旧 Worker。配置、升级和回滚步骤见 [Runner 接入与部署](profile-runner-integration.md)。

## 数据库与契约维护

001–006 迁移共同构成当前数据库升级历史，应全部保留。新增迁移需要保持幂等，并更新部署运行指南的迁移章节。

接口定义维护在 `contracts/customer-profile-api.openapi.yaml`，工作流输出维护在 `contracts/customer-profile-output.schema.json`。修改实现时同时检查相应契约和已有测试。
