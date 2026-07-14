# 08 · 测试

## pytest 配置

`pyproject.toml [tool.pytest.ini_options]`：

```toml
testpaths = ["tests"]
pythonpath = ["."]
markers = [
  "integration",
  "mysql_integration",
  "dify_integration",
  "api_http_e2e",
]
```

未被标记的测试默认会跑；要跑被标记的集成测试需要显式 `-m` 选中并且打开对应的环境变量。

## 测试布局

```
tests/
├── unit/
│   ├── test_api_health.py             /health 路由（不打真 DB，靠 stub）
│   ├── test_submit_task_api.py        提交接口正常 + 各种 400/503
│   ├── test_query_apis.py             列表 + 单查
│   ├── test_task_service.py           TaskService 用 in-memory repo
│   ├── test_state_machine.py          ALLOWED_TRANSITIONS
│   ├── test_dify_client.py            httpx-mock / monkey-patch streaming + polling
│   ├── test_dify_output_parser.py     normalize + required 校验
│   ├── test_worker_executor.py        execute_once 全分支
│   ├── test_worker_runner.py          loop + 终止
│   ├── test_settings.py               load_config 校验缺/默认值
│   ├── test_logging_utils.py          redact_config 不泄露 secret
│   ├── test_contract_registry.py      load_*  + validate_contract_baseline
│   └── test_migration_runner.py       简单跑 SQL（如果可用）
├── integration/
│   ├── test_real_mysql_dify.py        @pytest.mark.mysql_integration / dify_integration
│   ├── test_api_http_curl_e2e.py      @pytest.mark.api_http_e2e；要 RUN_API_HTTP_E2E=1
│   └── dify_debug_probe.py            手动探针，不在自动测试里
└── test_data/                          fixture 文件
```

## 跑测试的常用命令

```bash
# 全部单测
pytest tests/unit -v

# 单文件 / 单 case
pytest tests/unit/test_task_service.py -v
pytest tests/unit/test_task_service.py::test_submit_task -v

# 集成测试需要外部资源 + 显式 marker
pytest -m mysql_integration -v   # 需要 .env 指向真 MySQL
pytest -m dify_integration  -v   # 需要 .env 指向真 Dify
pytest -m api_http_e2e      -v   # 需要先启动 API 进程; 设置:
                                 #   RUN_API_HTTP_E2E=1
                                 #   API_E2E_BASE_URL=http://127.0.0.1:8000
```

集成测试**不会无意中触发**——既要 `-m` 选中，又要环境变量正确设置；这是设计：用单测保证逻辑正确，用集成测试验证真实连通。

## 测试哲学（看完代码后总结）

- **不 mock DB 的内部 SQL** —— `MySQLTaskRepository` 是数据库交互的边界，单测改成用 in-memory fake repository 来测 `TaskService`，集成测试才走真 MySQL。
- **DifyClient 单测 mock 的是 httpx**，验证多次响应分支（SSE 拿到 / 拿不到 run_id / poll 超时等）。
- **API 单测用 FastAPI `TestClient`**，构造一个用 fake repo + fake DifyClient 的 app 实例。

## 工具脚本

`scripts/api_e2e_curl.sh`：一个纯 bash + curl 的脚本，按 `submit → list → result(loop until terminal)` 的顺序跑一遍，方便在容器/远端机器上"是否通"。等价于 `tests/integration/test_api_http_curl_e2e.py` 的简化版。
