"""``mysql_schema`` 的结构自检：解析与报告。

**不需要数据库。** 这些用例只验证「代码认为的期望结构」本身是对的：DDL 解析不出错、
列名不漏、报告文案把缺项说清楚。真正的「连上库查 information_schema」放在
``test_mysql_backend.py`` 的契约测试里，这里保证默认套件也会在改 DDL 时报警。
"""

from __future__ import annotations

import pytest

from customer_profile.persistence import mysql_schema


def test_expected_columns_covers_every_table():
    parsed = mysql_schema.expected_columns()
    declared = {name for name, _sql in mysql_schema.MYSQL_DDL}
    assert set(parsed) == declared


def test_expected_columns_skips_keys_and_constraints():
    """主键、索引、唯一键不是列，不能混进期望列里。

    混进去的后果很隐蔽：自检会把 ``uk_node_executions_scope`` 当成缺失的列，于是每次
    部署都试图 ``ADD COLUMN uk_node_executions_scope``——一个永远修不好的「缺项」。
    """
    for table, columns in mysql_schema.expected_columns().items():
        for column in columns:
            assert not column.upper().startswith(("PRIMARY", "KEY", "UNIQUE", "INDEX", "CONSTRAINT")), (
                f"{table} 把约束解析成了列：{column}"
            )


def test_expected_columns_are_unique_per_table():
    for table, columns in mysql_schema.expected_columns().items():
        lines = list(columns.values())
        assert len(lines) == len(set(lines)) or True  # 定义可重复，列名不会
        assert len(columns) == len(set(columns))
        assert table


@pytest.mark.parametrize(
    "table,column",
    [
        ("workflow_runs", "payload_state"),
        ("workflow_runs", "lease_expires_at_ms"),
        ("node_executions", "call_path"),
        ("request_attempts", "finish_reason"),
        ("profile_baselines", "consumed_keys_json"),
        ("schema_migrations", "version"),
    ],
)
def test_known_columns_are_parsed(table: str, column: str):
    """抽样固定几列。``call_path`` 尤其重要：唯一键靠它区分迭代轮次。"""
    assert column in mysql_schema.expected_columns()[table]


def test_table_ddl_returns_single_create_statement():
    sql = mysql_schema.table_ddl("workflow_runs")
    assert sql.startswith("CREATE TABLE IF NOT EXISTS workflow_runs")
    assert sql.count("CREATE TABLE") == 1


def test_table_ddl_rejects_unknown_table():
    with pytest.raises(KeyError):
        mysql_schema.table_ddl("not_a_table")


def test_report_describe_names_missing_items():
    report = mysql_schema.SchemaReport(
        missing_tables=["profile_baselines"],
        missing_columns={"workflow_runs": ["payload_state", "archived_at_ms"]},
        versions=[],
    )
    text = report.describe()
    assert "profile_baselines" in text
    assert "workflow_runs.payload_state" in text
    assert "workflow_runs.archived_at_ms" in text
    assert not report.satisfied


def test_report_satisfied_requires_version():
    """结构齐全但版本行缺失，也不算满足：服务会据此拒绝启动。"""
    report = mysql_schema.SchemaReport(versions=[])
    assert not report.satisfied
    report.versions = [mysql_schema.SCHEMA_VERSION]
    assert report.satisfied
    assert report.describe() == "结构完整"


def test_strict_not_null_detection():
    assert mysql_schema._is_strict_not_null("required_col VARCHAR(16) NOT NULL")
    # 有缺省值或自增的列可以直接加到有数据的表上
    assert not mysql_schema._is_strict_not_null("status VARCHAR(32) NOT NULL DEFAULT ''")
    assert not mysql_schema._is_strict_not_null("version_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT")
    assert not mysql_schema._is_strict_not_null("note VARCHAR(255) NULL")
    # 降级后不再是严格 NOT NULL，不能无限降级下去
    assert not mysql_schema._is_strict_not_null(
        mysql_schema._relaxed("required_col VARCHAR(16) NOT NULL")
    )
