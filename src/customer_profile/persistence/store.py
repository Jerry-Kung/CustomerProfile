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

    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self._thread: _ConnectionThread | None = None
        self._busy_timeout_ms = busy_timeout_ms

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
        """把残留的 ``running`` 运行标记为 ``interrupted``（§7.3）。

        **只标 ``running``，不标 ``queued``**（V0.5.2 修正）。此前两者一起标，会让
        worker 一重启就把「还没开始的任务」误判成中断——而任务的持久化入队正是为了让它
        在重启后能继续。「重启不丢任务」与「把 queued 标 interrupted」不能同时成立，
        故保留 ``queued`` 交由 worker 重新领取。

        ``running`` 只标不续跑：进程中断不能证明外部副作用未发生（`Dify迁移任务说明.md`
        §7.3），因此这些运行**保留已完成结果**并交人工处理。
        """
        rows = await self._read(
            "SELECT run_id FROM workflow_runs WHERE status = ?", (RunStatus.RUNNING,)
        )
        run_ids = [r["run_id"] for r in rows]
        for run_id in run_ids:
            await self._write(
                "UPDATE workflow_runs SET status = ?, error = COALESCE(error, ?) "
                "WHERE run_id = ? AND status = ?",
                (
                    RunStatus.INTERRUPTED,
                    "进程中断：运行中任务未完成，已完成结果已保留",
                    run_id,
                    RunStatus.RUNNING,
                ),
            )
        return len(run_ids)

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

    async def claim_next_queued_run(
        self, worker_id: str, *, lease_ms: int
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

        def work(connection: sqlite3.Connection) -> dict[str, Any] | None:
            cursor = connection.execute(
                "UPDATE workflow_runs "
                "SET status = ?, claimed_at_ms = ?, claimed_by = ?, "
                "    lease_expires_at_ms = ? "
                "WHERE run_id = ("
                "  SELECT run_id FROM workflow_runs "
                "  WHERE status = ? AND parent_run_id IS NULL "
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

    async def queue_depth(self) -> int:
        """待跑（``queued``）的主运行条数。提交侧的容量判断用它。"""
        rows = await self._read(
            "SELECT COUNT(*) AS n FROM workflow_runs "
            "WHERE status = ? AND parent_run_id IS NULL",
            (RunStatus.QUEUED,),
        )
        return int(rows[0]["n"]) if rows else 0

    async def queue_snapshot(self) -> dict[str, int]:
        """队列概览：待跑、在跑、中断待处理。供 ``GET /runtime`` 展示。"""
        rows = await self._read(
            "SELECT status, COUNT(*) AS n FROM workflow_runs "
            "WHERE parent_run_id IS NULL AND status IN (?, ?, ?) "
            "GROUP BY status",
            (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.INTERRUPTED),
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
        会多次执行，靠 ``call_path`` 区分（`Dify迁移任务说明.md` §5.3）。
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
        return [
            _row_to_dict(r, json_fields=("request_json", "usage_json")) for r in rows
        ]

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
    ),
}
"""V0.5.2 起新增的列：表名 -> (列名, 类型) 序列。

``CREATE TABLE IF NOT EXISTS`` 对**已存在**的表不会补列，而 data/customer_profile.db
已在磁盘上。没有这一步，新列在运行期只表现为一句 no such column。
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
