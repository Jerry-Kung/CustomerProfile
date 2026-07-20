# Phase 03 执行 Prompt：Dify 集成与结果回写

## 目标与边界

本阶段完成 Dify 集成、output 解析校验与结果回写，形成核心价值链路。

- 目标：
  - 实现 Dify 客户端调用与错误分类
  - 支持图片参数装配（默认 `file_id` 模式）
  - 解析 output JSON 并按 schema 校验
  - 回写 `output/card_id/nickname/status/error_message`
- 边界：
  - 不扩展 OpenAPI 接口定义
  - 不变更数据库表结构合同
  - 不在本阶段做大规模性能优化

## 强制读取文件

- `contracts/customer-profile-api.openapi.yaml`
- `contracts/customer-profile-output.schema.json`
- `db/migrations/001_create_customer_profile_tasks.sql`
- `docs/dify-contract-alignment.md`
- `docs/async-architecture-decision.md`

## 输入上下文与实施约束

- 输入上下文：
  - Dify 输出以 `output` 字段为权威载体
  - `card_id` 与 `nickname` 为可选快捷字段，缺失或类型不匹配时提取为 `NULL`
  - 错误码需覆盖：`DIFY_REQUEST_FAILED`、`DIFY_TIMEOUT`、`DIFY_OUTPUT_INVALID_JSON`、`DIFY_OUTPUT_SCHEMA_MISMATCH`
- 实施约束：
  - 输出解析流程固定：字符串反序列化 -> schema 校验 -> 字段提取
  - 成功写 `succeeded`，失败写 `failed` + `error_message`
  - 敏感信息（API Key、原始请求敏感字段）不得直接明文日志输出

## 产出清单

- Dify 客户端与请求/响应适配层
- 图片模式选择逻辑（`DIFY_IMAGE_MODE`）
- output 解析器与 schema 校验器
- Worker 回写逻辑（成功/失败分支）
- 错误码映射实现与覆盖测试

## 验收标准

- Dify 成功返回后，任务状态变为 `succeeded` 并写入 `output/card_id/nickname`
- output 非法 JSON 时写 `failed` + `DIFY_OUTPUT_INVALID_JSON`
- output JSON 根节点不是对象时写 `failed` + `DIFY_OUTPUT_SCHEMA_MISMATCH`
- Dify 请求失败或超时时写对应失败码与错误信息
- 结果字段与 OpenAPI `TaskResult*` 响应模型可一致映射

## 自检清单

- [ ] 成功链路可完整从 `processing` 到 `succeeded`
- [ ] 四类 Dify 失败码都可复现并正确落库
- [ ] `card_id`、`nickname` 缺失或异常时任务仍可成功，快捷列写 `NULL`
- [ ] 日志无敏感信息泄漏

## 输出格式要求（执行后汇报）

1. 变更摘要（客户端/解析器/Worker/测试）
2. Dify 适配与错误分类说明
3. 结果回写与字段提取验证结果
4. 未覆盖场景与后续建议
