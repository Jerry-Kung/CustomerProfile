"""留痕持久化：建库、写入与查询。

支持两种后端（见 ``dbapi``）：SQLite 用于本地/批次/快照，MySQL 用于运行时。
语句形状由 ``dialect`` 提供，连接差异由 ``dbapi`` 抹平；**本模块不写任何一方的**
**专有语法**——出现 ``ON CONFLICT``、``RETURNING``、``PRAGMA`` 就说明漏到了这一层。

**线程模型是这里唯一需要小心的地方。** 数据库连接不是线程安全的
（``sqlite3.Connection`` 默认甚至禁止跨线程使用，``check_same_thread=False`` 只是
关掉那道检查，多线程共用会直接崩掉解释器）。因此本模块让连接有**唯一所有者**：
一个专属工作线程。所有读写都排进该线程的队列顺序执行，回调通过
``concurrent.futures.Future`` 回到调用方。这样既没有跨线程访问，也不需要调用方加锁。

这个工作线程**与用哪种数据库无关**，因此切 MySQL 后保留：它同时是 2026-11 那次
「容器 Up、执行停止」的修复点（``Future`` 取消竞态），不是 SQLite 的附属物。
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
from .dialect import MySqlDialect, SqliteDialect
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
    """独占一个数据库连接的工作线程。

    对外只暴露 :meth:`submit`：把一个可调用对象排进队列，在其上返回一个
    ``concurrent.futures.Future``。可调用对象由工作线程执行，因此它拿到的连接
    始终是同一个线程创建的。

    ``work`` 收到的参数是 :class:`~.dbapi.DbConnection`，不是某个驱动的连接对象。
    """

    def __init__(
        self,
        backend: Any,
        setup: Callable[[Any], None],
    ) -> None:
        self._backend = backend
        self._label = getattr(backend, "name", "db")
        self._commands: "queue.Queue[Any]" = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name=f"{self._label}-store", daemon=True
        )
        self._setup = setup
        self._started = threading.Event()
        self._start_error: BaseException | None = None
        self._last_result: Any = None
        """最近一次成功的返回值。与 ``work()`` 分离，是为了让重连分支能统一写回。"""
        self._last_ok_at_ms: int | None = None
        self._db_error: str | None = None
        self._reconnect_max_attempts = 3
        self._thread.start()
        self._started.wait()

    # ------------------------------------------------------------ 对外

    def submit(self, work: Callable[[Any], Any]) -> Future:
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

    @property
    def start_error(self) -> BaseException | None:
        """建连/setup 阶段留下的异常。``None`` 表示连上了。"""
        return self._start_error

    # ------------------------------------------------------------ 内部

    def _run(self) -> None:
        connection: Any = None
        try:
            connection = self._backend.connect()
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
                connection = self._execute_recovering(connection, work)
                if not future.done():
                    future.set_result(self._last_result)
            except BaseException as exc:
                # **写回前必须再判一次 done()。** 上一行的 ``future.cancelled()`` 只覆盖
                # 「排队期间就被取消」；如果取消发生在 ``work()`` 执行期间，``Future``
                # 会链式收到 CANCELLED，此时 ``set_result`` 抛 ``InvalidStateError``，
                # 再对同一个已取消对象 ``set_exception`` 会抛第二次——那次没有外层捕获，
                # 异常直接冲出 while 循环，工作线程死亡且跳过 ``connection.close()``。
                #
                # 线程一死，``Store._require_thread`` 就抛「留痕读写需要活动连接」，
                # 而这个进程仍存活，表现为「容器 Up、执行已停止」。2026-11 UAT 的
                # 长时间积压即由此而来；触发路径是心跳续租（``worker._heartbeat``）
                # 在任务收尾 ``heartbeat.cancel()`` 时正好撞上这次写回。
                #
                # ``done()`` 对 CANCELLED 也返回 True，因此这一句同时覆盖
                # 「已取消」与「已由别处设置结果」两种情形。
                #
                # 连接对象本身也在这里清掉：断开状态下后续操作必然再失败，留着它
                # 只会让每次操作都付一次超时。
                if self._backend.is_disconnect(exc):
                    self._mark_disconnected(exc)
                    connection = None
                if not future.done():
                    future.set_exception(exc)

        if connection is not None:
            connection.close()

    def _execute_recovering(self, connection: Any, work: Callable[[Any], Any]) -> Any:
        """执行一次操作；遇到断连就重连并把**同一次**操作重试一次。

        为什么可以重试：``work`` 里的写路径都是 upsert 或条件 UPDATE（幂等），
        纯 INSERT 只有 ``insert_attempt``——它对 ``attempt_id`` 自增，重试会多一行。
        因此这里判断的是「连接是不是已经死了」：连接在**开始执行前**就是断的，
        重试是安全的（那次操作根本没发出去）；如果在执行中才断，无法区分「服务端已提交
        但响应丢了」，此时**不重试**，把异常如实交给调用方（留痕是 fail_soft，不会让
        业务失败，但会留下错误日志）。
        """
        if connection is None:
            connection = self._reconnect()
            return self._invoke(connection, work)

        if not self._connection_alive(connection):
            self._mark_disconnected(ConnectionResetError("连接已失效"))
            connection = self._reconnect()
        return self._invoke(connection, work)

    def _invoke(self, connection: Any, work: Callable[[Any], Any]) -> Any:
        try:
            self._last_result = work(connection)
            self._last_ok_at_ms = _now_ms()
            return connection
        except BaseException as exc:
            if self._backend.is_disconnect(exc):
                # 执行中断开：重试可能重复一次已经生效的写，故只重连不重试。
                self._mark_disconnected(exc)
                raise
            raise

    def _reconnect(self) -> Any:
        """重建连接。连续失败会逐次放大间隔，避免把人打满。"""
        attempt = 0
        while True:
            try:
                connection = self._backend.connect()
                if attempt:
                    logger.warning("数据库已重连（此前失败 {} 次）", attempt)
                return connection
            except BaseException as exc:
                attempt += 1
                self._mark_disconnected(exc)
                if attempt >= self._reconnect_max_attempts:
                    raise
                time.sleep(min(2.0 ** (attempt - 1), 10.0) * 0.1)

    def _connection_alive(self, connection: Any) -> bool:
        probe = getattr(connection, "is_connected", None)
        if probe is None:
            return True
        try:
            return bool(probe())
        except Exception:
            return False

    def _mark_disconnected(self, exc: BaseException) -> None:
        self._db_error = f"{type(exc).__name__}: {exc}"[:300]
        logger.error("数据库连接中断：{}", self._db_error)

    def healthy(self) -> dict[str, Any]:
        """供 liveness 与健康检查读取的数据库状态。"""
        alive = self.alive
        fresh = self._last_ok_at_ms
        return {
            "db_alive": alive,
            "db_last_ok_at_ms": fresh,
            "db_error": self._db_error,
        }

    @property
    def last_ok_at_ms(self) -> int | None:
        return self._last_ok_at_ms


class Store:
    """留痕存储。所有方法都是协程，实际数据库调用在专属线程内执行。"""

    def __init__(
        self,
        backend: Any,
        *,
        busy_timeout_ms: int = 5000,
        archive_dir: Path | str | None = None,
        migrate: bool | None = None,
    ) -> None:
        # 兼容旧构造：``Store(path)`` / ``Store(Path(...))``。
        # 批次脚本、快照测试与排障工具都按「一个库文件」使用它，切 MySQL 不该逼它们改。
        if isinstance(backend, (str, Path)):
            from .dbapi import SqliteBackend

            backend = SqliteBackend(Path(backend), busy_timeout_ms=busy_timeout_ms)
        self._backend = backend
        self.path = getattr(backend, "path", None)
        """仅 SQLite 后端有意义；MySQL 上为 ``None``。保留是为了让「基线表在哪」这类
        排障脚本不至于在切库当天全部失效。"""
        self._thread: _ConnectionThread | None = None
        self.archive_dir = Path(archive_dir) if archive_dir else None
        """运行时数据包目录（存储瘦身）。``None`` 表示不读包——此时 ``packed`` 的
        行会如实返回空载荷并标 ``payload_missing``，不会静默当成「没有输出」。"""
        self._migrate = (backend.name == "sqlite") if migrate is None else migrate
        """是否在连接时应用表结构。**缺省由后端决定，没有配置开关**：

        - SQLite：建表 + 补列。文件是本地产物，「给个路径就能跑」是批次与测试的前提，
          没有第二个进程能替它建表。
        - MySQL：**不建表**。表由 ``runtime-migrate`` 容器建（随部署自动执行），
          服务账号因此完全不需要 DDL 权限。多个副本同时启动也不会抢 DDL 锁。
        """
        self._payload_cache: "OrderedDict[str, dict[str, Any] | None]" = OrderedDict()
        self._payload_cache_size = 4
        """按根运行缓存已解析的包。包解压后约数 MB，宁可重复解也不常驻太多。"""

    # ------------------------------------------------------------ 生命周期

    def connect(self) -> None:
        """连接并按需应用表结构。幂等。"""
        if self._thread is not None and self._thread.alive:
            return

        def setup(connection: Any) -> None:
            if self._backend.name == "sqlite":
                if self._migrate:
                    for statement in SCHEMA_STATEMENTS:
                        connection.execute(statement)
                    # 建表之后补列：CREATE TABLE IF NOT EXISTS 对**已存在**的库不会新增列，
                    # 而 data/customer_profile.db 已在磁盘上。不加这一步，新列在运行期
                    # 只表现为一句 "no such column"。
                    _apply_migrations(connection)
                    connection.commit()
                return

            from . import mysql_schema

            if self._migrate:
                mysql_schema.ensure_mysql_schema(connection)
            # **无条件校验，且期望值直接取代码里的 SCHEMA_VERSION**：库里没有「该用什么
            # 版本」这一项配置，代码就是唯一事实。结构对不上时拒绝启动——带着旧表结构
            # 跑起来的问题要到第一个新列才暴露，而那时现场显示的是「服务 Up、任务全失败」。
            mysql_schema.assert_expected_version(connection)

        self._thread = _ConnectionThread(self._backend, setup)
        # 启动期错误必须在这里抛出，而不是留给「第一次提交」。
        #
        # ``_ConnectionThread`` 把建连/setup 的异常存起来交给首个操作，这对 SQLite
        # 是合理的（路径写错时静默降级到「读不到」）。但**版本不匹配不允许这样**：
        # 那会让进程正常起来、健康检查通过、第一个业务任务才炸，现场表现是
        # 「服务 Up、任务全失败」——正是本次切库要避免的那类故障。
        error = self._thread.start_error
        if error is not None:
            self._thread.close()
            self._thread = None
            raise error

    def close(self) -> None:
        if self._thread is not None:
            self._thread.close()
            self._thread = None

    @property
    def connected(self) -> bool:
        return self._thread is not None and self._thread.alive

    @property
    def backend_name(self) -> str:
        return getattr(self._backend, "name", "unknown")

    @property
    def dialect(self) -> Any:
        return _dialect_for(self.backend_name)

    def health(self) -> dict[str, Any]:
        """数据库健康快照。未连接时也要能回答，而不是抛错。"""
        if self._thread is None:
            return {"db_alive": False, "db_last_ok_at_ms": None, "db_error": "未连接"}
        return self._thread.healthy()

    def _require_thread(self) -> _ConnectionThread:
        if self._thread is None or not self._thread.alive:
            raise RuntimeError(
                "Store 尚未 connect()（或已关闭）；留痕读写需要活动连接"
            )
        return self._thread

    # ------------------------------------------------------------ 执行

    async def _run(self, work: Callable[[Any], Any]) -> Any:
        """在专属线程上执行一次数据库操作并等待结果。"""
        future = self._require_thread().submit(work)
        return await asyncio.wrap_future(future)

    @staticmethod
    def _execute(
        sql: str, params: Sequence[Any] = ()
    ) -> Callable[[Any], list[dict[str, Any]]]:
        def work(connection: Any) -> list[dict[str, Any]]:
            cursor = connection.execute(sql, params)
            rows = cursor.fetchall()
            cursor.close()
            return rows

        return work

    @staticmethod
    def _execute_write(
        statements: Sequence[tuple[str, Sequence[Any]]],
    ) -> Callable[[Any], None]:
        """一次提交多条语句，共用一次 commit。"""

        def work(connection: Any) -> None:
            for sql, params in statements:
                connection.execute(sql, params)
            connection.commit()

        return work

    async def _read(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
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

        def work(connection: Any) -> int:
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
            self.dialect.insert_run_sql(),
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

        def work(connection: Any) -> bool:
            cursor = connection.execute(
                "UPDATE workflow_runs "
                "SET status = ?, "
                f"    error = {self.dialect.append_error_sql()}, "
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

        def work(connection: Any) -> dict[str, Any] | None:
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
        def work(connection: Any) -> str:
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
    ) -> Callable[[Any], None]:
        """清空这棵树各行的载荷列，并标上 ``packed``。

        标记写到树里**每一行**而不只是根运行：直接按 run_id 打开某个子运行时，
        读路径同样要知道它的载荷在包里，而不是「列是空的 = 没有输出」。
        """
        def work(connection: Any) -> None:
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

        def work(connection: Any) -> int:
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
        predicate, params = self.dialect.scope_predicate(workflow_scope)

        def work(connection: Any) -> int:
            # 只沿在跑运行的祖先查找主任务，避免恢复时遍历全部历史子流程。
            # 同名共享子工作流必须按其主任务归属恢复。
            #
            # 递归部分用 UNION ALL 而不是 UNION：树里同一 run_id 不会经由两条路径
            # 到达（parent_run_id 单值），DISTINCT 是白付的开销；MySQL 上它还会让
            # 临时表变大，而这里在启动路径上。
            lineage = (
                "WITH RECURSIVE lineage(run_id, parent_run_id, workflow_id) AS ("
                " SELECT run_id, parent_run_id, workflow_id FROM workflow_runs WHERE status = ?"
                " UNION ALL SELECT lineage.run_id, parent.parent_run_id, parent.workflow_id"
                " FROM workflow_runs parent JOIN lineage ON parent.run_id = lineage.parent_run_id"
                ") "
            )
            ids_sql = (
                lineage + "SELECT run_id FROM lineage "
                f"WHERE parent_run_id IS NULL AND {predicate}"
            )
            targets = connection.execute(
                ids_sql, (RunStatus.RUNNING, *params)
            ).fetchall()
            run_ids = [row["run_id"] for row in targets]
            if not run_ids:
                connection.commit()
                return 0
            marks = ",".join("?" for _ in run_ids)
            cursor = connection.execute(
                "UPDATE workflow_runs SET status = ?, error = COALESCE(error, ?) "
                f"WHERE status = ? AND run_id IN ({marks})",
                (RunStatus.INTERRUPTED,
                 "进程中断：运行中任务未完成，已完成结果已保留",
                 RunStatus.RUNNING, *run_ids),
            )
            marked = int(cursor.rowcount)
            cursor.close()
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
        """仅首次写入队列；重复请求绝不把 running/终态改回 queued。

        **用「纯 INSERT + 捕获唯一键冲突」而不是 upsert。** 两种后端的 upsert 在
        「已存在但内容未变」时都不肯老实说明自己有没有插入：

        - SQLite 的 ``ON CONFLICT DO NOTHING`` 还能靠 ``rowcount == 0`` 判断；
        - MySQL 的 ``ON DUPLICATE KEY UPDATE`` 在开了 ``CLIENT_FOUND_ROWS`` 后，
          「命中且未改变」返回 1、「确实插入」也返回 1——两者不可区分。

        而 ``CLIENT_FOUND_ROWS`` 又是 ``abandon_run`` / ``renew_lease`` 必需的（见
        ``dbapi.MySqlBackend.connect``）。与其为一条语句切换标志位，不如让「唯一键
        冲突」以异常的形式出现：那是数据库层面的确定事实，两种后端语义一致。
        """
        now = _now_ms()

        def work(connection: Any) -> bool:
            try:
                cursor = connection.execute(
                    "INSERT INTO workflow_runs (run_id, workflow_id, "
                    "workflow_display_name, business_ref, status, inputs_json, is_replay, "
                    "queued_at_ms, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run.run_id, run.workflow_id, run.workflow_display_name,
                     run.business_ref, RunStatus.QUEUED, json_dumps(run.inputs),
                     int(run.is_replay), now, now),
                )
            except BaseException as exc:
                # 冲突不是错误：调用方会回读既有运行并比对提交内容。
                # 但要让事务回到干净状态——SQLite 与 MySQL 都不会因唯一键冲突整体回滚，
                # 显式 rollback 只是把「这条语句失败」的事实同步给驱动。
                if getattr(self._backend, "is_duplicate_key", None) and \
                        self._backend.is_duplicate_key(exc):
                    connection.rollback()
                    return False
                raise
            cursor.close()
            connection.commit()
            return True

        return await self._run(work)

    async def claim_next_queued_run(
        self, worker_id: str, *, lease_ms: int, workflow_scope: WorkflowScope = "all"
    ) -> dict[str, Any] | None:
        """原子领取一个待跑运行；无待跑或已被别人领走时返回 ``None``。

        **两种后端各用自己最强的原语，但语义必须相同：同一行只被一方领到。**

        - SQLite：单条 ``UPDATE ... WHERE status='queued' RETURNING``。写事务串行，
          两个 worker 同时执行时只有一个能把某行改成 ``running``，另一个 ``RETURNING``
          无行返回。
        - MySQL：事务内 ``SELECT ... FOR UPDATE SKIP LOCKED`` 锁住候选行，再条件 UPDATE。
          不用 RETURNING（MySQL 不支持），也不用「先 SELECT 再 UPDATE」——后者两个 worker
          会读到同一行。``SKIP LOCKED`` 让被别的 worker 锁住的行直接跳过，而不是排队等，
          这正是多 worker 场景要的。

        两条路径的共同点：**改状态的 UPDATE 都带 ``AND status = 'queued'``**，即便出现
        意料之外的竞争也只会有一方 rowcount 为 1。
        """
        now = _now_ms()
        claimer = self.dialect.claim

        def work(connection: Any) -> dict[str, Any] | None:
            if self.dialect.claim_ready():
                sql, params = claimer(connection, now, worker_id, lease_ms, workflow_scope)
                cursor = connection.execute(
                    sql,
                    (
                        RunStatus.RUNNING, now, worker_id, now + int(lease_ms),
                        RunStatus.QUEUED, *params, RunStatus.QUEUED,
                    ),
                )
                row = cursor.fetchone()
                cursor.close()
                connection.commit()
                return dict(row) if row is not None else None

            # ---- MySQL：加锁选中 → 条件更新 → 回读
            sql, params = claimer(connection, now, worker_id, lease_ms, workflow_scope)
            try:
                cursor = connection.execute(sql, (RunStatus.QUEUED, *params))
                candidate = cursor.fetchone()
                cursor.close()
                if candidate is None:
                    connection.commit()
                    return None
                run_id = candidate["run_id"]
                update = connection.execute(
                    "UPDATE workflow_runs "
                    "SET status = ?, claimed_at_ms = ?, claimed_by = ?, "
                    "    lease_expires_at_ms = ? "
                    "WHERE run_id = ? AND status = ?",
                    (RunStatus.RUNNING, now, worker_id, now + int(lease_ms),
                     run_id, RunStatus.QUEUED),
                )
                claimed = update.rowcount > 0
                update.close()
                if not claimed:
                    connection.commit()
                    return None
                detail = connection.execute(
                    "SELECT run_id, workflow_id, business_ref, inputs_json, "
                    "       definition_version_id, is_replay, claimed_by, "
                    "       claimed_at_ms, lease_expires_at_ms "
                    "FROM workflow_runs WHERE run_id = ?",
                    (run_id,),
                )
                row = detail.fetchone()
                detail.close()
                connection.commit()
                return dict(row) if row is not None else None
            except BaseException:
                connection.rollback()
                raise

        return await self._run(work)
        now = _now_ms()
        predicate, params = self.dialect.scope_predicate(workflow_scope)

        def work(connection: Any) -> dict[str, Any] | None:
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

        def work(connection: Any) -> bool:
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

        def work(connection: Any) -> int:
            if self.dialect.name == "mysql":
                # MySQL 报 "This version of MySQL doesn't yet support
                # 'LIMIT & IN/ALL/ANY/SOME subquery'"：``UPDATE ... WHERE run_id IN
                # (SELECT ... LIMIT n)`` 直接不被支持。改成先取候选（可在子查询里
                # LIMIT），再按主键批量更新——两步之间**不需要**加锁：租约已过期，
                # 任何一方把它改回 queued 或别人抢先重领，第二次 UPDATE 的
                # ``status = running`` 条件会让它自然落空。
                picked = connection.execute(
                    "SELECT run_id FROM workflow_runs "
                    "WHERE status = ? AND lease_expires_at_ms IS NOT NULL "
                    "      AND lease_expires_at_ms <= ? "
                    "ORDER BY lease_expires_at_ms LIMIT ?",
                    (RunStatus.RUNNING, now, int(limit)),
                ).fetchall()
                run_ids = [row["run_id"] for row in picked]
                if not run_ids:
                    connection.commit()
                    return 0
                marks = ",".join("?" for _ in run_ids)
                cursor = connection.execute(
                    "UPDATE workflow_runs "
                    "SET status = ?, claimed_at_ms = NULL, claimed_by = NULL, "
                    "    lease_expires_at_ms = NULL "
                    f"WHERE status = ? AND run_id IN ({marks})",
                    (RunStatus.QUEUED, RunStatus.RUNNING, *run_ids),
                )
                changed = int(cursor.rowcount)
                cursor.close()
                connection.commit()
                return changed

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
        predicate, params = self.dialect.scope_predicate(workflow_scope)
        rows = await self._read(
            "SELECT COUNT(*) AS n FROM workflow_runs "
            f"WHERE status = ? AND parent_run_id IS NULL AND {predicate}",
            (RunStatus.QUEUED, *params),
        )
        return int(rows[0]["n"]) if rows else 0

    async def queue_snapshot(self, *, workflow_scope: WorkflowScope = "all") -> dict[str, int]:
        """队列概览：待跑、在跑、中断待处理。供 ``GET /runtime`` 展示。"""
        predicate, params = self.dialect.scope_predicate(workflow_scope)
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
            self.dialect.upsert_node_sql(),
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


def _row_to_dict(row: Any, json_fields: Iterable[str] = ()) -> dict[str, Any]:
    """把一行转成字典，顺便把 JSON 文本字段解析回对象。

    行来自 ``dbapi``，统一是 ``dict``（MySQL 的 ``DictCursor`` 与 SQLite 适配层一致）。
    """
    data = dict(row)
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


def _dialect_for(backend_name: str) -> Any:
    """按后端名取方言实现。未知取值直接报错，不回落。"""
    if backend_name == "sqlite":
        return SqliteDialect()
    if backend_name == "mysql":
        return MySqlDialect()
    raise ValueError(f"未知的持久化后端：{backend_name!r}")


def _now_ms() -> int:
    return int(time.time() * 1000)
