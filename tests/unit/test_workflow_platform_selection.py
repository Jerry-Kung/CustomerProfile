from __future__ import annotations

from src.app.bootstrap import _build_workflow_client
from src.config.settings import AppConfig, DifyConfig, MigrationConfig, MySQLConfig, QingTianConfig, TosConfig, UploadConfig, WorkerConfig
from src.infrastructure.dify.client import DifyClient
from src.infrastructure.qingtian.client import QingTianClient

import logging
from pathlib import Path


def _config(platform: str) -> AppConfig:
    return AppConfig(
        app_env="local",
        log_level="INFO",
        workflow_platform=platform,
        dify=DifyConfig(
            base_url="http://dify.example",
            run_url="http://dify.example/v1/workflows/run",
            api_key_test="dify-test",
            api_key_production="dify-prod",
            invitation_strategy_api_key="dify-inv",
            user_id="klh",
            image_mode="file_id",
            stream_init_timeout_ms=30000,
            poll_interval_ms=2000,
            poll_timeout_ms=2400000,
        ),
        qingtian=QingTianConfig(
            base_url="https://qingtian.example/api/proxy/api/v1",
            api_key_test="qt-test",
            api_key_production="qt-prod",
            invitation_strategy_api_key="qt-inv",
            user_id="klh",
            poll_interval_ms=2000,
            poll_timeout_ms=2400000,
        ),
        mysql=MySQLConfig(host="h", port=3306, user="u", password="p", database="d"),
        upload=UploadConfig(base_dir=Path("."), max_bytes_per_file=1024),
        worker=WorkerConfig(concurrency=1, poll_interval_seconds=1.0),
        tos=TosConfig(
            access_key_id="",
            access_key_secret="",
            region="",
            endpoint="",
            bucket="",
            presigned_url_expires_seconds=3600,
        ),
        migration=MigrationConfig(db_wait_max_tries=1, db_wait_interval_seconds=0.1),
    )


def test_dify_platform_returns_dify_client():
    client = _build_workflow_client(_config("dify"), logging.getLogger("test"))
    assert isinstance(client, DifyClient)


def test_qingtian_platform_returns_qingtian_client():
    client = _build_workflow_client(_config("qingtian"), logging.getLogger("test"))
    assert isinstance(client, QingTianClient)
