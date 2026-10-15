# 运维与调试脚本

从项目根目录执行。先安装 `pip install -e ".[dev]"`，配置项目根目录的 `.env`。数据库和 Dify 配置通过 `src/config/settings.py` 加载，脚本不保存固定的数据库连接信息。

## 提交单个测试任务

```bash
python scripts/submit_task.py --phone 13800138000 --batch-id manual-test
```

默认访问 Compose API `http://127.0.0.1:18008`，默认使用 `test` 环境。裸跑 API 时可传 `--base-url http://127.0.0.1:8000`。任务类型通过 `--task-type customer_profile|invitation_strategy` 选择，生产环境需要显式传 `--work-env production`。

脚本以表单提交手机号、批次、任务类型与环境。返回码为 0 表示 HTTP 请求成功，1 表示网络或 HTTP 错误，2 表示命令行参数错误。

## 只读查询任务

```bash
python scripts/inspect_phone_tasks.py --phone 13800138000
python scripts/inspect_phone_tasks.py --task-id 123 --task-id 456 --limit 20
```

数据库连接使用 `.env` 中的 `MYSQL_*`，无需声明未使用的工作流密钥。查询必须提供手机号或任务 ID，结果数限制在 1–1000 之间；同时提供两类条件时按 OR 查询。输出包括任务状态、目标、运行 ID、重试次数和错误信息，不查询完整画像输出。

## Dify 手动探针

```bash
WORKFLOW_PLATFORM=dify python scripts/debug/dify_probe.py --work-env test
```

探针使用当前项目 `.env`，需要完整的 Dify 与 MySQL 配置，按环境选择 `DIFY_API_KEY_TEST` 或 `DIFY_API_KEY_PRODUCTION`，读取 `tests/test_data/` 的手机号和图片。它会实际上传图片并诊断提交、兼容调用和轮询过程，属于手动联调工具，不纳入 pytest 自动测试，也不经过 backend 队列。

## Helm 工具

`generate_helm_production_values.py` 从导出的生产清单生成私密 values；`build_helm_package.py` 构建 Chart 包。参数、配置层次和升级步骤见 [Helm 部署与升级](../docs/helm-deploy.md)，生成的私密配置及安装包保持 Git 忽略。

## API HTTP 验证

```bash
API_E2E_BASE_URL=http://127.0.0.1:18008 bash scripts/api_e2e_curl.sh
```

此脚本会向已运行的 backend 提交测试任务。数据库迁移统一使用 `customer-profile-migrate` 或 `docker compose run --rm migrate`；旧的 003 专项迁移脚本已移至[历史归档](../docs/archive/tools/apply_migration_003.py.txt)。

上述 Python 工具均可先通过 `--help` 查看参数。
