from collections.abc import Callable

from src.domain.task.failure_callback import TaskFailure
from src.infrastructure.db.client import MySQLClient


class MySQLFailureCallbackRepository:
    """Durable, at-least-once delivery of confirmed terminal task failures.

    Scan the durable queue, including failures from workers that crashed before
    notification. A resubmitted task has a new generation; stale outbox entries
    remain safe because the API checks batch id and invitation execution id.
    """

    def __init__(self, db_client: MySQLClient, *, retry_seconds: int = 30):
        self._db_client = db_client
        self._retry_seconds = retry_seconds

    def enqueue_failed(self) -> None:
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT IGNORE INTO customer_profile_failure_outbox
                    (task_id, generation, customer_phone, batch_id, work_env, error_message,
                     task_type, execution_id)
                SELECT task.id, task.generation, task.customer_phonenumber, task.batch_id,
                       task.work_env, COALESCE(task.error_message, 'Analysis failed'),
                       task.task_type, task.execution_id
                FROM customer_profile_tasks AS task
                LEFT JOIN customer_profile_failure_outbox AS existing
                  ON existing.task_id = task.id AND existing.generation = task.generation
                WHERE task.status = 'failed'
                  AND (task.task_type = 'customer_profile'
                       OR (task.task_type = 'invitation_strategy' AND task.execution_id IS NOT NULL))
                  AND task.batch_id IS NOT NULL AND task.batch_id <> ''
                  AND existing.id IS NULL
                  AND COALESCE(task.last_error_code, '') NOT LIKE '%%SUBMIT_UNKNOWN'
                  AND COALESCE(task.last_error_code, '') NOT LIKE '%%RUN_STALLED'
                  AND COALESCE(task.last_error_code, '') NOT LIKE '%%REQUIRES_RECONCILIATION'
                  AND COALESCE(task.last_error_code, '') NOT LIKE '%%UNEXPECTED_ERROR'
                  AND COALESCE(task.error_message, '') NOT LIKE 'DIFY_TIMEOUT:%%'
                  AND COALESCE(task.error_message, '') NOT LIKE 'DIFY_REQUEST_FAILED:%%'
                  AND COALESCE(task.error_message, '') NOT LIKE '%%UNEXPECTED_ERROR:%%'
                  AND COALESCE(task.error_message, '') NOT LIKE 'INVITATION_REQUIRES_RECONCILIATION:%%'
            """)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def deliver_next(self, send: Callable[[TaskFailure], None]) -> bool:
        conn = self._db_client.connect()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("START TRANSACTION")
            cursor.execute("""
                SELECT id, customer_phone, batch_id, work_env, error_message, task_type, execution_id
                FROM customer_profile_failure_outbox
                WHERE delivered_at IS NULL AND next_attempt_at <= CURRENT_TIMESTAMP
                ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED
            """)
            row = cursor.fetchone()
            if row is None:
                conn.rollback()
                return False
            try:
                send(TaskFailure(
                    phone=row["customer_phone"], batch_id=row["batch_id"],
                    work_env=row["work_env"], error_message=row["error_message"],
                    task_type=row["task_type"], execution_id=row["execution_id"],
                ))
            except Exception:
                # Keep the payload until HTTP acknowledges it. A crash after a
                # successful HTTP call is retried safely by the API's batch guard.
                cursor.execute("""
                    UPDATE customer_profile_failure_outbox
                    SET attempts = attempts + 1,
                        next_attempt_at = DATE_ADD(CURRENT_TIMESTAMP, INTERVAL %s SECOND)
                    WHERE id = %s
                """, (self._retry_seconds, row["id"]))
                conn.commit()
                raise
            cursor.execute("""
                UPDATE customer_profile_failure_outbox
                SET delivered_at = CURRENT_TIMESTAMP, attempts = attempts + 1
                WHERE id = %s
            """, (row["id"],))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
