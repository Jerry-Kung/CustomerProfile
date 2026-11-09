-- 006: scalable workflow-target routing.
--
-- A pool describes routing/cost intent (local, commercial, or any), while a
-- target identifies one stable QingTian workflow/AppKey lineage.  Target ids
-- are VARCHAR values on purpose: adding another workflow must not require a
-- schema migration.
--
-- The replay-safe backfill binds processing work and retry_wait work that
-- already owns a runId. Plain queued work and retries awaiting a new run
-- remain unbound so target-specific Workers can compete for them.
--
-- The new attempt-audit columns intentionally remain nullable. New Workers
-- always populate them, while an emergency rollback to the pre-006 image can
-- still insert its older attempt shape. Re-running this migration backfills
-- any legacy NULLs before target-aware Workers start again.

SET @task_pool_existed = (
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'customer_profile_tasks'
          AND column_name = 'execution_pool'
    )
);

SET @add_task_pool = (
    SELECT IF(
        @task_pool_existed,
        'SELECT ''execution_pool exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN execution_pool VARCHAR(32) NULL COMMENT ''routing pool: local, commercial, or any'' AFTER execution_backend'
    )
);
PREPARE stmt_task_pool FROM @add_task_pool;
EXECUTE stmt_task_pool;
DEALLOCATE PREPARE stmt_task_pool;

SET @backfill_task_pool =
    'UPDATE customer_profile_tasks SET execution_pool = execution_backend WHERE execution_pool IS NULL';
PREPARE stmt_backfill_task_pool FROM @backfill_task_pool;
EXECUTE stmt_backfill_task_pool;
DEALLOCATE PREPARE stmt_backfill_task_pool;

SET @normalize_task_pool = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND column_name = 'execution_pool'
              AND is_nullable = 'NO'
              AND column_default = 'local'
        ),
        'SELECT ''execution_pool already normalized''',
        'ALTER TABLE customer_profile_tasks MODIFY COLUMN execution_pool VARCHAR(32) NOT NULL DEFAULT ''local'' COMMENT ''routing pool: local, commercial, or any'''
    )
);
PREPARE stmt_normalize_task_pool FROM @normalize_task_pool;
EXECUTE stmt_normalize_task_pool;
DEALLOCATE PREPARE stmt_normalize_task_pool;

SET @task_target_existed = (
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'customer_profile_tasks'
          AND column_name = 'workflow_target_id'
    )
);

SET @add_task_target = (
    SELECT IF(
        @task_target_existed,
        'SELECT ''workflow_target_id exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN workflow_target_id VARCHAR(128) NULL COMMENT ''stable workflow/AppKey target owning the current run'' AFTER execution_pool'
    )
);
PREPARE stmt_task_target FROM @add_task_target;
EXECUTE stmt_task_target;
DEALLOCATE PREPARE stmt_task_target;

SET @add_task_target_revision = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND column_name = 'workflow_target_revision'
        ),
        'SELECT ''workflow_target_revision exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN workflow_target_revision VARCHAR(64) NULL COMMENT ''non-secret target/key configuration revision'' AFTER workflow_target_id'
    )
);
PREPARE stmt_task_target_revision FROM @add_task_target_revision;
EXECUTE stmt_task_target_revision;
DEALLOCATE PREPARE stmt_task_target_revision;

UPDATE customer_profile_tasks
SET workflow_target_id = COALESCE(
        workflow_target_id,
        CONCAT(execution_backend, '-default')
    ),
    workflow_target_revision = COALESCE(workflow_target_revision, 'v1')
WHERE (
        workflow_target_id IS NULL
        OR workflow_target_revision IS NULL
    )
  AND (
        status = 'processing'
        OR (
            status = 'retry_wait'
            AND workflow_run_id IS NOT NULL
        )
    );

SET @task_policy_existed = (
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'customer_profile_tasks'
          AND column_name = 'retry_policy_version'
    )
);

SET @add_task_policy = (
    SELECT IF(
        @task_policy_existed,
        'SELECT ''retry_policy_version exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN retry_policy_version VARCHAR(64) NULL COMMENT ''immutable retry policy selected for this generation'' AFTER workflow_target_revision'
    )
);
PREPARE stmt_task_policy FROM @add_task_policy;
EXECUTE stmt_task_policy;
DEALLOCATE PREPARE stmt_task_policy;

SET @backfill_task_policy =
    'UPDATE customer_profile_tasks SET retry_policy_version = CASE WHEN task_type = ''invitation_strategy'' THEN ''single_target_v1'' ELSE ''local_first_v1'' END WHERE retry_policy_version IS NULL';
