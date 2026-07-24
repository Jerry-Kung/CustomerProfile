from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime

from src.config.observability import elapsed_ms, mask_phone
from src.domain.task.errors import TaskQueueFullError
from src.domain.task.models import TaskRecord
from src.infrastructure.db.client import MySQLClient

_QUEUE_ADMIT_LOCK_NAME = "cp_task_queue_admit"
_DEFAULT_TASK_TYPE = "customer_profile"


class MySQLTaskRepository:
    def __init__(
        self,
        db_client: MySQLClient,
        *,
        queue_max_waiting: int = 500,
        logger: logging.Logger | None = None,
    ):
        self._db_client = db_client
        self._queue_max_waiting = queue_max_waiting
        self._logger = logger or logging.getLogger(__name__)
        self._has_work_env_column_cache: bool | None = None
        self._has_batch_id_column_cache: bool | None = None
        self._has_task_type_column_cache: bool | None = None

    def upsert_queued(
        self,
        customer_phonenumber: str,
        *,
        work_env: str = "test",
        batch_id: str | None = None,
        task_type: str = _DEFAULT_TASK_TYPE,
    ) -> TaskRecord:
        start = time.perf_counter()
        conn = self._db_client.connect()
        lock_acquired = False
        try:
            has_work_env = self._has_work_env_column(conn)
            has_batch_id = self._has_batch_id_column(conn)
            has_task_type = self._has_task_type_column(conn)
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT GET_LOCK(%s, 10) AS acquired", (_QUEUE_ADMIT_LOCK_NAME,))
            lock_row = cursor.fetchone() or {}
            lock_acquired = int(lock_row.get("acquired") or 0) == 1
            if not lock_acquired:
                self._logger.warning(
                    "db_queue_admission_lock_timeout customer_phone_masked=%s lock_name=%s elapsed_ms=%s",
                    mask_phone(customer_phonenumber),
                    _QUEUE_ADMIT_LOCK_NAME,
                    elapsed_ms(start),
                )
                raise RuntimeError("failed to acquire queue admission lock")
            cursor.execute("START TRANSACTION")
            if has_task_type:
                cursor.execute(
                    """
                    SELECT status
                    FROM customer_profile_tasks
                    WHERE customer_phonenumber = %s AND task_type = %s
                    FOR UPDATE
                    """,
                    (customer_phonenumber, task_type),
                )
            else:
                cursor.execute(
                    """
                    SELECT status
                    FROM customer_profile_tasks
                    WHERE customer_phonenumber = %s
                    FOR UPDATE
                    """,
                    (customer_phonenumber,),
                )
            existed = cursor.fetchone()
            existed_status = str(existed.get("status")) if existed else None
            queued_count = self.count_queued(conn=conn)
            if existed_status != "queued" and queued_count >= self._queue_max_waiting:
                conn.rollback()
                self._logger.warning(
                    "db_queue_capacity_rejected customer_phone_masked=%s queued_count=%s max_waiting=%s existed_status=%s",
                    mask_phone(customer_phonenumber),
                    queued_count,
                    self._queue_max_waiting,
                    existed_status,
                )
                raise TaskQueueFullError(max_waiting=self._queue_max_waiting, queued_count=queued_count)
            self._execute_upsert(
                cursor=cursor,
                customer_phonenumber=customer_phonenumber,
                work_env=work_env,
                batch_id=batch_id,
                task_type=task_type,
                has_work_env=has_work_env,
                has_batch_id=has_batch_id,
                has_task_type=has_task_type,
            )
            conn.commit()
            cursor.execute(
                _select_task_by_phone_sql(has_work_env, has_batch_id, has_task_type),
                (customer_phonenumber, task_type) if has_task_type else (customer_phonenumber,),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("upsert succeeded but row not found")
            record = self._to_record(row)
            self._logger.info(
                "db_task_upsert_completed 说明=任务提交或重复提交已写入队列，previous_status非空时表示覆盖了已有任务 task_id=%s customer_phone_masked=%s previous_status=%s new_status=%s queued_count_before=%s elapsed_ms=%s",
                record.id,
                mask_phone(customer_phonenumber),
                existed_status,
                record.status,
                queued_count,
                elapsed_ms(start),
            )
            return record
        except TaskQueueFullError:
            raise
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception(
                "db_task_upsert_failed operation=upsert_queued customer_phone_masked=%s elapsed_ms=%s",
                mask_phone(customer_phonenumber),
                elapsed_ms(start),
            )
            raise
        finally:
            if lock_acquired:
                try:
                    unlock_cursor = conn.cursor()
                    unlock_cursor.execute("SELECT RELEASE_LOCK(%s)", (_QUEUE_ADMIT_LOCK_NAME,))
                except Exception:
                    pass
            conn.close()

    @staticmethod
    def _execute_upsert(
        *,
        cursor,
        customer_phonenumber: str,
        work_env: str,
        batch_id: str | None,
        task_type: str,
        has_work_env: bool,
        has_batch_id: bool,
        has_task_type: bool,
    ) -> None:
        columns = ["customer_phonenumber", "status", "queued_at"]
        placeholders = ["%s", "'queued'", "CURRENT_TIMESTAMP"]
        params: list[object] = [customer_phonenumber]
        update_clauses = [
            "status = VALUES(status)",
            "generation = generation + 1",
            "output = NULL",
            "card_id = NULL",
            "nickname = NULL",
            "error_message = NULL",
            "execution_backend = 'local'",
            "workflow_run_id = NULL",
            "workflow_run_started_at = NULL",
            "attempt_count = 0",
            "poll_recovery_count = 0",
            "next_retry_at = NULL",
            "last_error_code = NULL",
            "last_error_stage = NULL",
            "lease_expires_at = NULL",
            "claim_token = NULL",
            "queued_at = CURRENT_TIMESTAMP",
            "started_at = NULL",
            "finished_at = NULL",
            "updated_at = CURRENT_TIMESTAMP",
        ]
        if has_task_type:
            columns.append("task_type")
            placeholders.append("%s")
            params.append(task_type)
            update_clauses.append("task_type = VALUES(task_type)")
        if has_work_env:
            columns.append("work_env")
            placeholders.append("%s")
            params.append(work_env)
            update_clauses.append("work_env = VALUES(work_env)")
        if has_batch_id:
            columns.append("batch_id")
            placeholders.append("%s")
            params.append(batch_id)
            update_clauses.append("batch_id = VALUES(batch_id)")
        sql = (
            f"INSERT INTO customer_profile_tasks ({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"ON DUPLICATE KEY UPDATE {', '.join(update_clauses)}"
        )
        cursor.execute(sql, tuple(params))

    def claim_next_queued(self) -> TaskRecord | None:
        start = time.perf_counter()
        conn = self._db_client.connect()
        try:
            has_work_env = self._has_work_env_column(conn)
            has_batch_id = self._has_batch_id_column(conn)
            has_task_type = self._has_task_type_column(conn)
            cursor = conn.cursor(dictionary=True)
            cursor.execute("START TRANSACTION")
            cursor.execute(
                _select_next_queued_sql(has_work_env, has_batch_id, has_task_type)
            )
            row = cursor.fetchone()
            if row is None:
                conn.commit()
                return None
            cursor.execute(
                """
                UPDATE customer_profile_tasks
                SET status = 'processing', updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'queued'
                """,
                (row["id"],),
            )
            if cursor.rowcount != 1:
                conn.commit()
                self._logger.warning(
                    "db_task_claim_conflict task_id=%s affected_rows=%s elapsed_ms=%s",
                    row.get("id"),
                    cursor.rowcount,
                    elapsed_ms(start),
                )
                return None
            conn.commit()
            row["status"] = "processing"
            record = self._to_record(row)
            queued_at = row.get("updated_at")
            queue_wait_seconds = (
                max(0, int((datetime.now() - queued_at).total_seconds()))
                if isinstance(queued_at, datetime)
                else None
            )
            self._logger.info(
                "db_task_state_changed 说明=Worker从队列领取任务 task_id=%s transition=queued_to_processing customer_phone_masked=%s queue_wait_seconds=%s elapsed_ms=%s",
                record.id,
                mask_phone(record.customer_phonenumber),
                queue_wait_seconds,
                elapsed_ms(start),
            )
            return record
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception(
                "db_task_claim_failed operation=claim_next_queued elapsed_ms=%s",
                elapsed_ms(start),
            )
            raise
        finally:
            conn.close()

    def claim_next_eligible(
        self,
        *,
        lease_seconds: int,
        execution_backend: str,
        allow_new_tasks: bool,
    ) -> TaskRecord | None:
        """Claim either a new task or a due retry using a fenced worker lease.

        This path is used only by the opt-in QingTian retry executor.  The
        legacy ``claim_next_queued`` method remains intentionally unchanged for
        Dify and for deployments where retries are disabled.
        """
        start = time.perf_counter()
        conn = self._db_client.connect()
        try:
            has_work_env = self._has_work_env_column(conn)
            has_batch_id = self._has_batch_id_column(conn)
            has_task_type = self._has_task_type_column(conn)
            cursor = conn.cursor(dictionary=True)
            cursor.execute("START TRANSACTION")
            cursor.execute(
                _select_next_eligible_sql(has_work_env, has_batch_id, has_task_type),
                (execution_backend, int(allow_new_tasks)),
            )
            row = cursor.fetchone()
            if row is None:
                conn.commit()
                return None

            token = uuid.uuid4().hex
            cursor.execute(
                """
                UPDATE customer_profile_tasks
                SET status = 'processing',
                    execution_backend = %s,
                    claim_token = %s,
                    lease_expires_at = DATE_ADD(CURRENT_TIMESTAMP, INTERVAL %s SECOND),
                    started_at = CURRENT_TIMESTAMP,
                    next_retry_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND (
                    (
                        status = 'retry_wait'
                        AND next_retry_at <= CURRENT_TIMESTAMP
                        AND execution_backend = %s
                    )
                    OR (status = 'queued' AND %s = 1)
                  )
                """,
                (
                    execution_backend,
                    token,
                    lease_seconds,
                    row["id"],
                    execution_backend,
                    int(allow_new_tasks),
                ),
            )
            if cursor.rowcount != 1:
                conn.commit()
                self._logger.warning(
                    "db_retry_task_claim_conflict task_id=%s affected_rows=%s elapsed_ms=%s",
                    row.get("id"),
                    cursor.rowcount,
                    elapsed_ms(start),
                )
                return None
            conn.commit()

            row["status"] = "processing"
            row["claim_token"] = token
            row["execution_backend"] = execution_backend
            record = self._to_record(row)
            queued_at = row.get("queued_at") or row.get("updated_at")
            queue_wait_seconds = (
                max(0, int((datetime.now() - queued_at).total_seconds()))
                if isinstance(queued_at, datetime)
                else None
            )
            self._logger.info(
                "db_retry_task_claimed task_id=%s generation=%s execution_backend=%s attempt_count=%s has_workflow_run_id=%s queue_wait_seconds=%s lease_seconds=%s elapsed_ms=%s",
                record.id,
                record.generation,
                record.execution_backend,
                record.attempt_count,
                bool(record.workflow_run_id),
                queue_wait_seconds,
                lease_seconds,
                elapsed_ms(start),
            )
            return record
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception(
                "db_retry_task_claim_failed operation=claim_next_eligible elapsed_ms=%s",
                elapsed_ms(start),
            )
            raise
        finally:
            conn.close()

    def reserve_workflow_attempt(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        max_attempts: int,
        execution_backend: str,
        max_backend_attempts: int,
        route_reason: str,
    ) -> int | None:
        """Reserve the next remote submission before making the HTTP request.

        If the process dies after this reservation but before the remote run id
        is durable, recovery deliberately treats the submission as unknown and
        does not blindly submit again.
        """
        start = time.perf_counter()
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("START TRANSACTION")
            cursor.execute(
                """
                SELECT
                    task.attempt_count,
                    (
                        SELECT COUNT(1)
                        FROM customer_profile_task_attempts AS attempt
                        WHERE attempt.task_id = task.id
                          AND attempt.generation = task.generation
                          AND attempt.execution_backend = %s
                    ) AS backend_attempt_count
                FROM customer_profile_tasks AS task
                WHERE task.id = %s
                  AND task.generation = %s
                  AND task.claim_token = %s
                  AND task.status = 'processing'
                  AND task.execution_backend = %s
                FOR UPDATE
                """,
                (execution_backend, task_id, generation, claim_token, execution_backend),
            )
            row = cursor.fetchone()
            if (
                row is None
                or int(row.get("attempt_count") or 0) >= max_attempts
                or int(row.get("backend_attempt_count") or 0) >= max_backend_attempts
            ):
                conn.commit()
                return None
            attempt_no = int(row.get("attempt_count") or 0) + 1
            cursor.execute(
                """
                UPDATE customer_profile_tasks
                SET attempt_count = %s,
                    workflow_run_id = NULL,
                    workflow_run_started_at = NULL,
                    poll_recovery_count = 0,
                    next_retry_at = NULL,
                    last_error_code = NULL,
                    last_error_stage = NULL,
                    error_message = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND generation = %s
                  AND claim_token = %s
                  AND status = 'processing'
                  AND execution_backend = %s
                """,
                (attempt_no, task_id, generation, claim_token, execution_backend),
            )
            if cursor.rowcount != 1:
                conn.commit()
                return None
            cursor.execute(
                """
                INSERT INTO customer_profile_task_attempts
                    (
                        task_id, generation, attempt_no, execution_backend,
                        route_reason, status, started_at
                    )
                VALUES (%s, %s, %s, %s, %s, 'submitting', CURRENT_TIMESTAMP)
                """,
                (
                    task_id,
                    generation,
                    attempt_no,
                    execution_backend,
                    route_reason[:32],
                ),
            )
            conn.commit()
            self._logger.info(
                "db_workflow_attempt_reserved task_id=%s generation=%s attempt_no=%s elapsed_ms=%s",
                task_id,
                generation,
                attempt_no,
                elapsed_ms(start),
            )
            return attempt_no
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception(
                "db_workflow_attempt_reserve_failed task_id=%s generation=%s elapsed_ms=%s",
                task_id,
                generation,
                elapsed_ms(start),
            )
            raise
        finally:
            conn.close()

    def persist_workflow_run(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        attempt_no: int,
        workflow_run_id: str,
    ) -> bool:
        """Durably associate a QingTian runId before any result polling."""
        start = time.perf_counter()
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("START TRANSACTION")
            cursor.execute(
                """
                UPDATE customer_profile_tasks
                SET workflow_run_id = %s,
                    workflow_run_started_at = CURRENT_TIMESTAMP,
                    poll_recovery_count = 0,
                    next_retry_at = NULL,
                    last_error_code = NULL,
                    last_error_stage = NULL,
                    error_message = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND generation = %s
                  AND claim_token = %s
                  AND status = 'processing'
                """,
                (workflow_run_id, task_id, generation, claim_token),
            )
            updated = cursor.rowcount == 1
            if updated:
                cursor.execute(
                    """
                    UPDATE customer_profile_task_attempts
                    SET status = 'running', workflow_run_id = %s
                    WHERE task_id = %s AND generation = %s AND attempt_no = %s
                    """,
                    (workflow_run_id, task_id, generation, attempt_no),
                )
                updated = cursor.rowcount == 1
            if updated:
                conn.commit()
            else:
                conn.rollback()
            if updated:
                self._logger.info(
                    "db_workflow_run_persisted task_id=%s generation=%s attempt_no=%s run_id=%s elapsed_ms=%s",
                    task_id,
                    generation,
                    attempt_no,
                    workflow_run_id,
                    elapsed_ms(start),
                )
            return updated
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception(
                "db_workflow_run_persist_failed task_id=%s generation=%s attempt_no=%s elapsed_ms=%s",
                task_id,
                generation,
                attempt_no,
                elapsed_ms(start),
            )
            raise
        finally:
            conn.close()

    def refresh_lease(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        lease_seconds: int,
    ) -> bool:
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE customer_profile_tasks
                SET lease_expires_at = DATE_ADD(CURRENT_TIMESTAMP, INTERVAL %s SECOND),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND generation = %s
                  AND claim_token = %s
                  AND status = 'processing'
                """,
                (lease_seconds, task_id, generation, claim_token),
            )
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def mark_succeeded_claimed(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        output: dict,
        card_id: str | None,
        nickname: str | None,
        attempt_no: int | None,
    ) -> bool:
        start = time.perf_counter()
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("START TRANSACTION")
            cursor.execute(
                """
                UPDATE customer_profile_tasks
                SET status = 'succeeded',
                    output = %s,
                    card_id = %s,
                    nickname = %s,
                    error_message = NULL,
                    last_error_code = NULL,
                    last_error_stage = NULL,
                    next_retry_at = NULL,
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND generation = %s
                  AND claim_token = %s
                  AND status = 'processing'
                """,
                (json.dumps(output, ensure_ascii=False), card_id, nickname, task_id, generation, claim_token),
            )
            updated = cursor.rowcount == 1
            if updated and attempt_no is not None:
                cursor.execute(
                    """
                    UPDATE customer_profile_task_attempts
                    SET status = 'succeeded', finished_at = CURRENT_TIMESTAMP
                    WHERE task_id = %s AND generation = %s AND attempt_no = %s
                    """,
                    (task_id, generation, attempt_no),
                )
            if updated:
                conn.commit()
            else:
                conn.rollback()
            if updated:
                self._logger.info(
                    "db_retry_task_succeeded task_id=%s generation=%s attempt_no=%s output_size_bytes=%s elapsed_ms=%s",
                    task_id,
                    generation,
                    attempt_no,
                    len(json.dumps(output, ensure_ascii=False).encode("utf-8")),
                    elapsed_ms(start),
                )
            return updated
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception(
                "db_retry_task_succeed_failed task_id=%s generation=%s elapsed_ms=%s",
                task_id,
                generation,
                elapsed_ms(start),
            )
            raise
        finally:
            conn.close()

    def mark_failed_claimed(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        error_code: str,
        error_stage: str,
        error_message: str,
        attempt_no: int | None,
    ) -> bool:
        return self._finish_claimed_failure(
            task_id=task_id,
            generation=generation,
            claim_token=claim_token,
            error_code=error_code,
            error_stage=error_stage,
            error_message=error_message,
            attempt_no=attempt_no,
            terminal_attempt=True,
            retryable=False,
        )

    def schedule_retry(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        error_code: str,
        error_stage: str,
        error_message: str,
        delay_seconds: int,
        retain_workflow_run: bool,
        terminal_attempt: bool,
        attempt_no: int | None,
        increment_poll_recovery: bool = False,
        next_execution_backend: str | None = None,
    ) -> bool:
        start = time.perf_counter()
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("START TRANSACTION")
            workflow_run_clause = "workflow_run_id = workflow_run_id" if retain_workflow_run else "workflow_run_id = NULL"
            workflow_run_started_clause = (
                "workflow_run_started_at = COALESCE(workflow_run_started_at, CURRENT_TIMESTAMP)"
                if retain_workflow_run
                else "workflow_run_started_at = NULL"
            )
            if not retain_workflow_run:
                poll_recovery_clause = "poll_recovery_count = 0"
            elif increment_poll_recovery:
                poll_recovery_clause = "poll_recovery_count = poll_recovery_count + 1"
            else:
                poll_recovery_clause = "poll_recovery_count = poll_recovery_count"
            cursor.execute(
                f"""
                UPDATE customer_profile_tasks
                SET status = 'retry_wait',
                    execution_backend = COALESCE(%s, execution_backend),
                    {workflow_run_clause},
                    {workflow_run_started_clause},
                    {poll_recovery_clause},
                    next_retry_at = DATE_ADD(CURRENT_TIMESTAMP, INTERVAL %s SECOND),
                    error_message = %s,
                    last_error_code = %s,
                    last_error_stage = %s,
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND generation = %s
                  AND claim_token = %s
                  AND status = 'processing'
                """,
                (
                    next_execution_backend,
                    delay_seconds,
                    error_message[:1000],
                    error_code[:128],
                    error_stage[:32],
                    task_id,
                    generation,
                    claim_token,
                ),
            )
            updated = cursor.rowcount == 1
            if updated and terminal_attempt and attempt_no is not None:
                cursor.execute(
                    """
                    UPDATE customer_profile_task_attempts
                    SET status = 'failed',
                        failure_stage = %s,
                        error_code = %s,
                        error_message = %s,
                        retryable = 1,
                        finished_at = CURRENT_TIMESTAMP
                    WHERE task_id = %s AND generation = %s AND attempt_no = %s
                    """,
                    (
                        error_stage[:32],
                        error_code[:128],
                        error_message[:1000],
                        task_id,
                        generation,
                        attempt_no,
                    ),
                )
            if updated:
                conn.commit()
            else:
                conn.rollback()
            if updated:
                self._logger.warning(
                    "db_retry_task_scheduled task_id=%s generation=%s attempt_no=%s next_execution_backend=%s error_code=%s error_stage=%s retain_workflow_run=%s increment_poll_recovery=%s delay_seconds=%s elapsed_ms=%s",
                    task_id,
                    generation,
                    attempt_no,
                    next_execution_backend,
                    error_code,
                    error_stage,
                    retain_workflow_run,
                    increment_poll_recovery,
                    delay_seconds,
                    elapsed_ms(start),
                )
            return updated
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception(
                "db_retry_task_schedule_failed task_id=%s generation=%s elapsed_ms=%s",
                task_id,
                generation,
                elapsed_ms(start),
            )
            raise
        finally:
            conn.close()

    def mark_stalled_if_run_lifetime_exceeded(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        attempt_no: int | None,
        max_run_lifetime_seconds: int,
    ) -> bool:
        """Finalise a known run that exceeded its configured observation lifetime.

        This uses MySQL time rather than application-host time so an API/Worker
        deployment with a different timezone cannot accidentally keep a run
        alive forever or terminate it early.
        """
        start = time.perf_counter()
        error_code = "QINGTIAN_RUN_STALLED"
        error_stage = "poll"
        error_message = (
            "QINGTIAN_RUN_STALLED: workflow run exceeded configured observation "
            f"lifetime of {max_run_lifetime_seconds} seconds"
        )
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("START TRANSACTION")
            cursor.execute(
                """
                UPDATE customer_profile_tasks
                SET status = 'failed',
                    error_message = %s,
                    last_error_code = %s,
                    last_error_stage = %s,
                    next_retry_at = NULL,
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND generation = %s
                  AND claim_token = %s
                  AND status = 'processing'
                  AND workflow_run_id IS NOT NULL
                  AND workflow_run_started_at IS NOT NULL
                  AND workflow_run_started_at <= DATE_SUB(CURRENT_TIMESTAMP, INTERVAL %s SECOND)
                """,
                (
                    error_message,
                    error_code,
                    error_stage,
                    task_id,
                    generation,
                    claim_token,
                    max_run_lifetime_seconds,
                ),
            )
            updated = cursor.rowcount == 1
            if updated and attempt_no is not None:
                cursor.execute(
                    """
                    UPDATE customer_profile_task_attempts
                    SET status = 'failed',
                        failure_stage = %s,
                        error_code = %s,
                        error_message = %s,
                        retryable = 0,
                        finished_at = CURRENT_TIMESTAMP
                    WHERE task_id = %s AND generation = %s AND attempt_no = %s
                    """,
                    (
                        error_stage,
                        error_code,
                        error_message,
                        task_id,
                        generation,
                        attempt_no,
                    ),
                )
            if updated:
                conn.commit()
            else:
                conn.rollback()
            if updated:
                self._logger.warning(
                    "db_qingtian_run_stalled task_id=%s generation=%s attempt_no=%s max_run_lifetime_seconds=%s elapsed_ms=%s",
                    task_id,
                    generation,
                    attempt_no,
                    max_run_lifetime_seconds,
                    elapsed_ms(start),
                )
            return updated
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception(
                "db_qingtian_run_stalled_write_failed task_id=%s generation=%s elapsed_ms=%s",
                task_id,
                generation,
                elapsed_ms(start),
            )
            raise
        finally:
            conn.close()

    def recover_expired_retry_leases(self, *, limit: int) -> tuple[int, int]:
        """Recover known remote runs and safely finalise unknown submissions.

        A task with a durable run id can be polled again.  A claim with no run id
        and no reserved attempt has definitely not sent a remote request yet, so
        it can be requeued safely.  Once an attempt is reserved, a missing run
        id may mean that the remote submission succeeded just before a crash;
        that case is *not* blindly resubmitted.
        """
        start = time.perf_counter()
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("START TRANSACTION")
            cursor.execute(
                """
                UPDATE customer_profile_tasks
                SET status = 'retry_wait',
                    workflow_run_started_at = COALESCE(workflow_run_started_at, CURRENT_TIMESTAMP),
                    next_retry_at = CURRENT_TIMESTAMP,
                    error_message = 'WORKER_LEASE_EXPIRED: resuming persisted workflow run',
                    last_error_code = 'WORKER_LEASE_EXPIRED',
                    last_error_stage = 'poll',
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'processing'
                  AND workflow_run_id IS NOT NULL
                  AND lease_expires_at < CURRENT_TIMESTAMP
                ORDER BY lease_expires_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            resumed = int(cursor.rowcount)
            remaining = max(0, limit - resumed)
            requeued_before_submit = 0
            if remaining:
                cursor.execute(
                    """
                    UPDATE customer_profile_tasks
                    SET status = 'retry_wait',
                        next_retry_at = CURRENT_TIMESTAMP,
                        error_message = 'WORKER_LEASE_EXPIRED: retrying before workflow submission was reserved',
                        last_error_code = 'WORKER_LEASE_EXPIRED',
                        last_error_stage = 'claim',
                        lease_expires_at = NULL,
                        claim_token = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'processing'
                      AND workflow_run_id IS NULL
                      AND attempt_count = 0
                      AND lease_expires_at < CURRENT_TIMESTAMP
                    ORDER BY lease_expires_at ASC
                    LIMIT %s
                    """,
                    (remaining,),
                )
                requeued_before_submit = int(cursor.rowcount)
                remaining = max(0, remaining - requeued_before_submit)
            unknown = 0
            if remaining:
                cursor.execute(
                    """
                    UPDATE customer_profile_tasks
                    SET status = 'failed',
                        error_message = 'QINGTIAN_SUBMIT_UNKNOWN: worker lease expired before workflow run id was persisted',
                        last_error_code = 'QINGTIAN_SUBMIT_UNKNOWN',
                        last_error_stage = 'submit',
                        next_retry_at = NULL,
                        lease_expires_at = NULL,
                        claim_token = NULL,
                        finished_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'processing'
                      AND workflow_run_id IS NULL
                      AND attempt_count > 0
                      AND lease_expires_at < CURRENT_TIMESTAMP
                    ORDER BY lease_expires_at ASC
                    LIMIT %s
                    """,
                    (remaining,),
                )
                unknown = int(cursor.rowcount)
                if unknown:
                    cursor.execute(
                        """
                        UPDATE customer_profile_task_attempts AS attempt
                        INNER JOIN customer_profile_tasks AS task
                            ON task.id = attempt.task_id
                           AND task.generation = attempt.generation
                           AND task.attempt_count = attempt.attempt_no
                        SET attempt.status = 'abandoned',
                            attempt.failure_stage = 'submit',
                            attempt.error_code = 'QINGTIAN_SUBMIT_UNKNOWN',
                            attempt.error_message = 'worker lease expired before workflow run id was persisted',
                            attempt.retryable = 0,
                            attempt.finished_at = CURRENT_TIMESTAMP
                        WHERE attempt.status = 'submitting'
                          AND task.status = 'failed'
                          AND task.last_error_code = 'QINGTIAN_SUBMIT_UNKNOWN'
                        """
                    )
            conn.commit()
            if resumed or requeued_before_submit or unknown:
                self._logger.warning(
                    "db_retry_lease_recovered resumed_runs=%s requeued_before_submit=%s unknown_submissions_failed=%s limit=%s elapsed_ms=%s",
                    resumed,
                    requeued_before_submit,
                    unknown,
                    limit,
                    elapsed_ms(start),
                )
            return resumed + requeued_before_submit, unknown
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception("db_retry_lease_recovery_failed limit=%s elapsed_ms=%s", limit, elapsed_ms(start))
            raise
        finally:
            conn.close()

    def _finish_claimed_failure(
        self,
        *,
        task_id: int,
        generation: int,
        claim_token: str,
        error_code: str,
        error_stage: str,
        error_message: str,
        attempt_no: int | None,
        terminal_attempt: bool,
        retryable: bool,
    ) -> bool:
        start = time.perf_counter()
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("START TRANSACTION")
            cursor.execute(
                """
                UPDATE customer_profile_tasks
                SET status = 'failed',
                    error_message = %s,
                    last_error_code = %s,
                    last_error_stage = %s,
                    next_retry_at = NULL,
                    lease_expires_at = NULL,
                    claim_token = NULL,
                    finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND generation = %s
                  AND claim_token = %s
                  AND status = 'processing'
                """,
                (
                    error_message[:1000],
                    error_code[:128],
                    error_stage[:32],
                    task_id,
                    generation,
                    claim_token,
                ),
            )
            updated = cursor.rowcount == 1
            if updated and terminal_attempt and attempt_no is not None:
                cursor.execute(
                    """
                    UPDATE customer_profile_task_attempts
                    SET status = 'failed',
                        failure_stage = %s,
                        error_code = %s,
                        error_message = %s,
                        retryable = %s,
                        finished_at = CURRENT_TIMESTAMP
                    WHERE task_id = %s AND generation = %s AND attempt_no = %s
                    """,
                    (
                        error_stage[:32],
                        error_code[:128],
                        error_message[:1000],
                        int(retryable),
                        task_id,
                        generation,
                        attempt_no,
                    ),
                )
            if updated:
                conn.commit()
            else:
                conn.rollback()
            if updated:
                self._logger.warning(
                    "db_retry_task_failed task_id=%s generation=%s attempt_no=%s error_code=%s error_stage=%s elapsed_ms=%s",
                    task_id,
                    generation,
                    attempt_no,
                    error_code,
                    error_stage,
                    elapsed_ms(start),
                )
            return updated
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception(
                "db_retry_task_fail_write_failed task_id=%s generation=%s elapsed_ms=%s",
                task_id,
                generation,
                elapsed_ms(start),
            )
            raise
        finally:
            conn.close()

    def list_by_prefix(
        self,
        *,
        phone_prefix: str,
        page: int,
        page_size: int,
        task_type: str | None = None,
    ) -> tuple[list[TaskRecord], int]:
        offset = (page - 1) * page_size
        conn = self._db_client.connect()
        try:
            has_task_type = self._has_task_type_column(conn)
            cursor = conn.cursor(dictionary=True)
            select_cols = "id, customer_phonenumber, status, updated_at, card_id, nickname"
            if has_task_type:
                select_cols += ", task_type"
            where_clause = "WHERE customer_phonenumber LIKE CONCAT(%s, '%%')"
            params: list[object] = [phone_prefix]
            if has_task_type and task_type is not None:
                where_clause += " AND task_type = %s"
                params.append(task_type)
            cursor.execute(
                f"SELECT {select_cols} FROM customer_profile_tasks {where_clause} "
                f"ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                (*params, page_size, offset),
            )
            rows = cursor.fetchall() or []
            cursor.execute(
                f"SELECT COUNT(1) AS total FROM customer_profile_tasks {where_clause}",
                tuple(params),
            )
            count_row = cursor.fetchone() or {"total": 0}
            return [self._to_record(row) for row in rows], int(count_row["total"])
        finally:
            conn.close()

    def count_queued(self, conn=None) -> int:
        owns_conn = conn is None
        db_conn = conn or self._db_client.connect()
        try:
            cursor = db_conn.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT COUNT(1) AS total
                FROM customer_profile_tasks
                WHERE status = 'queued'
                """
            )
            row = cursor.fetchone() or {"total": 0}
            return int(row["total"])
        finally:
            if owns_conn:
                db_conn.close()

    def get_by_phone(
        self,
        customer_phonenumber: str,
        *,
        task_type: str = _DEFAULT_TASK_TYPE,
    ) -> TaskRecord | None:
        conn = self._db_client.connect()
        try:
            has_work_env = self._has_work_env_column(conn)
            has_batch_id = self._has_batch_id_column(conn)
            has_task_type = self._has_task_type_column(conn)
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                _select_task_by_phone_sql(has_work_env, has_batch_id, has_task_type),
                (customer_phonenumber, task_type) if has_task_type else (customer_phonenumber,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._to_record(row)
        finally:
            conn.close()

    def mark_succeeded(
        self,
        *,
        task_id: int,
        output: dict,
        card_id: str | None,
        nickname: str | None,
    ) -> None:
        start = time.perf_counter()
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE customer_profile_tasks
                SET status = 'succeeded',
                    output = %s,
                    card_id = %s,
                    nickname = %s,
                    error_message = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (json.dumps(output, ensure_ascii=False), card_id, nickname, task_id),
            )
            conn.commit()
            if cursor.rowcount != 1:
                self._logger.warning(
                    "db_task_state_change_unexpected task_id=%s transition=processing_to_succeeded affected_rows=%s",
                    task_id,
                    cursor.rowcount,
                )
            self._logger.info(
                "db_task_state_changed 说明=任务结果校验通过并成功回写任务表 task_id=%s transition=processing_to_succeeded affected_rows=%s output_size_bytes=%s elapsed_ms=%s",
                task_id,
                cursor.rowcount,
                len(json.dumps(output, ensure_ascii=False).encode("utf-8")),
                elapsed_ms(start),
            )
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception(
                "db_task_state_change_failed task_id=%s transition=processing_to_succeeded elapsed_ms=%s",
                task_id,
                elapsed_ms(start),
            )
            raise
        finally:
            conn.close()

    def mark_failed(self, *, task_id: int, error_message: str) -> None:
        start = time.perf_counter()
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE customer_profile_tasks
                SET status = 'failed',
                    error_message = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (error_message[:1000], task_id),
            )
            conn.commit()
            if cursor.rowcount != 1:
                self._logger.warning(
                    "db_task_state_change_unexpected task_id=%s transition=processing_to_failed affected_rows=%s",
                    task_id,
                    cursor.rowcount,
                )
            self._logger.info(
                "db_task_state_changed 说明=任务调用或结果校验失败并回写任务表 task_id=%s transition=processing_to_failed affected_rows=%s elapsed_ms=%s",
                task_id,
                cursor.rowcount,
                elapsed_ms(start),
            )
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception(
                "db_task_state_change_failed task_id=%s transition=processing_to_failed elapsed_ms=%s",
                task_id,
                elapsed_ms(start),
            )
            raise
        finally:
            conn.close()

    def requeue_stale_processing(self, *, timeout_seconds: int, limit: int) -> int:
        start = time.perf_counter()
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE customer_profile_tasks
                SET status = 'queued',
                    error_message = 'PROCESSING_TIMEOUT_RECOVERED',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'processing'
                  AND lease_expires_at IS NULL
                  AND updated_at < DATE_SUB(CURRENT_TIMESTAMP, INTERVAL %s SECOND)
                ORDER BY updated_at ASC
                LIMIT %s
                """,
                (timeout_seconds, limit),
            )
            conn.commit()
            affected = int(cursor.rowcount)
            if affected:
                self._logger.warning(
                    "db_stale_tasks_requeued affected=%s timeout_seconds=%s limit=%s elapsed_ms=%s",
                    affected,
                    timeout_seconds,
                    limit,
                    elapsed_ms(start),
                )
            return affected
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._logger.exception(
                "db_stale_tasks_requeue_failed timeout_seconds=%s limit=%s elapsed_ms=%s",
                timeout_seconds,
                limit,
                elapsed_ms(start),
            )
            raise
        finally:
            conn.close()

    def get_queue_stats(self) -> dict[str, object]:
        start = time.perf_counter()
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT
                    SUM(status = 'queued') AS queue_depth,
                    SUM(status = 'processing') AS processing_count,
                    SUM(status = 'retry_wait') AS retry_wait_count,
                    SUM(status = 'succeeded') AS succeeded_count,
                    SUM(status = 'failed') AS failed_count,
                    SUM(
                        status = 'processing'
                        AND updated_at < DATE_SUB(CURRENT_TIMESTAMP, INTERVAL 40 MINUTE)
                    ) AS processing_over_40m_count,
                    TIMESTAMPDIFF(
                        SECOND,
                        MIN(CASE WHEN status = 'queued' THEN updated_at END),
                        CURRENT_TIMESTAMP
                    ) AS oldest_queued_age_seconds,
                    TIMESTAMPDIFF(
                        SECOND,
                        MIN(CASE WHEN status = 'processing' THEN updated_at END),
                        CURRENT_TIMESTAMP
                    ) AS oldest_processing_age_seconds
                FROM customer_profile_tasks
                """
            )
            row = cursor.fetchone() or {}
            return {
                "queue_depth": int(row.get("queue_depth") or 0),
                "processing_count": int(row.get("processing_count") or 0),
                "retry_wait_count": int(row.get("retry_wait_count") or 0),
                "succeeded_count": int(row.get("succeeded_count") or 0),
                "failed_count": int(row.get("failed_count") or 0),
                "processing_over_40m_count": int(row.get("processing_over_40m_count") or 0),
                "oldest_queued_age_seconds": (
                    int(row["oldest_queued_age_seconds"])
                    if row.get("oldest_queued_age_seconds") is not None
                    else None
                ),
                "oldest_processing_age_seconds": (
                    int(row["oldest_processing_age_seconds"])
                    if row.get("oldest_processing_age_seconds") is not None
                    else None
                ),
            }
        except Exception:
            self._logger.exception(
                "db_queue_stats_failed elapsed_ms=%s",
                elapsed_ms(start),
            )
            raise
        finally:
            conn.close()

    def _has_work_env_column(self, conn) -> bool:
        if self._has_work_env_column_cache is not None:
            return self._has_work_env_column_cache
        self._has_work_env_column_cache = _column_exists(conn, "work_env")
        return self._has_work_env_column_cache

    def _has_batch_id_column(self, conn) -> bool:
        if self._has_batch_id_column_cache is not None:
            return self._has_batch_id_column_cache
        self._has_batch_id_column_cache = _column_exists(conn, "batch_id")
        return self._has_batch_id_column_cache

    def _has_task_type_column(self, conn) -> bool:
        if self._has_task_type_column_cache is not None:
            return self._has_task_type_column_cache
        self._has_task_type_column_cache = _column_exists(conn, "task_type")
        return self._has_task_type_column_cache

    @staticmethod
    def _to_record(row: dict) -> TaskRecord:
        output = row.get("output")
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError:
                output = None
        return TaskRecord(
            id=row.get("id"),
            customer_phonenumber=row["customer_phonenumber"],
            status=row["status"],
            updated_at=_to_iso(row.get("updated_at")),
            output=output,
            card_id=row.get("card_id"),
            nickname=row.get("nickname"),
            error_message=row.get("error_message"),
            work_env=(row.get("work_env") or "test"),
            batch_id=row.get("batch_id"),
            task_type=(row.get("task_type") or _DEFAULT_TASK_TYPE),
            generation=int(row.get("generation") or 1),
            execution_backend=(row.get("execution_backend") or "local"),
            workflow_run_id=row.get("workflow_run_id"),
            attempt_count=int(row.get("attempt_count") or 0),
            poll_recovery_count=int(row.get("poll_recovery_count") or 0),
            workflow_run_started_at=_to_iso(row.get("workflow_run_started_at")),
            next_retry_at=_to_iso(row.get("next_retry_at")),
            last_error_code=row.get("last_error_code"),
            last_error_stage=row.get("last_error_stage"),
            lease_expires_at=_to_iso(row.get("lease_expires_at")),
            claim_token=row.get("claim_token"),
            queued_at=_to_iso(row.get("queued_at")),
            started_at=_to_iso(row.get("started_at")),
            finished_at=_to_iso(row.get("finished_at")),
        )


def _column_exists(conn, column_name: str) -> bool:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = 'customer_profile_tasks'
          AND column_name = %s
        LIMIT 1
        """,
        (column_name,),
    )
    return cursor.fetchone() is not None


