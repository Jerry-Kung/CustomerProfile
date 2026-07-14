from __future__ import annotations

import json
import logging

from src.config.logging_utils import get_logger, log_config_summary, redact_config
from src.config.settings import (
    AppConfig,
    DifyConfig,
    MigrationConfig,
    MySQLConfig,
    QingTianConfig,
    TosConfig,
    UploadConfig,
    WorkerConfig,
)
from pathlib import Path


def _sample_config() -> AppConfig:
    return AppConfig(
        app_env="test",
        log_level="DEBUG",
        workflow_platform="dify",
        dify=DifyConfig(
            base_url="http://118.145.238.55",
            run_url="http://118.145.238.55/v1/workflows/run",
            api_key_test="app-CP-TEST-KEY",
            api_key_production="app-CP-PROD-KEY",
            invitation_strategy_api_key="app-INV-KEY-xxxxxxx",
            user_id="klh",
            image_mode="file_id",
            stream_init_timeout_ms=30000,
            poll_interval_ms=2000,
            poll_timeout_ms=900000,
        ),
        qingtian=QingTianConfig(
            base_url="https://qingtian.example/api/proxy/api/v1",
            api_key_test="qt-TEST-KEY",
            api_key_production="qt-PROD-KEY",
            invitation_strategy_api_key="qt-INV-KEY",
            user_id="klh",
            poll_interval_ms=2000,
            poll_timeout_ms=2400000,
        ),
        mysql=MySQLConfig(
            host="118.145.238.55",
            port=3306,
            user="root",
            password="123456xxxx",
            database="customer_profile",
        ),
        upload=UploadConfig(
            base_dir=Path("/tmp/uploads"),
            max_bytes_per_file=10 * 1024 * 1024,
        ),
        worker=WorkerConfig(concurrency=8, poll_interval_seconds=2.0),
        tos=TosConfig(
            access_key_id="",
            access_key_secret="",
            region="",
            endpoint="",
            bucket="",
            presigned_url_expires_seconds=3600,
        ),
        migration=MigrationConfig(db_wait_max_tries=30, db_wait_interval_seconds=2.0),
    )


def test_redact_config_masks_sensitive_values():
    redacted = redact_config(_sample_config())
    assert redacted["dify"]["api_key_test"].startswith("app-")
    assert "***" in redacted["dify"]["api_key_test"]
    assert "CP-TEST-KEY" not in redacted["dify"]["api_key_test"]
    assert "***" in redacted["dify"]["api_key_production"]
    assert "CP-PROD-KEY" not in redacted["dify"]["api_key_production"]
    assert "***" in redacted["dify"]["invitation_strategy_api_key"]
    assert "INV-KEY" not in redacted["dify"]["invitation_strategy_api_key"]
    assert "***" in redacted["mysql"]["password"]
    assert "123456xxxx" not in redacted["mysql"]["password"]


def test_log_config_summary_does_not_leak_secret(caplog):
    logger = get_logger("test_logger")
    logger.setLevel(logging.INFO)
    with caplog.at_level(logging.INFO):
        log_config_summary(logger, _sample_config())
    message = " ".join(record.getMessage() for record in caplog.records)
    assert "config_loaded" in message
    assert "CP-TEST-KEY" not in message
    assert "CP-PROD-KEY" not in message
    assert "INV-KEY-xxxxxxx" not in message
    assert "123456xxxx" not in message
    payload = message.split("config_loaded ", 1)[1]
    data = json.loads(payload)
    assert data["dify"]["api_key_test"].endswith(")")
    assert data["dify"]["invitation_strategy_api_key"].endswith(")")
