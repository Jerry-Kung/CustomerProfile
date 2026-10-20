# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project

Current documentation starts at `docs/README.md`; historical notes live under `docs/archive/` and are not implementation instructions. Operational tools are documented in `scripts/README.md`.

MVP backend for "customer profile" tasks: a FastAPI HTTP API accepts task submissions (phone number + optional WeChat images), persists them to MySQL, and a separate Worker process polls the queue, invokes the selected Dify, QingTian, or Runner workflow, validates its output, and writes results back. Recoverable adapters persist remote run IDs and use fenced leases. API and Worker remain separate process types. One Docker Compose file starts migration, API, local Worker, commercial Worker, and Runner Worker together.

The repo language is Python 3.11+. Documentation under `docs/` is in Chinese; code/comments are English.

## Commands

Install (editable, with dev extras):

```bash
pip install -e ".[dev]"
```

Entry points (defined in `pyproject.toml [project.scripts]`):

| Command | What it runs |
|---|---|
| `customer-profile-api` | HTTP API (uvicorn, reads `API_HOST`/`API_PORT`) — `src.app.run_api:main` |
| `customer-profile-worker` | Queue consumer loop — `src.app.run_worker:main` |
| `customer-profile-migrate` | Scans `db/migrations/*.sql` and applies every file in filename order (each is idempotent — `CREATE TABLE IF NOT EXISTS` / `information_schema` checks) — `src.app.run_migration:main` |
| `customer-profile-check-contracts` | Validates OpenAPI + JSON Schema in `contracts/` — `src.contracts.validate_contracts:main` |

ASGI entry for container/uvicorn parity: `uvicorn src.app.asgi:app --host 0.0.0.0 --port 8000`.

Tests (pytest configured in `pyproject.toml`, `pythonpath=["."]`):

```bash
pytest tests/unit -v                    # unit tests
pytest tests/unit/test_task_service.py::TestName -v   # single test
pytest -m mysql_integration             # real MySQL
pytest -m dify_integration              # real Dify
pytest -m api_http_e2e                  # against a running API (see below)
```

Custom markers: `integration`, `mysql_integration`, `dify_integration`, `api_http_e2e`. Integration tests are opt-in via env (e.g. `RUN_API_HTTP_E2E=1`, `API_E2E_BASE_URL=http://127.0.0.1:8000`). There is no lint/format config — don't add one without asking.

Docker:

```bash
docker compose up -d --build            # builds and starts API and all three Workers
HOST_API_PORT=8080 docker compose up -d # override host port mapping
```

`.env` at repo root is auto-loaded by `src/config/settings.py` via `python-dotenv` (does not override OS env). Required keys: `MYSQL_*` and the credentials/URLs for the selected workflow platform. The read-only database inspection tool uses `load_mysql_config()` without workflow credentials. Normal application and migration startup use `load_config()`. Compose additionally requires `PROFILE_RUNNER_BASE_URL_PRODUCTION`; it overrides the Runner container to `WORKFLOW_PLATFORM=runner`. See `docs/deploy-and-run.md` for the full list.

## Architecture

**Two-process split, single codebase.** Both processes share `bootstrap()` in `src/app/bootstrap.py`, which wires config → MySQL client → repository → `TaskService` → (for the worker) the selected workflow client + `WorkflowOutputParser` + `WorkerExecutor`. The API process only uses the `TaskService` write/read paths; the Worker process drives `WorkerExecutor`. The DB is the queue — there is no broker.

Layering (strict, follow when adding code):

