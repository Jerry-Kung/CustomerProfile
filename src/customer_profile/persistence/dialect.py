"""SQL 方言：只放「两种后端写法不同」的那几处。

**为什么单独一个模块。** 方言差异散在 ``store.py`` 里时，排查「某条语句在 MySQL 上
为什么结果不同」要逐个方法翻；集中之后，差异清单就是本文件的函数列表，一眼能数完。

**什么不在这里。** 只差占位符、只差行对象形状的东西由 ``dbapi`` 统一处理；这里只放
**语句结构本身不同**的：upsert 子句、队列领取、错误文本拼接、递归派生标记。

两条实现口径：

1. **参数占位符一律写 ``?``**，由 ``dbapi.convert_placeholders`` 翻译。
2. **每个函数都取 ``name`` 参数**，而不是各自 if：调用点只依赖 ``Dialect`` 对象，
   新增后端时不必再改 ``store.py``。
"""

from __future__ import annotations

from typing import Any, Sequence

from ..workflow_scope import scope_predicate


class SqliteDialect:
    name = "sqlite"

    # ------------------------------------------------------------ upsert

    def insert_run_sql(self) -> str:
        """写入或更新一行运行记录。

        ``status`` 只在**不是**「从一个非终态退回终态」时更新——避免迟到的
        ``running`` 覆盖已落定的终态。``created_at_ms``（队列排序键）不被后来者覆盖。
        """
        return (
            "INSERT INTO workflow_runs (run_id, workflow_id, workflow_display_name, "
            " parent_run_id, parent_node_id, call_path, business_ref, status, "
            " definition_version_id, inputs_json, outputs_json, error, started_at_ms, "
            " finished_at_ms, duration_ms, is_replay, queued_at_ms, claimed_at_ms, "
            " claimed_by, lease_expires_at_ms, created_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (run_id) DO UPDATE SET "
            " status = excluded.status, "
            " workflow_display_name = COALESCE(excluded.workflow_display_name, "
            "   workflow_runs.workflow_display_name), "
            " definition_version_id = COALESCE(excluded.definition_version_id, "
            "   workflow_runs.definition_version_id), "
            " inputs_json = COALESCE(excluded.inputs_json, workflow_runs.inputs_json), "
            " started_at_ms = COALESCE(excluded.started_at_ms, workflow_runs.started_at_ms), "
            " outputs_json = COALESCE(excluded.outputs_json, workflow_runs.outputs_json), "
            " finished_at_ms = COALESCE(excluded.finished_at_ms, workflow_runs.finished_at_ms), "
            " duration_ms = COALESCE(excluded.duration_ms, workflow_runs.duration_ms), "
            " error = COALESCE(excluded.error, workflow_runs.error), "
            " claimed_at_ms = COALESCE(excluded.claimed_at_ms, workflow_runs.claimed_at_ms), "
            " claimed_by = COALESCE(excluded.claimed_by, workflow_runs.claimed_by), "
            " lease_expires_at_ms = COALESCE(excluded.lease_expires_at_ms, "
            "   workflow_runs.lease_expires_at_ms)"
        )

    def upsert_node_sql(self) -> str:
        return (
            "INSERT INTO node_executions (run_id, node_id, node_title, node_type, "
            " call_path, status, branch, inputs_json, outputs_json, error, queued_at_ms, "
            " started_at_ms, duration_ms, sub_run_id, attempt_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 0)) "
            "ON CONFLICT (run_id, node_id, call_path) DO UPDATE SET "
            " status = excluded.status, branch = excluded.branch, "
            " inputs_json = COALESCE(excluded.inputs_json, node_executions.inputs_json), "
            " outputs_json = COALESCE(excluded.outputs_json, node_executions.outputs_json), "
            " error = excluded.error, duration_ms = excluded.duration_ms, "
            " sub_run_id = COALESCE(excluded.sub_run_id, node_executions.sub_run_id), "
            " attempt_count = COALESCE(excluded.attempt_count, node_executions.attempt_count), "
            " started_at_ms = COALESCE(excluded.started_at_ms, node_executions.started_at_ms), "
            " queued_at_ms = COALESCE(excluded.queued_at_ms, node_executions.queued_at_ms)"
        )

    # ------------------------------------------------------------ 文本拼接

    def append_error_sql(self, column: str = "error") -> str:
        """把一段说明追加到已有的错误文本后面。"""
        return f"COALESCE({column} || ' | ', '') || ?"

    # ------------------------------------------------------------ 范围谓词

    def scope_predicate(self, scope: str) -> tuple[str, tuple[str, ...]]:
        return scope_predicate(scope, dialect="sqlite")

    # ------------------------------------------------------------ 队列领取

    def claim(
        self, connection: Any, now_ms: int, worker_id: str, lease_ms: int, scope: str
    ) -> tuple[str, Sequence[Any]]:
        """返回一条语句；调用方执行后取 ``fetchone()``。"""
        predicate, params = self.scope_predicate(scope)
        sql = (
            "UPDATE workflow_runs "
            "SET status = ?, claimed_at_ms = ?, claimed_by = ?, "
            "    lease_expires_at_ms = ? "
            "WHERE run_id = ("
            "  SELECT run_id FROM workflow_runs "
            f"  WHERE status = ? AND parent_run_id IS NULL AND {predicate} "
            "  ORDER BY created_at_ms LIMIT 1"
            ") AND status = ? "
            "RETURNING run_id, workflow_id, business_ref, inputs_json, "
            "          definition_version_id, is_replay, claimed_by, "
            "          claimed_at_ms, lease_expires_at_ms"
        )
        return sql, params

    def claim_ready(self) -> bool:
        """领取是否为单语句原子操作。SQLite 的是；MySQL 的要开事务。"""
        return True

    # ------------------------------------------------------------ 恢复

    def mark_interrupted_is_single_statement(self) -> bool:
        return True


