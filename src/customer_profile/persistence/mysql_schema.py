"""MySQL 运行时库的表结构与迁移。

与 SQLite 侧 ``schema.SCHEMA_STATEMENTS`` 是**同一份业务模型的两套物理实现**，
不是「另一套 schema」。列的含义、可空性、默认值语义必须逐列对齐；对不上的地方
在这里注明理由，而不是在代码里各写一半。

三条口径：

1. **建表必须幂等**（``CREATE TABLE IF NOT EXISTS``）。UAT 与生产的初始化脚本可能
   被执行多次，也可能由 DBA 手工执行；「第一次跑成功、第二次报错」会让部署流程
   无法重试。
2. **字符集与排序规则写在每个表上**，不依赖库级缺省。UAT 与开发实例的缺省可能不同，
   而排序规则决定 ``run_id`` / ``status`` 的比较是否大小写敏感——那是会静默改变
   查询结果的一类差异。
3. **键列用 ``ascii_bin``**：这些值（run_id、node_id、hash）都是 ASCII 标识符，
   二进制比较与 SQLite 的 ``TEXT`` 语义一致，且索引更小。
"""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

SCHEMA_VERSION = "2026-11-12.1"
"""期望的 schema 版本。服务启动时与 ``schema_migrations`` 的最新记录比对。"""


TABLE_OPTIONS = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs"

"""``as_cs`` 而非实例缺省的 ``ai_ci``。

SQLite 的 TEXT 比较是二进制语义；``ai_ci`` 会让 ``'Running' = 'running'`` 成立，
于是「查一个不存在的状态」会返回别的状态的行。这类漂移在开发期看不出，上线后
表现为偶发的重复领取或漏查。
"""