PREPARE stmt_backfill_task_policy FROM @backfill_task_policy;
EXECUTE stmt_backfill_task_policy;
DEALLOCATE PREPARE stmt_backfill_task_policy;

SET @normalize_task_policy = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND column_name = 'retry_policy_version'
              AND is_nullable = 'NO'
              AND column_default = 'local_first_v1'
        ),
        'SELECT ''retry_policy_version already normalized''',
        'ALTER TABLE customer_profile_tasks MODIFY COLUMN retry_policy_version VARCHAR(64) NOT NULL DEFAULT ''local_first_v1'' COMMENT ''immutable retry policy selected for this generation'''
    )
);
PREPARE stmt_normalize_task_policy FROM @normalize_task_policy;
EXECUTE stmt_normalize_task_policy;
DEALLOCATE PREPARE stmt_normalize_task_policy;

SET @attempt_pool_existed = (
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'customer_profile_task_attempts'
          AND column_name = 'execution_pool'
    )
);

SET @add_attempt_pool = (
    SELECT IF(
        @attempt_pool_existed,
        'SELECT ''attempt execution_pool exists''',
        'ALTER TABLE customer_profile_task_attempts ADD COLUMN execution_pool VARCHAR(32) NULL COMMENT ''actual pool used by this attempt'' AFTER execution_backend'
    )
);
PREPARE stmt_attempt_pool FROM @add_attempt_pool;
EXECUTE stmt_attempt_pool;
DEALLOCATE PREPARE stmt_attempt_pool;

SET @backfill_attempt_pool =
    'UPDATE customer_profile_task_attempts SET execution_pool = execution_backend WHERE execution_pool IS NULL';
PREPARE stmt_backfill_attempt_pool FROM @backfill_attempt_pool;
EXECUTE stmt_backfill_attempt_pool;
DEALLOCATE PREPARE stmt_backfill_attempt_pool;

SET @normalize_attempt_pool = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_task_attempts'
              AND column_name = 'execution_pool'
              AND is_nullable = 'YES'
              AND column_default IS NULL
        ),
        'SELECT ''attempt execution_pool already rollback-compatible''',
        'ALTER TABLE customer_profile_task_attempts MODIFY COLUMN execution_pool VARCHAR(32) NULL DEFAULT NULL COMMENT ''actual pool used by this attempt'''
    )
);
PREPARE stmt_normalize_attempt_pool FROM @normalize_attempt_pool;
EXECUTE stmt_normalize_attempt_pool;
DEALLOCATE PREPARE stmt_normalize_attempt_pool;

SET @attempt_target_existed = (
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'customer_profile_task_attempts'
          AND column_name = 'workflow_target_id'
    )
);

SET @add_attempt_target = (
    SELECT IF(
        @attempt_target_existed,
        'SELECT ''attempt workflow_target_id exists''',
        'ALTER TABLE customer_profile_task_attempts ADD COLUMN workflow_target_id VARCHAR(128) NULL COMMENT ''stable workflow/AppKey target used by this attempt'' AFTER execution_pool'
    )
);
PREPARE stmt_attempt_target FROM @add_attempt_target;
EXECUTE stmt_attempt_target;
DEALLOCATE PREPARE stmt_attempt_target;

SET @add_attempt_target_revision = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_task_attempts'
              AND column_name = 'workflow_target_revision'
        ),
        'SELECT ''attempt workflow_target_revision exists''',
        'ALTER TABLE customer_profile_task_attempts ADD COLUMN workflow_target_revision VARCHAR(64) NULL COMMENT ''non-secret target/key configuration revision'' AFTER workflow_target_id'
    )
);
PREPARE stmt_attempt_target_revision FROM @add_attempt_target_revision;
EXECUTE stmt_attempt_target_revision;
DEALLOCATE PREPARE stmt_attempt_target_revision;

SET @add_attempt_policy = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_task_attempts'
              AND column_name = 'retry_policy_version'
        ),
        'SELECT ''attempt retry_policy_version exists''',
        'ALTER TABLE customer_profile_task_attempts ADD COLUMN retry_policy_version VARCHAR(64) NULL COMMENT ''retry policy active for this attempt'' AFTER workflow_target_revision'
    )
);
PREPARE stmt_attempt_policy FROM @add_attempt_policy;
EXECUTE stmt_attempt_policy;
DEALLOCATE PREPARE stmt_attempt_policy;

UPDATE customer_profile_task_attempts AS attempt
INNER JOIN customer_profile_tasks AS task
    ON task.id = attempt.task_id
