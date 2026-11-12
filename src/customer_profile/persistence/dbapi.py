"""数据库后端：把「一个连接」的差异收敛到这一层。

项目有两个后端，取舍见 ``docs/feature-db-mysql/01-方案.md``：

============  ==============================================  ==================================
后端          用途                                            连接对象
============  ==============================================  ==================================
SQLite        运行时（缺省）、批次库、快照、本地复现            ``sqlite3.Connection``
MySQL         生产/UAT 运行时（多进程、多容器、可横向扩展）     ``mysql.connector`` 连接
============  ==============================================  ==================================

**不变的是上层。** ``Store`` 的每个方法都只认 ``execute(sql, params)`` 这一种形状，
本模块负责把各自驱动的游标、行对象、参数占位符、事务开始语句翻译成同一套。
这样 ``store.py`` 里的 SQL 只表达业务意图，不表达方言。

两条实现口径，都是刻意的：

1. **行统一成 ``dict``。** ``sqlite3.Row`` 支持 ``row["列名"]`` 但不支持 ``dict(row)``
   的语义（它按位置迭代），MySQL 的 ``DictCursor`` 直接给 ``dict``。统一成 ``dict``
   后，``_row_to_dict`` 之类的地方不用再判类型。
2. **参数占位符统一写 ``?``。** 代码里出现的是 ``?``，转换只发生在 :func:`convert_placeholders`。
   反向做（代码写 ``%s``、SQLite 侧转换）会在 ``LIKE '%…%'`` 这类字面量上出事，
   而本仓库的 SQL 字面量里没有 ``?``。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Protocol, Sequence

from loguru import logger


MYSQL_DUPLICATE_ENTRY = 1062
"""MySQL 唯一键冲突的 errno。``enqueue_run_once`` 用它区分「已存在」与真正的故障。"""

_MYSQL_DISCONNECT_ERRNOS = frozenset(
    {
        2006,  # server has gone away
        2013,  # lost connection during query
        2055,  # lost connection at 'reading authorization packet'
        4031,  # server shutdown / idle timeout (8.0.24+)
    }
)
"""断连类错误。只有这些值得重连重试；语法错误、约束冲突重试一万次也一样。"""


def convert_placeholders(sql: str, target: str) -> str:
    """把 ``?`` 换成目标驱动的占位符，**跳过引号内的内容**。

    不是 ``sql.replace("?", "%s")``：后者会改写字符串字面量里的 ``?``。
    当前 SQL 里没有这种字面量，但这类转换一旦被误用，症状是「某条查询在特定数据下
    才报错」，比现在多写十几行扫描器贵得多。

    只处理 ``'…'`` 与 ``"…"`` 两种引号，以及 SQL 标准的 ``''`` 转义。
    """
    if "?" not in sql:
        return sql
    out: list[str] = []
    quote: str | None = None
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if quote is not None:
            out.append(char)
            if char == quote:
                # '' 是转义后的单引号，不是字符串结束
                if index + 1 < length and sql[index + 1] == quote:
                    out.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            out.append(target)
        else:
            out.append(char)
        index += 1
    return "".join(out)


class DbCursor(Protocol):
    """游标的最小公共面。"""

    rowcount: int
    lastrowid: int | None

    def fetchall(self) -> list[dict[str, Any]]: ...

    def fetchone(self) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


class DbConnection(Protocol):
    """连接的最小公共面。事务由调用方显式 ``commit`` / ``rollback``。"""

    def execute(self, sql: str, params: Sequence[Any] = ()) -> DbCursor: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------- SQLite


class _SqliteCursor:
    __slots__ = ("_cursor",)

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        """支持 ``for row in connection.execute(...)``。

        上层有若干处直接迭代游标（例如 SQLite 侧的 ``PRAGMA table_info``），
        保留这个能力比逐个改成 ``fetchall()`` 便宜，也不会漏掉新写的调用点。
        """
        for row in self._cursor:
            yield dict(row)

    def fetchone(self) -> dict[str, Any] | None:
        row = self._cursor.fetchone()
        return dict(row) if row is not None else None

    def close(self) -> None:
        self._cursor.close()


class SqliteConnection:
    """``sqlite3.Connection`` 的适配。SQL 与参数原样透传。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _SqliteCursor:
        cursor = self._connection.execute(sql, tuple(params))
        cursor.row_factory = sqlite3.Row
        return _SqliteCursor(cursor)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def set_trace_callback(self, callback: Any) -> None:
        """透传给底层连接。SQLite 独占；测试用它观察「一次快照读到哪些语句」。"""
        self._connection.set_trace_callback(callback)

    def executemany(self, sql: str, seq: Any) -> Any:
        """批量执行。测试与脚本灌数据用；业务写路径不走这里。"""
        return self._connection.executemany(sql, seq)

    @property
    def raw(self) -> sqlite3.Connection:
        return self._connection


