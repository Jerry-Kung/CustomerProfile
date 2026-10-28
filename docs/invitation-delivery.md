# 邀约最终状态与回写确认

适用分支 `main`；同时部署 customer-profile-api 的 `master` 或已整合的 `develop`。Web 和独立画像生成服务无需改动。

- API 为每次邀约生成独立 `execution_id`，原业务 `batch_id` 不变；backend 入队、青天输入、回调全程透传。
- 相同执行标识重发提交只返回当前任务，不创建新的 generation。更换批次或环境复用标识会返回 409。
- 有执行标识的邀约在青天成功后，需要向 API 的 `GET /api/v1/remote/data/invitation/{phone}/status` 核对当前执行、状态和话术/检核落库。
- 确认未回写的错误为 `INVITATION_WRITEBACK_MISSING`，进入最终失败通知；核对 HTTP/网络不可用时保留原 runId，有界恢复查询，不创建新工作流。
- 最终失败复用 `customer_profile_failure_outbox`，通过 `/callback/invitation-failed` 携带 `execution_id`、`final=true` 重试投递。没有执行标识的历史邀约由 API 审计脚本处理。
- 提交未知、远程未确认、需要核对的任务不自动发失败通知。

先运行 `008_add_invitation_execution_delivery.sql`，再升级 API/全部 Worker；它只新增任务执行标识及通知表的类型、执行标识列，可重复执行。

现有 `PROFILE_FAILURE_CALLBACK_ENABLED`、`PROFILE_API_BASE_URL[_TEST/_PRODUCTION]` 和 `PROFILE_API_KEY[_TEST/_PRODUCTION]` 同时用于画像失败通知、邀约失败通知和邀约回写核对；启用 API 执行标识前必须配置成功。Key 与 API `M2M_API_KEY` 一致。

Compose 普通擎天组默认消费画像和邀约，可通过 `QINGTIAN_WORKER_TASK_TYPES=invitation_strategy`
只消费邀约，再用 `QINGTIAN_WORKER_CONCURRENCY` 单独设置并发。旧
`INVITATION_WORKER_CONCURRENCY` 不再生效；画像由具备相应能力的擎天、商用及 Runner
共同领取。完整配置与迁移见[统一 Worker 配置](worker-configuration.md)。

完整发布顺序、青天修改、API 开关及历史修复命令见 **customer-profile-api 的 `docs/invitation-delivery.md`**：先 API 迁移并保持 `INVITATION_EXECUTION_ID_ENABLED=false` → backend 迁移升级 → 青天新增可选输入并在成功回调原样回传 → API 开启开关 → 验证新任务 → 预览并审计修复旧失败状态。未更新青天前不要开启该开关。
