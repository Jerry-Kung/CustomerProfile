"""``ENUM-02``：枚举语义映射的验收用例。

钉住五件事：

1. **关闭即原样**：开关关掉时节点在发起调用前返回，产物逐字节透传（省钱是本开关的意义）。
2. **本地能定的不送检**：合法键、展示名、旧键别名、跨字段路由、占位串、废弃键形态，
   一律不进提示词（``能写代码的不交给模型``）。
3. **契约缺口不猜**：``frequent_social_media`` 的「微信」不送检——送检必然得到 ``weibo``。
4. **模型输出只是建议**：不在合法集内的键、没送检过的取值、不在清单里的字段，全部丢弃。
5. **落地同时改 facts 与 notes，并清掉已被解决的候选**：只改一处的话，L2 兜底会把原值
   按候选集写回去。
"""

from __future__ import annotations

import json

import pytest

from customer_profile import enum_mapping as em
from customer_profile.workflows import customer_profile_pipeline as pipe


def note(key, value):
    return {
        "attribute_key": key,
        "attribute_value": value,
        "reasoning_detail": {"summary": "", "steps": [], "evidence": [], "confidence": 0.5},
    }


class FakeLLM:
    """记录提示词、返回固定裁决的假客户端（不出网）。"""

    def __init__(self, verdict):
        self.verdict = verdict
        self.prompts: list[str] = []

    async def llm_call(self, prompt, system=None, **kwargs):
        self.prompts.append(prompt)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


def mapped(field, value, key):
    return {"mappings": [{"field": field, "value": value, "key": key}]}


@pytest.fixture(autouse=True)
def _fresh_cache():
    em.reset_cache()
    yield
    em.reset_cache()


# ---------------------------------------------------------------- 关闭即原样

async def test_disabled_switch_passes_everything_through():
    """反例：开关关闭时**不发起调用**，产物与输入逐字节一致。"""
    llm = FakeLLM(mapped("婚姻状况", "已婚有子女", "has_children"))
    out = await pipe.enum_map(
        facts_json=json.dumps({"family_structure": "已婚有子女"}, ensure_ascii=False),
        notes_updates_json=json.dumps([note("family_structure", "已婚有子女")], ensure_ascii=False),
        llm=llm,
        enabled=False,
    )
    assert out["enum_map_status"] == "disabled"
    assert out["enum_map_calls"] == 0
    assert llm.prompts == []
    assert json.loads(out["facts_json"])["family_structure"] == "已婚有子女"


# ---------------------------------------------------------------- 送检前的本地过滤

def test_local_deterministic_values_are_not_collected():
    """合法键 / 展示名 / 旧键别名都能本地定，不进提示词。"""
    groups = em.collect(
        {
            "family_structure": "已婚无孩",       # 展示名
            "payment_method": "loan",            # 合法键
            "purchase_purpose": "commute",        # 旧键别名 → family
        },
        [],
    )
    assert groups == []


def test_placeholder_and_key_shaped_values_are_not_collected():
    """占位长句与废弃键形态各有归属，不该送语义映射。"""
    groups = em.collect(
        {
            "family_structure": "暂无明确家庭结构信息",
            "purchase_type": "replacement",
        },
        [],
    )
    assert groups == []


def test_cross_field_route_is_not_sent():
    """``首购`` 在 ``purchase_nature`` 有对应键，属于换字段，不属于映射。

    反例是「贷款」：``purchase_nature`` 里没有它，投影层搬不走，才该进映射清单
    （由模型判它到底有没有可落点）。
    """
    assert em.collect({"purchase_type": "首购"}, []) == []
    assert [g["attribute"] for g in em.collect({"purchase_type": "贷款"}, [])] == [
        "purchase_type"
    ]


def test_contract_gap_value_is_never_sent():
    """契约缺口不送检：送检必然换来 ``weibo``，拒绝的成本由代码承担。"""
    groups = em.collect({"frequent_social_media": "微信"}, [])
    assert groups == []


