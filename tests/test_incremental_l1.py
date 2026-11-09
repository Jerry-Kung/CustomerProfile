"""增量 L1 输入与表达跳过（``01`` §6.1 / §6.4）。

两条判据都是「省成本」的，出错不会报错，只会让产物悄悄变样或成本悄悄回去，因此
必须钉住：非增量运行**逐字节不变**、增量运行只装新记录、事实没变时**零 LLM**。
"""

from __future__ import annotations

import json

from customer_profile.incremental import context as ictx
from customer_profile.incremental.diff import logical_key, record_key
from customer_profile.workflows import customer_profile_l3 as l3
from customer_profile.workflows import customer_profile_pipeline as pipe


def _record(channel, content, *, source_key="k1", version=1, source_name=None):
    return {
        "record_id": f"rec_{channel}",
        "channel": channel,
        "source_name": source_name or channel,
        "source_class": "declared",
        "status": "ok",
        "created_at": "2026-11-02T10:00:00",
        "source_key": source_key,
        "source_version": version,
        "content": content,
        "content_kind": "json",
    }


def _records(*rows):
    return json.dumps(list(rows), ensure_ascii=False)


def _ctx(rows, *, base_facts=None, previous_level="", sections=None):
    keys = [record_key(r) for r in rows]
    logical = [logical_key(r) for r in rows]
    return ictx.dumps(
        ictx.build(
            new_keys=keys,
            new_logical=logical,
            base_fields=[],
            base_facts=base_facts or {},
            base_sections=sections or {},
            previous_level=previous_level,
        )
    )


# ---------------------------------------------------------------- L1 输入

def test_non_incremental_input_is_unchanged():
    """空上下文：输出与不带参数调用逐字节相同。"""
    records = _records(_record("销售跟进记录", "客户说想换车"))
    assert pipe.build_l1_input(records)["output"] == pipe.build_l1_input(records, "", "")["output"]
    assert pipe.build_l1_input(records)["incremental"] == "false"


def test_incremental_input_only_carries_new_records():
    new_row = _record("销售跟进记录", "客户这周又来店里看了一次", source_key="new")
    old_row = _record("销售跟进记录", "客户上个月来过一次", source_key="old")
    outcome = pipe.build_l1_input(
        _records(old_row, new_row), incremental_ctx=_ctx([new_row])
    )
    assert outcome["incremental"] == "true"
    assert "客户这周又来店里看了一次" in outcome["output"]
    assert "客户上个月来过一次" not in outcome["output"]


def test_incremental_input_appends_settled_facts_block():
    new_row = _record("销售跟进记录", "客户说预算提到 25 万")
    outcome = pipe.build_l1_input(
        _records(new_row),
        incremental_ctx=_ctx([new_row], base_facts={"budget_range": "20万"}),
    )
    text = outcome["output"]
    assert "上一版已定稿事实" in text
    assert "budget_range：20万" in text
    # 清单必须标注「不得作为取证来源」，否则模型会照抄当断言
    assert "不得作为取证来源" in text


def test_settled_block_sits_before_the_record_body():
    """清单在记录正文之前——记录原文必须仍是模型最后读到的内容。"""
    new_row = _record("销售跟进记录", "客户说预算提到 25 万")
    text = pipe.build_l1_input(
        _records(new_row),
        incremental_ctx=_ctx([new_row], base_facts={"budget_range": "20万"}),
    )["output"]
    assert text.index("上一版已定稿事实") < text.index("客户说预算提到 25 万")


def test_no_settled_block_when_base_facts_empty():
    new_row = _record("销售跟进记录", "客户说预算提到 25 万")
    text = pipe.build_l1_input(_records(new_row), incremental_ctx=_ctx([new_row]))["output"]
    assert "上一版已定稿事实" not in text


# ---------------------------------------------------------------- 表达跳过

def test_express_is_skipped_when_facts_are_identical():
    facts = {"occupation": "教师", "deal_level": "B"}
    outcome = l3.decide_express(
        json.dumps(facts, ensure_ascii=False),
        incremental_ctx=ictx.dumps(
            ictx.build(
                new_keys=["a"],
                new_logical=["a"],
                base_fields=[],
                base_facts=facts,
                base_sections={"profile_summary": {"summary": "关注空间"}},
                facts_sig=__import__(
                    "customer_profile.incremental.base_claims", fromlist=["x"]
                ).base_facts_signature(facts),
            )
        ),
    )
    assert outcome["skip"] == "true"
    assert json.loads(outcome["sections_json"])["profile_summary"] == {"summary": "关注空间"}


def test_express_is_not_skipped_when_facts_changed():
    base = {"occupation": "教师"}
    outcome = l3.decide_express(
        json.dumps({"occupation": "医生"}, ensure_ascii=False),
        incremental_ctx=ictx.dumps(
            ictx.build(
                new_keys=["a"],
                new_logical=["a"],
                base_fields=[],
                base_facts=base,
                base_sections={"profile_summary": {"summary": "旧"}},
                facts_sig=__import__(
                    "customer_profile.incremental.base_claims", fromlist=["x"]
                ).base_facts_signature(base),
            )
        ),
    )
    assert outcome["skip"] == "false"


def test_express_is_not_skipped_without_a_base():
    outcome = l3.decide_express("{}", incremental_ctx="")
    assert outcome["skip"] == "false"


def test_assemble_reuses_previous_sections_when_express_did_not_run():
    """表达层没跑（跳过路径）：装配照样出九节，用的是上一版的文本。"""
    reused = {
        "profile_summary": {"summary": "关注空间"},
        "inferred_tags": ["务实"],
        "usage_scenarios": ["家庭出行"],
        "communication_strategies": ["周末联系"],
        "customer_overview": {"summary": "家庭用户"},
        "purchase_motivations": ["接送孩子"],
        "product_preferences": ["大空间"],
        "resistances": ["预算紧"],
        "emotion_state": {"summary": "观望"},
    }
    outcome = l3.assemble_l3(
        "13900000001",
        "b-1",
        "{}",
        "[]",
        "",
        reused_sections_json=json.dumps(reused, ensure_ascii=False),
    )
    result = json.loads(outcome["result1"])
    assert result["analysis_result"]["product_preferences"] == ["大空间"]
    assert outcome["section_count"] == 9
