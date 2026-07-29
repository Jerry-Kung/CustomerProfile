from __future__ import annotations

import logging
import random
import threading
import time

from src.config.observability import elapsed_ms, log_context, mask_phone, safe_text
from src.config.settings import RetryConfig
from src.domain.task.models import TaskRecord
from src.domain.task.retry_policy import (
    LOCAL_FIRST_V1,
    SINGLE_TARGET_V1,
    attempt_limits,
    next_execution_pool,
)
from src.domain.task.service import TaskService
from src.infrastructure.qingtian.errors import (
    QingTianRequestFailed,
    QingTianTimeout,
    QingTianWorkflowTerminated,
)
from src.infrastructure.workflow.errors import (
    WorkflowError,
    WorkflowOutputInvalidJson,
    WorkflowOutputSchemaMismatch,
    WorkflowRequestFailed,
    WorkflowTimeout,
)
from src.infrastructure.workflow.output_parser import WorkflowOutputParser
from src.infrastructure.workflow.protocol import WorkflowClient
from src.worker.routing import CommercialQueueGate, QueuePressure
from src.worker.target_health import TargetCircuitBreaker


_WORKER_UNEXPECTED_ERROR_CODE = "WORKER_UNEXPECTED_ERROR"
_BACKOFF_JITTER_RATIO = 0.2


class _RetryLeaseLost(RuntimeError):
    """The worker no longer owns the claimed task, so it must stop polling."""


