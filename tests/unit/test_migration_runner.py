from __future__ import annotations

from pathlib import Path

import src.infrastructure.db.migration_runner as migration_runner
from src.config.settings import MySQLConfig
from src.contracts.registry import ContractPaths


class _FakeCursor:
    def __init__(self):
        self.executed: list[str] = []
        self.closed = False
        self.with_rows = False

    def execute(self, statement: str):
        self.executed.append(statement)
        return None

    def fetchall(self):
        return []

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self):
        self.cursor_objs: list[_FakeCursor] = []
        self.commits = 0
        self.closed = False

    def cursor(self):
        c = _FakeCursor()
        self.cursor_objs.append(c)
        return c

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _make_paths(tmp_path: Path, migrations_dir: Path) -> ContractPaths:
    return ContractPaths(
        openapi_path=tmp_path / "openapi.yaml",
        output_schema_path=tmp_path / "schema.json",
        migration_sql_path=migrations_dir / "001.sql",
        migrations_dir=migrations_dir,
    )


def test_run_migration_executes_every_sql_file_in_order(monkeypatch, tmp_path: Path):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_create.sql").write_text("CREATE TABLE t1(id INT);", encoding="utf-8")
    (migrations_dir / "002_alter.sql").write_text("ALTER TABLE t1 ADD c INT;", encoding="utf-8")
    paths = _make_paths(tmp_path, migrations_dir)

    fake = _FakeConnection()
    monkeypatch.setattr(migration_runner.mysql.connector, "connect", lambda **_: fake)

    config = MySQLConfig(host="127.0.0.1", port=3306, user="u", password="p", database="d")
    migration_runner.run_migration(config, paths)

    assert fake.closed is True
    assert fake.commits == 2
    executed = [c.executed[0] for c in fake.cursor_objs]
    assert executed[0].startswith("CREATE TABLE")
    assert executed[1].startswith("ALTER TABLE")


def test_split_sql_statements_handles_comments_quotes_and_prepare_pattern():
    sql = """
    -- a leading comment with a stray ; semicolon
    SET @v = (SELECT IF(EXISTS(SELECT 1), 'has ''quoted; semi'' inside', 'fallback'));
    PREPARE s FROM @v;
    EXECUTE s;
    DEALLOCATE PREPARE s;
    """
    stmts = migration_runner._split_sql_statements(sql)
    assert len(stmts) == 4
    assert stmts[0].startswith("SET @v =")
    assert "'has ''quoted; semi'' inside'" in stmts[0]
    assert stmts[1] == "PREPARE s FROM @v"
    assert stmts[2] == "EXECUTE s"
    assert stmts[3] == "DEALLOCATE PREPARE s"


def test_split_sql_statements_drops_empty_trailing_statement():
    assert migration_runner._split_sql_statements("CREATE TABLE x(id INT);  ") == [
        "CREATE TABLE x(id INT)"
    ]
    assert migration_runner._split_sql_statements("   \n-- only a comment\n") == []
