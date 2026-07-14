# Phase 04 执行 Prompt：查询接口完善

## 目标与边界

本阶段完成面向前端的查询闭环：列表查询与结果查询三态响应。

- 目标：
  - 实现列表查询（手机号前缀 + 分页 + 默认排序）
  - 实现结果查询（精确手机号 + succeeded/in-progress/failed 三态）
  - 完成统一响应体与错误码映射
- 边界：
  - 不新增业务表与状态
  - 不修改 OpenAPI 既定路径与响应语义
  - 不重构 Worker 主流程

## 强制读取文件

- `contracts/customer-profile-api.openapi.yaml`
- `contracts/customer-profile-output.schema.json`
- `db/migrations/001_create_customer_profile_tasks.sql`
- `docs/mysql-schema-design.md`

## 输入上下文与实施约束

- 输入上下文：
  - 列表接口路径：`GET /v1/customer-profile/tasks`
  - 结果接口路径：`GET /v1/customer-profile/tasks/{customer_phonenumber}/result`
  - 查询维度：前缀匹配和精确匹配，均基于 `customer_phonenumber`
- 实施约束：
  - 列表响应不得返回大块 `output`，仅返回轻量字段
  - 默认按 `updated_at DESC` 排序
  - 结果查询状态映射必须与 OpenAPI 响应码一致（`200/202/422/404`）

## 产出清单

- 列表查询 API（参数校验、分页、响应映射）
- 结果查询 API（三态分支、错误码与消息映射）
- Repository 查询实现（前缀检索、总数统计、精确查询）
- API 层测试（参数错误、分页边界、状态映射）

## 验收标准

- 列表查询可按前缀返回任务，且分页参数生效
- 列表项字段符合 `TaskListItem`，不包含完整 `output`
- 结果查询在 `succeeded` 时返回 `output`，在 `queued/processing` 返回 `202`，在 `failed` 返回 `422`
- 不存在任务时返回 `404 TASK_NOT_FOUND`
- 所有响应结构与 OpenAPI 契约保持一致

## 自检清单

- [ ] `phone_prefix`、`page`、`page_size` 参数校验符合合同
- [ ] 前缀查询使用左匹配，结果顺序正确
- [ ] 三态响应码与 `code` 字段映射正确
- [ ] 查询接口错误码不与业务失败码混淆

## 输出格式要求（执行后汇报）

1. 变更摘要（控制器/服务/仓储/测试）
2. 查询行为与响应映射说明
3. 合同一致性验证结果
4. 剩余缺口与后续优化建议