class WorkerExecutor:
    def __init__(
        self,
        *,
        task_service: TaskService,
        workflow_client: WorkflowClient,
        output_parser: WorkflowOutputParser,
        logger: logging.Logger,
        platform_name: str = "dify",
        worker_role: str = "local",
        workflow_target_id: str | None = None,
        workflow_target_revision: str = "v1",
        worker_target_mode: str = "active",
        worker_task_types: tuple[str, ...] = ("customer_profile", "invitation_strategy"),
        worker_work_envs: tuple[str, ...] = ("test", "production"),
        worker_target_failure_threshold: int = 3,
        worker_target_cooldown_seconds: int = 60,
        commercial_queue_gate: CommercialQueueGate | None = None,
        retry_config: RetryConfig | None = None,
    ):
        if worker_role not in {"local", "commercial"}:
            raise ValueError("worker_role must be local or commercial")
        if worker_role == "commercial" and commercial_queue_gate is None:
            raise ValueError("commercial worker requires a commercial_queue_gate")
        if worker_target_mode not in {"active", "draining", "recovery_only"}:
            raise ValueError("worker_target_mode must be active, draining, or recovery_only")
        self._task_service = task_service
        self._workflow_client = workflow_client
        self._output_parser = output_parser
        self._logger = logger
        self._platform_name = platform_name
        self._worker_role = worker_role
        self._workflow_target_id = workflow_target_id or f"{worker_role}-default"
        self._workflow_target_revision = workflow_target_revision
        self._worker_target_mode = worker_target_mode
        self._worker_task_types = worker_task_types
        self._worker_work_envs = worker_work_envs
        self._target_circuit = TargetCircuitBreaker(
            failure_threshold=worker_target_failure_threshold,
            cooldown_seconds=worker_target_cooldown_seconds,
        )
        self._commercial_queue_gate = commercial_queue_gate
        self._retry_config = retry_config or RetryConfig()
        self._retry_recovery_lock = threading.Lock()
        self._last_retry_recovery_monotonic = 0.0

    def operational_snapshot(self) -> dict[str, object]:
        snapshot_fn = getattr(self._task_service.repository, "get_queue_stats", None)
        snapshot = dict(snapshot_fn()) if callable(snapshot_fn) else {}
        snapshot.update(self._target_circuit.snapshot())
        return snapshot

    def execute_once(self) -> str:
        execute_start = time.perf_counter()
        retry_enabled = self._supports_qingtian_retry()
        self._maybe_recover_expired_retry_leases(enabled=retry_enabled)
        allow_unbound_work = (
            self._worker_target_mode == "active"
            and self._target_circuit.allow_unbound_work()
        )
        allow_new_tasks = allow_unbound_work and self._route_allows_new_tasks()
        allow_unbound_retries = allow_unbound_work
        if not retry_enabled and not allow_new_tasks:
            return "idle"
        # Claim is isolated: a DB error here must NOT bubble up (would kill the worker thread).
        # No task is in 'processing' yet, so return "error" and let the runner retry on next tick.
        try:
            claim_start = time.perf_counter()
            claimed = self._task_service.claim_next_task(
                retry_enabled=retry_enabled,
                lease_seconds=self._retry_config.lease_seconds if retry_enabled else None,
                execution_pool=self._worker_role,
                workflow_target_id=self._workflow_target_id,
                workflow_target_revision=self._workflow_target_revision,
                allow_new_tasks=allow_new_tasks,
                allow_unbound_retries=allow_unbound_retries,
                task_types=self._worker_task_types,
                work_envs=self._worker_work_envs,
            )
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
            execution_pool=claimed.execution_pool,
            workflow_target_id=claimed.workflow_target_id,
            retry_policy_version=claimed.retry_policy_version,
        ):
            if retry_enabled and claimed.claim_token:
                return self._execute_retryable_qingtian(
                    claimed=claimed,
                    execute_start=execute_start,
                    claim_start=claim_start,
                )
            try:
                self._logger.info(
                    "worker_task_started 说明=Worker开始处理已领取任务 customer_phone_masked=%s claim_elapsed_ms=%s",
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
                    "worker_task_completed 说明=任务处理完成并已成功回写任务表 status=succeeded customer_phone_masked=%s workflow_elapsed_ms=%s total_elapsed_ms=%s",
                    mask_phone(phone),
                    elapsed_ms(workflow_start),
                    elapsed_ms(execute_start),
                )
                return "succeeded"
            except (WorkflowTimeout, WorkflowRequestFailed, WorkflowOutputInvalidJson, WorkflowOutputSchemaMismatch) as exc:
                self._safe_mark_failed(task_id, f"{exc.code}: {str(exc)}")
                self._logger.warning(
                    "worker_task_completed 说明=任务处理失败，错误信息已回写任务表 status=failed customer_phone_masked=%s error_code=%s detail=%s total_elapsed_ms=%s",
                    mask_phone(phone),
                    exc.code,
                    safe_text(exc, 800),
                    elapsed_ms(execute_start),
                )
                return "failed"
            except WorkflowError as exc:
                self._safe_mark_failed(task_id, f"{exc.code}: {str(exc)}")
                self._logger.warning(
                    "worker_task_completed 说明=任务处理失败，错误信息已回写任务表 status=failed customer_phone_masked=%s error_code=%s detail=%s total_elapsed_ms=%s",
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

    def _supports_qingtian_retry(self) -> bool:
        if (
            self._platform_name != "qingtian"
        ):
            return False
        return all(
            callable(getattr(self._workflow_client, method, None))
            for method in ("submit_workflow", "poll_workflow")
        )

    def _maybe_recover_expired_retry_leases(self, *, enabled: bool) -> None:
        if not enabled:
            return
        now = time.monotonic()
        with self._retry_recovery_lock:
            if now - self._last_retry_recovery_monotonic < self._retry_config.recovery_interval_seconds:
                return
            self._last_retry_recovery_monotonic = now
        try:
            self._task_service.recover_expired_retry_leases(limit=100)
        except Exception:
            # A recovery scan is best effort.  Claiming is still safe because
            # the DB transaction and fencing token protect each active task.
            self._logger.exception("worker_retry_lease_recovery_failed")

    def _execute_retryable_qingtian(
        self,
        *,
        claimed: TaskRecord,
        execute_start: float,
        claim_start: float,
    ) -> str:
        """Run the repo-only QingTian recovery flow.

        The only remote protocol used here is the existing submit response's
        ``runId`` and the existing query endpoint.  No workflow input or
        QingTian-side behaviour is changed.
        """
        if claimed.id is None or not claimed.claim_token:
            self._logger.error("worker_retry_task_missing_claim_identity task_id=%s", claimed.id)
            return "error"

        task_id = claimed.id
        generation = claimed.generation
        claim_token = claimed.claim_token
        if claimed.execution_pool != self._worker_role:
            self._logger.error(
                "worker_retry_pool_mismatch task_id=%s claimed_pool=%s worker_pool=%s",
                task_id,
                claimed.execution_pool,
                self._worker_role,
            )
            return "error"
        if (
            claimed.workflow_target_id != self._workflow_target_id
            or claimed.workflow_target_revision != self._workflow_target_revision
        ):
            self._logger.error(
                "worker_retry_target_mismatch task_id=%s claimed_target=%s claimed_revision=%s worker_target=%s worker_revision=%s",
                task_id,
                claimed.workflow_target_id,
                claimed.workflow_target_revision,
                self._workflow_target_id,
                self._workflow_target_revision,
            )
            return "error"
        attempt_no: int | None = claimed.attempt_count or None
        local_attempt_count = claimed.local_attempt_count
        commercial_attempt_count = claimed.commercial_attempt_count
        workflow_run_id = claimed.workflow_run_id
        heartbeat = self._lease_heartbeat(
            task_id=task_id,
            generation=generation,
            claim_token=claim_token,
        )
        workflow_start = time.perf_counter()

        try:
            self._logger.info(
                "worker_retry_task_started task_id=%s generation=%s execution_pool=%s workflow_target_id=%s retry_policy_version=%s attempt_count=%s poll_recovery_count=%s has_workflow_run_id=%s claim_elapsed_ms=%s",
                task_id,
                generation,
                claimed.execution_pool,
                claimed.workflow_target_id,
                claimed.retry_policy_version,
                claimed.attempt_count,
                claimed.poll_recovery_count,
                bool(workflow_run_id),
                elapsed_ms(claim_start),
            )
            if workflow_run_id:
                if self._mark_stalled_if_run_lifetime_exceeded(
                    task_id=task_id,
                    generation=generation,
                    claim_token=claim_token,
                    attempt_no=attempt_no,
                ):
                    return "failed"
                output_raw = self._workflow_client.poll_workflow(
                    workflow_run_id=workflow_run_id,
                    work_env=claimed.work_env,
                    task_type=claimed.task_type,
                    on_poll=heartbeat,
                )
            else:
                limits = attempt_limits(
                    policy_version=claimed.retry_policy_version,
                    execution_pool=claimed.execution_pool,
                    max_total_attempts=self._retry_config.max_workflow_attempts,
                )
                attempt_no = self._task_service.repository.reserve_workflow_attempt(
                    task_id=task_id,
                    generation=generation,
                    claim_token=claim_token,
                    max_attempts=limits.max_total_attempts,
                    execution_pool=claimed.execution_pool,
                    workflow_target_id=self._workflow_target_id,
                    workflow_target_revision=self._workflow_target_revision,
                    retry_policy_version=claimed.retry_policy_version,
                    max_pool_attempts=limits.max_pool_attempts,
                    max_target_attempts=limits.max_target_attempts,
                    route_reason=self._route_reason(claimed),
                )
                if attempt_no is None:
                    return self._finish_retry_failure(
                        task_id=task_id,
                        generation=generation,
                        claim_token=claim_token,
                        attempt_no=None,
                        error_code="QINGTIAN_RETRY_EXHAUSTED",
                        error_stage="submit",
                        error_message="maximum QingTian workflow attempts exhausted",
                    )
                if claimed.execution_pool == "local":
                    local_attempt_count += 1
                else:
                    commercial_attempt_count += 1
                submitted_run_id = self._workflow_client.submit_workflow(
                    customer_phonenumber=claimed.customer_phonenumber,
                    work_env=claimed.work_env,
                    batch_id=claimed.batch_id,
                    task_type=claimed.task_type,
                )
                # Receiving a concrete run id is a successful target-level
                # exchange even if the later result poll times out.
                self._record_target_success()
                persisted = self._task_service.repository.persist_workflow_run(
                    task_id=task_id,
                    generation=generation,
                    claim_token=claim_token,
                    attempt_no=attempt_no,
                    workflow_run_id=submitted_run_id,
                )
                if not persisted:
                    # The remote run may exist, but we cannot safely poll or
                    # resubmit it until a durable id exists.  Lease recovery
                    # will surface this as QINGTIAN_SUBMIT_UNKNOWN.
                    self._logger.error(
                        "worker_qingtian_run_id_persist_rejected task_id=%s generation=%s attempt_no=%s",
                        task_id,
                        generation,
                        attempt_no,
                    )
                    return "error"
                workflow_run_id = submitted_run_id
                output_raw = self._workflow_client.poll_workflow(
                    workflow_run_id=workflow_run_id,
                    work_env=claimed.work_env,
                    task_type=claimed.task_type,
                    on_poll=heartbeat,
                )
            parsed = self._output_parser.parse(output_raw)
        except _RetryLeaseLost:
            self._logger.warning(
                "worker_retry_lease_lost task_id=%s generation=%s; stopping without a terminal write",
                task_id,
                generation,
            )
            return "error"
        except QingTianWorkflowTerminated as exc:
            self._record_target_success()
            return self._retry_new_run_or_fail(
                claimed=claimed,
                task_id=task_id,
                generation=generation,
                claim_token=claim_token,
                attempt_no=attempt_no,
                error_code=exc.code,
                error_stage="workflow",
                error_message=str(exc),
                local_attempt_count=local_attempt_count,
                commercial_attempt_count=commercial_attempt_count,
            )
        except (WorkflowOutputInvalidJson, WorkflowOutputSchemaMismatch) as exc:
            self._record_target_success()
            return self._retry_new_run_or_fail(
                claimed=claimed,
                task_id=task_id,
                generation=generation,
                claim_token=claim_token,
                attempt_no=attempt_no,
                error_code=exc.code,
                error_stage="output",
                error_message=str(exc),
                local_attempt_count=local_attempt_count,
                commercial_attempt_count=commercial_attempt_count,
            )
        except QingTianTimeout as exc:
            if exc.stage == "submit":
                self._record_target_failure(exc)
            if workflow_run_id:
                return self._schedule_retry(
                    task_id=task_id,
                    generation=generation,
                    claim_token=claim_token,
                    attempt_no=attempt_no,
                    error_code=exc.code,
                    error_stage="poll",
                    error_message=str(exc),
                    retain_workflow_run=True,
                    terminal_attempt=False,
                    poll_recovery_count=claimed.poll_recovery_count,
                    increment_poll_recovery=True,
                )
            return self._finish_retry_failure(
                task_id=task_id,
                generation=generation,
                claim_token=claim_token,
                attempt_no=attempt_no,
                error_code="QINGTIAN_SUBMIT_UNKNOWN",
                error_stage="submit",
                error_message=str(exc),
            )
        except QingTianRequestFailed as exc:
            # A definite submit-side 4xx such as malformed workflow input is
            # task-specific, not evidence that the target itself is unhealthy.
            # Authentication/rate-limit rejections are marked retryable by the
            # client and still count toward the target circuit.
            if not (
                exc.stage == "submit"
                and exc.status_code is not None
                and 400 <= exc.status_code < 500
                and not exc.retryable
            ):
                self._record_target_failure(exc)
            if workflow_run_id or exc.stage == "poll":
                return self._schedule_retry(
                    task_id=task_id,
                    generation=generation,
                    claim_token=claim_token,
                    attempt_no=attempt_no,
                    error_code=exc.code,
                    error_stage="poll",
                    error_message=str(exc),
                    retain_workflow_run=True,
                    terminal_attempt=False,
                    poll_recovery_count=claimed.poll_recovery_count,
                    increment_poll_recovery=True,
                    retry_after_seconds=exc.retry_after_seconds,
                )
            if exc.stage == "submit" and exc.retryable:
                return self._retry_new_run_or_fail(
                    claimed=claimed,
                    task_id=task_id,
                    generation=generation,
                    claim_token=claim_token,
                    attempt_no=attempt_no,
                    error_code=exc.code,
                    error_stage="submit",
                    error_message=str(exc),
                    retry_after_seconds=exc.retry_after_seconds,
                    local_attempt_count=local_attempt_count,
                    commercial_attempt_count=commercial_attempt_count,
                )
            if (
                exc.stage == "submit"
                and exc.status_code is not None
                and 400 <= exc.status_code < 500
            ):
                return self._finish_retry_failure(
                    task_id=task_id,
                    generation=generation,
                    claim_token=claim_token,
                    attempt_no=attempt_no,
                    error_code=exc.code,
                    error_stage="submit",
                    error_message=str(exc),
                )
            return self._finish_retry_failure(
                task_id=task_id,
                generation=generation,
                claim_token=claim_token,
                attempt_no=attempt_no,
                error_code="QINGTIAN_SUBMIT_UNKNOWN",
                error_stage="submit",
                error_message=str(exc),
            )
        except Exception:
            # Do not convert arbitrary local/DB failures into a remote retry:
            # a durable run id, if any, remains recoverable after its lease.
            self._logger.exception(
                "worker_retry_unexpected_execution_error task_id=%s generation=%s",
                task_id,
                generation,
            )
            return "error"

        self._record_target_success()
        try:
            persisted = self._task_service.repository.mark_succeeded_claimed(
                task_id=task_id,
                generation=generation,
                claim_token=claim_token,
                output=parsed.output,
                card_id=parsed.card_id,
                nickname=parsed.nickname,
                attempt_no=attempt_no,
            )
        except Exception:
            # The remote run is already durable; lease recovery will re-poll it
            # and retry only the local result persistence path.
            self._logger.exception(
                "worker_retry_result_persist_failed task_id=%s generation=%s",
                task_id,
                generation,
            )
            return "error"
        if not persisted:
            self._logger.warning(
                "worker_retry_result_persist_rejected task_id=%s generation=%s",
                task_id,
                generation,
            )
            return "error"
        self._logger.info(
            "worker_retry_task_completed task_id=%s generation=%s attempt_no=%s workflow_elapsed_ms=%s total_elapsed_ms=%s",
            task_id,
            generation,
            attempt_no,
            elapsed_ms(workflow_start),
            elapsed_ms(execute_start),
        )
        return "succeeded"

    def _lease_heartbeat(self, *, task_id: int, generation: int, claim_token: str):
        last_heartbeat = 0.0

        def heartbeat() -> None:
            nonlocal last_heartbeat
            now = time.monotonic()
            if now - last_heartbeat < self._retry_config.lease_heartbeat_seconds:
                return
            try:
                renewed = self._task_service.repository.refresh_lease(
                    task_id=task_id,
                    generation=generation,
                    claim_token=claim_token,
                    lease_seconds=self._retry_config.lease_seconds,
                )
            except Exception as exc:
                raise _RetryLeaseLost("could not refresh retry lease") from exc
            if not renewed:
                raise _RetryLeaseLost("retry lease was no longer owned")
            last_heartbeat = now

        return heartbeat

    def _retry_new_run_or_fail(
        self,
        *,
        claimed: TaskRecord,
        task_id: int,
        generation: int,
        claim_token: str,
        attempt_no: int | None,
        error_code: str,
        error_stage: str,
        error_message: str,
        local_attempt_count: int,
        commercial_attempt_count: int,
        retry_after_seconds: int | None = None,
    ) -> str:
        next_pool = (
            next_execution_pool(
                policy_version=claimed.retry_policy_version,
                attempt_count=attempt_no,
                local_attempt_count=local_attempt_count,
                commercial_attempt_count=commercial_attempt_count,
                max_total_attempts=self._retry_config.max_workflow_attempts,
            )
            if attempt_no is not None
            else None
        )
        if next_pool is None:
            return self._finish_retry_failure(
                task_id=task_id,
                generation=generation,
                claim_token=claim_token,
                attempt_no=attempt_no,
                error_code=("QINGTIAN_RETRY_EXHAUSTED" if attempt_no is not None else error_code),
                error_stage=error_stage,
                error_message=error_message,
            )
        return self._schedule_retry(
            task_id=task_id,
            generation=generation,
            claim_token=claim_token,
            attempt_no=attempt_no,
            error_code=error_code,
            error_stage=error_stage,
            error_message=error_message,
            retain_workflow_run=False,
            terminal_attempt=True,
            retry_after_seconds=retry_after_seconds,
            next_execution_pool=next_pool,
        )

    def _schedule_retry(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        attempt_no: int | None,
        error_code: str,
        error_stage: str,
        error_message: str,
        retain_workflow_run: bool,
        terminal_attempt: bool,
        poll_recovery_count: int = 0,
        increment_poll_recovery: bool = False,
        retry_after_seconds: int | None = None,
        next_execution_pool: str | None = None,
    ) -> str:
        if retain_workflow_run and increment_poll_recovery:
            # A scheduled recovery represents one more full local observation
            # window for the same remote run.  It is intentionally independent
            # from ``attempt_count``: no new QingTian workflow is submitted.
            if poll_recovery_count >= self._retry_config.max_poll_recoveries:
                return self._finish_retry_failure(
                    task_id=task_id,
                    generation=generation,
                    claim_token=claim_token,
                    attempt_no=attempt_no,
                    error_code="QINGTIAN_RUN_STALLED",
                    error_stage="poll",
                    error_message=(
                        "QINGTIAN_RUN_STALLED: workflow run reached the maximum "
                        f"of {self._retry_config.max_poll_recoveries} recovery polls; "
                        "no new QingTian workflow run was created"
                    ),
                )
            if self._mark_stalled_if_run_lifetime_exceeded(
                task_id=task_id,
                generation=generation,
                claim_token=claim_token,
                attempt_no=attempt_no,
            ):
                return "failed"

        attempt_index = max(0, (attempt_no or 1) - 1)
        backoff = self._retry_config.backoff_seconds[
            min(attempt_index, len(self._retry_config.backoff_seconds) - 1)
        ]
        jittered_backoff = max(
            1,
            int(
                round(
                    backoff
                    * random.uniform(
                        1.0 - _BACKOFF_JITTER_RATIO,
                        1.0 + _BACKOFF_JITTER_RATIO,
                    )
                )
            ),
        )
        delay_seconds = max(jittered_backoff, retry_after_seconds or 0)
        try:
            scheduled = self._task_service.repository.schedule_retry(
                task_id=task_id,
                generation=generation,
                claim_token=claim_token,
                error_code=error_code,
                error_stage=error_stage,
                error_message=error_message,
                delay_seconds=delay_seconds,
                retain_workflow_run=retain_workflow_run,
                terminal_attempt=terminal_attempt,
                attempt_no=attempt_no,
                increment_poll_recovery=increment_poll_recovery,
                next_execution_pool=next_execution_pool,
            )
        except Exception:
            self._logger.exception(
                "worker_retry_schedule_write_failed task_id=%s generation=%s",
                task_id,
                generation,
            )
            return "error"
        return "retrying" if scheduled else "error"

    def _mark_stalled_if_run_lifetime_exceeded(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        attempt_no: int | None,
    ) -> bool:
        """Use database time to enforce the lifetime of one persisted runId."""
        try:
            return self._task_service.repository.mark_stalled_if_run_lifetime_exceeded(
                task_id=task_id,
                generation=generation,
                claim_token=claim_token,
                attempt_no=attempt_no,
                max_run_lifetime_seconds=self._retry_config.max_run_lifetime_seconds,
            )
        except Exception:
            # We cannot safely continue polling if the durable guard could not
            # be evaluated; let lease recovery make the persisted run eligible
            # again rather than masking a database outage as a remote failure.
            self._logger.exception(
                "worker_retry_run_lifetime_check_failed task_id=%s generation=%s",
                task_id,
                generation,
            )
            raise

    def _finish_retry_failure(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        attempt_no: int | None,
        error_code: str,
        error_stage: str,
        error_message: str,
    ) -> str:
        try:
            failed = self._task_service.repository.mark_failed_claimed(
                task_id=task_id,
                generation=generation,
                claim_token=claim_token,
                error_code=error_code,
                error_stage=error_stage,
                error_message=error_message,
                attempt_no=attempt_no,
            )
        except Exception:
            self._logger.exception(
                "worker_retry_final_failure_write_failed task_id=%s generation=%s",
                task_id,
                generation,
            )
            return "error"
        return "failed" if failed else "error"

    def _route_reason(self, claimed: TaskRecord) -> str:
        if claimed.retry_policy_version == SINGLE_TARGET_V1:
            return "single_target_retry" if claimed.attempt_count else "single_target"
        if claimed.retry_policy_version != LOCAL_FIRST_V1:
            return "parallel_retry" if claimed.attempt_count else "parallel"
        if claimed.execution_pool == "commercial":
            return "commercial_fallback" if claimed.local_attempt_count else "burst"
        return "local_retry" if claimed.local_attempt_count else "normal"

    def _route_allows_new_tasks(self) -> bool:
        if self._worker_role == "local":
            return True
        assert self._commercial_queue_gate is not None
        try:
            snapshot = self.operational_snapshot()
            pressure = QueuePressure(
                queue_depth=int(snapshot.get("queue_depth") or 0),
                oldest_queued_age_seconds=(
                    int(snapshot["oldest_queued_age_seconds"])
                    if snapshot.get("oldest_queued_age_seconds") is not None
                    else None
                ),
            )
            allowed = self._commercial_queue_gate.allow_new(pressure)
            self._logger.debug(
                "commercial_queue_gate allowed=%s queue_depth=%s oldest_queued_age_seconds=%s",
                allowed,
                pressure.queue_depth,
                pressure.oldest_queued_age_seconds,
            )
            return allowed
        except Exception:
            self._logger.exception("commercial_queue_gate_check_failed")
            return False

    def _record_target_failure(self, exc: Exception) -> None:
        opened = self._target_circuit.record_failure()
        if opened:
            snapshot = self._target_circuit.snapshot()
            self._logger.warning(
                "workflow_target_circuit_opened workflow_target_id=%s failure_type=%s cooldown_seconds=%s",
                self._workflow_target_id,
                type(exc).__name__,
                snapshot["target_circuit_remaining_seconds"],
            )

    def _record_target_success(self) -> None:
        if self._target_circuit.record_success():
            self._logger.info(
                "workflow_target_circuit_reset workflow_target_id=%s",
                self._workflow_target_id,
            )

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
