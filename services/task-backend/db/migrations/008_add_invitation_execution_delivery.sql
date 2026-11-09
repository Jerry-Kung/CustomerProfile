-- Extend existing storage; legacy invitations are reconciled explicitly, not auto-notified.

SET @add_delivery_column = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'customer_profile_tasks' AND column_name = 'execution_id'),
        'SELECT 1',
        'ALTER TABLE customer_profile_tasks ADD COLUMN execution_id VARCHAR(64) NULL COMMENT ''invitation execution identity, independent of business batch'''
    )
);
PREPARE delivery_column FROM @add_delivery_column;
EXECUTE delivery_column;
DEALLOCATE PREPARE delivery_column;

SET @add_delivery_column = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'customer_profile_failure_outbox' AND column_name = 'task_type'),
        'SELECT 1',
        'ALTER TABLE customer_profile_failure_outbox ADD COLUMN task_type VARCHAR(32) NOT NULL DEFAULT ''customer_profile'''
    )
);
PREPARE delivery_column FROM @add_delivery_column;
EXECUTE delivery_column;
DEALLOCATE PREPARE delivery_column;

SET @add_delivery_column = (
    SELECT IF(
        EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'customer_profile_failure_outbox' AND column_name = 'execution_id'),
        'SELECT 1',
        'ALTER TABLE customer_profile_failure_outbox ADD COLUMN execution_id VARCHAR(64) NULL'
    )
);
PREPARE delivery_column FROM @add_delivery_column;
EXECUTE delivery_column;
DEALLOCATE PREPARE delivery_column;
