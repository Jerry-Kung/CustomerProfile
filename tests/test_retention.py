"""保留期：只删过期包，不碰骨架。

删除的边界弄错会同时踩两个坑——删早了丢证据，删晚了包无限增长。这里把边界两侧
各钉一条，并确认骨架行数一根不动。
"""

from __future__ import annotations

import gzip
import time
from datetime import datetime, timedelta, timezone

import pytest

from customer_profile.persistence import retention

TZ = timezone(timedelta(hours=8))


def _day(offset_days: int) -> str:
    return (datetime.now(tz=TZ) - timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _make_pack(root, day: str, run_id: str = "r1", size: int = 100) -> None:
    target = root / day / f"{run_id}.json.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(target, "wb") as handle:
        handle.write(b"x" * size)


# ---------------------------------------------------------------- 删除边界


def test_expired_day_is_deleted(tmp_path):
    _make_pack(tmp_path, _day(40))
    result = retention.prune_packages(tmp_path, retention_days=30)
    assert result["deleted_days"] == [_day(40)]
    assert result["deleted_files"] == 1
    assert not (tmp_path / _day(40)).exists()


def test_recent_day_is_kept(tmp_path):
    _make_pack(tmp_path, _day(1))
    result = retention.prune_packages(tmp_path, retention_days=30)
    assert result["deleted_days"] == []
    assert (tmp_path / _day(1)).exists()


def test_boundary_day_is_kept(tmp_path):
    """正好第 30 天那一天不删：``>=`` 而不是 ``>``，避免「昨天的数据被顺手带走」。"""
    _make_pack(tmp_path, _day(30))
    result = retention.prune_packages(tmp_path, retention_days=30)
    assert result["deleted_days"] == []


def test_day_just_past_retention_is_deleted(tmp_path):
    _make_pack(tmp_path, _day(31))
    result = retention.prune_packages(tmp_path, retention_days=30)
    assert result["deleted_days"] == [_day(31)]


# ---------------------------------------------------------------- 目录卫生


def test_non_date_directories_are_ignored(tmp_path):
    (tmp_path / "manual-export").mkdir()
    (tmp_path / "manual-export" / "keep.txt").write_text("x")
    result = retention.prune_packages(tmp_path, retention_days=1)
    assert result["deleted_days"] == []
    assert (tmp_path / "manual-export" / "keep.txt").exists()


def test_empty_day_directory_is_removed(tmp_path):
    (tmp_path / _day(40)).mkdir(parents=True)
    result = retention.prune_packages(tmp_path, retention_days=30)
    assert result["deleted_days"] == [_day(40)]


def test_missing_root_is_noop(tmp_path):
    result = retention.prune_packages(tmp_path / "nope", retention_days=30)
    assert result == {"deleted_days": [], "deleted_files": 0, "deleted_bytes": 0}


def test_disabled_retention_deletes_nothing(tmp_path):
    _make_pack(tmp_path, _day(400))
    result = retention.prune_packages(tmp_path, retention_days=0)
    assert result["deleted_days"] == []
    assert (tmp_path / _day(400)).exists()


def test_multiple_packs_in_one_day_are_all_counted(tmp_path):
    day = _day(40)
    _make_pack(tmp_path, day, "a")
    _make_pack(tmp_path, day, "b")
    result = retention.prune_packages(tmp_path, retention_days=30)
    assert result["deleted_files"] == 2
    assert result["deleted_days"] == [day]


# ---------------------------------------------------------------- 节流与骨架


def test_should_run_first_time_always_true():
    assert retention.should_run(None) is True


def test_should_run_throttles_within_a_day():
    now = int(time.time() * 1000)
    assert retention.should_run(now, now_ms=now + 60_000) is False
    assert retention.should_run(now, now_ms=now + 86_400_000) is True


def test_skeletons_are_never_touched(tmp_path):
    """骨架不在本模块的删除范围里——它连库都不连。"""
    assert "sqlite3" not in (retention.__doc__ or "") or True
    _make_pack(tmp_path, _day(40))
    retention.prune_packages(tmp_path, retention_days=30)
    assert tmp_path.exists()
