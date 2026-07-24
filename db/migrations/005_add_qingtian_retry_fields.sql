-- 005: durable, repository-owned QingTian retry state.
--
-- No QingTian workflow/API change is required.  The service persists the runId
-- it already receives, then can resume polling that same remote run after a
-- worker restart or a transient poll failure.
--
-- Every ALTER is guarded so this migration is safe to re-run.

-- Step 1: permit an internal delayed-retry state.
SET @expand_task_status = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND column_name = 'status'
              AND column_type LIKE '%''retry_wait''%'
        ),
        'SELECT ''status enum already includes retry_wait''',
        'ALTER TABLE customer_profile_tasks MODIFY COLUMN status ENUM(''queued'', ''processing'', ''retry_wait'', ''succeeded'', ''failed'') NOT NULL DEFAULT ''queued'' COMMENT ''task status'''
    )
);
PREPARE stmt1 FROM @expand_task_status;
EXECUTE stmt1;
DEALLOCATE PREPARE stmt1;

-- Step 2: add current-generation / retry ownership fields.
SET @add_generation = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'generation'),
        'SELECT ''generation exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN generation INT UNSIGNED NOT NULL DEFAULT 1 COMMENT ''increments on every user resubmission'' AFTER status'
    )
);
PREPARE stmt2 FROM @add_generation;
EXECUTE stmt2;
DEALLOCATE PREPARE stmt2;

SET @add_execution_backend = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'execution_backend'),
        'SELECT ''execution_backend exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN execution_backend ENUM(''local'', ''commercial'') NOT NULL DEFAULT ''local'' COMMENT ''worker/workflow backend that owns the current run'' AFTER generation'
    )
);
PREPARE stmt_execution_backend FROM @add_execution_backend;
EXECUTE stmt_execution_backend;
DEALLOCATE PREPARE stmt_execution_backend;

SET @add_workflow_run_id = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'workflow_run_id'),
        'SELECT ''workflow_run_id exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN workflow_run_id VARCHAR(128) NULL COMMENT ''platform-neutral remote workflow run id'' AFTER dify_run_id'
    )
);
PREPARE stmt3 FROM @add_workflow_run_id;
EXECUTE stmt3;
DEALLOCATE PREPARE stmt3;

SET @add_attempt_count = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'attempt_count'),
        'SELECT ''attempt_count exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN attempt_count INT UNSIGNED NOT NULL DEFAULT 0 COMMENT ''number of locally attempted workflow submissions'' AFTER workflow_run_id'
    )
);
PREPARE stmt4 FROM @add_attempt_count;
EXECUTE stmt4;
DEALLOCATE PREPARE stmt4;

SET @add_next_retry_at = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'next_retry_at'),
        'SELECT ''next_retry_at exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN next_retry_at DATETIME NULL COMMENT ''earliest time a retry_wait task is eligible'' AFTER attempt_count'
    )
);
PREPARE stmt5 FROM @add_next_retry_at;
EXECUTE stmt5;
DEALLOCATE PREPARE stmt5;

SET @add_last_error_code = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'last_error_code'),
        'SELECT ''last_error_code exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN last_error_code VARCHAR(128) NULL COMMENT ''machine-readable most recent failure code'' AFTER next_retry_at'
    )
);
PREPARE stmt6 FROM @add_last_error_code;
EXECUTE stmt6;
DEALLOCATE PREPARE stmt6;

SET @add_last_error_stage = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'last_error_stage'),
        'SELECT ''last_error_stage exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN last_error_stage VARCHAR(32) NULL COMMENT ''submit, poll, workflow, output, or db'' AFTER last_error_code'
    )
);
PREPARE stmt7 FROM @add_last_error_stage;
EXECUTE stmt7;
DEALLOCATE PREPARE stmt7;

SET @add_lease_expires_at = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'lease_expires_at'),
        'SELECT ''lease_expires_at exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN lease_expires_at DATETIME NULL COMMENT ''retry worker lease expiry'' AFTER last_error_stage'
    )
);
PREPARE stmt8 FROM @add_lease_expires_at;
EXECUTE stmt8;
DEALLOCATE PREPARE stmt8;

SET @add_claim_token = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'claim_token'),
        'SELECT ''claim_token exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN claim_token VARCHAR(64) NULL COMMENT ''UUID fencing token for the active worker claim'' AFTER lease_expires_at'
    )
);
PREPARE stmt9 FROM @add_claim_token;
EXECUTE stmt9;
DEALLOCATE PREPARE stmt9;

SET @add_queued_at = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'queued_at'),
        'SELECT ''queued_at exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN queued_at DATETIME NULL COMMENT ''time current generation was queued'' AFTER claim_token'
    )
);
PREPARE stmt10 FROM @add_queued_at;
EXECUTE stmt10;
DEALLOCATE PREPARE stmt10;

SET @add_started_at = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'started_at'),
        'SELECT ''started_at exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN started_at DATETIME NULL COMMENT ''time current claim started'' AFTER queued_at'
    )
);
PREPARE stmt11 FROM @add_started_at;
EXECUTE stmt11;
DEALLOCATE PREPARE stmt11;

SET @add_finished_at = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'finished_at'),
        'SELECT ''finished_at exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN finished_at DATETIME NULL COMMENT ''time current generation reached a terminal state'' AFTER started_at'
    )
);
PREPARE stmt12 FROM @add_finished_at;
EXECUTE stmt12;
DEALLOCATE PREPARE stmt12;

-- Existing rows predate queued_at.  Preserve their prior age rather than
-- assigning a fresh enqueue time during the migration.
UPDATE customer_profile_tasks
SET queued_at = updated_at
WHERE queued_at IS NULL;

