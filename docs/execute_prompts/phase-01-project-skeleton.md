# Phase 01 执行 Prompt：工程骨架与基础设施接入

## 目标与边界

本阶段完成最小可运行工程骨架，保证 API 与 Worker 可独立启动。

- 目标：
  - 建立目录结构、双入口与依赖装配
  - 完成 MySQL 连接、健康检查与迁移执行入口
  - 建立统一日志和错误处理中间层（最小实现）
- 边界：
  - 不实现完整提交链路与 Dify 真实调用
  - 不改动 OpenAPI、JSON Schema、SQL 迁移合同
  - 不硬编码任何敏感配置

## 强制读取文件

在开始实现前必须读取并遵循以下文件：

- `contracts/customer-profile-api.openapi.yaml`
- `contracts/customer-profile-output.schema.json`
- `db/migrations/001_create_customer_profile_tasks.sql`
- `docs/async-architecture-decision.md`
- `docs/mysql-schema-design.md`
- `docs/dify-contract-alignment.md`

## 输入上下文与实施约束

- 输入上下文：
  - 架构必须采用“Web API + DB 驱动 Worker”双进程模型
  - API 和 Worker 共享领域规则，不复制业务逻辑
  - MySQL 是统一状态源与幂等锚点
- 实施约束：
  - 目录与分层职责需可直接承接后续阶段代码
  - 依赖方向固定：`api/worker -> domain -> infrastructure(接口注入)`
  - 所有配置由环境变量映射并支持脱敏输出

## 后端目标目录结构（冻结版）

```text
src/
  app/                  # 启动装配、生命周期、依赖注入
  config/               # 环境变量映射、配置校验、脱敏输出
  api/                  # HTTP 路由、请求校验、响应映射、错误映射
  domain/
    task/               # 任务聚合、状态机规则、领域服务（纯业务）
  infrastructure/
    db/                 # 连接池、Repository 实现、迁移执行器
    dify/               # Dify 客户端抽象（接口与适配）
  worker/               # 调度循环、抢占策略、重试/超时处理
  contracts/            # 运行期引用的合同文件或镜像
tests/
  unit/                 # 纯领域与工具单测
  integration/          # DB/API/Worker 组合测试
scripts/                # 本地启动、迁移、质量检查脚本
```

## API/Worker 分层职责映射（冻结版）

### API 进程职责

- 只处理短事务：
  - 参数校验（手机号、可选图片字段）
  - 调用领域服务做提交（upsert 为 `queued`）
  - 返回 `accepted` 语义，不等待 Dify
- 不承载：
  - Dify 长耗时轮询
  - 任务状态补偿
  - 复杂重试逻辑

### Worker 进程职责

- 只处理异步执行链路：
  - 扫描并抢占 `queued -> processing`
  - 调用 Dify 客户端并接收结果
  - 解析 output + schema 校验 + 回写 `succeeded/failed`
  - 记录 `error_message` 与错误码分类
- 不承载：
  - HTTP 协议适配
  - 面向客户端的响应组装

### Domain 层职责（API 与 Worker 共享）

- 封装统一业务规则：
  - 状态机约束：`queued -> processing -> succeeded | failed`
  - 同手机号幂等覆盖（upsert + 清理旧结果字段）
  - 结果字段提取（`card_id`、`nickname`）规则
- 保持无框架耦合，避免将 HTTP 或 SQL 细节写入领域层

### Infrastructure 层职责

- DB：
  - 提供任务仓储接口实现
  - 隔离 SQL 细节与事务边界
- Dify：
  - 提供统一客户端接口
  - 规范错误分类：`DIFY_REQUEST_FAILED`、`DIFY_TIMEOUT`、`DIFY_OUTPUT_INVALID_JSON`、`DIFY_OUTPUT_SCHEMA_MISMATCH`

## 进程与模块依赖规则

依赖方向必须固定为：

`api/worker -> domain -> infrastructure(通过接口反向注入)`

强制规则：

- `api` 不得直接依赖 `worker`
- `worker` 不得直接依赖 `api`
- `domain` 不得 import HTTP 框架或数据库驱动
- `infrastructure` 不得反向依赖 `api`

## 产出清单

- 已创建/确认 `src` 顶层目录及核心分层目录
- 提供 API 入口与 Worker 入口（可启动到空循环）
- 提供统一配置加载与脱敏日志输出
- 提供 DB 健康检查与迁移执行入口
- 提供统一错误模型与日志中间层（最小实现）

## 验收标准

- API 进程可启动并返回基础健康检查
- Worker 进程可启动、轮询空队列并优雅停止
- 跨层依赖符合分层规则，无反向耦合
- 配置加载与脱敏输出可验证，且无密钥硬编码
- 迁移执行入口与现有 SQL 基线兼容

## 自检清单

- [ ] API 启动后可返回基础健康检查
- [ ] Worker 启动后可进入轮询空循环并优雅停止
- [ ] 目录结构与本文件一致，无跨层反向依赖
- [ ] 无敏感配置硬编码，真实密钥不入库
- [ ] 关键状态与错误码命名与合同文档一致

## 输出格式要求（执行后汇报）

执行本 Prompt 后，输出必须包含：

1. 变更摘要（新增/修改文件列表）
2. 分层职责说明（API、Worker、Domain、Infrastructure）
3. 验证结果（启动、健康检查、基础测试）
4. 已知风险与下一阶段建议
