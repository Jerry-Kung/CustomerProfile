"""SQLite 持久化：建库、写入与查询。

用标准库 ``sqlite3`` 而非 ORM——V0.2 的表结构与查询都很直白，引入 ORM 只会多一层
需要解释的间接（`Dify迁移任务说明.md` §4「不引入未实际需要的重型框架」）。

**线程模型是这里唯一需要小心的地方。** ``sqlite3.Connection`` 不是线程安全的：
默认甚至禁止跨线程使用，而 ``check_same_thread=False`` 只是关掉那道检查，并不能让
并发访问变安全——多线程同时用一个连接会直接崩掉解释器（实测在 Windows + Python 3.14
上表现为段错误，不是可捕获的异常）。

因此本模块让连接有**唯一所有者**：一个专属工作线程。所有读写都排进该线程的队列顺序
执行，回调通过 ``concurrent.futures.Future`` 回到调用方。这样既没有跨线程访问，也不
需要调用方加锁。副作用是读写不再并发——V0.2 是单进程单 worker，留痕不是瓶颈，可以接受。
"""

from __future__ import annotations

import asyncio
import json
import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..definitions import RunStatus
from .schema import (
    SCHEMA_STATEMENTS,
    RunRecordRow,
    definition_hash,
    json_dumps,
)

_SHUTDOWN = object()
"""投进命令队列即要求工作线程退出。"""


DEFAULT_RUN_ORDER = "created_desc"
"""运行列表的缺省排序。最新优先，与「先看刚跑的那次」的使用习惯一致。"""

_RUN_ORDERINGS: dict[str, str] = {
    "created_desc": "created_at_ms DESC",
    "created_asc": "created_at_ms ASC",
    "duration_desc": "COALESCE(duration_ms, 0) DESC, created_at_ms DESC",
    "duration_asc": "COALESCE(duration_ms, 0) ASC, created_at_ms DESC",
}


def _run_order_by(order_by: str) -> str:
    """把排序键翻成 ``ORDER BY`` 子句。

    **白名单，不是拼接**。``order_by`` 来自查询参数，直接插进 SQL 就是注入点；
    未知取值回落到缺省而不是报错——排序参数写错不该让整个列表打不开。
    """
    return _RUN_ORDERINGS.get(order_by, _RUN_ORDERINGS[DEFAULT_RUN_ORDER])


