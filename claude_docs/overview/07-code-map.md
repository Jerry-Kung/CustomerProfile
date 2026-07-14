# 07 · 源码地图

## 树状概览（带职责）

```
customer_profile_backend/
├── pyproject.toml                project metadata, entry-point scripts, pytest config
├── Dockerfile                    单镜像同时跑 api / worker，命令通过 compose 覆盖
├── docker-compose.yml            两个服务 (api / worker) + 共享 volume task_uploads
├── contracts/
│   ├── customer-profile-api.openapi.yaml   API 契约权威源
│   └── customer-profile-output.schema.json  Dify 输出契约（仅 required 强约束）
├── db/migrations/
│   ├── 001_create_customer_profile_tasks.sql  baseline 建表
│   └── 002_add_task_image_columns.sql         追加 wechat_*_image 列（幂等）
├── docs/                         设计文档（中文），见 README.md 索引
├── scripts/api_e2e_curl.sh       一个 curl 跑通三接口的脚本
├── src/
│   ├── api/                      ── FastAPI 层
│   │   ├── app.py                create_api_app(): 装中间件 / 异常处理 / 路由
│   │   ├── routes_tasks.py       三个业务路由的处理函数
│   │   ├── schemas.py            Pydantic 请求/响应模型
│   │   ├── errors.py             AppError(code, message, status_code, details)
│   │   └── middlewares.py        request_log_middleware (缓存 raw_body + 计时)
│   ├── app/                      ── application/composition root
│   │   ├── bootstrap.py          DI 装配：config → db → service → executor
│   │   ├── asgi.py               uvicorn ASGI 入口 (app = create_application())
│   │   ├── run_api.py            customer-profile-api 入口脚本
│   │   ├── run_worker.py         customer-profile-worker 入口脚本
│   │   └── run_migration.py      customer-profile-migrate 入口脚本（只跑 001）
│   ├── config/
│   │   ├── settings.py           load_config() + 全部 dataclass config
│   │   └── logging_utils.py      get_logger() + redact_config() + log_config_summary()
│   ├── contracts/
│   │   ├── registry.py           ContractPaths / load_openapi / load_output_schema
│   │   └── validate_contracts.py customer-profile-check-contracts 入口
│   ├── domain/                   ── 纯 Python，无 I/O
│   │   └── task/
│   │       ├── models.py         TaskRecord 数据类
│   │       ├── repository.py     TaskRepository (typing.Protocol)
│   │       ├── service.py        TaskService 业务用例
│   │       ├── state_machine.py  ALLOWED_TRANSITIONS + can_transition()
│   │       └── errors.py         TaskQueueFullError
│   ├── infrastructure/           ── domain Protocol 的具体实现
│   │   ├── db/
│   │   │   ├── client.py         MySQLClient (薄包装 mysql.connector.connect)
│   │   │   ├── task_repository.py  MySQLTaskRepository (核心 SQL)
│   │   │   └── migration_runner.py 跑单个 SQL 文件
│   │   ├── dify/
│   │   │   ├── client.py         DifyClient (SSE+blocking+poll 三段)
│   │   │   ├── output_parser.py  解析+规范化+校验 Dify 输出
│   │   │   └── errors.py         DifyError 家族 (code 类属性)
│   │   ├── storage/
│   │   │   └── task_upload_store.py  TaskUploadStore.save_optional() 保存图片
│   │   └── tos/
│   │       ├── client.py         火山引擎 TOS 上传/预签名 (未集成进 bootstrap)
│   │       └── errors.py
│   └── worker/
│       ├── runner.py             WorkerRunner: 多线程主循环 + graceful stop
│       └── executor.py           WorkerExecutor.execute_once(): 单任务处理
└── tests/
    ├── unit/                     pytest 单测
    ├── integration/              pytest 标记隔离的真实集成测试
    └── test_data/                测试夹具
```

## 关键依赖反转关系

```
TaskService ──depends on──▶ TaskRepository (Protocol)
                                  ▲
                                  │ implements
                            MySQLTaskRepository

WorkerExecutor ──depends on──▶ TaskService
                              + DifyClient
                              + DifyOutputParser
```

`TaskService` 不知道 MySQL；`WorkerExecutor` 不知道 HTTP；`DifyClient` 不知道 task 表。**改动时保持这种无知**。

## 常去的入口

| 你要做的事 | 先看 |
|---|---|
| 改接口/响应 schema | `src/api/schemas.py` + `src/api/routes_tasks.py` + `contracts/customer-profile-api.openapi.yaml` |
| 改任务状态机 | `src/domain/task/state_machine.py` + `MySQLTaskRepository.{claim_next_queued, mark_*}` |
| 改 Dify 调用方式 | `src/infrastructure/dify/client.py` |
| 改 Dify 输出契约 | `contracts/customer-profile-output.schema.json` + 必要时 `output_parser.normalize_profile_output_dict` |
| 加 env 变量 | `src/config/settings.py` + `logging_utils.redact_config` |
| 加迁移 | `db/migrations/NNN_*.sql`，手动 `mysql < ...`（`run_migration.py` 当前只跑 001） |
| 加新错误码 | `src/infrastructure/dify/errors.py`（如果是 Dify 类）+ `routes_tasks._extract_error_code()` 的白名单 + OpenAPI |
