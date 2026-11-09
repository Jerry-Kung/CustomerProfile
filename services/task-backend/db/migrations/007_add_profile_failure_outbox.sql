-- Final profile failure callbacks, delivered at least once and fenced by batch id.
CREATE TABLE IF NOT EXISTS customer_profile_failure_outbox (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    task_id BIGINT NOT NULL,
    generation INT NOT NULL,
    customer_phone VARCHAR(20) NOT NULL,
    batch_id VARCHAR(64) NOT NULL,
    work_env VARCHAR(20) NOT NULL,
    error_message TEXT NOT NULL,
    attempts INT NOT NULL DEFAULT 0,
    next_attempt_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_profile_failure_task_generation (task_id, generation),
    KEY idx_profile_failure_pending (delivered_at, next_attempt_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
