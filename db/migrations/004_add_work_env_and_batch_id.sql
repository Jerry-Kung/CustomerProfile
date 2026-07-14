-- 004: add work_env and batch_id columns.
-- The submit API has always accepted `work_env` (test|production) and
-- `batch_id`, but no earlier migration created the storage. The repository
-- silently fell back to literal 'test' on read and dropped the field on
-- write, which meant every production submission ran against the test Dify
-- key. This migration adds the columns so the values actually persist.
-- Idempotent: each step inspects information_schema before executing DDL.

-- Step 1: add work_env column if missing.
SET @add_work_env = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND column_name = 'work_env'
        ),
        'SELECT ''work_env exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN work_env VARCHAR(32) NOT NULL DEFAULT ''test'' COMMENT ''dify workflow environment: test or production'' AFTER status'
    )
);
PREPARE stmt1 FROM @add_work_env;
EXECUTE stmt1;
DEALLOCATE PREPARE stmt1;

-- Step 2: add batch_id column if missing.
SET @add_batch_id = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND column_name = 'batch_id'
        ),
        'SELECT ''batch_id exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN batch_id VARCHAR(64) NULL COMMENT ''client-supplied batch identifier'' AFTER work_env'
    )
);
PREPARE stmt2 FROM @add_batch_id;
EXECUTE stmt2;
DEALLOCATE PREPARE stmt2;
