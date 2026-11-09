from __future__ import annotations

import logging

from src.api.app import create_api_app
from src.config.logging_utils import (
    configure_root_logging,
    get_logger,
    log_config_summary,
    log_runtime_identity,
)
from src.config.settings import AppConfig, load_config
from src.domain.task.service import TaskService
from src.infrastructure.db.client import MySQLClient
from src.infrastructure.db.task_repository import MySQLTaskRepository
from src.infrastructure.dify.client import DifyClient
from src.infrastructure.qingtian.client import QingTianClient
from src.infrastructure.runner.client import RunnerClient
from src.infrastructure.invitation_runner.client import InvitationRunnerClient
from src.infrastructure.workflow.output_parser import WorkflowOutputParser
from src.infrastructure.workflow.protocol import WorkflowClient
from src.infrastructure.workflow.invitation_writeback import InvitationWritebackVerifier
from src.worker.executor import WorkerExecutor


def _task_services(component: str) -> tuple[AppConfig, MySQLClient, TaskService]:
    config = load_config(component=component)
    # Configure root logging FIRST so any logging.getLogger(__name__) call in dify/tos/output_parser
    # modules emits to stderr instead of being swallowed by a handler-less root logger.
    configure_root_logging(component)
    logger = get_logger("bootstrap")
    log_config_summary(logger, config)
    db_client = MySQLClient(config.mysql)
    repository = MySQLTaskRepository(
        db_client,
        queue_max_waiting=config.task_queue_max_waiting,
        default_retry_policy_version=config.retry.policy_version,
        logger=get_logger("task_repository"),
    )
    _warn_if_task_type_column_missing(logger, db_client, repository)
    task_service = TaskService(
        repository=repository,
        logger=get_logger("task_service"),
    )
    return config, db_client, task_service


def bootstrap(component: str = "worker") -> tuple[AppConfig, MySQLClient, TaskService, WorkerExecutor]:
    """Build execution dependencies only for a Worker process."""
    if component != "worker":
        raise ValueError("bootstrap builds workers; use create_application for the API")
    config, db_client, task_service = _task_services(component)
    logger = get_logger("bootstrap")
    workflow_client = _build_workflow_client(config, logger)
    executor = WorkerExecutor(
        task_service=task_service,
        workflow_client=workflow_client,
        output_parser=WorkflowOutputParser(),
        logger=get_logger("worker_executor"),
        platform_name=config.workflow_platform,
        worker_role=config.worker.role,
        workflow_target_id=config.worker.target_id,
        workflow_target_revision=config.worker.target_revision,
        worker_target_mode=config.worker.target_mode,
        worker_task_types=config.worker.task_types,
        worker_work_envs=config.worker.work_envs,
        worker_target_failure_threshold=config.worker.target_failure_threshold,
        worker_target_cooldown_seconds=config.worker.target_cooldown_seconds,
        retry_config=config.retry,
        invitation_writeback=InvitationWritebackVerifier(config.failure_callback),
        # Old pre-005 claims have no lease. Recover them only after the
        # configured QingTian poll timeout plus one recovery-scan interval.
        legacy_processing_timeout_seconds=max(
            1,
            (_workflow_poll_timeout_ms(config) + 999) // 1000
            + config.retry.recovery_interval_seconds,
        ),
    )
    return config, db_client, task_service, executor


def _build_workflow_client(config: AppConfig, logger: logging.Logger) -> WorkflowClient:
    if config.workflow_platform == "invitation_runner":
        logger.info("workflow_client_selected platform=invitation_runner")
        return InvitationRunnerClient(config=config.invitation_runner)
    if config.workflow_platform == "runner":
        logger.info("workflow_client_selected platform=runner 说明=使用独立画像生成服务")
        return RunnerClient(config=config.runner)
    if config.workflow_platform == "qingtian":
        logger.info("workflow_client_selected platform=qingtian")
        return QingTianClient(config=config.qingtian)
    logger.info("workflow_client_selected platform=dify")
    return DifyClient(config=config.dify)


def _workflow_poll_timeout_ms(config: AppConfig) -> int:
    if config.workflow_platform == "invitation_runner":
        return config.invitation_runner.poll_timeout_ms
    if config.workflow_platform == "runner":
        return config.runner.poll_timeout_ms
    if config.workflow_platform == "qingtian":
        return config.qingtian.poll_timeout_ms
    return config.dify.poll_timeout_ms


def _warn_if_task_type_column_missing(logger, db_client, repository) -> None:
    """Warm legacy-column capabilities and warn if migration 003 is missing.

    Without this, invitation_strategy submissions silently run as customer_profile.
    Probe is best-effort: a DB failure here must not block startup.
    """
    try:
        conn = db_client.connect()
        try:
            repository._has_work_env_column(conn)
            repository._has_batch_id_column(conn)
            if not repository._has_task_type_column(conn):
                logger.warning(
                    "task_type_column_missing migration=003_add_task_type.sql "
                    "effect=multi-type submissions will be miscategorized as customer_profile"
                )
        finally:
            conn.close()
    except Exception:
        logger.exception("task_type_column_probe_failed")


def create_application():
    config, db_client, task_service = _task_services("api")
    app = create_api_app(db_client=db_client, task_service=task_service, api_config=config.api)
    log_runtime_identity(get_logger("api"), component="api", app_env=config.app_env)
    return app
