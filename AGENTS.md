# Repository Agent Instructions

## Current service

Customer-profile task API plus configurable asynchronous Workers sharing a MySQL queue.
Current production adapters are `qingtian` and `runner`; `dify` is legacy compatibility.
Start documentation at `docs/README.md`; `docs/worker-configuration.md` is authoritative for
worker scopes, replicas, credentials, configuration precedence and upgrade/rollback.
Historical documents under `docs/archive/` are reference material, not current instructions.

## Commands

- Install: `pip install -e '.[dev]'` (Python 3.11+).
- Unit tests: `pytest tests/unit -q`.
- Isolated MySQL tests: `RUN_WORKER_MYSQL_INTEGRATION=1 WORKER_TEST_MYSQL_PORT=3307 pytest -m worker_mysql_integration`.
  They accept only loopback and create/drop their own random test database; never use business data.
- Entry points: customer-profile-api, customer-profile-worker, customer-profile-migrate,
  customer-profile-check-contracts (see pyproject.toml).
- Compose: `scripts/compose.sh [--env-file /absolute/path/env] ...` keeps interpolation and
  env_file injection aligned. Use `config -q`; do not print expanded production credentials.
- There is no lint/format configuration; don't introduce one without asking.

## Boundaries

- `src/api`: HTTP validation and error mapping; synchronous database endpoints use FastAPI's thread pool.
- `src/domain/task`: records, protocols, retry policy and domain errors. No HTTP exceptions or platform I/O.
- `src/infrastructure`: MySQL transactions, platform adapters and business callbacks.
- `src/worker`: shared executor, independent maintenance and thread capacity; no provider-specific admission policy.
- `src/app`: component wiring. API and migrations must not require workflow credentials or initialize an executor.
- `src/config/settings.py`: all environment reads, component-scoped validation and dotenv precedence.
  Logging uses its `env_value` helper so dotenv loads before sinks are configured.

The API does not currently accept image fields. Upload/TOS helpers are historical standalone adapters,
not initialized application dependencies. Output parsing requires an extensible JSON object and extracts
optional display metadata; provider-specific identity/envelope validation lives in the adapter. Keep
contracts and tests consistent rather than claiming strict validation of arbitrary model fields.

## Queue and worker invariants

- A Worker group declares platform, task types, work environments, concurrency, target ID/revision and mode.
- Same-target replicas share identical platform/endpoint/credentials/revision; different targets use different IDs.
- `local/commercial` pool metadata is retained for old tasks and old retry budgets, not new-task admission.
- All compatible active targets compete with `FOR UPDATE SKIP LOCKED`; no weights or commercial queue gate.
- New/unbound tasks must match the configured capability scope. Owned runs may drain after scope changes;
  retain their original URL, credentials, target identity and revision until finished.
- New submissions default to `single_target_v1`. New-run retries keep target ID/revision while clearing
  only the previous run ID. Preserve old tasks' persisted retry policy.
- Claimed updates must match task ID + generation + claim token + processing status. Retain these atomic
  database guards when refactoring; the readable state-machine map does not replace transaction guards.
- Submission identity is (phone, task_type), with generation/optional execution_id protecting resubmission.
  Invitation delivery additionally confirms business batch/execution identity before marking success.
- Once a remote run ID is durable, resume that run. Unknown submissions/uncertain side effects must not
  be blindly retried on another target.
- Never reset elapsed-time metrics with lease heartbeats. Use queued_at / run start / started_at for ages.
- Worker idle logs stay DEBUG. Recovery maintenance must work while all execution slots are occupied.

## Configuration and migrations

Root .env loads without overriding OS env. .env.example contains placeholders only. Compose service fields
are explicitly mapped to the uniform runtime contract; per-process concurrency multiplied by replicas
is total configured capacity. Config changes require container recreation, not just restart.

Migrations scan all db/migrations/*.sql in order, executing statements on one connection. Every migration
must be replay-safe; keep the splitter's session-variable semantics. Current migrations are 001–009.
009 restores lost single-target retry bindings from the same generation's latest attempt. A previously
attempted single-target retry without provable lineage must stay blocked and observable, not be guessed
from its local/commercial pool. Update deployment docs whenever adding migrations.

Never modify/push production configuration, run real workflows, or migrate a business database just to
validate code. Prefer isolated tests. Keep passwords, API keys and tokens out of source and tool output.
