# Phase 02 执行 Prompt：提交任务链路（入队与抢占）

> 历史资料：记录编写时的设计与操作方式。当前实现和部署请从[文档导航](../../README.md)进入。


## 目标与边界

本阶段只打通“提交即入队 + Worker 抢占”的主干，不包含真实 Dify 结果处理。

- 目标：
  - 实现提交接口并执行 upsert 重置逻辑
  - 实现 Worker 抢占 `queued -> processing`
  - 验证重复提交幂等行为
- 边界：
  - 不实现 Dify 调用、output 解析、schema 校验
  - 不改动 OpenAPI 合同语义与状态码定义
  - 不引入外部 MQ（保持 DB 驱动 Worker）

## 强制读取文件

- `contracts/customer-profile-api.openapi.yaml`
- `contracts/customer-profile-output.schema.json`
- `db/migrations/001_create_customer_profile_tasks.sql`
- `docs/archive/initial-design/mysql-schema-design.md`
- `docs/archive/initial-design/async-architecture-decision.md`

## 输入上下文与实施约束

- 输入上下文：
  - `POST /v1/customer-profile/tasks` 返回 `202 accepted` 语义
  - 唯一业务键为 `customer_phonenumber`
  - 状态机最小闭环：`queued -> processing -> succeeded | failed`
- 实施约束：
  - 提交必须 upsert，并清理旧的 `output/card_id/nickname/error_message`
  - Worker 仅能抢占 `queued`，抢占后立即写 `processing`
  - 需考虑并发提交/并发抢占的一致性（事务或条件更新）

## 产出清单

- 提交接口实现（参数校验、accepted 响应、错误映射）
- 任务仓储 upsert 方法与字段重置逻辑
- Worker 抢占逻辑（批量或单条均可）
- 最小日志埋点（提交、抢占、状态变更）
- 幂等行为测试用例（重复提交、并发提交）

## 验收标准

- 同手机号重复提交后仅保留一条任务记录
- 重复提交会重置状态为 `queued` 且清理旧结果字段
- Worker 成功将可处理任务从 `queued` 置为 `processing`
- `POST` 接口不等待 Dify，返回延迟不受长任务影响
- 关键响应结构与 OpenAPI `TaskAcceptedResponse` 一致

## 自检清单

- [ ] 正常提交返回 `202` 且 `status` 为 `queued/processing`
- [ ] 非法手机号返回参数错误（`400`）
- [ ] 并发抢占不出现同一任务被重复处理
- [ ] 关键状态字段写库与迁移字段完全一致

## 输出格式要求（执行后汇报）

1. 变更摘要（API/仓储/Worker/测试）
2. 幂等与并发控制设计说明
3. 验证结果（接口测试与数据库状态）
4. 已知风险与下一阶段接口点