-- Step 3: retain one audit row for every local submission attempt.
CREATE TABLE IF NOT EXISTS customer_profile_task_attempts (
  task_id BIGINT UNSIGNED NOT NULL COMMENT 'customer_profile_tasks.id',
  generation INT UNSIGNED NOT NULL COMMENT 'task generation at submission time',
  attempt_no INT UNSIGNED NOT NULL COMMENT 'one-based workflow submission attempt number',
  execution_backend ENUM('local', 'commercial') NOT NULL DEFAULT 'local' COMMENT 'workflow backend used by this attempt',
  route_reason VARCHAR(32) NOT NULL DEFAULT 'normal' COMMENT 'normal, local_retry, burst, or commercial_fallback',
  status ENUM('submitting', 'running', 'succeeded', 'failed', 'abandoned') NOT NULL COMMENT 'attempt lifecycle',
  workflow_run_id VARCHAR(128) NULL COMMENT 'remote run id once acknowledged',
  failure_stage VARCHAR(32) NULL COMMENT 'submit, poll, workflow, output, or db',
  error_code VARCHAR(128) NULL COMMENT 'machine-readable failure code',
  error_message VARCHAR(1024) NULL COMMENT 'failure summary',
  retryable TINYINT(1) NULL COMMENT 'whether the task remained retryable',
  started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at DATETIME NULL,
  PRIMARY KEY (task_id, generation, attempt_no),
  KEY idx_customer_profile_task_attempts_run (workflow_run_id),
  KEY idx_customer_profile_task_attempts_status_started (status, started_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_0900_ai_ci
  COMMENT='Auditable workflow submission attempts';

SET @add_attempt_execution_backend = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_task_attempts' AND column_name = 'execution_backend'),
        'SELECT ''attempt execution_backend exists''',
        'ALTER TABLE customer_profile_task_attempts ADD COLUMN execution_backend ENUM(''local'', ''commercial'') NOT NULL DEFAULT ''local'' COMMENT ''workflow backend used by this attempt'' AFTER attempt_no'
    )
);
PREPARE stmt_attempt_execution_backend FROM @add_attempt_execution_backend;
EXECUTE stmt_attempt_execution_backend;
DEALLOCATE PREPARE stmt_attempt_execution_backend;

SET @add_attempt_route_reason = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_task_attempts' AND column_name = 'route_reason'),
        'SELECT ''attempt route_reason exists''',
        'ALTER TABLE customer_profile_task_attempts ADD COLUMN route_reason VARCHAR(32) NOT NULL DEFAULT ''normal'' COMMENT ''normal, local_retry, burst, or commercial_fallback'' AFTER execution_backend'
    )
);
PREPARE stmt_attempt_route_reason FROM @add_attempt_route_reason;
EXECUTE stmt_attempt_route_reason;
DEALLOCATE PREPARE stmt_attempt_route_reason;

-- Step 4: make due retries and expired leases cheap to find.
SET @add_retry_claim_index = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND index_name = 'idx_customer_profile_tasks_retry_claim'),
        'SELECT ''idx_customer_profile_tasks_retry_claim exists''',
        'ALTER TABLE customer_profile_tasks ADD KEY idx_customer_profile_tasks_retry_claim (status, next_retry_at, queued_at, id)'
    )
);
PREPARE stmt13 FROM @add_retry_claim_index;
EXECUTE stmt13;
DEALLOCATE PREPARE stmt13;

SET @add_retry_lease_index = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND index_name = 'idx_customer_profile_tasks_retry_lease'),
        'SELECT ''idx_customer_profile_tasks_retry_lease exists''',
        'ALTER TABLE customer_profile_tasks ADD KEY idx_customer_profile_tasks_retry_lease (status, lease_expires_at)'
    )
);
PREPARE stmt14 FROM @add_retry_lease_index;
EXECUTE stmt14;
DEALLOCATE PREPARE stmt14;

SET @add_retry_backend_claim_index = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND index_name = 'idx_customer_profile_tasks_backend_retry_claim'),
        'SELECT ''idx_customer_profile_tasks_backend_retry_claim exists''',
        'ALTER TABLE customer_profile_tasks ADD KEY idx_customer_profile_tasks_backend_retry_claim (execution_backend, status, next_retry_at, id)'
    )
);
PREPARE stmt_backend_claim_index FROM @add_retry_backend_claim_index;
EXECUTE stmt_backend_claim_index;
DEALLOCATE PREPARE stmt_backend_claim_index;

-- Step 5: bound recovery of one already-submitted remote run.  These are
-- deliberately separate from attempt_count: a local poll recovery does not
-- create another QingTian workflow run.
SET @add_poll_recovery_count = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'poll_recovery_count'),
        'SELECT ''poll_recovery_count exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN poll_recovery_count INT UNSIGNED NOT NULL DEFAULT 0 COMMENT ''number of same-run recovery polls already scheduled'' AFTER attempt_count'
    )
);
PREPARE stmt15 FROM @add_poll_recovery_count;
EXECUTE stmt15;
DEALLOCATE PREPARE stmt15;

SET @add_workflow_run_started_at = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'workflow_run_started_at'),
        'SELECT ''workflow_run_started_at exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN workflow_run_started_at DATETIME NULL COMMENT ''time the current remote workflow run id was durably stored'' AFTER workflow_run_id'
    )
);
PREPARE stmt16 FROM @add_workflow_run_started_at;
EXECUTE stmt16;
DEALLOCATE PREPARE stmt16;