def test_collect_groups_by_field_and_dedupes():
    groups = em.collect(
        {"family_structure": "已婚有子女"},
        [note("family_structure", "已婚有子女"), note("payment_method", "分期")],
    )
    by_field = {group["attribute"]: group for group in groups}
    assert by_field["family_structure"]["values"] == ["已婚有子女"]
    assert by_field["payment_method"]["values"] == ["分期"]


def test_prompt_view_hides_internal_keys():
    """模型只看见中文字段名，看不见 ``family_structure``（checker 同口径）。"""
    groups = em.collect({"family_structure": "已婚有子女"}, [])
    text = json.dumps(em.prompt_view(groups), ensure_ascii=False)
    assert "family_structure" not in text
    assert "婚姻状况" in text or "家庭结构" in text


# ---------------------------------------------------------------- 模型输出的复核

def test_review_drops_key_outside_legal_set():
    groups = em.collect({"family_structure": "已婚有子女"}, [])
    accepted, dropped = em.review(mapped("家庭结构", "已婚有子女", "weibo"), groups)
    assert accepted == {}
    assert any("合法集" in item["reason"] for item in dropped)


def test_review_drops_value_not_in_brief():
    groups = em.collect({"family_structure": "已婚有子女"}, [])
    accepted, _ = em.review(mapped("家庭结构", "没送过的值", "has_children"), groups)
    assert accepted == {}


def test_review_accepts_explicit_null_as_unmapped():
    groups = em.collect({"payment_method": "分期"}, [])
    accepted, dropped = em.review(mapped("付款方式", "分期", None), groups)
    assert accepted == {}
    assert any("无法映射" in item["reason"] for item in dropped)


# ---------------------------------------------------------------- 落地

async def test_mapped_value_lands_in_facts_and_notes():
    """正例：facts 与 notes 同时换成合法键，转确认被清掉，留痕可查「原值 → 键」。"""
    llm = FakeLLM(mapped("家庭结构", "已婚有子女", "has_children"))
    out = await pipe.enum_map(
        facts_json=json.dumps({"family_structure": "已婚有子女"}, ensure_ascii=False),
        notes_updates_json=json.dumps([note("family_structure", "已婚有子女")], ensure_ascii=False),
        to_confirm_json=json.dumps(
            [{"attribute_key": "family_structure", "candidates": ["已婚有子女"], "reason": "x"}],
            ensure_ascii=False,
        ),
        llm=llm,
        templates=None,
        enabled=True,
    )
    assert out["enum_map_status"] == "ok"
    assert json.loads(out["facts_json"])["family_structure"] == "has_children"
    notes = json.loads(out["notes_updates_json"])
    assert notes[0]["attribute_value"] == "has_children"
    # 已被解决的转确认不再出现（字段已经合法，留着只会让销售做无用核对）。
    assert json.loads(out["to_confirm_json"]) == []
    traces = json.loads(out["rule_trace_json"])
    assert traces and traces[0]["rule_id"] == "ENUM-02"
    assert traces[0]["before"] == ["已婚有子女"]
    assert traces[0]["after"] == ["has_children"]
    # 置信档不动：映射是猜测，不是客户原话。
    assert json.loads(out["basis_json"]).get("family_structure") != "fact"
    assert out["enum_map_mapped"] == 1


async def test_multi_value_field_maps_each_part():
    """多选字段逐项映射：合法项原样保留，非法项换成键，连接符与形态不变。"""
    llm = FakeLLM(
        {
            "mappings": [
                # 字段名必须与送检清单逐字一致（渲染时用的是契约的 attribute_name）。
                {"field": "核心关注点", "value": "空间小", "key": "space"},
            ]
        }
    )
    out = await pipe.enum_map(
        facts_json=json.dumps({"core_concerns": "safety||空间小"}, ensure_ascii=False),
        notes_updates_json="[]",
        llm=llm,
        enabled=True,
        template_name="customer_profile_pipeline__pipeline_enum_map",
    )
    assert json.loads(out["facts_json"])["core_concerns"] == "safety||space"
    assert out["enum_map_mapped"] == 1


