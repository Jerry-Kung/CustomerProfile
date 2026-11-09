-- Customer profile analysis tasks and results.
-- Compatible with MySQL 8.0+.

CREATE TABLE IF NOT EXISTS customer_profile_tasks (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT 'surrogate primary key',
  customer_phonenumber VARCHAR(32) NOT NULL COMMENT 'unique business key',
  status ENUM('queued', 'processing', 'succeeded', 'failed') NOT NULL DEFAULT 'queued' COMMENT 'task status',
  output JSON NULL COMMENT 'raw Dify output JSON payload',
  card_id VARCHAR(128) NULL COMMENT 'parsed from output.card_id',
  nickname VARCHAR(255) NULL COMMENT 'parsed from output.nickname',
  error_message VARCHAR(1024) NULL COMMENT 'failure reason summary',
  wechat_homepage_image VARCHAR(512) NULL COMMENT 'wechat homepage image path or url',
  wechat_moments_image VARCHAR(512) NULL COMMENT 'wechat moments image path or url',
  dify_run_id VARCHAR(128) NULL COMMENT 'optional async run identifier',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'row create time',
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'row update time',
  PRIMARY KEY (id),
  UNIQUE KEY uk_customer_profile_tasks_phone (customer_phonenumber),
  KEY idx_customer_profile_tasks_phone_prefix (customer_phonenumber),
  KEY idx_customer_profile_tasks_status_updated (status, updated_at DESC),
  KEY idx_customer_profile_tasks_updated (updated_at DESC)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_0900_ai_ci
  COMMENT='Customer profile analysis task state and final result';