def _run_filter_clause(
    *,
    workflow_id: str | None = None,
    status: str | None = None,
    business_ref: str | None = None,
    include_children: bool = False,
    created_after_ms: int | None = None,
    created_before_ms: int | None = None,
) -> tuple[str, list[Any]]:
    """构造 ``WHERE`` 子句与参数。

    列表与计数**共用**本函数，是刻意的：两者条件一旦分叉，分页的总页数会与实际数据
    不匹配，表现为「翻到后半段是空页」，且看起来像后端丢了数据。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if workflow_id:
        clauses.append("workflow_id = ?")
        params.append(workflow_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if business_ref:
        clauses.append("business_ref = ?")
        params.append(business_ref)
    if not include_children:
        clauses.append("parent_run_id IS NULL")
    if created_after_ms is not None:
        clauses.append("created_at_ms >= ?")
        params.append(created_after_ms)
    if created_before_ms is not None:
        clauses.append("created_at_ms <= ?")
        params.append(created_before_ms)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


class _ConnectionThread:
    """独占一个 ``sqlite3.Connection`` 的工作线程。

    对外只暴露 :meth:`submit`：把一个可调用对象排进队列，在其上返回一个
    ``concurrent.futures.Future``。可调用对象由工作线程执行，因此它拿到的连接
    始终是同一个线程创建的。
    """

    def __init__(self, path: Path, setup: Callable[[sqlite3.Connection], None]) -> None:
        self._path = path
        self._commands: "queue.Queue[Any]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name=f"sqlite:{path.name}", daemon=True
        )
        self._setup = setup
        self._started = threading.Event()
        self._start_error: BaseException | None = None
        self._thread.start()
        self._started.wait()

    # ------------------------------------------------------------ 对外

    def submit(self, work: Callable[[sqlite3.Connection], Any]) -> Future:
        """把一次数据库操作排进队列。连接已关闭时返回一个已置异常的 Future。

        返回 Future 而不是等待结果：调用方用 ``asyncio.wrap_future`` 把它接回协程，
        事件循环不会被阻塞。
        """
        future: Future = Future()
        if self._start_error is not None:
            future.set_exception(self._start_error)
            return future
        self._commands.put((work, future))
        return future

    def close(self) -> None:
        """要求工作线程退出并等待它结束。幂等。"""
        if not self._thread.is_alive():
            return
        self._commands.put(_SHUTDOWN)
        self._thread.join(timeout=10)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    # ------------------------------------------------------------ 内部

    def _run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path)
            connection.row_factory = sqlite3.Row
            # WAL 让读写互不阻塞；foreign_keys 默认关闭，需显式打开
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._setup(connection)
        except BaseException as exc:  # 建库失败：把错误交给首个提交者
            self._start_error = exc
        finally:
            self._started.set()

        while True:
            item = self._commands.get()
            if item is _SHUTDOWN:
                break
            work, future = item
            if future.cancelled():
                continue
            try:
                future.set_result(work(connection))
            except BaseException as exc:
                future.set_exception(exc)

        if connection is not None:
            connection.close()


class Store:
    """留痕存储。所有方法都是协程，实际 sqlite 调用在专属线程内执行。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._thread: _ConnectionThread | None = None

    # ------------------------------------------------------------ 生命周期

    def connect(self) -> None:
        """建库并应用表结构。幂等。"""
        if self._thread is not None and self._thread.alive:
            return

        def setup(connection: sqlite3.Connection) -> None:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            connection.commit()

        self._thread = _ConnectionThread(self.path, setup)

    def close(self) -> None:
        if self._thread is not None:
            self._thread.close()
            self._thread = None

    @property
    def connected(self) -> bool:
        return self._thread is not None and self._thread.alive

    def _require_thread(self) -> _ConnectionThread:
        if self._thread is None or not self._thread.alive:
            raise RuntimeError(
                "Store 尚未 connect()（或已关闭）；留痕读写需要活动连接"
            )
        return self._thread

    # ------------------------------------------------------------ 执行

    async def _run(self, work: Callable[[sqlite3.Connection], Any]) -> Any:
        """在专属线程上执行一次数据库操作并等待结果。"""
        future = self._require_thread().submit(work)
        return await asyncio.wrap_future(future)

    @staticmethod
    def _execute(
        sql: str, params: Sequence[Any] = ()
    ) -> Callable[[sqlite3.Connection], list[sqlite3.Row]]:
        def work(connection: sqlite3.Connection) -> list[sqlite3.Row]:
            cursor = connection.execute(sql, params)
            rows = cursor.fetchall()
            cursor.close()
            return rows

        return work

    @staticmethod
    def _execute_write(
        statements: Sequence[tuple[str, Sequence[Any]]],
    ) -> Callable[[sqlite3.Connection], None]:
        """一次提交多条语句，共用一次 commit。"""

        def work(connection: sqlite3.Connection) -> None:
            for sql, params in statements:
                connection.execute(sql, params)
            connection.commit()

        return work

    async def _read(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await self._run(self._execute(sql, params))

    async def _write(self, sql: str, params: Sequence[Any] = ()) -> None:
        await self._run(self._execute_write([(sql, params)]))

    # ------------------------------------------------------------ 版本快照

    async def ensure_definition_version(
        self,
        workflow_id: str,
        definition: Mapping[str, Any],
        *,
        template_hashes: Mapping[str, str] | None = None,
        code_commit: str | None = None,
    ) -> int:
        """登记一次定义快照，返回 ``version_id``。同一定义重复登记返回既有 ID。"""
        digest = definition_hash(definition)

        def work(connection: sqlite3.Connection) -> int:
            row = connection.execute(
                "SELECT version_id FROM definition_versions "
                "WHERE workflow_id = ? AND definition_hash = ?",
                (workflow_id, digest),
            ).fetchone()
            if row is not None:
                return int(row["version_id"])
            cursor = connection.execute(
                "INSERT INTO definition_versions "
                "(workflow_id, definition_hash, definition_json, "
                " template_hashes_json, code_commit, created_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    workflow_id,
                    digest,
                    json.dumps(definition, ensure_ascii=False),
                    json_dumps(dict(template_hashes or {})),
                    code_commit,
                    _now_ms(),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

        return await self._run(work)

    async def definition_version(self, version_id: int) -> dict[str, Any] | None:
        rows = await self._read(
            "SELECT * FROM definition_versions WHERE version_id = ?", (version_id,)
        )
        if not rows:
            return None
        return _row_to_dict(
            rows[0], json_fields=("definition_json", "template_hashes_json")
        )

    # ------------------------------------------------------------ 运行

    async def insert_run(self, run: RunRecordRow) -> None:
        await self._write(
            "INSERT INTO workflow_runs (run_id, workflow_id, workflow_display_name, "
            " parent_run_id, parent_node_id, call_path, business_ref, status, "
            " definition_version_id, inputs_json, outputs_json, error, started_at_ms, "
            " finished_at_ms, duration_ms, is_replay, created_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.run_id,
                run.workflow_id,
                run.workflow_display_name,
                run.parent_run_id,
                run.parent_node_id,
                run.call_path,
                run.business_ref,
                run.status,
                run.definition_version_id,
                json_dumps(run.inputs),
                json_dumps(run.outputs),
                run.error,
                run.started_at_ms,
                run.finished_at_ms,
                run.duration_ms,
                1 if run.is_replay else 0,
                run.created_at_ms or _now_ms(),
            ),
        )

    async def update_run_finished(
        self,
        run_id: str,
        *,
        status: str,
        outputs: Mapping[str, Any] | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        await self._write(
            "UPDATE workflow_runs SET status = ?, outputs_json = ?, error = ?, "
            "finished_at_ms = ?, duration_ms = ? WHERE run_id = ?",
            (
                status,
                json_dumps(dict(outputs or {})),
                error,
                _now_ms(),
                duration_ms,
                run_id,
            ),
        )

    async def set_run_status(self, run_id: str, status: str) -> None:
        await self._write(
            "UPDATE workflow_runs SET status = ? WHERE run_id = ?", (status, run_id)
        )

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = await self._read(
            "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
        )
        if not rows:
            return None
        return _row_to_dict(rows[0], json_fields=("inputs_json", "outputs_json"))

    async def list_runs(
        self,
        *,
        workflow_id: str | None = None,
        status: str | None = None,
        business_ref: str | None = None,
        include_children: bool = False,
        created_after_ms: int | None = None,
        created_before_ms: int | None = None,
        order_by: str = DEFAULT_RUN_ORDER,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where, params = _run_filter_clause(
            workflow_id=workflow_id,
            status=status,
            business_ref=business_ref,
            include_children=include_children,
            created_after_ms=created_after_ms,
            created_before_ms=created_before_ms,
        )
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        rows = await self._read(
            f"SELECT * FROM workflow_runs {where} "
            f"ORDER BY {_run_order_by(order_by)} LIMIT ? OFFSET ?",
            params,
        )
        return [
            _row_to_dict(r, json_fields=("inputs_json", "outputs_json")) for r in rows
        ]

    async def count_runs(
        self,
        *,
        workflow_id: str | None = None,
        status: str | None = None,
        business_ref: str | None = None,
        include_children: bool = False,
        created_after_ms: int | None = None,
        created_before_ms: int | None = None,
    ) -> int:
        """与 :meth:`list_runs` **共用同一套筛选条件**的总数。

        刻意不保留「不带筛选的总数」这条捷径：分页控件要的是「当前筛选下共几页」，
        两者用不同条件算出的数字会让翻页在后半段变成空白页，且看着像后端丢了数据。
        """
        where, params = _run_filter_clause(
            workflow_id=workflow_id,
            status=status,
            business_ref=business_ref,
            include_children=include_children,
            created_after_ms=created_after_ms,
            created_before_ms=created_before_ms,
        )
        rows = await self._read(
            f"SELECT COUNT(*) AS n FROM workflow_runs {where}", params
        )
        return int(rows[0]["n"]) if rows else 0

    async def mark_running_as_interrupted(self) -> int:
        """把残留的 ``running`` / ``queued`` 运行标记为 ``interrupted``（§7.3）。

        进程重启不能证明外部副作用未发生，因此这些运行**保留已完成结果**并交人工处理，
        不做自动续跑。
        """
        rows = await self._read(
            "SELECT run_id FROM workflow_runs WHERE status IN (?, ?)",
            (RunStatus.RUNNING, RunStatus.QUEUED),
        )
        run_ids = [r["run_id"] for r in rows]
        for run_id in run_ids:
            await self._write(
                "UPDATE workflow_runs SET status = ?, error = COALESCE(error, ?) "
                "WHERE run_id = ? AND status IN (?, ?)",
                (
                    RunStatus.INTERRUPTED,
                    "进程中断：运行中任务未完成，已完成结果已保留",
                    run_id,
                    RunStatus.RUNNING,
                    RunStatus.QUEUED,
                ),
            )
        return len(run_ids)

    # ------------------------------------------------------------ 节点执行

    async def upsert_node_execution(
        self,
        *,
        run_id: str,
        node_id: str,
        node_title: str,
        node_type: str,
        call_path: str,
        status: str,
        branch: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        outputs: Mapping[str, Any] | None = None,
        error: str | None = None,
        queued_at_ms: int | None = None,
        started_at_ms: int | None = None,
        duration_ms: int | None = None,
        sub_run_id: str | None = None,
    ) -> None:
        """写入或更新一条节点执行记录。

        以 ``(run_id, node_id, call_path)`` 为唯一键：同一静态节点 ID 在迭代或子流程里
        会多次执行，靠 ``call_path`` 区分（`Dify迁移任务说明.md` §5.3）。
        """
        await self._write(
            "INSERT INTO node_executions (run_id, node_id, node_title, node_type, "
            " call_path, status, branch, inputs_json, outputs_json, error, queued_at_ms, "
            " started_at_ms, duration_ms, sub_run_id, attempt_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT (run_id, node_id, call_path) DO UPDATE SET "
            " status = excluded.status, branch = excluded.branch, "
            " inputs_json = COALESCE(excluded.inputs_json, node_executions.inputs_json), "
            " outputs_json = COALESCE(excluded.outputs_json, node_executions.outputs_json), "
            " error = excluded.error, duration_ms = excluded.duration_ms, "
            " sub_run_id = COALESCE(excluded.sub_run_id, node_executions.sub_run_id)",
            (
                run_id,
                node_id,
                node_title,
                node_type,
                call_path,
                status,
                branch,
                json_dumps(dict(inputs or {})) or None,
                json_dumps(dict(outputs or {})) or None,
                error,
                queued_at_ms,
                started_at_ms,
                duration_ms,
                sub_run_id,
            ),
        )

    async def bump_attempt_count(
        self, *, run_id: str, node_id: str, call_path: str, count: int
    ) -> None:
        await self._write(
            "UPDATE node_executions SET attempt_count = attempt_count + ? "
            "WHERE run_id = ? AND node_id = ? AND call_path = ?",
            (count, run_id, node_id, call_path),
        )

    async def list_node_executions(self, run_id: str) -> list[dict[str, Any]]:
        rows = await self._read(
            "SELECT * FROM node_executions WHERE run_id = ? "
            "ORDER BY COALESCE(started_at_ms, 0), execution_id",
            (run_id,),
        )
        return [
            _row_to_dict(r, json_fields=("inputs_json", "outputs_json")) for r in rows
        ]

    # ------------------------------------------------------------ 请求尝试

    async def insert_attempt(
        self,
        *,
        run_id: str,
        node_id: str,
        call_path: str,
        attempt_no: int,
        kind: str,
        target: str | None = None,
        request: Any = None,
        response_text: str | None = None,
        status_code: int | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
        error_code: str | None = None,
        usage: Mapping[str, Any] | None = None,
        provider_request_id: str | None = None,
        replayed: bool = False,
    ) -> None:
        await self._write(
            "INSERT INTO request_attempts (run_id, node_id, call_path, attempt_no, kind, "
            " target, request_json, response_text, status_code, duration_ms, error, "
            " error_code, usage_json, provider_request_id, replayed, created_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                node_id,
                call_path,
                attempt_no,
                kind,
                target,
                json_dumps(request),
                response_text,
                status_code,
                duration_ms,
                error,
                error_code,
                json_dumps(dict(usage or {})) if usage else None,
                provider_request_id,
                1 if replayed else 0,
                _now_ms(),
            ),
        )

    async def list_attempts(
        self, run_id: str, *, node_id: str | None = None
    ) -> list[dict[str, Any]]:
        if node_id:
            rows = await self._read(
                "SELECT * FROM request_attempts WHERE run_id = ? AND node_id = ? "
                "ORDER BY attempt_id",
                (run_id, node_id),
            )
        else:
            rows = await self._read(
                "SELECT * FROM request_attempts WHERE run_id = ? ORDER BY attempt_id",
                (run_id,),
            )
        return [
            _row_to_dict(r, json_fields=("request_json", "usage_json")) for r in rows
        ]

    async def count_attempts(self, run_id: str) -> int:
        rows = await self._read(
            "SELECT COUNT(*) AS n FROM request_attempts WHERE run_id = ?", (run_id,)
        )
        return int(rows[0]["n"]) if rows else 0

    # ------------------------------------------------------------ 子运行树

    async def list_child_runs(self, parent_run_id: str) -> list[dict[str, Any]]:
        rows = await self._read(
            "SELECT * FROM workflow_runs WHERE parent_run_id = ? ORDER BY created_at_ms",
            (parent_run_id,),
        )
        return [
            _row_to_dict(r, json_fields=("inputs_json", "outputs_json")) for r in rows
        ]


def _row_to_dict(row: sqlite3.Row, json_fields: Iterable[str] = ()) -> dict[str, Any]:
    """把一行转成字典，顺便把 JSON 文本字段解析回对象。"""
    data = {key: row[key] for key in row.keys()}
    for field_name in json_fields:
        if field_name in data:
            parsed = data[field_name]
            if isinstance(parsed, str):
                try:
                    data[field_name] = json.loads(parsed)
                except ValueError:
                    data[field_name] = None
            if field_name == "definition_json" and data.get(field_name) is not None:
                data["definition"] = data.pop(field_name)
    return data


def _now_ms() -> int:
    return int(time.time() * 1000)
