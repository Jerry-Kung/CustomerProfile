"""运行时库初始化入口：``python -m customer_profile.persistence.init_db``。

**为什么放在 ``src/`` 而不是 ``scripts/``。** 镜像只带 ``src/``（见 ``.dockerignore``
的白名单），而初始化必须在目标机上随容器执行——UAT 与生产的内网 MySQL 从本地不可达，
不能「在开发机建好再搬过去」。放进包里，镜像自带，不需要放宽 dockerignore。

``scripts/init_runtime_db.py`` 是同一份逻辑的宿主机入口（支持 ``--emit-sql`` 导出纯 SQL
交给 DBA）。两者共用本模块，不各写一份。

## 三种运行方式

| 方式 | 谁在用 | 行为 |
|---|---|---|
| ``docker compose up`` | 日常部署 | 由 ``runtime-migrate`` 容器自动执行，服务等它成功后才启动 |
| ``--check`` | 巡检 | 只自检，不改库；不满足时以非零码退出 |
| ``--emit-sql`` | 无 DDL 权限的环境 | 生成纯 SQL 交给 DBA 执行 |

## 幂等与自愈

每次执行都会比对**实际结构**与 ``mysql_schema.MYSQL_DDL``，只补缺失的表和列：

- 首次部署：建库、建全部表、登记迁移版本；
- 再次部署：什么都不改，打印「结构完整」；
- 升级带来新列：只 ``ADD COLUMN`` 缺的列，已有数据不动；
- 表被误删：只重建缺的那张。

因此运维不需要记住「这个环境初始化过没有」，也不需要判断升级后该补什么。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_BAD_INPUT = 2
EXIT_SCHEMA_INSATISFIED = 3
EXIT_NOT_MYSQL = 4
"""不是 MySQL 后端（且未声明可以跳过）。**单独的退出码**：部署流程用它区分跳过与失败。"""


def _mask(url: Any) -> str:
    """打日志用的脱敏 URL。非字符串（如 SQLite 的文件路径）原样转字符串。"""
    if not isinstance(url, str):
        return str(url)
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    head, _, tail = rest.rpartition("@")
    return f"{scheme}://{head.split(':', 1)[0]}:***@{tail}"


def _host_and_schema(url: str) -> tuple[str, str]:
    rest = url.partition("://")[2]
    host = rest.rpartition("@")[2].split("/", 1)[0]
    schema = rest.rsplit("/", 1)[-1].split("?", 1)[0]
    return host, schema


def fetch_sql(database: str) -> list[str]:
    """拼出可交给 DBA 的脚本。"""
    from . import mysql_schema

    statements: list[str] = [
        f"-- 运行时库初始化：{database}",
        "-- 由 customer_profile.persistence.init_db 生成。可重复执行。",
        "--",
        "-- 注意：本脚本按「库为空」编写。若库已存在且只缺个别列，",
        "-- 直接执行不会补列（CREATE TABLE IF NOT EXISTS 对已存在的表不生效），",
        "-- 请改用容器：docker compose run --rm runtime-migrate",
        "",
        f"CREATE DATABASE IF NOT EXISTS `{database}` "
        "DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_0900_as_cs;",
        f"USE `{database}`;",
        "",
    ]
    statements.extend(mysql_schema.ddl_statements())
    for version, note in mysql_schema.MIGRATION_NOTES:
        statements.append(
            "INSERT INTO schema_migrations (version, applied_at_ms, note) VALUES "
            f"('{version}', UNIX_TIMESTAMP() * 1000, '{note}') "
            "AS new ON DUPLICATE KEY UPDATE note = new.note;"
        )
    statements.extend(
        [
            "",
            "-- 核对：应看到 6 张表",
            "SHOW TABLES;",
            "SELECT version, FROM_UNIXTIME(applied_at_ms / 1000) AS applied_at "
            "FROM schema_migrations ORDER BY applied_at_ms;",
        ]
    )
    return statements


def ensure_database(connection: Any, database: str) -> str:
    """确保库存在。返回 ``exists`` / ``created``。

    **先查再建**，不是直接 ``CREATE DATABASE IF NOT EXISTS``：生产账号通常只有库内
    权限、没有 ``CREATE DATABASE``，而库是 DBA 预先建好的。无条件执行建库语句会因权限
    不足直接失败——一次本可以成功的初始化，被一句多余的话挡在门外。
    """
    row = connection.execute(
        "SELECT SCHEMA_NAME AS name FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = ?",
        (database,),
    ).fetchone()
    if row is not None:
        return "exists"
    try:
        connection.execute(
            f"CREATE DATABASE IF NOT EXISTS `{database}` "
            "DEFAULT CHARACTER SET utf8mb4 DEFAULT COLLATE utf8mb4_0900_as_cs"
        )
        connection.commit()
    except Exception as exc:
        raise RuntimeError(
            f"库 {database} 不存在，且当前账号无法创建它：{exc}。"
            "请让 DBA 先建库，或给初始化账号 CREATE DATABASE 权限"
        ) from exc
    return "created"


def apply(connection: Any, database: str, *, create_database: bool = True) -> dict[str, Any]:
    """自检并只补缺失项，返回概况。"""
    from . import mysql_schema

    database_state = "skipped"
    if create_database:
        database_state = ensure_database(connection, database)
    # 建库后连接上的默认库可能还指向旧值，显式切换一次。
    connection.execute(f"USE `{database}`")
    report = mysql_schema.ensure_schema(connection)

    versions = connection.execute(
        "SELECT version, applied_at_ms, note FROM schema_migrations ORDER BY applied_at_ms"
    ).fetchall()
    return {
        "database_state": database_state,
        "database": connection.execute("SELECT DATABASE() AS d").fetchone()["d"],
        "created": report.tables_created,
        "columns_added": report.columns_added,
        "relaxed_columns": report.relaxed_columns,
        "versions": [dict(row) for row in versions],
        "satisfied": report.satisfied,
    }


def check(connection: Any) -> dict[str, Any]:
    """只自检，不改库。"""
    from . import mysql_schema

    report = mysql_schema.inspect(connection)
    return {
        "report": report,
        "satisfied": report.satisfied,
        "database": connection.execute("SELECT DATABASE() AS d").fetchone()["d"],
    }


def _init_sqlite(backend: Any, *, check_only: bool) -> int:
    """SQLite 后端：复用 Store 启动时那套幂等建表 + 补列。

    服务进程本身也会给 SQLite 建表（见 ``Store._migrate`` 的缺省规则），这里再写一遍
    是为了让**部署链与运行链走同一条代码路径**：两种后端都由 ``runtime-migrate`` 在
    服务启动前先把结构准备好，失败就停在部署阶段。

    SQLite 没有 ``information_schema``，结构自检就是「建表 + 补列」本身：两条都幂等，
    执行完即代表结构与代码一致。所以这里不做单独的自检分支。
    """
    if check_only:
        print(
            f"SQLite 后端（{_mask(backend.path)}）：结构由初始化脚本维护，"
            "请直接执行初始化（去掉 --check）",
            flush=True,
        )
        return EXIT_OK

    connection = backend.connect()
    try:
        from .store import SCHEMA_STATEMENTS, _apply_migrations

        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        _apply_migrations(connection.raw)
        connection.commit()
        tables = [
            str(dict(row)["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
    finally:
        connection.close()

    print(f"当前库（SQLite）：{backend.path}")
    print(f"表清单（{len(tables)}）：{', '.join(tables)}")
    print("自检结果：结构完整")
    return EXIT_OK


def _print_report(info: dict[str, Any]) -> None:
    print(f"当前库：{info['database']}")
    if info.get("created"):
        print(f"已建表：{', '.join(info['created'])}")
    if info.get("columns_added"):
        print(f"已补列：{', '.join(info['columns_added'])}")
    if info.get("relaxed_columns"):
        print(
            "注意：以下列要求 NOT NULL 且无默认值，已先建成可空——"
            "老行需要业务侧回填后再手工收紧：" + "、".join(info["relaxed_columns"]),
            file=sys.stderr,
        )
    for version in info["versions"]:
        note = version.get("note") if isinstance(version, dict) else ""
        name = version.get("version") if isinstance(version, dict) else version
        print(f"迁移版本：{name}  {note}")
    state = "结构完整" if info["satisfied"] else "结构不完整"
    print(f"自检结果：{state}")


def main(argv: list[str] | None = None) -> int:
    from .dbapi import MySqlBackend, build_backend

    parser = argparse.ArgumentParser(
        description="初始化 / 自检运行时 MySQL 库（幂等；只补缺失项）"
    )
    parser.add_argument("--url", default="", help="DATABASE_URL；缺省读环境变量")
    parser.add_argument(
        "--check", action="store_true", help="只自检不改库；结构不满足时退出码 3"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的语句")
    parser.add_argument("--emit-sql", default="", help="把等价 SQL 写到指定文件后退出")
    parser.add_argument(
        "--skip-create-database",
        action="store_true",
        help="库已由 DBA 建好、账号没有 CREATE DATABASE 权限时使用",
    )
    parser.add_argument(
        "--allow-sqlite",
        action="store_true",
        help="DATABASE_URL 是 SQLite 时视为成功跳过（退出码 0）。部署脚本用这个，"
        "这样同一个 compose 文件在本地 SQLite 环境也能 up 起来",
    )
    args = parser.parse_args(argv)

    url = (args.url or os.environ.get("DATABASE_URL", "")).strip()
    if not url:
        print("缺少 DATABASE_URL：用 --url 传入，或在 .env / 环境变量里配置", file=sys.stderr)
        return EXIT_BAD_INPUT

    backend = build_backend(url)
    if not isinstance(backend, MySqlBackend):
        if not args.allow_sqlite:
            print(
                f"DATABASE_URL={_mask(url)} 不是 MySQL 后端；本入口只用于 MySQL"
                "（SQLite 请加 --allow-sqlite）",
                file=sys.stderr,
            )
            return EXIT_NOT_MYSQL
        # 部署链对两种后端做同一件事：服务启动前把结构准备好。
        return _init_sqlite(backend, check_only=args.check)

    host, schema = _host_and_schema(url)
    print(f"目标实例：{host}  库：{schema}", flush=True)

    if args.emit_sql:
        target = Path(args.emit_sql)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n\n".join(fetch_sql(backend.database)) + "\n", encoding="utf-8")
        print(f"已生成 SQL：{target}")
        return EXIT_OK

    if args.dry_run:
        for statement in fetch_sql(backend.database):
            print(statement.rstrip().rstrip(";") + ";")
            print()
        print("（--dry-run：未连接数据库）")
        return EXIT_OK

    # **先不带库名连**：目标库可能还不存在，带库名建连会直接
    # ``1049 Unknown database``，连「先查再建库」那步都走不到。
    # ``--skip-create-database`` 时库必由 DBA 建好，直接按库名连。
    connection = backend.connect(database=None if args.skip_create_database else "")
    try:
        if args.check:
            database = backend.database
            exists = connection.execute(
                "SELECT SCHEMA_NAME AS name FROM information_schema.SCHEMATA "
                "WHERE SCHEMA_NAME = ?",
                (database,),
            ).fetchone()
            if exists is None:
                print(f"库 {database} 不存在：尚未初始化", file=sys.stderr)
                return EXIT_SCHEMA_INSATISFIED
            connection.execute(f"USE `{database}`")
            info = check(connection)
            print(f"当前库：{info['database']}")
            print(f"自检结果：{info['report'].describe()}")
            if not info["satisfied"]:
                print(
                    "库结构不满足业务需要。执行初始化可自动补齐："
                    "docker compose up -d（或 docker compose run --rm runtime-migrate）",
                    file=sys.stderr,
                )
                return EXIT_SCHEMA_INSATISFIED
            return EXIT_OK

        info = apply(connection, backend.database,
                     create_database=not args.skip_create_database)
    finally:
        connection.close()

    if info["database_state"] == "created":
        print(f"已创建库：{info['database']}")
    _print_report(info)
    if not info["satisfied"]:
        # ``apply`` 内部已校验过；走到这里说明结构与期望仍有差距，不能静默成功——
        # 部署流程靠退出码决定要不要放服务启动。
        return EXIT_SCHEMA_INSATISFIED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
