# Customer Profile Backend — Overview Docs

本目录是 Claude Code 在本项目继续工作时的**首要参考输入**。文档基于 2026-05 时点的代码与 `docs/` 资料整理；当代码与本文档冲突时，**以代码为准**。

## 阅读顺序

1. [`01-business-and-scope.md`](01-business-and-scope.md) — 业务问题、核心用例、异步语义
2. [`02-architecture.md`](02-architecture.md) — 进程划分、调用流、部署拓扑
3. [`03-api-contract.md`](03-api-contract.md) — 三个对外接口的契约、错误码、HTTP 状态码
4. [`04-data-model.md`](04-data-model.md) — `customer_profile_tasks` 表、状态机、队列机制
5. [`05-worker-and-dify.md`](05-worker-and-dify.md) — Worker 主循环、Dify 协议、输出解析
6. [`06-configuration.md`](06-configuration.md) — 环境变量与 `AppConfig` 字段映射
7. [`07-code-map.md`](07-code-map.md) — 源码目录与职责对照
8. [`08-testing.md`](08-testing.md) — 单测/集成测试布局、pytest 标记
9. [`09-deployment.md`](09-deployment.md) — 本地、Docker、迁移
10. [`10-operational-notes.md`](10-operational-notes.md) — 日志、可观测性、常见故障定位

## 一句话项目摘要

一个 **FastAPI + 独立 Worker** 的两进程后端：HTTP API 接收 "对一个手机号生成客户画像" 的任务请求，将其作为一行 MySQL 记录入队；Worker 进程轮询 MySQL 队列，调用 Dify 工作流（10-15 分钟级），把解析后的画像 JSON 写回该行。无消息队列、无 Redis，**MySQL 既是状态库又是任务队列**。

## 编码与协作约定（写代码前务必先看）

- **分层严格**：`api → domain → infrastructure`。`domain` 是纯 Python，不做 I/O；`infrastructure` 实现 `domain` 定义的 `Protocol`。详见 [`02-architecture.md`](02-architecture.md)。
- **所有环境变量都从 `src/config/settings.py` 出**，不要在其它地方 `os.getenv`。
- **状态转换走 `src/domain/task/state_machine.py`**：`queued → processing → {succeeded, failed}`，终态是吸收态，无重试边。
- **失败有错误码**：Worker 把 Dify 错误归一为 4 个稳定错误码（`DIFY_REQUEST_FAILED` / `DIFY_TIMEOUT` / `DIFY_OUTPUT_INVALID_JSON` / `DIFY_OUTPUT_SCHEMA_MISMATCH`），API 通过前缀匹配从 `error_message` 还原 `code` 字段返回前端。
- **API 文档语言**：`docs/` 是中文，代码注释/标识符是英文，保持现状。
