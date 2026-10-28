-- Earlier workers cleared the target when retrying a definite failure as a new
-- remote run. Restore that lineage from the last attempt before enabling several
-- compatible targets. Never infer an AppKey/endpoint from the local/commercial pool.
-- Replay-safe: only unbound, same-generation, single-target retries are changed.
UPDATE customer_profile_tasks AS task
INNER JOIN customer_profile_task_attempts AS attempt
    ON attempt.task_id = task.id
   AND attempt.generation = task.generation
   AND attempt.attempt_no = task.attempt_count
SET task.workflow_target_id = attempt.workflow_target_id,
    task.workflow_target_revision = attempt.workflow_target_revision,
    task.execution_pool = COALESCE(attempt.execution_pool, attempt.execution_backend),
    task.execution_backend = attempt.execution_backend
WHERE task.status = 'retry_wait'
  AND task.retry_policy_version = 'single_target_v1'
  AND task.workflow_run_id IS NULL
  AND task.workflow_target_id IS NULL
  AND task.attempt_count > 0
  AND attempt.status = 'failed'
  AND attempt.retryable = 1
  AND attempt.workflow_target_id IS NOT NULL
  AND attempt.workflow_target_revision IS NOT NULL;
