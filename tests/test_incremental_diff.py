"""记录差分的表驱动测试。

样本形态取自 ``experiments/uat_data/uat-traces-20261102-20261103`` 的真实响应，
脱敏后内联（不读归档目录，测试在干净检出上也能跑）。四条最要紧的口径：

1. 基础渠道换 ``id`` / ``fetched_at`` **不算变化**——310 对相邻运行全被误判，是实测过的坑；
2. 过程资料 ``source_version`` 递增算**改版**，不是「删一条加一条」；
3. 记录消失算移除，``operation=removed`` 的行不算存在；
4. 键集合可往返序列化（基线存的就是它）。
"""

from __future__ import annotations

import json

from customer_profile.incremental.diff import (
    consumed_keys,
    content_digest,
    diff_histories,
    record_key,
)


def _base_row(**overrides) -> dict:
    row = {
        "channel": "OntologyLeadDistribution",
        "id": 1001,
        "source_key": None,
        "source_version": None,
        "created_at": "2026-10-30T10:12:48",
        "raw_payload": {
            "fetched_at": "2026-10-30T10:12:48.088027",
            "data": {"lead_id": "L-1", "store": "武汉店"},
        },
    }
    row.update(overrides)
    return row


def _process_row(source_key: str, version: int, *, channel="OntologyLeadFollowRecord", text="客户来电咨询") -> dict:
    return {
        "channel": channel,
        "id": 2000 + version,
        "source_key": source_key,
        "source_version": version,
        "created_at": f"2026-10-3{version}T10:00:00",
        "raw_payload": {"content": text, "source_version": version},
    }


class TestBaseChannelKey:
    def test_id_and_fetched_at_changes_are_not_changes(self):
        """基础渠道每轮重抓：id 与 fetched_at 全变，内容没变就不算新增。"""
        first = _base_row()
        second = _base_row(
            id=9999,
            created_at="2026-11-01T08:00:00",
            raw_payload={
                "fetched_at": "2026-11-01T08:00:00.123456",
                "data": {"lead_id": "L-1", "store": "武汉店"},
            },
        )
        assert record_key(first) == record_key(second)
        delta = diff_histories([first], [second])
        assert delta["added"] == []
        assert delta["removed"] == []
        assert len(delta["unchanged"]) == 1
        assert expected_tier_of(delta) == "T0"

    def test_content_change_counts_as_changed(self):
        first = _base_row()
        second = _base_row(
            id=9999,
            raw_payload={
                "fetched_at": "2026-11-01T08:00:00.123456",
                "data": {"lead_id": "L-1", "store": "汉口店"},
            },
        )
        assert record_key(first) != record_key(second)
        delta = diff_histories([first], [second])
        assert delta["added"] and delta["removed"]
        assert delta["removed"][0]["channel"] == "OntologyLeadDistribution"

    def test_digest_ignores_nested_volatile_keys(self):
        left = {"channel": "X", "raw_payload": {"a": 1, "inner": {"fetched_at": "t1", "b": 2}}}
        right = {"channel": "X", "raw_payload": {"a": 1, "inner": {"fetched_at": "t2", "b": 2}}}
        assert content_digest(left) == content_digest(right)


class TestProcessChannelKey:
    def test_version_bump_is_changed_not_add_remove(self):
        first = _process_row("abc123", 1)
        second = _process_row("abc123", 2, text="客户来电咨询，预算三十万")
        delta = diff_histories([first], [second])
        assert len(delta["changed"]) == 1
        assert delta["added"] == []
        assert delta["removed"] == []
        assert expected_tier_of(delta) == "T1"

    def test_new_record_is_added(self):
        first = _process_row("abc123", 1)
        second = _process_row("def456", 1, text="第二次跟进")
        delta = diff_histories([first, second], [first, second])
        delta = diff_histories([first], [first, second])
        assert [item["logical_key"] for item in delta["added"]] == ["OntologyLeadFollowRecord|def456"]

    def test_multiple_rows_same_channel_all_kept(self):
        """跟进/订单是天生多条的渠道：三条跟进是三条独立记录。"""
        rows = [_process_row(f"k{i}", 1, text=f"第 {i} 次跟进") for i in range(3)]
        delta = diff_histories([], rows)
        assert len(delta["added"]) == 3


class TestRemoval:
    def test_missing_key_is_removed(self):
        kept = _process_row("abc123", 1)
        gone = _process_row("zzz999", 1)
        delta = diff_histories([kept, gone], [kept])
        assert [item["logical_key"] for item in delta["removed"]] == [
            "OntologyLeadFollowRecord|zzz999"
        ]

    def test_operation_removed_row_is_not_a_record(self):
        marker = _process_row("abc123", 1)
        marker["raw_payload"] = {"operation": "removed", "reason": "上游删除"}
        delta = diff_histories([], [marker])
        assert delta["added"] == []
        assert consumed_keys([marker]) == []


class TestConsumedKeys:
    def test_roundtrip_through_json(self):
        rows = [_base_row(), _process_row("abc123", 2), _process_row("def456", 1)]
        keys = consumed_keys(rows)
        assert keys == sorted(json.loads(json.dumps(keys)))
        assert len(keys) == len(set(keys)) == 3

    def test_no_baseline_means_everything_added(self):
        rows = [_base_row(), _process_row("abc123", 1)]
        delta = diff_histories(None, rows)
        assert delta["no_baseline"] is True
        assert len(delta["added"]) == 2


def expected_tier_of(delta: dict) -> str:
    """与 ``build_incremental_fixtures.expected_tier`` 同口径的最小复刻（不含录音分支）。"""
    touched = delta["added_records"] + delta["changed_records"]
    if not touched and not delta["removed"]:
        return "T0"
    return "T1"