# ---------------------------------------------------------------- MySQL


class _MySqlCursor:
    __slots__ = ("_cursor",)

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._cursor.fetchall())

    def fetchone(self) -> dict[str, Any] | None:
        row = self._cursor.fetchone()
        return dict(row) if row is not None else None

    def close(self) -> None:
        self._cursor.close()


class MySqlConnection:
    """``mysql.connector`` 连接的适配。

    使用 ``DictCursor`` 让行直接是 ``dict``；``?`` 占位符在这里换成 ``%s``。
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _MySqlCursor:
        cursor = self._connection.cursor(dictionary=True)
        try:
            cursor.execute(convert_placeholders(sql, "%s"), tuple(params))
        except BaseException:
            cursor.close()
            raise
        return _MySqlCursor(cursor)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        try:
            self._connection.close()
        except Exception:  # 已经断了的连接，关闭失败无需上抛
            pass


# ---------------------------------------------------------------- 后端


class SqliteBackend:
    """本地文件后端。连接失败即启动失败，没有「稍后重连」这回事。"""

    name = "sqlite"

    def __init__(self, path: Any, *, busy_timeout_ms: int = 5000) -> None:
        from pathlib import Path

        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self) -> SqliteConnection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        # 增量回收空闲页。载荷在收尾时被清空（存储瘦身），回收要跟着走，
        # 否则删掉的行只会进 freelist、文件永不缩小。**必须在建表之前设置**。
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        # WAL 让读写互不阻塞；foreign_keys 默认关闭，需显式打开
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        # API 与 worker 是两个进程、各持一个连接，写-写相遇时若没有 busy_timeout，
        # SQLite 会立刻抛 "database is locked" 而不是等一会儿。
        connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        connection.execute("PRAGMA synchronous=NORMAL")
        return SqliteConnection(connection)

    @staticmethod
    def is_disconnect(exc: BaseException) -> bool:
        """本地文件不会「断连」。锁冲突与磁盘错误都由上层如实上报，不重试。"""
        return False

    @staticmethod
    def is_duplicate_key(exc: BaseException) -> bool:
        if isinstance(exc, sqlite3.IntegrityError):
            return "unique constraint" in str(exc).lower()
        return False


class MySqlBackend:
    """远程实例后端。

    **一次只有一个连接。** 上层是「独占线程 + 命令队列」的串行模型（见
    ``store._ConnectionThread``），并发连接在串行调用下没有收益，只会多出池耗尽的
    一种故障形态。要并发时改的是工作线程数，不是这里的连接数。
    """

    name = "mysql"

    def __init__(
        self,
        *,
        host: str,
        port: int = 3306,
        user: str,
        password: str,
        database: str,
        charset: str = "utf8mb4",
        connect_timeout_s: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.charset = charset
        self.connect_timeout_s = connect_timeout_s

    # ------------------------------------------------------------ 描述

    def describe(self) -> str:
        """给人看的位置描述。**不含密码。**"""
        return f"mysql://{self.user}@{self.host}:{self.port}/{self.database}"

    # ------------------------------------------------------------ 连接

    def connect(self, *, database: str | None = None) -> MySqlConnection:
        """建立连接。

        ``database`` 缺省用 DSN 里的库。**传空串表示不选库**——初始化流程要用它：
        目标库还不存在时，带库名建连会直接 1049 ``Unknown database``，连
        「先查再建库」那一步都走不到。不选库时账号仍能读 ``information_schema``，
        建库之后 ``USE`` 切过去即可。
        """
        import mysql.connector
        from mysql.connector.constants import ClientFlag

        target = self.database if database is None else database
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "charset": self.charset,
            "connection_timeout": int(self.connect_timeout_s),
            "autocommit": False,
            # FOUND_ROWS：让 rowcount 返回「匹配行数」而不是「实际改变行数」。
            # 缺省语义下，「值本来就等于目标值」的 UPDATE 返回 0，``abandon_run`` /
            # ``renew_lease`` 会把这种情况误判成「没改到 / 续租失败」，而 SQLite 的
            # changes() 一直是匹配行数——不打开就是一次静默的语义漂移。
            "client_flags": [ClientFlag.FOUND_ROWS],
        }
        if target:
            kwargs["database"] = target
        connection = mysql.connector.connect(**kwargs)
        return MySqlConnection(connection)

    @staticmethod
    def is_disconnect(exc: BaseException) -> bool:
        try:
            from mysql.connector import Error as MySqlError

            if isinstance(exc, MySqlError):
                return getattr(exc, "errno", None) in _MYSQL_DISCONNECT_ERRNOS
            if isinstance(exc, (ConnectionError, OSError)):
                return True
        except ImportError:  # 驱动不可用时不假装能判断
            return False
        return False

    @staticmethod
    def is_duplicate_key(exc: BaseException) -> bool:
        try:
            from mysql.connector import Error as MySqlError

            return isinstance(exc, MySqlError) and getattr(exc, "errno", None) == MYSQL_DUPLICATE_ENTRY
        except ImportError:
            return False


# ---------------------------------------------------------------- 构造


def build_backend(
    database_url: str,
    *,
    sqlite_busy_timeout_ms: int = 5000,
    connect_timeout_s: float = 10.0,
) -> Any:
    """按 ``DATABASE_URL`` 的 scheme 选后端。

    支持两种写法：

    - ``sqlite:///<路径>``（相对路径按仓库根解析）
    - ``mysql://<user>:<password>@<host>:<port>/<schema>?charset=utf8mb4``

    **未知 scheme 直接报错，不回落缺省。** 「以为连了 MySQL 其实写进了本地文件」
    是本项目最难查的一类问题（静默写到别处，两边都有数据）。
    """
    from pathlib import Path
    from urllib.parse import unquote, urlsplit

    raw = (database_url or "").strip()
    if raw.startswith("sqlite:///"):
        from pathlib import Path

        # 相对路径按仓库根解析（与 Settings.sqlite_path 同口径），不按进程工作目录。
        project_root = Path(__file__).resolve().parents[3]
        target = Path(raw[len("sqlite:///") :])
        if not target.is_absolute():
            target = project_root / target
        return SqliteBackend(target, busy_timeout_ms=sqlite_busy_timeout_ms)

    if raw.startswith("mysql://") or raw.startswith("mysql+"):
        parts = _split_mysql_url(raw)
        if not parts.hostname or not parts.path.lstrip("/"):
            raise ValueError(
                f"DATABASE_URL 缺少主机或库名：{_mask(raw)!r}。"
                "形如 mysql://user:password@host:3306/schema"
            )
        query = dict(
            item.split("=", 1) for item in parts.query.split("&") if "=" in item
        )
        return MySqlBackend(
            host=parts.hostname,
            port=parts.port or 3306,
            user=unquote(parts.username or ""),
            password=unquote(parts.password or ""),
            database=unquote(parts.path.lstrip("/")),
            charset=query.get("charset", "utf8mb4"),
            connect_timeout_s=connect_timeout_s,
        )

    raise ValueError(
        f"不支持的 DATABASE_URL：{_mask(raw)!r}；"
        "只接受 sqlite:/// 或 mysql:// 前缀"
    )


def _split_mysql_url(raw: str) -> Any:
    """解析 MySQL DSN，容忍密码里的 ``@``、``#``、``/``。

    标准做法是密码做百分号编码，但运维现场几乎不会这么做——而 ``urlsplit`` 遇到
    未编码的 ``@`` 会把密码截断、把后半段当成主机名，报出来的却是「缺少主机」，
    照着实测值排查会走很远弯路。

    这里按**最后一个** ``@`` 切分凭据与主机：用户/密码里不会包含 ``@`` 之后的主机段，
    而主机段里的 ``@`` 更不可能出现。

    ``#`` 也不能直接交给 ``urlsplit``（会被当成 fragment 起点并被丢弃），
    所以先按 ``?`` 拆查询串、再按 ``/`` 拆库名，最后才落到凭据。
    """
    from urllib.parse import unquote, urlsplit

    scheme, _, rest = raw.partition("://")
    head, _, query = rest.partition("?")
    credentials, _, hostpart = head.rpartition("@")
    if not credentials:  # 没有凭据段
        hostpart = head
    normalized = f"{scheme}://{hostpart}" + (f"?{query}" if query else "")
    parts = urlsplit(normalized)

    class _Parts:
        hostname = parts.hostname
        port = parts.port
        path = parts.path
        query = parts.query

    if credentials:
        user, sep, password = credentials.partition(":")
        _Parts.username = user if sep else credentials
        _Parts.password = password if sep else ""
    else:
        _Parts.username = ""
        _Parts.password = ""
    return _Parts


def _mask(url: str) -> str:
    """把 URL 里的密码抹掉再进错误信息。"""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    credentials, _, tail = rest.partition("@")
    user, sep, _password = credentials.partition(":")
    return f"{scheme}://{user}{sep}***@{tail}" if sep else f"{scheme}://{credentials}@{tail}"


def log_backend(backend: Any) -> None:
    """启动时把「连到哪」写一行日志。不含凭据。"""
    if isinstance(backend, MySqlBackend):
        logger.info("持久化后端：MySQL（{}）", backend.describe())
    else:
        logger.info("持久化后端：SQLite（{}）", backend.path)