async def test_llm_failure_keeps_original_value_and_confirms():
    """失败退回今天的行为：保留原值，且**必须**留一条 to_confirm。"""
    out = await pipe.enum_map(
        facts_json=json.dumps({"family_structure": "已婚有子女"}, ensure_ascii=False),
        notes_updates_json=json.dumps([note("family_structure", "已婚有子女")], ensure_ascii=False),
        llm=FakeLLM(RuntimeError("boom")),
        enabled=True,
    )
    assert out["enum_map_status"] == "failed"
    assert out["enum_map_error"]
    assert json.loads(out["facts_json"])["family_structure"] == "已婚有子女"
    confirm = json.loads(out["to_confirm_json"])
    assert [item["attribute_key"] for item in confirm] == ["family_structure"]
    # 对客文案（reason / suggested_action）不得出现规则编号或内部键名；
    # ``attribute_key`` 本身是 to_confirm 的契约字段，不算文案。
    prose = " ".join(
        f"{item.get('reason', '')} {item.get('suggested_action', '')}" for item in confirm
    )
    assert "ENUM-02" not in prose
    assert "family_structure" not in prose


async def test_contract_gap_value_goes_to_confirm_without_calling_out():
    """没有别的待映射值时，微信这一类根本不该触发调用。"""
    llm = FakeLLM(mapped("常用社交平台", "微信", "weibo"))
    out = await pipe.enum_map(
        facts_json=json.dumps({"frequent_social_media": "微信"}, ensure_ascii=False),
        notes_updates_json="[]",
        llm=llm,
        enabled=True,
    )
    assert llm.prompts == []
    assert out["enum_map_status"] == "no_candidate"


# ---------------------------------------------------------------- 结构化枚举与缓存

def test_structured_enum_string_key_is_rewritten_not_split():
    """结构化串只改 ``key``，``input_value`` 与 JSON 形态原样保留。"""
    result = em.apply_mappings(
        {"trade_in_requirement": '{"key": "有", "input_value": "旧车"}'},
        [note("trade_in_requirement", '{"key": "有", "input_value": "旧车"}')],
        [],
        [],
        {("trade_in_requirement", "有"): "yes"},
    )
    patched = json.loads(result["facts"]["trade_in_requirement"])
    assert patched == {"key": "yes", "input_value": "旧车"}


async def test_second_case_reuses_cache():
    """同一取值第二次出现不再调用模型（跨案复用）。"""
    first = FakeLLM(mapped("家庭结构", "已婚有子女", "has_children"))
    args = dict(
        facts_json=json.dumps({"family_structure": "已婚有子女"}, ensure_ascii=False),
        notes_updates_json="[]",
        llm=first,
        enabled=True,
    )
    await pipe.enum_map(**args)
    assert len(first.prompts) == 1
    second = FakeLLM(mapped("家庭结构", "已婚有子女", "has_children"))
    out = await pipe.enum_map(**{**args, "llm": second})
    assert second.prompts == []
    assert json.loads(out["facts_json"])["family_structure"] == "has_children"


# ---------------------------------------------------------------- 残留转确认的口径

def test_local_decidable_value_is_not_reported_as_unmappable():
    """本地能翻的取值不该进「无法归入可选范围」的转确认（2026-10-28 实测误报）。

    ``education=本科`` 在回写出口能翻成 ``bachelor``，却曾被报成无法归入可选范围——
    销售会照这条提示去核对一个本就正确的字段。
    """
    leftovers = pipe._illegal_enum_values({"education": "本科"}, [])
    assert leftovers == {}

    # 契约缺口照旧要报：业务方需要知道缺键。
    gap = pipe._illegal_enum_values({"frequent_social_media": "微信"}, [])
    assert gap == {"frequent_social_media": ["微信"]}

    # 真正的非法值照旧要报。
    real = pipe._illegal_enum_values({"purchase_type": "贷款"}, [])
    assert real == {"purchase_type": ["贷款"]}
