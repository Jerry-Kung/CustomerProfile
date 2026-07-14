# 01 · 业务范围与异步语义

## 业务问题

给定一个客户的 **手机号**（可选附加微信主页/朋友圈截图），调用一个 Dify 工作流跑出该客户的**画像 JSON**（必含 `card_id`、`nickname`，可含 `confidence`、`summary`、`tags` 等）。Dify 单次工作流可能耗时 **10–15 分钟**，所以接口必须是**异步**的。

业务实体只有一个：**画像分析任务（task）**，用 `customer_phonenumber` 作为唯一业务键。同一手机号同一时刻**只有一行记录**——重新提交相当于"用新一轮分析覆盖旧结果"，不会产生第二行。

## 三个核心用例

| 用例 | 入口 | 说明 |
|---|---|---|
| 提交画像分析任务 | `POST /v1/customer-profile/tasks` | 入队即返回 `202`，不等 Dify |
| 列出某号段下的任务 | `GET /v1/customer-profile/tasks?phone_prefix=...` | 前端 "历史/检索" 页 |
| 查询某手机号的当前结果 | `GET /v1/customer-profile/tasks/{phone}/result` | 前端轮询入口；按状态返回 200/202/422/404 |

接口契约详见 [`03-api-contract.md`](03-api-contract.md)。

## 异步语义的关键决策

来源：`docs/async-architecture-decision.md`，已落地于代码。

- **不引入 MQ / Redis**。"任务队列"就是 `customer_profile_tasks` 表，Worker 通过 `SELECT ... FOR UPDATE SKIP LOCKED` 抢占 `queued` 行。理由：MVP 阶段已有 MySQL，再加一个中间件运维成本不划算。
- **提交接口秒级返回 `202`**，不等待 Dify 完成；提交时执行 upsert 把旧结果字段清空。
- **重复提交即重试**：同一手机号再次提交会把状态改回 `queued` 并清空 `output / card_id / nickname / error_message`，触发 Worker 重新跑一次。没有独立的"重试"接口。
- **前端通过轮询结果接口拿最终态**（建议 2–5s 间隔，总上限 ~20 分钟）。

## 状态机

```
queued ──claim──▶ processing ──┬──▶ succeeded   (终态)
                               └──▶ failed      (终态)
```

- 仅 `queued` 可被 Worker 抢占。
- `succeeded` / `failed` 是吸收态；只有"重新提交"会把行重置回 `queued`。
- `processing` 行可被 `requeue_stale_processing()` 在超时后回收为 `queued`（带 `error_message = 'PROCESSING_TIMEOUT_RECOVERED'`），但当前**没有任何定时任务调用它**——是一个保留的运维手段。

约束在 `src/domain/task/state_machine.py:1-13` 集中表达。所有 transition 修改必须**同时**更新这张表和 `MySQLTaskRepository` 中的 `claim_next_queued / mark_succeeded / mark_failed` SQL。

## 不在范围内 / 已知边界

- **没有鉴权**。HTTP 接口对内部前端开放，依赖网络层（反向代理、安全组）做边界。
- **没有用户体系**，没有"任务归属"的概念。
- **没有 CORS 严控**，默认 `CORS_ALLOW_ORIGINS=*`（见 `src/api/app.py:32-44`）。
- **没有 rate limiting**。OpenAPI 里写了 `429`，但代码层没有实现限流器；遇到压力靠 `TASK_QUEUE_MAX_WAITING` 兜底。
- **没有 webhook 回调**。前端必须轮询。
- **图片字段当前只 echo 不上传到 Dify**：`DifyClient._resolve_image_inputs()` 仅原样转发 `raw_inputs`，并未真正走 file_id/multipart/url 三种模式中的任何一种；OpenAPI 与 `SubmitTaskRequest` 里保留了 `wechat_*_image` 字段，但 `routes_tasks.py:submit_task` 当前只接收 `customer_phonenumber / work_env / batch_id` 三个表单字段——**图片链路是预留的，未接通**。改之前先确认 Dify 工作流是否真的需要这两张图。