def _to_iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _select_task_by_phone_sql(has_work_env: bool, has_batch_id: bool, has_task_type: bool) -> str:
    work_env_expr = "work_env" if has_work_env else "'test' AS work_env"
    batch_id_expr = "batch_id" if has_batch_id else "NULL AS batch_id"
    task_type_expr = "task_type" if has_task_type else f"'{_DEFAULT_TASK_TYPE}' AS task_type"
    where_clause = (
        "WHERE customer_phonenumber = %s AND task_type = %s"
        if has_task_type
        else "WHERE customer_phonenumber = %s"
    )
    return f"""
                SELECT
                    id, customer_phonenumber, status, updated_at, output, card_id, nickname, error_message,
                    {work_env_expr},
                    {batch_id_expr},
                    {task_type_expr},
                    generation, execution_backend, workflow_run_id, attempt_count, poll_recovery_count,
                    workflow_run_started_at, next_retry_at,
                    last_error_code, last_error_stage, lease_expires_at, claim_token,
                    queued_at, started_at, finished_at
                FROM customer_profile_tasks
                {where_clause}
                """


def _select_next_queued_sql(has_work_env: bool, has_batch_id: bool, has_task_type: bool) -> str:
    work_env_expr = "work_env" if has_work_env else "'test' AS work_env"
    batch_id_expr = "batch_id" if has_batch_id else "NULL AS batch_id"
    task_type_expr = "task_type" if has_task_type else f"'{_DEFAULT_TASK_TYPE}' AS task_type"
    return f"""
                SELECT
                    id, customer_phonenumber, status, updated_at, output, card_id, nickname, error_message,
                    {work_env_expr},
                    {batch_id_expr},
                    {task_type_expr},
                    generation, execution_backend, workflow_run_id, attempt_count, poll_recovery_count,
                    workflow_run_started_at, next_retry_at,
                    last_error_code, last_error_stage, lease_expires_at, claim_token,
                    queued_at, started_at, finished_at
                FROM customer_profile_tasks
                WHERE status = 'queued'
                ORDER BY updated_at ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """


