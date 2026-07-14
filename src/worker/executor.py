from __future__ import annotations

import logging
import time

from src.config.observability import elapsed_ms, log_context, mask_phone, safe_text
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

    def operational_snapshot(self) -> dict[str, object]:
        snapshot_fn = getattr(self._task_service.repository, "get_queue_stats", None)
        if not callable(snapshot_fn):
            return {}
        return snapshot_fn()

    def execute_once(self) -> str:
        execute_start = time.perf_counter()
        # Claim is isolated: a DB error here must NOT bubble up (would kill the worker thread).
        # No task is in 'processing' yet, so return "error" and let the runner retry on next tick.
        try:
            claim_start = time.perf_counter()
            claimed = self._task_service.claim_next_task()
        except Exception:
            self._logger.exception(
                "worker_claim_failed claim_elapsed_ms=%s total_elapsed_ms=%s",
                elapsed_ms(claim_start),
                elapsed_ms(execute_start),
            )
            return "error"

        if claimed is None:
            return "idle"
        if claimed.id is None:
            self._logger.error(
                "worker_task_missing_id customer_phone_masked=%s",
                mask_phone(claimed.customer_phonenumber),
            )
            return "error"

        task_id = claimed.id
        phone = claimed.customer_phonenumber

        with log_context(
            task_id=task_id,
            batch_id=claimed.batch_id,
            task_type=claimed.task_type,
            work_env=claimed.work_env,
            workflow_platform=self._platform_name,
        ):
            try:
                self._logger.info(
                    "worker_task_started customer_phone_masked=%s claim_elapsed_ms=%s",
                    mask_phone(phone),
                    elapsed_ms(claim_start),
                )
                workflow_start = time.perf_counter()
                self._logger.info("worker_workflow_invoke_started")
                output_raw = self._workflow_client.run_workflow(
                    customer_phonenumber=claimed.customer_phonenumber,
                    work_env=claimed.work_env,
                    batch_id=claimed.batch_id,
                    task_type=claimed.task_type,
                )
                self._logger.info(
                    "worker_workflow_invoke_completed elapsed_ms=%s output_size_bytes=%s",
                    elapsed_ms(workflow_start),
                    len(output_raw.encode("utf-8")),
                )

                parse_start = time.perf_counter()
                parsed = self._output_parser.parse(output_raw)
                self._logger.info(
                    "worker_output_parse_completed elapsed_ms=%s output_keys=%s",
                    elapsed_ms(parse_start),
                    sorted(parsed.output.keys()),
                )

                db_start = time.perf_counter()
                self._task_service.repository.mark_succeeded(
                    task_id=task_id,
                    output=parsed.output,
                    card_id=parsed.card_id,
                    nickname=parsed.nickname,
                )
                self._logger.info(
                    "worker_db_update_completed transition=processing_to_succeeded elapsed_ms=%s",
                    elapsed_ms(db_start),
                )
                self._logger.info(
                    "worker_task_completed status=succeeded customer_phone_masked=%s workflow_elapsed_ms=%s total_elapsed_ms=%s",
                    mask_phone(phone),
                    elapsed_ms(workflow_start),
                    elapsed_ms(execute_start),
                )
                return "succeeded"
            except (WorkflowTimeout, WorkflowRequestFailed, WorkflowOutputInvalidJson, WorkflowOutputSchemaMismatch) as exc:
                self._safe_mark_failed(task_id, f"{exc.code}: {str(exc)}")
                self._logger.warning(
                    "worker_task_completed status=failed customer_phone_masked=%s error_code=%s detail=%s total_elapsed_ms=%s",
                    mask_phone(phone),
                    exc.code,
                    safe_text(exc, 800),
                    elapsed_ms(execute_start),
                )
                return "failed"
            except WorkflowError as exc:
                self._safe_mark_failed(task_id, f"{exc.code}: {str(exc)}")
                self._logger.warning(
                    "worker_task_completed status=failed customer_phone_masked=%s error_code=%s detail=%s total_elapsed_ms=%s",
                    mask_phone(phone),
                    exc.code,
                    safe_text(exc, 800),
                    elapsed_ms(execute_start),
                )
                return "failed"
            except Exception as exc:
                # P0 safety net: an unexpected exception MUST NOT kill the worker thread.
                self._logger.exception(
                    "worker_task_unexpected_error customer_phone_masked=%s exc_type=%s total_elapsed_ms=%s",
                    mask_phone(phone),
                    type(exc).__name__,
                    elapsed_ms(execute_start),
                )
                detail = safe_text(f"{type(exc).__name__}: {str(exc)}", 800)
                self._safe_mark_failed(task_id, f"{_WORKER_UNEXPECTED_ERROR_CODE}: {detail}")
                return "failed"

    def _safe_mark_failed(self, task_id: int, error_message: str) -> None:
        """mark_failed must not itself raise back into the caller — that would re-introduce the bug we are fixing."""
        try:
            start = time.perf_counter()
            self._task_service.repository.mark_failed(
                task_id=task_id,
                error_message=error_message,
            )
            self._logger.info(
                "worker_db_update_completed transition=processing_to_failed elapsed_ms=%s",
                elapsed_ms(start),
            )
        except Exception:
            self._logger.exception(
                "worker_mark_failed_secondary_failure task_id=%s", task_id,
            )
