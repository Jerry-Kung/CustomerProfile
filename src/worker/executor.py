from __future__ import annotations

import logging

from src.domain.task.service import TaskService
from src.infrastructure.workflow.errors import (
    WorkflowError,
    WorkflowOutputInvalidJson,
    WorkflowOutputSchemaMismatch,
    WorkflowRequestFailed,
    WorkflowTimeout,
)
from src.infrastructure.workflow.output_parser import WorkflowOutputParser
from src.infrastructure.workflow.protocol import WorkflowClient


_WORKER_UNEXPECTED_ERROR_CODE = "WORKER_UNEXPECTED_ERROR"


class WorkerExecutor:
    def __init__(
        self,
        *,
        task_service: TaskService,
        workflow_client: WorkflowClient,
        output_parser: WorkflowOutputParser,
        logger: logging.Logger,
        platform_name: str = "dify",
    ):
        self._task_service = task_service
        self._workflow_client = workflow_client
        self._output_parser = output_parser
        self._logger = logger
        self._platform_name = platform_name

    def execute_once(self) -> str:
        # Claim is isolated: a DB error here must NOT bubble up (would kill the worker thread).
        # No task is in 'processing' yet, so return "error" and let the runner retry on next tick.
        try:
            claimed = self._task_service.claim_next_task()
        except Exception:
            self._logger.exception("worker_claim_failed")
            return "error"

        if claimed is None:
            return "idle"
        if claimed.id is None:
            self._logger.error("worker_task_missing_id customer_phonenumber=%s", claimed.customer_phonenumber)
            return "error"

        task_id = claimed.id
        phone = claimed.customer_phonenumber

        try:
            self._logger.info(
                "worker_workflow_invoke_begin task_id=%s platform=%s task_type=%s work_env=%s",
                task_id,
                self._platform_name,
                claimed.task_type,
                claimed.work_env,
            )
            output_raw = self._workflow_client.run_workflow(
                customer_phonenumber=claimed.customer_phonenumber,
                work_env=claimed.work_env,
                batch_id=claimed.batch_id,
                task_type=claimed.task_type,
            )
            parsed = self._output_parser.parse(output_raw)
            self._task_service.repository.mark_succeeded(
                task_id=task_id,
                output=parsed.output,
                card_id=parsed.card_id,
                nickname=parsed.nickname,
            )
            self._logger.info(
                "worker_task_succeeded id=%s customer_phonenumber=%s task_type=%s",
                task_id,
                phone,
                claimed.task_type,
            )
            return "succeeded"
        except (WorkflowTimeout, WorkflowRequestFailed, WorkflowOutputInvalidJson, WorkflowOutputSchemaMismatch) as exc:
            err_text = str(exc)
            if len(err_text) > 800:
                err_text = err_text[:800] + "..."
            self._safe_mark_failed(task_id, f"{exc.code}: {str(exc)}")
            self._logger.warning(
                "worker_task_failed id=%s customer_phonenumber=%s error_code=%s detail=%s",
                task_id,
                phone,
                exc.code,
                err_text,
            )
            return "failed"
        except WorkflowError as exc:
            self._safe_mark_failed(task_id, f"{exc.code}: {str(exc)}")
            self._logger.warning(
                "worker_task_failed id=%s customer_phonenumber=%s error_code=%s",
                task_id,
                phone,
                exc.code,
            )
            return "failed"
        except Exception as exc:
            # P0 safety net: an unexpected exception MUST NOT kill the worker thread.
            # The row was claimed (status=processing); mark it failed so it does not stick silently.
            self._logger.exception(
                "worker_task_unexpected_error id=%s customer_phonenumber=%s exc_type=%s",
                task_id,
                phone,
                type(exc).__name__,
            )
            detail = f"{type(exc).__name__}: {str(exc)}"
            if len(detail) > 800:
                detail = detail[:800] + "..."
            self._safe_mark_failed(task_id, f"{_WORKER_UNEXPECTED_ERROR_CODE}: {detail}")
            return "failed"

    def _safe_mark_failed(self, task_id: int, error_message: str) -> None:
        """mark_failed must not itself raise back into the caller — that would re-introduce the bug we are fixing."""
        try:
            self._task_service.repository.mark_failed(
                task_id=task_id,
                error_message=error_message,
            )
        except Exception:
            self._logger.exception(
                "worker_mark_failed_secondary_failure task_id=%s", task_id,
            )