def _select_next_eligible_sql(has_work_env: bool, has_batch_id: bool, has_task_type: bool) -> str:
    work_env_expr = "work_env" if has_work_env else "'test' AS work_env"
    batch_id_expr = "batch_id" if has_batch_id else "NULL AS batch_id"
    task_type_expr = "task_type" if has_task_type else f"'{_DEFAULT_TASK_TYPE}' AS task_type"
    return f"""
                SELECT
                    id, customer_phonenumber, status, updated_at, output, card_id, nickname, error_message,
                    {work_env_expr},
                    {batch_id_expr},
                    {task_type_expr},
                    generation, execution_backend, workflow_run_id, attempt_count, poll_recovery_count,
                    workflow_run_started_at, next_retry_at,
                    last_error_code, last_error_stage, lease_expires_at, claim_token,
                    queued_at, started_at, finished_at
                FROM customer_profile_tasks
                WHERE (
                        status = 'retry_wait'
                        AND next_retry_at <= CURRENT_TIMESTAMP
                        AND execution_backend = %s
                      )
                   OR (status = 'queued' AND %s = 1)
                ORDER BY
                    CASE
                        WHEN status = 'retry_wait' AND workflow_run_id IS NOT NULL THEN 0
                        WHEN status = 'retry_wait' THEN 1
                        ELSE 2
                    END,
                    COALESCE(next_retry_at, queued_at, updated_at) ASC,
                    id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