class MySqlDialect:
    name = "mysql"

    # ------------------------------------------------------------ upsert

    def insert_run_sql(self) -> str:
        # 8.0.19+ 的行别名语法。不要用 VALUES(x)：8.2 上已弃用，且列顺序一变就静默错位。
        return (
            "INSERT INTO workflow_runs (run_id, workflow_id, workflow_display_name, "
            " parent_run_id, parent_node_id, call_path, business_ref, status, "
            " definition_version_id, inputs_json, outputs_json, error, started_at_ms, "
            " finished_at_ms, duration_ms, is_replay, queued_at_ms, claimed_at_ms, "
            " claimed_by, lease_expires_at_ms, created_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) AS new "
            "ON DUPLICATE KEY UPDATE "
            " status = new.status, "
            " workflow_display_name = COALESCE(new.workflow_display_name, "
            "   workflow_runs.workflow_display_name), "
            " definition_version_id = COALESCE(new.definition_version_id, "
            "   workflow_runs.definition_version_id), "
            " inputs_json = COALESCE(new.inputs_json, workflow_runs.inputs_json), "
            " started_at_ms = COALESCE(new.started_at_ms, workflow_runs.started_at_ms), "
            " outputs_json = COALESCE(new.outputs_json, workflow_runs.outputs_json), "
            " finished_at_ms = COALESCE(new.finished_at_ms, workflow_runs.finished_at_ms), "
            " duration_ms = COALESCE(new.duration_ms, workflow_runs.duration_ms), "
            " error = COALESCE(new.error, workflow_runs.error), "
            " claimed_at_ms = COALESCE(new.claimed_at_ms, workflow_runs.claimed_at_ms), "
            " claimed_by = COALESCE(new.claimed_by, workflow_runs.claimed_by), "
            " lease_expires_at_ms = COALESCE(new.lease_expires_at_ms, "
            "   workflow_runs.lease_expires_at_ms)"
        )

    def upsert_node_sql(self) -> str:
        return (
            "INSERT INTO node_executions (run_id, node_id, node_title, node_type, "
            " call_path, status, branch, inputs_json, outputs_json, error, queued_at_ms, "
            " started_at_ms, duration_ms, sub_run_id, attempt_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 0)) AS new "
            "ON DUPLICATE KEY UPDATE "
            " status = new.status, branch = new.branch, "
            " inputs_json = COALESCE(new.inputs_json, node_executions.inputs_json), "
            " outputs_json = COALESCE(new.outputs_json, node_executions.outputs_json), "
            " error = new.error, duration_ms = new.duration_ms, "
            " sub_run_id = COALESCE(new.sub_run_id, node_executions.sub_run_id), "
            " attempt_count = COALESCE(new.attempt_count, node_executions.attempt_count), "
            " started_at_ms = COALESCE(new.started_at_ms, node_executions.started_at_ms), "
            " queued_at_ms = COALESCE(new.queued_at_ms, node_executions.queued_at_ms)"
        )

    # ------------------------------------------------------------ 文本拼接

    def append_error_sql(self, column: str = "error") -> str:
        # MySQL 的 || 在缺省 sql_mode 下是「逻辑或」，不是拼接（PIPES_AS_CONCAT 未开）。
        return f"CONCAT(COALESCE(CONCAT({column}, ' | '), ''), ?)"

    # ------------------------------------------------------------ 范围谓词

    def scope_predicate(self, scope: str) -> tuple[str, tuple[str, ...]]:
        return scope_predicate(scope, dialect="mysql")

    # ------------------------------------------------------------ 队列领取

    def claim(
        self, connection: Any, now_ms: int, worker_id: str, lease_ms: int, scope: str
    ) -> tuple[str, Sequence[Any]]:
        """MySQL 不支持 RETURNING，领取拆成三步（见 ``Store.claim_next_queued_run``）。"""
        predicate, params = self.scope_predicate(scope)
        sql = (
            "SELECT run_id FROM workflow_runs "
            f"WHERE status = ? AND parent_run_id IS NULL AND {predicate} "
            "ORDER BY created_at_ms LIMIT 1 FOR UPDATE SKIP LOCKED"
        )
        return sql, params

    def claim_ready(self) -> bool:
        return False

    def mark_interrupted_is_single_statement(self) -> bool:
        return False