MYSQL_DDL: tuple[tuple[str, str], ...] = (
    (
        "workflow_runs",
        """
        CREATE TABLE IF NOT EXISTS workflow_runs (
            run_id                VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            workflow_id           VARCHAR(128) NOT NULL,
            workflow_display_name VARCHAR(255) NULL,
            parent_run_id         VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NULL,
            parent_node_id        VARCHAR(96) NULL,
            call_path             VARCHAR(512) NOT NULL DEFAULT '',
            business_ref          VARCHAR(128) NULL,
            status                VARCHAR(32) NOT NULL,
            definition_version_id INT NULL,
            inputs_json           LONGTEXT NULL,
            outputs_json          LONGTEXT NULL,
            error                 TEXT NULL,
            started_at_ms         BIGINT NULL,
            finished_at_ms        BIGINT NULL,
            duration_ms           BIGINT NULL,
            is_replay             TINYINT(1) NOT NULL DEFAULT 0,
            queued_at_ms          BIGINT NULL,
            claimed_at_ms         BIGINT NULL,
            claimed_by            VARCHAR(255) NULL,
            lease_expires_at_ms   BIGINT NULL,
            created_at_ms         BIGINT NOT NULL,
            payload_state         VARCHAR(16) NULL,
            payload_mode          VARCHAR(16) NULL,
            archived_at_ms        BIGINT NULL,
            PRIMARY KEY (run_id),
            KEY idx_runs_workflow (workflow_id),
            KEY idx_runs_parent (parent_run_id),
            KEY idx_runs_business (business_ref),
            KEY idx_runs_status (status),
            KEY idx_runs_queue (status, parent_run_id, created_at_ms)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs
          COMMENT='工作流运行与队列（复用同表，见 ADR 0001）'
        """,
    ),
    (
        "definition_versions",
        """
        CREATE TABLE IF NOT EXISTS definition_versions (
            version_id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            workflow_id          VARCHAR(128) NOT NULL,
            definition_hash      CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            definition_json      LONGTEXT NOT NULL,
            template_hashes_json LONGTEXT NULL,
            code_commit          VARCHAR(64) NULL,
            created_at_ms        BIGINT NOT NULL,
            PRIMARY KEY (version_id),
            UNIQUE KEY uk_definition_versions_hash (workflow_id, definition_hash)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs
          COMMENT='工作流定义快照：历史运行展示当时的图与提示词'
        """,
    ),
    (
        "node_executions",
        """
        CREATE TABLE IF NOT EXISTS node_executions (
            execution_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            run_id         VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            node_id        VARCHAR(96) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            node_title     VARCHAR(255) NULL,
            node_type      VARCHAR(64) NOT NULL,
            call_path      VARCHAR(512) NOT NULL DEFAULT '',
            status         VARCHAR(32) NOT NULL,
            branch         VARCHAR(64) NULL,
            inputs_json    LONGTEXT NULL,
            outputs_json   LONGTEXT NULL,
            error          TEXT NULL,
            queued_at_ms   BIGINT NULL,
            started_at_ms  BIGINT NULL,
            duration_ms    BIGINT NULL,
            sub_run_id     VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NULL,
            attempt_count  INT NOT NULL DEFAULT 0,
            PRIMARY KEY (execution_id),
            UNIQUE KEY uk_node_executions_scope (run_id, node_id, call_path),
            KEY idx_nodes_run (run_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs
          COMMENT='节点执行明细：唯一键含 call_path，迭代各轮靠它区分'
        """,
    ),
    (
        "request_attempts",
        """
        CREATE TABLE IF NOT EXISTS request_attempts (
            attempt_id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            run_id              VARCHAR(48) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            node_id             VARCHAR(96) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            call_path           VARCHAR(512) NOT NULL DEFAULT '',
            attempt_no          INT NOT NULL,
            kind                VARCHAR(16) NOT NULL,
            target              VARCHAR(1024) NULL,
            request_json        LONGTEXT NULL,
            response_text       LONGTEXT NULL,
            status_code         INT NULL,
            duration_ms         BIGINT NULL,
            error               TEXT NULL,
            error_code          VARCHAR(64) NULL,
            usage_json          TEXT NULL,
            provider_request_id VARCHAR(128) NULL,
            replayed            TINYINT(1) NOT NULL DEFAULT 0,
            anomaly             VARCHAR(255) NULL,
            finish_reason       VARCHAR(64) NULL,
            created_at_ms       BIGINT NOT NULL,
            PRIMARY KEY (attempt_id),
            KEY idx_attempts_run (run_id, node_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs
          COMMENT='每次外部请求尝试：请求体、响应原文、usage、耗时、错误'
        """,
    ),
    (
        "profile_baselines",
        """
        CREATE TABLE IF NOT EXISTS profile_baselines (
            phone              VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            compat_key         VARCHAR(255) NOT NULL DEFAULT '',
            source_run_id      VARCHAR(48) NULL,
            updated_at_ms      BIGINT NOT NULL,
            stage              VARCHAR(64) NOT NULL DEFAULT '',
            deal_level         VARCHAR(64) NOT NULL DEFAULT '',
            deal_level_detail  VARCHAR(255) NOT NULL DEFAULT '',
            consumed_keys_json LONGTEXT NULL,
            callback_json      LONGTEXT NULL,
            PRIMARY KEY (phone)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs
          COMMENT='增量画像基线：按手机号一行，保存上次实际发出的回传体'
        """,
    ),
    (
        "schema_migrations",
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            applied_at_ms BIGINT NOT NULL,
            note        VARCHAR(255) NULL,
            PRIMARY KEY (version)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_as_cs
          COMMENT='迁移版本记录：服务启动时比对期望版本'
        """,
    ),
)

"""表名 → DDL。顺序即依赖顺序（``workflow_runs`` 先于引用它的两张子表）。

**不建外键**是有意的，见 ``docs/feature-db-mysql/01-方案.md`` §4.2：留痕是 fail_soft，
子运行的行可能晚于父行到达；外键会把「暂时缺父行」变成硬失败。task-backend 的
既有迁移同样不用外键，风格一致。
"""


MIGRATION_NOTES = (
    (SCHEMA_VERSION, "初始化运行时库：四层留痕四表 + 增量基线 + 迁移记录"),
)

_NON_COLUMN_PREFIXES = (
    "PRIMARY KEY",
    "UNIQUE KEY",
    "KEY ",
    "INDEX ",
    "CONSTRAINT",
    "FULLTEXT",
    "SPATIAL",
)


@functools.lru_cache(maxsize=1)
def expected_columns() -> dict[str, dict[str, str]]:
    """从 ``MYSQL_DDL`` 解析出「表 → 列 → 列定义」。

    自检必须与建表共用**同一份**定义。另写一份期望结构，等于把 DDL 抄两遍——抄错一个
    列名时自检会说「齐全」，而建表语句里那一列根本不存在。这里直接解析 DDL 文本，
    所以「期望结构」永远等于「会执行的建表语句」。

    解析按行进行：每条列定义的结尾是逗号。约束行（主键、索引）跳过。
    """
    expected: dict[str, dict[str, str]] = {}
    for name, sql in MYSQL_DDL:
        body = sql.split("(", 1)[1].rsplit(")", 1)[0]
        columns: dict[str, str] = {}
        for raw in body.splitlines():
            line = raw.strip().rstrip(",")
            if not line or line.upper().startswith(_NON_COLUMN_PREFIXES):
                continue
            column, _, definition = line.partition(" ")
            if not definition.strip():
                continue
            columns[column] = line
        expected[name] = columns
    return expected


def table_ddl(table: str) -> str:
    for name, sql in MYSQL_DDL:
        if name == table:
            return sql.strip()
    raise KeyError(f"MYSQL_DDL 里没有表 {table}")


@dataclass
class SchemaReport:
    """自检结果。``satisfied`` 只看**结果**，不看过程。"""

    missing_tables: list[str] = field(default_factory=list)
    missing_columns: dict[str, list[str]] = field(default_factory=dict)
    tables_created: list[str] = field(default_factory=list)
    columns_added: list[str] = field(default_factory=list)
    """实际补上的列，形如 ``workflow_runs.payload_state``。"""
    relaxed_columns: list[str] = field(default_factory=list)
    """``NOT NULL`` 且无默认值的列只能先建成可空——需要人工回填后再收紧。"""
    versions: list[str] = field(default_factory=list)

    @property
    def satisfies_version(self) -> bool:
        return SCHEMA_VERSION in self.versions

    @property
    def satisfied(self) -> bool:
        return not self.missing_tables and not self.missing_columns and self.satisfies_version

    def describe(self) -> str:
        """一句话说清现状，供部署日志与报错共用。"""
        problems: list[str] = []
        if self.missing_tables:
            problems.append("缺表 " + "、".join(self.missing_tables))
        if self.missing_columns:
            problems.append(
                "缺列 "
                + "、".join(
                    f"{table}.{column}"
                    for table, columns in sorted(self.missing_columns.items())
                    for column in columns
                )
            )
        if not self.satisfies_version:
            problems.append(f"缺迁移版本 {SCHEMA_VERSION}")
        return "结构完整" if not problems else "；".join(problems)


def inspect(connection: Any) -> SchemaReport:
    """比对库中实际结构与期望结构。**只读**，不改库。

    用 ``information_schema`` 而不是 ``SHOW COLUMNS``：一次查询就能拿到全部表，且
    权限要求相同（``information_schema`` 只看得见当前账号有权访问的对象）。
    """
    rows = connection.execute(
        "SELECT table_name AS table_name, column_name AS column_name "
        "FROM information_schema.columns WHERE table_schema = DATABASE()"
    ).fetchall()
    actual: dict[str, set[str]] = {}
    for row in rows:
        data = dict(row)
        actual.setdefault(str(data["table_name"]), set()).add(str(data["column_name"]))

    report = SchemaReport()
    for table, columns in expected_columns().items():
        if table not in actual:
            report.missing_tables.append(table)
            continue
        absent = [column for column in columns if column not in actual[table]]
        if absent:
            report.missing_columns[table] = absent
    try:
        report.versions = current_versions(connection)
    except Exception:
        # schema_migrations 自己在缺表清单里时，这条查询必然失败；那属于「缺表」，
        # 不是独立故障，不该盖掉更准确的信息。
        report.versions = []
    return report


def _is_strict_not_null(definition: str) -> bool:
    """``NOT NULL`` 且没有默认值、也不是自增：直接 ADD 到有数据的表上会被拒。"""
    upper = definition.upper()
    return (
        "NOT NULL" in upper
        and "DEFAULT" not in upper
        and "AUTO_INCREMENT" not in upper
    )


def _relaxed(definition: str) -> str:
    """把 ``NOT NULL`` 换成 ``NULL``，用于无法直接添加的列。"""
    return definition.replace("NOT NULL", "NULL")


def _register_versions(execute: Any, now_ms: int) -> None:
    stamp = now_ms if now_ms is not None else int(time.time() * 1000)
    for version, note in MIGRATION_NOTES:
        # 行别名语法（8.0.19+）。``VALUES(col)`` 在 8.0.20 起被标为过时，会在
        # 错误日志里刷警告；这里与 store 的 upsert 用同一种写法。
        execute(
            "INSERT INTO schema_migrations (version, applied_at_ms, note) "
            "VALUES (?, ?, ?) AS new ON DUPLICATE KEY UPDATE note = new.note",
            (version, stamp, note),
        )


LOCK_NAME = "customer_profile_runtime_schema"
"""建表互斥锁的名字。

**为什么需要它。** 部署时四个运行服务与初始化容器可能同时启动。``CREATE TABLE IF
NOT EXISTS`` 在并发下并不安全：两条语句都先查后建，其中一个会拿到
``Table already exists`` 或 DDL 元数据锁超时，于是「一次正常部署」以随机一个容器
启动失败收场。``GET_LOCK`` 是 MySQL 的服务端互斥，会话结束自动释放，不需要外部
协调进程。
"""


def _acquire_lock(connection: Any, timeout_s: int) -> None:
    row = connection.execute("SELECT GET_LOCK(?, ?) AS got", (LOCK_NAME, int(timeout_s))).fetchone()
    got = int((dict(row) if row is not None else {}).get("got") or 0)
    if got != 1:
        raise RuntimeError(
            f"等待建表锁 {LOCK_NAME} 超时（{timeout_s}s）。"
            "可能有另一个进程正在初始化；稍后重试，或先执行 "
            "docker compose run --rm runtime-migrate"
        )


def _release_lock(connection: Any) -> None:
    try:
        # **必须把返回集读掉**。``SELECT RELEASE_LOCK(...)`` 会留下一个未读结果集，
        # 同一连接上下一条语句就会报 "Unread result found"——锁释放本身成功，
        # 却让后面所有查询失败。
        connection.execute("SELECT RELEASE_LOCK(?) AS released", (LOCK_NAME,)).fetchall()
    except Exception:
        # 连接断开时会话锁已由服务端释放；这里失败不代表状态有问题，且不能
        # 让它盖掉正在向外抛的初始化异常。
        pass


def ensure_schema(
    connection: Any, *, now_ms: int | None = None, lock_timeout_s: int = 60
) -> SchemaReport:
    """**自检 + 只补缺的**。返回本次实际做了什么。

    这是自动部署的核心：运维不需要判断「这个库是不是已经初始化过」，也不需要记得
    升级后要补哪一列。四种状态都能收敛到「结构完整」：

    - 空库：建全部表并登记版本；
    - 已初始化的库：什么都不改（幂等）；
    - 缺表（被误删、或新版本引入了新表）：只建缺的那张；
    - 缺列（升级带来的新列）：只 ``ADD COLUMN`` 缺的那几列。
      已有的数据不动，列的内容为 ``NULL``，由后续业务流程自然回填。

    整个过程在 ``GET_LOCK`` 下进行，多个进程同时启动时串行执行；**拿到锁之后才
    inspect**——排队等待期间别的进程可能已经把表建好了，先查后会对着过期的结论
    重复建表。
    """
    _acquire_lock(connection, lock_timeout_s)
    try:
        report = inspect(connection)
        for table in report.missing_tables:
            connection.execute(table_ddl(table))
            report.tables_created.append(table)
        for table, columns in list(report.missing_columns.items()):
            definitions = expected_columns()[table]
            for column in columns:
                definition = definitions[column]
                try:
                    connection.execute(f"ALTER TABLE `{table}` ADD COLUMN {definition}")
                except Exception:
                    if not _is_strict_not_null(definition):
                        raise
                    # ``NOT NULL`` 且无默认值：老行上填不出合法值。先建可空，
                    # 让部署继续；这一列是否要回填、什么时候收紧，是人的决定，
                    # 不是脚本的。
                    connection.execute(f"ALTER TABLE `{table}` ADD COLUMN {_relaxed(definition)}")
                    report.relaxed_columns.append(f"{table}.{column}")
                report.columns_added.append(f"{table}.{column}")
        connection.commit()

        # 补完之后再登记版本：版本行**声称**结构已就绪，先登记后补表会让一次失败的
        # 初始化留下「版本说没问题、表其实没建」的库。
        _register_versions(
            connection.execute, now_ms if now_ms is not None else int(time.time() * 1000)
        )
        connection.commit()

        final = inspect(connection)
        if not final.satisfied:
            raise RuntimeError("运行时库结构与期望不符，且自动修复未完成：" + final.describe())
    finally:
        _release_lock(connection)
    # 把「做了什么」带回去，同时保留最终状态（用于打印「已完整」这类情形）。
    final.tables_created = report.tables_created
    final.columns_added = report.columns_added
    final.relaxed_columns = report.relaxed_columns
    return final


def ensure_mysql_schema(connection: Any) -> SchemaReport:
    """``Store.connect()`` 在 ``migrate=True`` 时调用。"""
    return ensure_schema(connection)


def current_versions(connection: Any) -> list[str]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY applied_at_ms"
    ).fetchall()
    return [str(dict(row)["version"]) for row in rows]


def assert_expected_version(connection: Any, *, required: Sequence[str] = (SCHEMA_VERSION,)) -> None:
    """校验库里的迁移版本覆盖了代码期望的版本，**并且结构确实在**。

    只比版本行是不够的：版本行只证明「有人执行过初始化脚本」，不证明表还在。手工
    删过表、或初始化中途失败，版本行照样存在——服务会正常启动，直到第一次写才炸。

    缺版本时**拒绝启动**而不是「边跑边补」：生产库的建表权限不在服务账号手里，
    静默补表在权限受限时会退化成「一半表存在、一半不存在」，那种状态比直接起不来
    难查得多。
    """
    report = inspect(connection)
    present = set(report.versions)
    missing = [version for version in required if version not in present]
    if missing or report.missing_tables or report.missing_columns:
        detail = report.describe()
        if missing:
            detail = f"缺少迁移版本 {'、'.join(missing)}（现有：{sorted(present) or '无'}）；" + detail
        raise RuntimeError(
            f"运行时库与期望结构不符：{detail}。"
            "请先执行数据库初始化脚本或 runtime-migrate 容器"
            "（docker compose up 会自动执行）。"
        )
