"""``用户人工反馈数据提取`` 的 code 节点：读回体换形状后仍要挑得出赞同/反对。

``split_feedback_notes`` 原来直接读字典 ``basic_notes``；2026-11-17 接口改版后容器换成
与回传体同形的列表 ``basic_notes_updates``，``reasoning_summary`` 也改名成
``reasoning_detail.summary``。旧实现在改版后 455/455 次产出「无反馈」。

这里钉的是**读取来源**，不是工作流编排：给定一份响应体，两组字段要按 ``ai_feedback``
分对，且新旧两种形状都给同样的结论。
"""

from __future__ import annotations

import json
from pathlib import Path

from customer_profile.workflows.user_feedback_data import split_feedback_notes

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "replay" / "human_corrected_info.json"


def _fixture_text(phone: str) -> str:
    """取固定响应里某个号码的响应原文（与 ``test_human_corrected_info`` 共用同一份夹具）。"""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["http"][f"GET /api/v1/remote/data/profile/{phone}"][0]["text"]


def _split(payload) -> tuple[list[str], list[str]]:
    out = split_feedback_notes(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    )
    approved = json.loads(out["approved_feedback_json"])["approved_notes"]
    rejected = json.loads(out["rejected_feedback_json"])["rejected_notes"]
    return (
        [next(iter(entry)) for entry in approved],
        [next(iter(entry)) for entry in rejected],
    )


def test_new_list_container_is_split_by_feedback():
    approved, rejected = _split(_fixture_text("13800000005"))
    # fixture 里 ``title`` 的 ``ai_feedback`` 是 1（赞同），``customer_name`` 是 null。
    assert approved == ["title"]
    assert rejected == []


def test_new_structure_keeps_the_summary_as_reasoning_summary():
    """``reasoning_summary`` 这个输出键名保持不变，取值改从 ``reasoning_detail.summary`` 来。"""
    body = {
        "basic_notes_updates": [
            {
                "attribute_key": "age",
                "attribute_value": "35",
                "reasoning_detail": {"summary": "原始数据：预约年龄", "steps": [], "evidence": []},
                "is_locked": 0,
                "ai_feedback": 1,
            }
        ]
    }
    out = split_feedback_notes(json.dumps(body, ensure_ascii=False))
    approved = json.loads(out["approved_feedback_json"])["approved_notes"]
    assert approved == [{"age": {"value": "35", "reasoning_summary": "原始数据：预约年龄"}}]


def test_null_entries_are_ignored():
    body = {
        "basic_notes_updates": [
            {"attribute_key": "hobbies", "attribute_value": None, "reasoning_detail": None,
             "is_locked": 0, "ai_feedback": None}
        ]
    }
    approved, rejected = _split(body)
    assert approved == [] and rejected == []


def test_old_dict_container_still_works():
    """未被改写的旧响应（归档批次）仍要读到，否则回放旧批次会静默变成「无反馈」。"""
    body = {
        "basic_notes": {
            "budget_range": {"value": "30万", "reasoning_summary": "客户自述", "ai_feedback": 0},
            "age": {"value": "35", "reasoning_summary": "原始数据", "ai_feedback": 1},
        }
    }
    approved, rejected = _split(body)
    assert approved == ["age"]
    assert rejected == ["budget_range"]
