-- 运行时库初始化：customer_profile_runtime

-- 由 scripts/init_runtime_db.py 生成。可重复执行。



CREATE DATABASE IF NOT EXISTS `customer_profile_runtime` DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_0900_as_cs;

USE `customer_profile_runtime`;



CREATE TABLE IF NOT EXISTS workflow_runs (
            run_id                VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            workflow_id           VARCHAR(128) NOT NULL,
            workflow_display_name VARCHAR(255) NULL,
            parent_run_id         VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NULL,
            parent_node_id        VARCHAR(96) NULL,
            call_path             VARCHAR(512) NOT NULL DEFAULT '',
            business_ref          VARCHAR(128) NULL,
            status                VARCHAR(32) NOT NULL,
            definition_version_id INT NULL,
            inputs_json           LONGTEXT NULL,
            outputs_json          LONGTEXT NULL,
            error                 TEXT NULL,
            started_at_ms         BIGINT NULL,
            finished_at_ms        BIGINT NULL,
            duration_ms           BIGINT NULL,
            is_replay             TINYINT(1) NOT NULL DEFAULT 0,
            queued_at_ms          BIGINT NULL,
            claimed_at_ms         BIGINT NULL,
            claimed_by            VARCHAR(255) NULL,
            lease_expires_at_ms   BIGINT NULL,
            created_at_ms         BIGINT NOT NULL,
            payload_state         VARCHAR(16) NULL,
            payload_mode          VARCHAR(16) NULL,
            archived_at_ms        BIGINT NULL,
            PRIMARY KEY (run_id),
            KEY idx_runs_workflow (workflow_id),
            KEY idx_runs_parent (parent_run_id),
            KEY idx_runs_business (business_ref),
            KEY idx_runs_status (status),
            KEY idx_runs_queue (status, parent_run_id, created_at_ms)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs
          COMMENT='工作流运行与队列（复用同表，见 ADR 0001）'

CREATE TABLE IF NOT EXISTS definition_versions (
            version_id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            workflow_id          VARCHAR(128) NOT NULL,
            definition_hash      CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            definition_json      LONGTEXT NOT NULL,
            template_hashes_json LONGTEXT NULL,
            code_commit          VARCHAR(64) NULL,
            created_at_ms        BIGINT NOT NULL,
            PRIMARY KEY (version_id),
            UNIQUE KEY uk_definition_versions_hash (workflow_id, definition_hash)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs
          COMMENT='工作流定义快照：历史运行展示当时的图与提示词'

CREATE TABLE IF NOT EXISTS node_executions (
            execution_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            run_id         VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            node_id        VARCHAR(96) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            node_title     VARCHAR(255) NULL,
            node_type      VARCHAR(64) NOT NULL,
            call_path      VARCHAR(512) NOT NULL DEFAULT '',
            status         VARCHAR(32) NOT NULL,
            branch         VARCHAR(64) NULL,
            inputs_json    LONGTEXT NULL,
            outputs_json   LONGTEXT NULL,
            error          TEXT NULL,
            queued_at_ms   BIGINT NULL,
            started_at_ms  BIGINT NULL,
            duration_ms    BIGINT NULL,
            sub_run_id     VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NULL,
            attempt_count  INT NOT NULL DEFAULT 0,
            PRIMARY KEY (execution_id),
            UNIQUE KEY uk_node_executions_scope (run_id, node_id, call_path),
            KEY idx_nodes_run (run_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs
          COMMENT='节点执行明细：唯一键含 call_path，迭代各轮靠它区分'

CREATE TABLE IF NOT EXISTS request_attempts (
            attempt_id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            run_id              VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            node_id             VARCHAR(96) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            call_path           VARCHAR(512) NOT NULL DEFAULT '',
            attempt_no          INT NOT NULL,
            kind                VARCHAR(16) NOT NULL,
            target              VARCHAR(1024) NULL,
            request_json        LONGTEXT NULL,
            response_text       LONGTEXT NULL,
            status_code         INT NULL,
            duration_ms         BIGINT NULL,
            error               TEXT NULL,
            error_code          VARCHAR(64) NULL,
            usage_json          TEXT NULL,
            provider_request_id VARCHAR(128) NULL,
            replayed            TINYINT(1) NOT NULL DEFAULT 0,
            anomaly             VARCHAR(255) NULL,
            finish_reason       VARCHAR(64) NULL,
            created_at_ms       BIGINT NOT NULL,
            PRIMARY KEY (attempt_id),
            KEY idx_attempts_run (run_id, node_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs
          COMMENT='每次外部请求尝试：请求体、响应原文、usage、耗时、错误'

CREATE TABLE IF NOT EXISTS profile_baselines (
            phone              VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            compat_key         VARCHAR(255) NOT NULL DEFAULT '',
            source_run_id      VARCHAR(48) NULL,
            updated_at_ms      BIGINT NOT NULL,
            stage              VARCHAR(64) NOT NULL DEFAULT '',
            deal_level         VARCHAR(64) NOT NULL DEFAULT '',
            deal_level_detail  VARCHAR(255) NOT NULL DEFAULT '',
            consumed_keys_json LONGTEXT NULL,
            callback_json      LONGTEXT NULL,
            PRIMARY KEY (phone)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs
          COMMENT='增量画像基线：按手机号一行，保存上次实际发出的回传体'

CREATE TABLE IF NOT EXISTS schema_migrations (
            version     VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            applied_at_ms BIGINT NOT NULL,
            note        VARCHAR(255) NULL,
            PRIMARY KEY (version)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs
          COMMENT='迁移版本记录：服务启动时比对期望版本'

INSERT INTO schema_migrations (version, applied_at_ms, note) VALUES ('2026-11-12.1', UNIX_TIMESTAMP() * 1000, '初始化运行时库：四层留痕四表 + 增量基线 + 迁移记录') ON DUPLICATE KEY UPDATE note = VALUES(note);



-- 核对：应看到 6 张表

SHOW TABLES;

SELECT version, FROM_UNIXTIME(applied_at_ms / 1000) AS applied_at FROM schema_migrations ORDER BY applied_at_ms;