- `src/api/` — FastAPI app, routes (`routes_tasks.py`), Pydantic schemas, error mapping, middleware. Knows nothing about MySQL/Dify directly.
- `src/domain/task/` — `TaskService`, `TaskRepository` protocol, `TaskRecord` model, `state_machine.py`, domain errors. Pure Python; no I/O.
- `src/infrastructure/` — concrete adapters: `db/` (MySQL client + `MySQLTaskRepository` implementing the domain protocol), `dify/`, `qingtian/`, and `runner/` (platform HTTP adapters), `workflow/` (shared protocol + output parser against `contracts/customer-profile-output.schema.json`), `storage/` (local upload dir), `tos/` (Volcano Engine object storage).
- `src/worker/` — `WorkerExecutor.execute_once()` claims one task and runs the selected workflow call; `WorkerRunner` drives it on threads with configured `concurrency`.
- `src/config/` — `load_config()` returns a frozen `AppConfig` of frozen sub-configs. All env access goes through here; do not read `os.getenv` elsewhere.
- `src/contracts/` — loads/validates the OpenAPI spec (`customer-profile-api.openapi.yaml`) and the Dify output JSON Schema; the parser uses the schema at runtime.

**Task state machine** (`src/domain/task/state_machine.py`): `queued → processing → {retry_wait, succeeded, failed}` and `retry_wait → {processing, failed}`. Terminal states are sinks within an execution generation; resubmission is handled by the queue upsert. Transitions are enforced by `can_transition`. New states require updating this map and the repository's claim/update SQL together.

**Queue mechanics.** `MySQLTaskRepository.claim_next_queued` uses `SELECT ... FOR UPDATE SKIP LOCKED` so multiple Worker threads/processes can run concurrently with `WORKER_CONCURRENCY > 1`. `TASK_QUEUE_MAX_WAITING` caps queued depth separately for each task type on submit; over-cap submissions raise `TaskQueueFullError`, mapped to an API error by `src/api/errors.py`.

**Upsert-on-submit.** Submitting a task for the same (`customer_phonenumber`, `task_type`) re-queues the existing row rather than inserting a new one — see `repository.upsert_queued`. There is no separate "retry" endpoint; resubmission is the retry.

**Image handling.** Submissions can be multipart with `wechat_*_image` files. `TaskUploadStore` writes them under `TASK_UPLOAD_DIR` (defaults to `<repo>/data/task-uploads`). When `DIFY_IMAGE_MODE=file_id` (default), the Worker must be able to read the same paths — in Docker that means api+worker share the same volume.

**Runner queue consumption.** Eligible Workers compete for the same unbound queue through `FOR UPDATE SKIP LOCKED`; free threads claim work up to each Worker's configured concurrency. There is no percentage split or profile spillover delay. Retired `PROFILE_ROUTING_*` and `PROFILE_RUNNER_TRAFFIC_PERCENT` settings are ignored. Runner has an independent target ID/revision, submits `customer_profile_pipeline`, handles customer profiles only, and leaves invitation tasks to the existing workflow. Bound remote runs still resume on their original target/revision. See `docs/profile-runner-integration.md`.

**Dify integration.** `DifyClient` calls the configured `DIFY_RUN_URL` (streaming or blocking depending on the workflow), with separate API keys per `work_env` (`test` vs `production` — chosen per submission). `WorkflowOutputParser` validates the workflow output against `contracts/customer-profile-output.schema.json`; schema changes must be made there, not in code.

**Migrations.** SQL files in `db/migrations/`, applied in numeric order by `customer-profile-migrate` (scans `*.sql`, splits each file on `;` — respecting `--` line comments and single-quoted strings — and runs the statements on one shared connection so `SET @var ... PREPARE ... EXECUTE` patterns in 002/003 keep their session-level variables). `multi=True` is not used: `mysql-connector-python` 9.x removed it. All files are idempotent, so the runner is safe to re-run. In `docker compose`, a dedicated `migrate` service runs once at startup; `api` and `worker` wait on its successful exit. When adding a migration, also update `docs/deploy-and-run.md` §5.

## Conventions worth knowing

- All env reads go through `src/config/settings.py`; raise `ValueError` for missing required vars there, not at call sites.
- Repository methods return `TaskRecord` dataclasses, never raw dicts/rows.
- Logging uses `src/config/logging_utils.get_logger(name)`. Worker `idle` ticks log at DEBUG (not INFO) to avoid log spam — keep it that way.
- Domain errors live in `src/domain/task/errors.py` and are translated to HTTP at the API boundary in `src/api/errors.py`. Don't raise HTTPException from the domain layer.
