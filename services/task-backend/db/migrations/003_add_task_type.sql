-- 003: introduce multi-type task support.
-- Adds task_type column and switches the business unique key from
-- (customer_phonenumber) to (customer_phonenumber, task_type) so that a single
-- phone number can simultaneously hold tasks of different types.
-- Idempotent: each step inspects information_schema before executing DDL.

-- Step 1: add task_type column if missing.
SET @add_task_type = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND column_name = 'task_type'
        ),
        'SELECT ''task_type exists''',
        'ALTER TABLE customer_profile_tasks ADD COLUMN task_type VARCHAR(64) NOT NULL DEFAULT ''customer_profile'' COMMENT ''task category, e.g. customer_profile or invitation_strategy'' AFTER status'
    )
);
PREPARE stmt1 FROM @add_task_type;
EXECUTE stmt1;
DEALLOCATE PREPARE stmt1;

-- Step 2: drop legacy single-column unique key on customer_phonenumber if it still exists.
SET @drop_legacy_unique = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND index_name = 'uk_customer_profile_tasks_phone'
        ),
        'ALTER TABLE customer_profile_tasks DROP INDEX uk_customer_profile_tasks_phone',
        'SELECT ''uk_customer_profile_tasks_phone already removed'''
    )
);
PREPARE stmt2 FROM @drop_legacy_unique;
EXECUTE stmt2;
DEALLOCATE PREPARE stmt2;

-- Step 3: add composite unique key (customer_phonenumber, task_type) if missing.
SET @add_new_unique = (
    SELECT IF(
        EXISTS (
            SELECT 1
            FROM information_schema.statistics
            WHERE table_schema = DATABASE()
              AND table_name = 'customer_profile_tasks'
              AND index_name = 'uk_customer_profile_tasks_phone_type'
        ),
        'SELECT ''uk_customer_profile_tasks_phone_type exists''',
        'ALTER TABLE customer_profile_tasks ADD UNIQUE KEY uk_customer_profile_tasks_phone_type (customer_phonenumber, task_type)'
    )
);
PREPARE stmt3 FROM @add_new_unique;
EXECUTE stmt3;
DEALLOCATE PREPARE stmt3;
