from __future__ import annotations

import json
import logging
import time
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
        columns = ["customer_phonenumber", "status"]
        placeholders = ["%s", "'queued'"]
        params: list[object] = [customer_phonenumber]
        update_clauses = [
            "status = VALUES(status)",
            "output = NULL",
            "card_id = NULL",
            "nickname = NULL",
            "error_message = NULL",
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

    def mark_succeeded(self, *, task_id: int, output: dict, card_id: str, nickname: str) -> None:
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
                    {task_type_expr}
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
                    {task_type_expr}
                FROM customer_profile_tasks
                WHERE status = 'queued'
                ORDER BY updated_at ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
