"""回传体基线的存储测试。

四件事要钉住：单行覆盖写、``consumed_keys`` 序列化往返、结构异常时**当作没有基线**
（宁可多跑一次全量，也不要拿半份底座往下叠）、以及键集合只增不减的合并口径。
"""

from __future__ import annotations

import json

import pytest

from customer_profile.persistence.profile_baselines import (
    BaselineRepository,
    ProfileBaseline,
    merge_keys,
    summarize,
)


def _callback(batch_id: str = "b-1") -> dict:
    return {
        "phone": "13900000001",
        "batch_id": batch_id,
        "analysis_result": {
            "profile_summary": {"name": "张三"},
            "basic_notes_updates": [
                {
                    "attribute_key": "customer_name",
                    "attribute_value": "张三",
                    "reasoning_detail": {"summary": "登记信息", "steps": [], "evidence": [], "confidence": 0.9},
                }
            ],
        },
    }


def _baseline(phone: str = "13900000001", **overrides) -> ProfileBaseline:
    values = {
        "phone": phone,
        "compat_key": "abc123",
        "source_run_id": "run-1",
        "updated_at_ms": 1_700_000_000_000,
        "stage": "lead",
        "deal_level": "B",
        "deal_level_detail": '{"summary": "预算明确"}',
        "consumed_keys": ("OntologyLeadFollowRecord|k1|v1", "OntologyOrder|k2|v3"),
        "callback": _callback(),
    }
    values.update(overrides)
    return ProfileBaseline(**values)


@pytest.fixture
async def repo(tmp_path):
    from customer_profile.persistence.store import Store

    store = Store(tmp_path / "baseline.db")
    store.connect()
    try:
        yield BaselineRepository(store)
    finally:
        store.close()


async def test_roundtrip_every_field(repo):
    await repo.save(_baseline())
    loaded = await repo.load("13900000001")
    assert loaded is not None
    assert loaded.compat_key == "abc123"
    assert loaded.stage == "lead"
    assert loaded.deal_level == "B"
    # 落库时按键排序，读回即有序；这里断言集合相同而不锁定顺序。
    assert set(loaded.consumed_keys) == {
        "OntologyOrder|k2|v3",
        "OntologyLeadFollowRecord|k1|v1",
    }
    assert loaded.callback["analysis_result"]["profile_summary"]["name"] == "张三"


async def test_save_is_single_row_overwrite(repo):
    await repo.save(_baseline(deal_level="C", consumed_keys=("a",)))
    await repo.save(_baseline(deal_level="A", consumed_keys=("a", "b")))
    assert await repo.count() == 1
    loaded = await repo.load("13900000001")
    assert loaded is not None
    assert loaded.deal_level == "A"
    assert len(loaded.consumed_keys) == 2


async def test_missing_phone_returns_none(repo):
    assert await repo.load("13900000099") is None


async def test_structurally_broken_baseline_is_treated_as_absent(repo):
    """回传体里没有 ``analysis_result`` 的基线不可用——不能把半份底座交给下游。"""
    await repo.save(_baseline(callback={"phone": "13900000001", "batch_id": "b"}))
    assert await repo.load("13900000001") is None


async def test_broken_json_column_is_treated_as_absent(repo):
    await repo.save(_baseline())
    await repo.store._write(
        "UPDATE profile_baselines SET consumed_keys_json = ? WHERE phone = ?",
        ("{not-json", "13900000001"),
    )
    loaded = await repo.load("13900000001")
    # 键坏了只是丢键集合（下次按无键差分，会多判新增），回传体仍可用，因此基线仍返回。
    assert loaded is not None
    assert loaded.consumed_keys == ()


async def test_delete(repo):
    await repo.save(_baseline())
    await repo.delete("13900000001")
    assert await repo.load("13900000001") is None


def test_merge_keys_never_shrinks():
    """键集合只增不减：上一轮消费过、本轮接口没返回的记录，删掉会让它下轮重新算「新增」。"""
    assert merge_keys(("a", "b"), ("b", "c")) == ("a", "b", "c")


def test_summarize_has_no_callback_body():
    summary = summarize(_baseline())
    assert summary["found"] is True
    assert summary["consumed_keys"] == 2
    assert "callback" not in summary
    assert json.dumps(summary, ensure_ascii=False).find("张三") < 0
