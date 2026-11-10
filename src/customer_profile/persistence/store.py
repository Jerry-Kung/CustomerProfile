"""SQLite 持久化：建库、写入与查询。

用标准库 ``sqlite3`` 而非 ORM——V0.2 的表结构与查询都很直白，引入 ORM 只会多一层
需要解释的间接（既定约束「不引入未实际需要的重型框架」）。

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
from collections import OrderedDict
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from loguru import logger

from ..definitions import RunStatus
from ..workflow_scope import WorkflowScope, scope_predicate
from . import archive
from .schema import (
    SCHEMA_STATEMENTS,
    RunRecordRow,
    definition_hash,
    json_dumps,
)

_SHUTDOWN = object()
"""投进命令队列即要求工作线程退出。"""

_CACHE_MISS = object()
"""包缓存里「没有这一项」的哨兵。用 ``None`` 不行——``None`` 本身就是
「读过、但读不到」的结果，两者混在一起会让每个缺失的包都被反复重读并重复记日志。"""

PAYLOAD_IN_DB = "in_db"
"""载荷仍在库内：本进程未收尾、崩溃遗留，或存量旧行。"""

PAYLOAD_PACKED = "packed"
"""载荷已搬进包，库内载荷列为空。"""

PAYLOAD_PURGED = "purged"
"""存量清理：载荷已丢弃且没有包，只剩骨架与根输出。"""

PAYLOAD_STATES = (PAYLOAD_IN_DB, PAYLOAD_PACKED, PAYLOAD_PURGED)
"""合法取值。读写两侧共用，避免各写一份字面量。"""


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

    def __init__(
        self,
        path: Path,
        setup: Callable[[sqlite3.Connection], None],
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._path = path
        self._commands: "queue.Queue[Any]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name=f"sqlite:{path.name}", daemon=True
        )
        self._setup = setup
        self._busy_timeout_ms = busy_timeout_ms
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
            # 增量回收空闲页。留痕的载荷在收尾时被清空（存储瘦身），回收要跟着走，
            # 否则删掉的行只会进 freelist、文件永不缩小。**必须在建表之前设置**：
            # auto_vacuum 只在建库时生效，对既有库要另行 VACUUM 才切换模式。
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            # WAL 让读写互不阻塞；foreign_keys 默认关闭，需显式打开
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            # V0.5.2 起 API 与 worker 是两个进程、各持一个连接，写-写相遇时若没有
            # busy_timeout，SQLite 会立刻抛 "database is locked" 而不是等一会儿。
            # WAL 下 synchronous=NORMAL 已足够安全（崩溃最多丢最后一个事务，
            # 而任务队列本就设计为可被重新领取）。
            connection.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
            connection.execute("PRAGMA synchronous=NORMAL")
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

    def __init__(
        self,
        path: Path | str,
        *,
        busy_timeout_ms: int = 5000,
        archive_dir: Path | str | None = None,
    ) -> None:
        self.path = Path(path)
        self._thread: _ConnectionThread | None = None
        self._busy_timeout_ms = busy_timeout_ms
        self.archive_dir = Path(archive_dir) if archive_dir else None
        """运行时数据包目录（存储瘦身）。``None`` 表示不读包——此时 ``packed`` 的
        行会如实返回空载荷并标 ``payload_missing``，不会静默当成「没有输出」。"""
        self._payload_cache: "OrderedDict[str, dict[str, Any] | None]" = OrderedDict()
        self._payload_cache_size = 4
        """按根运行缓存已解析的包。包解压后约数 MB，宁可重复解也不常驻太多。"""

    # ------------------------------------------------------------ 生命周期

    def connect(self) -> None:
        """建库并应用表结构。幂等。"""
        if self._thread is not None and self._thread.alive:
            return

        def setup(connection: sqlite3.Connection) -> None:
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            # 建表之后补列：CREATE TABLE IF NOT EXISTS 对**已存在**的库不会新增列，
            # 而 data/customer_profile.db 已在磁盘上。不加这一步，新列在运行期
            # 只表现为一句 "no such column"。
            _apply_migrations(connection)
            connection.commit()

        self._thread = _ConnectionThread(
            self.path, setup, busy_timeout_ms=self._busy_timeout_ms
        )

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
        """写入或更新一行运行记录。

        **幂等 upsert**（V0.5.2 起）。此前的纯 ``INSERT`` 在队列模型下必然冲突：
        提交时先落一行 ``queued``，worker 真正开跑时留痕层还会再写一行 ``running``，
        同一个 ``run_id`` 会撞主键。用 upsert 让「入队」与「开跑」写同一行，
        同时保证 ``created_at_ms``（队列排序键）不被后来的写入覆盖。

        ``status`` 只在**不是**「从一个非终态退回终态」时更新——避免迟到的
        ``running`` 覆盖已落定的终态。
        """
        await self._write(
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
            " claimed_at_ms = COALESCE(excluded.claimed_at_ms, workflow_runs.claimed_at_ms), "
            " claimed_by = COALESCE(excluded.claimed_by, workflow_runs.claimed_by), "
            " lease_expires_at_ms = COALESCE(excluded.lease_expires_at_ms, "
            "   workflow_runs.lease_expires_at_ms)",
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
                run.queued_at_ms,
                run.claimed_at_ms,
                run.claimed_by,
                run.lease_expires_at_ms,
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

    async def abandon_run(self, run_id: str) -> bool:
        """把一条 ``interrupted`` 运行终结为 ``cancelled``（V0.5.4 的人工处理出口）。

        返回是否真的改到了。

        **用条件 UPDATE 而不是「先查后写」。** 先查后写之间存在窗口，而窗口内可能发生
        两件事：并发的人工终结会让双方都以为自己成功了；更要紧的是，该运行可能已被
        另一个 worker 重新领取并开始执行——此时把它改成 cancelled 会与一个正在跑的
        运行冲突，界面上看不到任何在跑的东西，但它确实在写外部接口。

        只接受 ``interrupted`` 是刻意的：这是「结掉一条已经停掉的运行」，不是「停止一条
        正在跑的运行」（后者是 ``/cancel`` 的职责）。两者混用会让「我点了取消但任务还在
        跑」这种最难查的状态出现。
        """
        now = _now_ms()

        def work(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "UPDATE workflow_runs "
                "SET status = ?, "
                "    error = COALESCE(error || ' | ', '') || ?, "
                "    finished_at_ms = COALESCE(finished_at_ms, ?) "
                "WHERE run_id = ? AND status = ?",
                (
                    RunStatus.CANCELLED,
                    "人工确认后终结：中断运行不再续跑",
                    now,
                    run_id,
                    RunStatus.INTERRUPTED,
                ),
            )
            connection.commit()
            return cursor.rowcount > 0

        return bool(await self._run(work))

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        """一行运行记录，载荷按需从包回填。

        ``payload_state`` 与 ``payload_missing`` 一并带出：调用方据此区分「这次运行
        本来就没有输出」与「输出在包里 / 包丢了」。前端可以忽略它们，但接口不能不说。
        """
        rows = await self._read(
            "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
        )
        if not rows:
            return None
        data = _row_to_dict(rows[0], json_fields=("inputs_json", "outputs_json"))
        state, overlay, missing = await self.payload_overlay(run_id)
        data["payload_state"] = state
        data["payload_missing"] = missing
        if overlay and overlay.get("run"):
            packed = overlay["run"]
            data["inputs_json"] = packed.get("inputs") or {}
            data["outputs_json"] = packed.get("outputs") or {}
        return data

    async def trace_run_ids(
        self,
        *,
        run_id: str | None = None,
        workflow_id: str | None = None,
        status: str | None = None,
        business_ref: str | None = None,
        created_after_ms: int | None = None,
        created_before_ms: int | None = None,
    ) -> list[str]:
        """固定本次导出的运行集合；默认只选主任务，不受列表 500 条上限影响。"""
        where, params = _run_filter_clause(
            workflow_id=workflow_id,
            status=status,
            business_ref=business_ref,
            include_children=run_id is not None,
            created_after_ms=created_after_ms,
            created_before_ms=created_before_ms,
        )
        if run_id is not None:
            where += " AND run_id = ?" if where else "WHERE run_id = ?"
            params.append(run_id)
        rows = await asyncio.shield(self._read(
            f"SELECT run_id FROM workflow_runs {where} "
            "ORDER BY created_at_ms, run_id", params,
        ))
        return [row["run_id"] for row in rows]

    async def execution_trace(self, run_id: str) -> dict[str, Any] | None:
        """在一次只读事务中获取运行、全部后代、节点、请求尝试及当时的版本快照。"""
        tree = (
            "WITH RECURSIVE tree(run_id) AS ("
            " SELECT run_id FROM workflow_runs WHERE run_id = ?"
            " UNION SELECT child.run_id FROM workflow_runs AS child"
            " JOIN tree ON child.parent_run_id = tree.run_id) "
        )

        def work(connection: sqlite3.Connection) -> dict[str, Any] | None:
            connection.execute("BEGIN")
            try:
                captured_at_ms = _now_ms()
                rows = connection.execute(
                    tree + "SELECT run.* FROM workflow_runs AS run "
                    "JOIN tree ON run.run_id = tree.run_id "
                    "ORDER BY run.created_at_ms, run.run_id", (run_id,),
                ).fetchall()
                if not rows:
                    return None
                runs = {}
                for row in rows:
                    run = _row_to_dict(row, ("inputs_json", "outputs_json"))
                    run["inputs"] = run.pop("inputs_json") or {}
                    run["outputs"] = run.pop("outputs_json") or {}
                    run["is_replay"] = bool(run["is_replay"])
                    run.update(nodes=[], attempts=[], child_run_ids=[])
                    runs[run["run_id"]] = run
                for run in runs.values():
                    if run["parent_run_id"] in runs:
                        runs[run["parent_run_id"]]["child_run_ids"].append(run["run_id"])

                nodes = connection.execute(
                    tree + "SELECT node.* FROM node_executions AS node "
                    "JOIN tree ON node.run_id = tree.run_id "
                    "ORDER BY COALESCE(node.started_at_ms, 0), node.execution_id",
                    (run_id,),
                ).fetchall()
                for row in nodes:
                    runs[row["run_id"]]["nodes"].append(
                        _row_to_dict(row, ("inputs_json", "outputs_json"))
                    )
                attempts = connection.execute(
                    tree + "SELECT attempt.* FROM request_attempts AS attempt "
                    "JOIN tree ON attempt.run_id = tree.run_id ORDER BY attempt.attempt_id",
                    (run_id,),
                ).fetchall()
                for row in attempts:
                    runs[row["run_id"]]["attempts"].append(
                        _row_to_dict(row, ("request_json", "usage_json"))
                    )
                versions = connection.execute(
                    tree + "SELECT version.* FROM definition_versions AS version "
                    "WHERE version.version_id IN (SELECT run.definition_version_id "
                    "FROM workflow_runs AS run JOIN tree ON run.run_id = tree.run_id)",
                    (run_id,),
                ).fetchall()
                return {
                    "root_run_id": run_id,
                    "captured_at_ms": captured_at_ms,
                    "runs": runs,
                    "definitions": {
                        str(row["version_id"]): _row_to_dict(
                            row, ("definition_json", "template_hashes_json")
                        ) for row in versions
                    },
                }
            finally:
                connection.rollback()

        # 下载中断不应取消数据库线程正在完成的 Future。
        trace = await asyncio.shield(self._run(work))
        if trace is not None:
            trace = await self._backfill_trace(trace)
        return trace

    # ------------------------------------------------------------ 载荷回填（存储瘦身）

    async def _backfill_trace(self, trace: dict[str, Any]) -> dict[str, Any]:
        """整棵树按包回填。导出与「搬运前的读」都经过这里。

        只在根运行标记为 ``packed`` 时动手：``in_db`` 的树本来就在库里，多此一举会
        平白解一次包；``purged`` 的树没有包，回了也是空。
        """
        root_id = trace.get("root_run_id")
        root = trace["runs"].get(root_id) or {}
        state = root.get("payload_state")
        trace["payload_state"] = state or PAYLOAD_IN_DB
        if state != PAYLOAD_PACKED:
            return trace

        package = await self._package_for(root_id)
        if package is None:
            trace["payload_missing"] = True
            return trace
        trace["payload_missing"] = False
        # 导出侧据此判断本次导出的完整度（high=full / low=sliced）。
        trace["payload_mode"] = (package.get("manifest") or {}).get("persist_mode")

        packed_runs = {r.get("run_id"): r for r in package.get("runs") or []}
        nodes_by_run: dict[str, dict[Any, dict[str, Any]]] = {}
        for node in package.get("nodes") or []:
            nodes_by_run.setdefault(node.get("run_id"), {})[node.get("execution_id")] = node
        attempts_by_run: dict[str, dict[Any, dict[str, Any]]] = {}
        for attempt in package.get("attempts") or []:
            attempts_by_run.setdefault(attempt.get("run_id"), {})[attempt.get("attempt_id")] = attempt

        for run_id, run in trace["runs"].items():
            packed = packed_runs.get(run_id)
            if packed is not None:
                run["inputs"] = packed.get("inputs") or {}
                run["outputs"] = packed.get("outputs") or {}
            packed_nodes = nodes_by_run.get(run_id, {})
            for node in run.get("nodes") or []:
                item = packed_nodes.get(node.get("execution_id"))
                if item is None:
                    continue
                node["inputs_json"] = item.get("inputs_json")
                node["outputs_json"] = item.get("outputs_json")
                if item.get("payload_truncated"):
                    node["payload_truncated"] = item["payload_truncated"]
                if item.get("outputs_truncated_from") is not None:
                    node["outputs_truncated_from"] = item["outputs_truncated_from"]
            packed_attempts = attempts_by_run.get(run_id, {})
            for attempt in run.get("attempts") or []:
                item = packed_attempts.get(attempt.get("attempt_id"))
                if item is None:
                    continue
                attempt["request_json"] = item.get("request_json")
                attempt["response_text"] = item.get("response_text")
                attempt["usage_json"] = item.get("usage_json")
            self._apply_end_node_fallback(run.get("nodes") or [], run.get("outputs"))
        return trace


    async def _run_row(self, run_id: str) -> dict[str, Any] | None:
        """取一行运行骨架（不做包回填）。回填逻辑自己要用，不能递归进 get_run。"""
        rows = await self._read(
            "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
        )
        return _row_to_dict(rows[0]) if rows else None

    async def _root_run_id(self, run_id: str) -> str:
        """沿 ``parent_run_id`` 上溯到根运行。树只有一两层，但迭代会加深。"""
        def work(connection: sqlite3.Connection) -> str:
            row = connection.execute(
                "WITH RECURSIVE up(run_id, parent_run_id) AS ("
                " SELECT run_id, parent_run_id FROM workflow_runs WHERE run_id = ?"
                " UNION SELECT p.run_id, p.parent_run_id FROM workflow_runs p"
                " JOIN up ON p.run_id = up.parent_run_id) "
                "SELECT run_id FROM up WHERE parent_run_id IS NULL LIMIT 1",
                (run_id,),
            ).fetchone()
            return str(row["run_id"]) if row is not None else run_id

        return await self._run(work)

    async def _package_for(self, root_run_id: str) -> dict[str, Any] | None:
        """读回并缓存一棵运行树的包。读不到返回 ``None``（调用方据此标缺失）。

        **不抛异常**：包缺失或损坏是「证据没了」，不是「这次请求失败」。让接口继续
        返回骨架、并在响应里标 ``payload_missing``，比直接 500 有用得多。
        """
        cached = self._payload_cache.get(root_run_id, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            self._payload_cache.move_to_end(root_run_id)
            return cached

        package: dict[str, Any] | None = None
        if self.archive_dir is not None:
            root = await self._run_row(root_run_id)
            created_at = (root or {}).get("created_at_ms")
            target = archive.package_path(self.archive_dir, root_run_id, created_at)
            try:
                package = await asyncio.to_thread(archive.read_package, target)
            except FileNotFoundError:
                logger.error(
                    "留痕包缺失：{}（运行 {} 的载荷已不在库内，也无法从包读回）",
                    target, root_run_id,
                )
            except Exception as exc:  # 损坏、版本不兼容等
                logger.error(
                    "留痕包读取失败：{}（{}: {}）",
                    target, type(exc).__name__, exc,
                )

        self._payload_cache[root_run_id] = package
        self._payload_cache.move_to_end(root_run_id)
        while len(self._payload_cache) > self._payload_cache_size:
            self._payload_cache.popitem(last=False)
        return package

    async def payload_overlay(
        self, run_id: str
    ) -> tuple[str | None, dict[str, Any] | None, bool]:
        """给一个运行算出 (payload_state, overlay, payload_missing)。

        ``overlay`` 的形状是 ``{"run": {...}, "nodes": {execution_id: {...}},
        "attempts": {attempt_id: {...}}}``；``None`` 表示不需要回填——载荷还在库里
        （``in_db``）或者已经丢弃且没有包（``purged``）。
        """
        row = await self._run_row(run_id)
        if row is None:
            return None, None, False
        state = row.get("payload_state") or PAYLOAD_IN_DB
        if state != PAYLOAD_PACKED:
            return state, None, False

        root_run_id = await self._root_run_id(run_id)
        package = await self._package_for(root_run_id)
        if package is None:
            return state, None, True

        overlay: dict[str, Any] = {"run": None, "nodes": {}, "attempts": {}}
        for run in package.get("runs") or []:
            if run.get("run_id") == run_id:
                overlay["run"] = run
                break
        for node in package.get("nodes") or []:
            if node.get("run_id") == run_id:
                overlay["nodes"][node.get("execution_id")] = node
        for attempt in package.get("attempts") or []:
            if attempt.get("run_id") == run_id:
                overlay["attempts"][attempt.get("attempt_id")] = attempt
        return state, overlay, False

    @staticmethod
    def _apply_end_node_fallback(
        nodes: Sequence[dict[str, Any]], run_outputs: Mapping[str, Any] | None
    ) -> None:
        """``end`` 节点的输出在包里不单存，读回时用所在运行的输出补上。

        这不算「猜」：调度层本来就是把这些出口节点的输出合并成运行输出的，
        两者在打包前逐字节相同（抽样 60/60）。
        """
        for node in nodes:
            if node.get("node_type") == archive.END_NODE_TYPE and not node.get("outputs_json"):
                node["outputs_json"] = dict(run_outputs or {})

    # ------------------------------------------------------------ 载荷搬运（存储瘦身）

    async def finalize_tree(
        self,
        root_run_id: str,
        *,
        mode: str,
        archive_dir: Path,
        media_index: Mapping[str, str] | None = None,
        code_commit: str | None = None,
        compress_level: int = 6,
    ) -> dict[str, Any]:
        """把一棵运行树的运行时数据搬进包，成功后清空库内载荷。

        **顺序是这里唯一的硬约束**：先读树 → 裁剪 → 写盘（含 fsync）→ 成功后才在
        写事务里清空。任何一步失败都把异常抛给调用方，库内载荷保持原样、状态仍是
        ``in_db``。反过来做（先清后写）一旦写盘失败就没有第二份副本了。

        返回包的写入结果（路径、字节数、sha256），便于调用方记日志。
        """
        trace = await self.execution_trace(root_run_id)
        if trace is None:
            raise LookupError(f"运行 {root_run_id} 不存在或已无记录")

        package = archive.build_package(
            trace, mode=mode, media_index=media_index, code_commit=code_commit
        )
        root = trace["runs"].get(root_run_id) or {}
        target = archive.package_path(
            archive_dir, root_run_id, root.get("created_at_ms")
        )
        written = await asyncio.to_thread(
            archive.write_package, package, target, compress_level=compress_level
        )

        run_ids = list(trace["runs"])
        await self._run(self._clear_payloads(run_ids, root_run_id, mode))
        written["mode"] = mode
        written["counts"] = package["manifest"]["counts"]
        return written

    @staticmethod
    def _clear_payloads(
        run_ids: Sequence[str], root_run_id: str, mode: str
    ) -> Callable[[sqlite3.Connection], None]:
        """清空这棵树各行的载荷列，并标上 ``packed``。

        标记写到树里**每一行**而不只是根运行：直接按 run_id 打开某个子运行时，
        读路径同样要知道它的载荷在包里，而不是「列是空的 = 没有输出」。
        """
        def work(connection: sqlite3.Connection) -> None:
            now = _now_ms()
            try:
                connection.execute("BEGIN")
                for run_id in run_ids:
                    connection.execute(
                        "UPDATE node_executions SET inputs_json = NULL, "
                        "outputs_json = NULL WHERE run_id = ?",
                        (run_id,),
                    )
                    connection.execute(
                        "UPDATE request_attempts SET request_json = NULL, "
                        "response_text = NULL, usage_json = NULL WHERE run_id = ?",
                        (run_id,),
                    )
                    connection.execute(
                        "UPDATE workflow_runs SET outputs_json = NULL, "
                        "payload_state = ?, payload_mode = ?, archived_at_ms = ? "
                        "WHERE run_id = ?",
                        (PAYLOAD_PACKED, mode, now, run_id),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

        return work

    async def payload_kinds(self, run_ids: Sequence[str]) -> set[str]:
        """一批根运行的载荷完整度：``full`` / ``sliced`` / ``skeleton``。

        导出响应头在推流前就要定，因此不能边流边算。这里一次查完：
        ``purged`` 或没有包 → skeleton；``low`` 档 → sliced；其余 → full。
        """
        if not run_ids:
            return {"full"}
        marks = ",".join("?" for _ in run_ids)
        rows = await self._read(
            f"SELECT payload_state, payload_mode FROM workflow_runs "
            f"WHERE run_id IN ({marks})",
            list(run_ids),
        )
        kinds: set[str] = set()
        for row in rows:
            state = row["payload_state"]
            if state == PAYLOAD_PURGED:
                kinds.add("skeleton")
            elif state == PAYLOAD_PACKED and (row["payload_mode"] or "").lower() == "low":
                kinds.add("sliced")
            else:
                kinds.add("full")
        return kinds or {"full"}

    async def purge_tree_payloads(self, root_run_id: str) -> int:
        """存量处置：就地清空一棵树的载荷并标 ``purged``，不动根运行输出。

        与 :meth:`finalize_tree` 的区别是**不写包**。用于「旧数据不迁移」的一次性清理：
        旧行的问题已经查过，为它们再建一套回灌通道不划算（设计稿 §11.1）。
        根运行的 ``outputs_json`` 保留——那是我们当时真正写出去的东西。
        """
        trace = await self.execution_trace(root_run_id)
        if trace is None:
            return 0
        run_ids = list(trace["runs"])

        def work(connection: sqlite3.Connection) -> int:
            now = _now_ms()
            try:
                connection.execute("BEGIN")
                for run_id in run_ids:
                    connection.execute(
                        "UPDATE node_executions SET inputs_json = NULL, "
                        "outputs_json = NULL WHERE run_id = ?",
                        (run_id,),
                    )
                    connection.execute(
                        "UPDATE request_attempts SET request_json = NULL, "
                        "response_text = NULL, usage_json = NULL WHERE run_id = ?",
                        (run_id,),
                    )
                    if run_id != root_run_id:
                        connection.execute(
                            "UPDATE workflow_runs SET outputs_json = NULL WHERE run_id = ?",
                            (run_id,),
                        )
                    connection.execute(
                        "UPDATE workflow_runs SET payload_state = ?, archived_at_ms = ? "
                        "WHERE run_id = ? AND payload_state IS NULL",
                        (PAYLOAD_PURGED, now, run_id),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            return len(run_ids)

        return int(await self._run(work))

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

    async def mark_running_as_interrupted(self, *, workflow_scope: WorkflowScope = "all") -> int:
        """把残留的 ``running`` 运行标记为 ``interrupted``（§7.3）。

        **只标 ``running``，不标 ``queued``**（V0.5.2 修正）。此前两者一起标，会让
        worker 一重启就把「还没开始的任务」误判成中断——而任务的持久化入队正是为了让它
        在重启后能继续。「重启不丢任务」与「把 queued 标 interrupted」不能同时成立，
        故保留 ``queued`` 交由 worker 重新领取。

        ``running`` 只标不续跑：进程中断不能证明外部副作用未发生（既定约束），因此这些运行**保留已完成结果**并交人工处理。
        """
        predicate, params = scope_predicate(workflow_scope)

        def work(connection: sqlite3.Connection) -> int:
            # 只沿在跑运行的祖先查找主任务，避免恢复时遍历全部历史子流程。
            # 同名共享子工作流必须按其主任务归属恢复。
            cursor = connection.execute(
                "WITH RECURSIVE lineage(run_id, parent_run_id, workflow_id) AS ("
                " SELECT run_id, parent_run_id, workflow_id FROM workflow_runs WHERE status = ?"
                " UNION SELECT lineage.run_id, parent.parent_run_id, parent.workflow_id"
                " FROM workflow_runs parent JOIN lineage ON parent.run_id = lineage.parent_run_id"
                ") UPDATE workflow_runs SET status = ?, error = COALESCE(error, ?)"
                " WHERE status = ? AND run_id IN ("
                f" SELECT run_id FROM lineage WHERE parent_run_id IS NULL AND {predicate})"
                " RETURNING run_id",
                (RunStatus.RUNNING, RunStatus.INTERRUPTED,
                 "进程中断：运行中任务未完成，已完成结果已保留", RunStatus.RUNNING, *params),
            )
            marked = len(cursor.fetchall())
            connection.commit()
            return marked

        return await self._run(work)

    # ------------------------------------------------------------ 队列（V0.5.2）

    async def enqueue_run(self, run: RunRecordRow) -> None:
        """把一个运行入队：写一行 ``status='queued'``，并记下入队时刻。

        队列复用 ``workflow_runs`` 表（用户 2026-09-24 决定）。这样「运行」只有一个
        事实来源——另建队列表会让同一件事有两份状态需要同步，而同步失败的表现是
        「队列里有、运行列表里没有」这种最难查的错。

        子运行**不入队**：它们由 ``SubWorkflowRunner`` 直接执行，共用同一张表但不参与
        排队，否则主入口一次扇出十几路会与父运行抢队列名额。
        """
        row = RunRecordRow(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            workflow_display_name=run.workflow_display_name,
            parent_run_id=run.parent_run_id,
            parent_node_id=run.parent_node_id,
            call_path=run.call_path,
            business_ref=run.business_ref,
            status=RunStatus.QUEUED,
            inputs=run.inputs,
            is_replay=run.is_replay,
            queued_at_ms=run.queued_at_ms or _now_ms(),
        )
        await self.insert_run(row)

    async def enqueue_run_once(self, run: RunRecordRow) -> bool:
        """仅首次写入队列；重复请求绝不把 running/终态改回 queued。"""
        now = _now_ms()

        def work(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "INSERT INTO workflow_runs (run_id, workflow_id, workflow_display_name, "
                "business_ref, status, inputs_json, is_replay, queued_at_ms, created_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (run_id) DO NOTHING",
                (run.run_id, run.workflow_id, run.workflow_display_name, run.business_ref,
                 RunStatus.QUEUED, json_dumps(run.inputs), int(run.is_replay), now, now),
            )
            inserted = cursor.rowcount == 1
            cursor.close()
            connection.commit()
            return inserted

        return await self._run(work)

    async def claim_next_queued_run(
        self, worker_id: str, *, lease_ms: int, workflow_scope: WorkflowScope = "all"
    ) -> dict[str, Any] | None:
        """原子领取一个待跑运行；无待跑或已被别人领走时返回 ``None``。

        **单条 UPDATE ... WHERE status='queued' + RETURNING 保证只被领一次。**
        SQLite 的写事务是串行的，因此两个 worker 同时执行本语句时，只有一个能把
        某一行从 ``queued`` 改成 ``running``——另一个的 ``WHERE`` 不再命中，
        ``RETURNING`` 无行返回。这比「先 SELECT 再 UPDATE」安全：后者之间存在窗口，
        窗口内两个 worker 会领到同一个 run_id 并把它跑两遍。

        ``RETURNING`` 需要 SQLite ≥ 3.35（本机实测 3.50.4）。
        """
        now = _now_ms()
        predicate, params = scope_predicate(workflow_scope)

        def work(connection: sqlite3.Connection) -> dict[str, Any] | None:
            cursor = connection.execute(
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
                "          claimed_at_ms, lease_expires_at_ms",
                (
                    RunStatus.RUNNING,
                    now,
                    worker_id,
                    now + int(lease_ms),
                    RunStatus.QUEUED,
                    *params,
                    RunStatus.QUEUED,
                ),
            )
            row = cursor.fetchone()
            connection.commit()
            return dict(row) if row is not None else None

        return await self._run(work)

    async def renew_lease(
        self, run_id: str, worker_id: str, *, lease_ms: int
    ) -> bool:
        """续租。返回是否续上（运行已不在本 worker 名下时返回 ``False``）。

        长运行（主入口实测约 1282 s）远超缺省租约，必须靠心跳续租，否则会被别的
        worker 当作「租约过期」重新领走并跑第二遍。
        """
        now = _now_ms()

        def work(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "UPDATE workflow_runs SET lease_expires_at_ms = ? "
                "WHERE run_id = ? AND claimed_by = ? AND status = ?",
                (now + int(lease_ms), run_id, worker_id, RunStatus.RUNNING),
            )
            connection.commit()
            return cursor.rowcount > 0

        return bool(await self._run(work))

    async def requeue_expired_leases(self, *, limit: int = 50) -> int:
        """把租约过期的运行退回 ``queued``，供任何 worker 重新领取。

        这是「worker 重启不丢任务」的落地：一个 worker 崩溃后，它领走的运行会停在
        ``running`` 且租约不再续期；到期后由本方法退回队列，另一个 worker（或同一个
        worker 重启后）接着跑。

        **退回的是整条运行，不是从断点续跑**——这符合「不做任意节点断点自动续跑」的
        约束：能重领的前提是幂等，而是否真正幂等由回写协议决定（V0.5.5）。
        """
        now = _now_ms()

        def work(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "UPDATE workflow_runs "
                "SET status = ?, claimed_at_ms = NULL, claimed_by = NULL, "
                "    lease_expires_at_ms = NULL "
                "WHERE run_id IN ("
                "  SELECT run_id FROM workflow_runs "
                "  WHERE status = ? AND lease_expires_at_ms IS NOT NULL "
                "        AND lease_expires_at_ms <= ? "
                "  ORDER BY lease_expires_at_ms LIMIT ?"
                ")",
                (RunStatus.QUEUED, RunStatus.RUNNING, now, int(limit)),
            )
            connection.commit()
            return int(cursor.rowcount)

        return await self._run(work)

    async def queue_depth(self, *, workflow_scope: WorkflowScope = "all") -> int:
        """待跑（``queued``）的主运行条数。提交侧的容量判断用它。"""
        predicate, params = scope_predicate(workflow_scope)
        rows = await self._read(
            "SELECT COUNT(*) AS n FROM workflow_runs "
            f"WHERE status = ? AND parent_run_id IS NULL AND {predicate}",
            (RunStatus.QUEUED, *params),
        )
        return int(rows[0]["n"]) if rows else 0

    async def queue_snapshot(self, *, workflow_scope: WorkflowScope = "all") -> dict[str, int]:
        """队列概览：待跑、在跑、中断待处理。供 ``GET /runtime`` 展示。"""
        predicate, params = scope_predicate(workflow_scope)
        rows = await self._read(
            "SELECT status, COUNT(*) AS n FROM workflow_runs "
            f"WHERE parent_run_id IS NULL AND {predicate} AND status IN (?, ?, ?) "
            "GROUP BY status",
            (*params, RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.INTERRUPTED),
        )
        counts = {str(r["status"]): int(r["n"]) for r in rows}
        return {
            "queued": counts.get(RunStatus.QUEUED, 0),
            "running": counts.get(RunStatus.RUNNING, 0),
            "interrupted": counts.get(RunStatus.INTERRUPTED, 0),
        }

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
        attempt_count: int | None = None,
    ) -> None:
        """写入或更新一条节点执行记录。

        以 ``(run_id, node_id, call_path)`` 为唯一键：同一静态节点 ID 在迭代或子流程里
        会多次执行，靠 ``call_path`` 区分。
        """
        await self._write(
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
            " queued_at_ms = COALESCE(excluded.queued_at_ms, node_executions.queued_at_ms)",
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
                attempt_count,
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
        """节点执行明细，载荷按需从包回填。

        ``end`` 节点的输出不单独入包（与运行输出逐字节相同），回填时用运行输出补上，
        否则详情页里出口节点会显示成「没有输出」。
        """
        rows = await self._read(
            "SELECT * FROM node_executions WHERE run_id = ? "
            "ORDER BY COALESCE(started_at_ms, 0), execution_id",
            (run_id,),
        )
        nodes = [
            _row_to_dict(r, json_fields=("inputs_json", "outputs_json")) for r in rows
        ]
        state, overlay, missing = await self.payload_overlay(run_id)
        if overlay:
            packed = overlay["nodes"]
            for node in nodes:
                item = packed.get(node.get("execution_id"))
                if item is None:
                    continue
                node["inputs_json"] = item.get("inputs_json")
                node["outputs_json"] = item.get("outputs_json")
                if item.get("payload_truncated"):
                    node["payload_truncated"] = item["payload_truncated"]
                if item.get("outputs_truncated_from") is not None:
                    node["outputs_truncated_from"] = item["outputs_truncated_from"]
            if missing:
                for node in nodes:
                    node["payload_missing"] = True
            run_row = await self._run_row(run_id)
            run_outputs = None
            if overlay.get("run") is not None:
                run_outputs = overlay["run"].get("outputs")
            elif run_row is not None:
                run_outputs = json_loads(run_row.get("outputs_json"))
            self._apply_end_node_fallback(nodes, run_outputs)
        elif missing:
            for node in nodes:
                node["payload_missing"] = True
        return nodes

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
        anomaly: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        await self._write(
            "INSERT INTO request_attempts (run_id, node_id, call_path, attempt_no, kind, "
            " target, request_json, response_text, status_code, duration_ms, error, "
            " error_code, usage_json, provider_request_id, replayed, anomaly, "
            " finish_reason, created_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                anomaly,
                finish_reason,
                _now_ms(),
            ),
        )

    async def list_attempts(
        self,
        run_id: str,
        *,
        node_id: str | None = None,
        call_path: str | None = None,
    ) -> list[dict[str, Any]]:
        """取一次运行的外部请求尝试。

        ``call_path`` 与 ``node_id`` 一起用才精确：迭代类工作流的**同一个静态节点 ID**
        在每轮都会重新执行，只按 ``node_id`` 过滤会把各轮尝试混成一堆，看不出哪次属于
        哪一轮。节点的 ``call_path`` 就在 ``node_executions`` 行里，调用方直接传回即可。
        """
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if node_id is not None:
            clauses.append("node_id = ?")
            params.append(node_id)
        if call_path is not None:
            clauses.append("call_path = ?")
            params.append(call_path)
        rows = await self._read(
            f"SELECT * FROM request_attempts WHERE {' AND '.join(clauses)} "
            "ORDER BY attempt_id",
            params,
        )
        attempts = [
            _row_to_dict(r, json_fields=("request_json", "usage_json")) for r in rows
        ]
        state, overlay, missing = await self.payload_overlay(run_id)
        if overlay:
            packed = overlay["attempts"]
            for attempt in attempts:
                item = packed.get(attempt.get("attempt_id"))
                if item is None:
                    continue
                attempt["request_json"] = item.get("request_json")
                attempt["response_text"] = item.get("response_text")
                attempt["usage_json"] = item.get("usage_json")
            if missing:
                for attempt in attempts:
                    attempt["payload_missing"] = True
        elif missing:
            for attempt in attempts:
                attempt["payload_missing"] = True
        return attempts

    async def count_attempts(
        self,
        run_id: str,
        *,
        node_id: str | None = None,
        call_path: str | None = None,
    ) -> int:
        """该运行（可限定到节点/调用路径）的尝试条数。

        ``node_finished`` 用它在节点行上落一个**派生**的 ``attempt_count``，而不是在
        记录每次尝试时自增。派生与来源表天然一致；自增强调「每次尝试恰好加一」，
        一旦某条路径漏记或多记就永久偏移，且从行上看不出错——本项目已两次踩到
        「看起来记了、其实没记」。
        """
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if node_id is not None:
            clauses.append("node_id = ?")
            params.append(node_id)
        if call_path is not None:
            clauses.append("call_path = ?")
            params.append(call_path)
        rows = await self._read(
            f"SELECT COUNT(*) AS n FROM request_attempts WHERE {' AND '.join(clauses)}",
            params,
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


_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "workflow_runs": (
        ("queued_at_ms", "INTEGER"),
        ("claimed_at_ms", "INTEGER"),
        ("claimed_by", "TEXT"),
        ("lease_expires_at_ms", "INTEGER"),
        # 存储瘦身：载荷搬进包之后的状态标记。没有它，读路径无法区分
        # 「已搬进包」与「本来就没有」，而那正是最容易被误读成「管线没产出」的情形。
        ("payload_state", "TEXT"),
        ("payload_mode", "TEXT"),
        ("archived_at_ms", "INTEGER"),
    ),
    "request_attempts": (
        ("anomaly", "TEXT"),
        ("finish_reason", "TEXT"),
    ),
}
"""V0.5.2 起新增的列：表名 -> (列名, 类型) 序列。

``CREATE TABLE IF NOT EXISTS`` 对**已存在**的表不会补列，而 data/customer_profile.db
已在磁盘上。没有这一步，新列在运行期只表现为一句 no such column。

``request_attempts.anomaly`` / ``finish_reason`` 自 v3.3+ 添加，用于诊断异常重试与静默降级。
"""


def _apply_migrations(connection: sqlite3.Connection) -> None:
    """给既有库补上缺失的列。幂等。

    ``ALTER TABLE ... ADD COLUMN`` 本身**不幂等**（重复执行报 duplicate column
    name），因此先读 ``PRAGMA table_info`` 判断列是否已存在。

    API 与 worker 两个进程启动时都会跑本函数，存在「两个进程同时补同一列」的窗口：
    后来者会拿到 duplicate column name。那不是故障——另一个进程已经补好了，
    因此吞掉该错误，其余错误照常抛出。
    """
    for table, columns in _ADDED_COLUMNS.items():
        existing = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not existing:
            # 表还不存在（由 SCHEMA_STATEMENTS 建出时会带上全部列）
            continue
        for name, column_type in columns:
            if name in existing:
                continue
            try:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {column_type}"
                )
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise


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
