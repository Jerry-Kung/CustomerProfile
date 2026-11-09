# 异步架构决策（async-architecture）

> 历史资料：记录编写时的设计与操作方式。当前实现和部署请从[文档导航](../../README.md)进入。


## 决策结论

- **异步载体选型**：采用 **DB 驱动 Worker（MySQL 任务表 + 轮询/抢占）**，不引入外部消息队列作为第一阶段依赖。
- **提交接口返回语义**：`POST` 提交接口仅做“接收 + 入库 + 触发异步执行”，不等待 Dify 最终结果，返回“任务已接收”的同步确认。

## 为什么选 DB 驱动 Worker

在当前仓库范围内，已具备：

- 任务主表迁移：`db/migrations/001_create_customer_profile_tasks.sql`
- 关键状态字段：`queued` / `processing` / `succeeded` / `failed`
- 唯一业务键：`customer_phonenumber`

因此 DB 驱动 Worker 可以最小成本落地，并满足以下要求：

- 避免 10-15 分钟 Dify 调用阻塞 HTTP 请求；
- 利用现有数据库状态实现可追踪、可恢复、可审计；
- 支持同手机号幂等覆盖（upsert）与失败重试扩展；
- 无需新增 MQ 运维组件即可上线第一版。

## 提交接口（POST）同步返回语义

### 接口行为

1. 校验入参（手机号必填；图片可选）。
2. 执行 upsert：
   - 将记录状态置为 `queued`；
   - 清空旧结果字段（`output`、`card_id`、`nickname`、`error_message`）。
3. 触发异步处理（由 Worker 扫描 `queued` 任务并消费）。
4. HTTP **秒级返回**，不等待 Dify 结果。

### 推荐返回码与响应体

- `202 Accepted`：任务已接收并进入异步处理（推荐）。
- `200 OK`：若历史兼容要求必须使用 200，也应表达“已接收、未完成”语义。

推荐响应体（示例）：

```json
{
  "customer_phonenumber": "13800138000",
  "status": "queued",
  "message": "task accepted"
}
```

## Worker 执行语义（与接口配套）

- Worker 周期性扫描 `queued` 记录，按批次抢占并置为 `processing`。
- 调用 Dify 成功后写回：
  - `status = succeeded`
  - `output`、`card_id`、`nickname`
- 失败时写回：
  - `status = failed`
  - `error_message`

> 若后续需要更强吞吐/解耦，可在不改 API 语义的前提下，从 DB 驱动平滑迁移到 MQ（提交接口仍保持 `accepted` 语义）。

## 状态机定义（最小闭环）

`queued -> processing -> succeeded | failed`

约束：

- 仅 `queued` 可被 Worker 抢占；
- `processing` 超时可通过补偿任务回收（后续实现）；
- 重复提交同手机号时，允许将状态重置为 `queued` 触发新一轮分析。

## 与现有文档对齐

- 与 `docs/archive/initial-design/mysql-schema-design.md` 的 upsert 和查询模板一致；
- 与 `docs/archive/initial-design/dify-contract-alignment.md` 的“后台等待/轮询”策略一致；
- 补全了“异步载体最终选择”与“提交接口同步返回语义”两个待定项。
