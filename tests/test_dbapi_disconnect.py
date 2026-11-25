"""断连判据测试。

2026-11-25 UAT 的 ``/runs`` 500 有两个根因，其中一个在判据上：
``_mysql_connector.MySQLInterfaceError`` 是 ``mysql.connector.Error`` 的子类，
但 ``errno`` 恒为 ``None``（C 扩展层只带一句文本）。原来只按 errno 判断，于是
「服务端掐了连接」被当成普通业务错误——既不重连、也不清连接，进程从此每次请求
都用同一条死连接，直到重启。

这里把「有 errno 看 errno、没 errno 看文本」这条规则钉死，防止以后又被简化成
`getattr(exc, "errno", None) in {...}`。
"""

from __future__ import annotations

import pytest

from customer_profile.persistence.dbapi import (
    MySqlBackend,
    _looks_like_disconnect,
)


def test_lost_connection_text_is_recognised():
    assert _looks_like_disconnect("Lost connection to MySQL server during query")
    assert _looks_like_disconnect("MySQL Connection not available.")
    assert _looks_like_disconnect("MySQL server has gone away")


def test_ordinary_sql_error_is_not_a_disconnect():
    assert not _looks_like_disconnect(
        "1064 (42000): You have an error in your SQL syntax"
    )
    assert not _looks_like_disconnect("Duplicate entry 'x' for key 'PRIMARY'")


def test_interface_error_without_errno_is_a_disconnect():
    """C 扩展层的 2013 没有 errno，必须靠文本兜底。"""
    errors = pytest.importorskip("mysql.connector.errors")

    exc = errors.MySQLInterfaceError("Lost connection to MySQL server during query")
    assert getattr(exc, "errno", None) is None, "前提：这类异常没有 errno"
    assert MySqlBackend.is_disconnect(exc) is True


def test_errno_wins_over_message_text():
    """errno 有值时它就是唯一依据——文本里出现 connection 也不能误判。"""
    errors = pytest.importorskip("mysql.connector.errors")

    exc = errors.ProgrammingError(
        errno=1064, msg="syntax error near 'connection'"
    )
    assert MySqlBackend.is_disconnect(exc) is False