SET attempt.workflow_target_id = COALESCE(
        attempt.workflow_target_id,
        CONCAT(attempt.execution_backend, '-default')
    ),
    attempt.workflow_target_revision = COALESCE(
        attempt.workflow_target_revision,
        'v1'
    ),
    attempt.retry_policy_version = COALESCE(
        attempt.retry_policy_version,
        task.retry_policy_version
    )
WHERE attempt.workflow_target_id IS NULL
   OR attempt.workflow_target_revision IS NULL
   OR attempt.retry_policy_version IS NULL;

UPDATE customer_profile_task_attempts
SET workflow_target_id = COALESCE(
        workflow_target_id,
        CONCAT(execution_backend, '-default')
    ),
    workflow_target_revision = COALESCE(workflow_target_revision, 'v1'),
    retry_policy_version = COALESCE(retry_policy_version, 'local_first_v1')
WHERE workflow_target_id IS NULL
   OR workflow_target_revision IS NULL
   OR retry_policy_version IS NULL;

SET @normalize_attempt_target = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_task_attempts'
              AND column_name = 'workflow_target_id'
              AND is_nullable = 'YES'
        ),
        'SELECT ''attempt workflow_target_id already rollback-compatible''',
        'ALTER TABLE customer_profile_task_attempts MODIFY COLUMN workflow_target_id VARCHAR(128) NULL COMMENT ''stable workflow/AppKey target used by this attempt'''
    )
);
PREPARE stmt_normalize_attempt_target FROM @normalize_attempt_target;
EXECUTE stmt_normalize_attempt_target;
DEALLOCATE PREPARE stmt_normalize_attempt_target;

SET @normalize_attempt_target_revision = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_task_attempts'
              AND column_name = 'workflow_target_revision'
              AND is_nullable = 'YES'
              AND column_default IS NULL
        ),
        'SELECT ''attempt workflow_target_revision already rollback-compatible''',
        'ALTER TABLE customer_profile_task_attempts MODIFY COLUMN workflow_target_revision VARCHAR(64) NULL DEFAULT NULL COMMENT ''non-secret target/key configuration revision'''
    )
);
PREPARE stmt_normalize_attempt_target_revision FROM @normalize_attempt_target_revision;
EXECUTE stmt_normalize_attempt_target_revision;
DEALLOCATE PREPARE stmt_normalize_attempt_target_revision;

SET @normalize_attempt_policy = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_task_attempts'
              AND column_name = 'retry_policy_version'
              AND is_nullable = 'YES'
              AND column_default IS NULL
        ),
        'SELECT ''attempt retry_policy_version already rollback-compatible''',
        'ALTER TABLE customer_profile_task_attempts MODIFY COLUMN retry_policy_version VARCHAR(64) NULL DEFAULT NULL COMMENT ''retry policy active for this attempt'''
    )
);
PREPARE stmt_normalize_attempt_policy FROM @normalize_attempt_policy;
EXECUTE stmt_normalize_attempt_policy;
DEALLOCATE PREPARE stmt_normalize_attempt_policy;

SET @add_target_retry_claim_index = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND index_name = 'idx_customer_profile_tasks_target_retry_claim'
        ),
        'SELECT ''target retry claim index exists''',
        'ALTER TABLE customer_profile_tasks ADD KEY idx_customer_profile_tasks_target_retry_claim (workflow_target_id, status, next_retry_at, id)'
    )
);
PREPARE stmt_target_retry_claim_index FROM @add_target_retry_claim_index;
EXECUTE stmt_target_retry_claim_index;
DEALLOCATE PREPARE stmt_target_retry_claim_index;

SET @add_pool_retry_claim_index = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND index_name = 'idx_customer_profile_tasks_pool_retry_claim'
        ),
        'SELECT ''pool retry claim index exists''',
        'ALTER TABLE customer_profile_tasks ADD KEY idx_customer_profile_tasks_pool_retry_claim (execution_pool, status, next_retry_at, id)'
    )
);
PREPARE stmt_pool_retry_claim_index FROM @add_pool_retry_claim_index;
EXECUTE stmt_pool_retry_claim_index;
DEALLOCATE PREPARE stmt_pool_retry_claim_index;

SET @add_attempt_target_run_index = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_task_attempts'
              AND index_name = 'idx_customer_profile_task_attempts_target_run'
        ),
        'SELECT ''attempt target run index exists''',
        'ALTER TABLE customer_profile_task_attempts ADD KEY idx_customer_profile_task_attempts_target_run (workflow_target_id, workflow_run_id)'
    )
);
PREPARE stmt_attempt_target_run_index FROM @add_attempt_target_run_index;
EXECUTE stmt_attempt_target_run_index;
DEALLOCATE PREPARE stmt_attempt_target_run_index;
