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
from src.contracts.registry import default_contract_paths
from src.infrastructure.db.client import MySQLClient
from src.infrastructure.db.task_repository import MySQLTaskRepository
from src.infrastructure.dify.client import DifyClient
from src.infrastructure.qingtian.client import QingTianClient
from src.infrastructure.storage.task_upload_store import TaskUploadStore
from src.infrastructure.workflow.output_parser import WorkflowOutputParser
from src.infrastructure.workflow.protocol import WorkflowClient
from src.worker.executor import WorkerExecutor
from src.worker.routing import CommercialQueueGate, CommercialQueueThreshold


def bootstrap(component: str = "app") -> tuple[AppConfig, MySQLClient, TaskService, WorkerExecutor, TaskUploadStore]:
    config = load_config()
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
    upload_store = TaskUploadStore(config.upload)
    contracts = default_contract_paths()
    workflow_client = _build_workflow_client(config, logger)
    commercial_queue = config.commercial_queue
    executor = WorkerExecutor(
        task_service=task_service,
        workflow_client=workflow_client,
        output_parser=WorkflowOutputParser(schema_path=contracts.output_schema_path),
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
        commercial_queue_gate=CommercialQueueGate(
            CommercialQueueThreshold(
                queue_depth=commercial_queue.threshold,
                routing_mode=commercial_queue.routing_mode,
                spillover_delay_seconds=commercial_queue.spillover_delay_seconds,
            )
        ),
        retry_config=config.retry,
        # Old pre-005 claims have no lease. Recover them only after the
        # configured QingTian poll timeout plus one recovery-scan interval.
        legacy_processing_timeout_seconds=max(
            1,
            (config.qingtian.poll_timeout_ms + 999) // 1000
            + config.retry.recovery_interval_seconds,
        ),
    )
    return config, db_client, task_service, executor, upload_store


def _build_workflow_client(config: AppConfig, logger: logging.Logger) -> WorkflowClient:
    if config.workflow_platform == "qingtian":
        logger.info("workflow_client_selected platform=qingtian")
        return QingTianClient(config=config.qingtian)
    logger.info("workflow_client_selected platform=dify")
    return DifyClient(config=config.dify)


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
    config, db_client, task_service, _, upload_store = bootstrap("api")
    app = create_api_app(db_client=db_client, task_service=task_service, upload_store=upload_store)
    log_runtime_identity(get_logger("api"), component="api", app_env=config.app_env)
    return app
