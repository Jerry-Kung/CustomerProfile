# 06 · 配置与环境变量

唯一权威实现：`src/config/settings.py`（其它任何文件读 `os.getenv` 都视为越界）。

## 加载顺序

1. `load_config()` 第一次调用时执行 `_load_dotenv_from_project_root()`：从仓库根（`Path(settings.py).parents[2]`）找 `.env`，用 `python-dotenv` 加载到 `os.environ` 但 **`override=False`**——已有的 OS 环境变量优先。
2. 用 `_require_env / _env_int / _env_str` 逐个读，组装成 `AppConfig`（`@dataclass(frozen=True)`）。
3. 通过 `redact_config()` 把脱敏过的 config 打到 `bootstrap` 日志里，便于排查"是否读到了预期的 env"。

## 必填变量（缺一就启动失败）

| Env | 用途 |
|---|---|
| `DIFY_BASE_URL` | 用于拼接 `GET /v1/workflows/run/{id}` 轮询 URL |
| `DIFY_RUN_URL` | 直接 POST 这个 URL 启动工作流（不是从 BASE 拼出来的） |
| `DIFY_API_KEY_TEST` 或 `DIFY_API_KEY` | `work_env=test` 用 |
| `DIFY_API_KEY_PRODUCTION` | `work_env=production` 用 |
| `DIFY_USER_ID` | Dify `user` 字段 |
| `MYSQL_HOST` / `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` | DB 连接 |

## 可选变量与默认值

| Env | 默认 | 说明 |
|---|---|---|
| `APP_ENV` | `local` | 仅打日志 |
| `LOG_LEVEL` | `INFO` | 直接传给 stdlib logging |
| `API_HOST` | `0.0.0.0` | uvicorn bind |
| `API_PORT` | `8000` | **本地默认**；Docker 里 compose 覆盖为 `18008` |
| `MYSQL_PORT` | `3306` | |
| `DIFY_IMAGE_MODE` | `file_id` | 仅日志输出，当前未影响实际行为 |
| `DIFY_STREAM_INIT_TIMEOUT_MS` | `30000` | SSE 首字节超时 |
| `DIFY_POLL_INTERVAL_MS` | `2000` | poll 间隔 |
| `DIFY_POLL_TIMEOUT_MS` | `2400000` | 40 min |
| `WORKER_CONCURRENCY` | `5` | Worker 线程数，必须 ≥ 1 |
| `WORKER_POLL_INTERVAL_MS` | `2000` | 每个 Worker 线程的 idle 节流 |
| `TASK_QUEUE_MAX_WAITING` | `500` | 入队容量上限，必须 ≥ 1 |
| `TASK_UPLOAD_DIR` | `<repo>/data/task-uploads` | API 写、Worker 读；Docker 里默认 `/var/lib/customer-profile/task-uploads` |
| `TASK_UPLOAD_MAX_BYTES_PER_FILE` | `10485760` (10 MiB) | 单个图片上限 |
| `CORS_ALLOW_ORIGINS` | `*` | `,` 分隔；含 `*` 即 wildcard 模式 |
| `TOS_ACCESS_KEY_ID` / `TOS_ACCESS_KEY_SECRET` / `TOS_REGION` / `TOS_ENDPOINT` / `TOS_BUCKET` | `""` | 火山引擎 TOS；仅 `TosClient` 用，**未在 bootstrap 中被构造** |
| `TOS_PRESIGNED_URL_EXPIRES_SECONDS` | `3600` | 同上 |
| `HOST_API_PORT` | `18008` | docker-compose.yml 用，仅影响宿主机映射端口 |

`HOST_API_PORT` 是**仅 docker-compose 读取**的——不进 Python 进程。

## `AppConfig` 结构

```
AppConfig(frozen=True)
├── app_env: str
├── log_level: str
├── dify: DifyConfig
│   ├── base_url, run_url
│   ├── api_key_test, api_key_production (脱敏后打印)
│   ├── user_id, image_mode
│   └── stream_init_timeout_ms, poll_interval_ms, poll_timeout_ms
├── mysql: MySQLConfig (host, port, user, password, database)
├── upload: UploadConfig (base_dir: Path, max_bytes_per_file: int)
├── worker: WorkerConfig (concurrency, poll_interval_seconds [float])
├── tos: TosConfig (...)
└── task_queue_max_waiting: int = 500
```

所有 dataclass 都 `frozen=True`，**禁止运行时改 config**。要"热改"配置就重启进程。

## 添加新配置项的清单

1. 在 `settings.py` 对应 sub-config dataclass 加字段 + 在 `load_config()` 里读；
2. 如果是敏感值，在 `logging_utils.redact_config()` 用 `_mask_secret()` 包一下；
3. 如果是必填，用 `_require_env()`（启动即失败），否则给默认值；
4. 在本文档表格添加一行；
5. 如果跟容器化有关，同步 `.env.example`（如果存在）和 `docker-compose.yml`。
