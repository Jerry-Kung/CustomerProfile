"""L1 三层合并层（``merge_l1_layers``）的回归用例。

**为什么这几条必须有**：v3 三分层把「一次直出全部字段」拆成「1a 事实层 + 1c 分析投影层」，
而 1c 提示词明说「事实清单已给的字段不用重复」——两层是**互补**而非重叠。少了合并这一步，
1a 检出的事实（``intended_brand=埃安`` 这类带锚点的 ``fact``）不进账本，最终 Notes 直接变空。
实测 8 案：1a=90 / 1c=156 / 重叠仅 6 / 仅 1a 有 84。这里的用例把这个漏洞钉死。
"""

from __future__ import annotations

import json

from customer_profile.workflows import customer_profile_pipeline as pipe


def _facts(*entries):
    return json.dumps(list(entries), ensure_ascii=False)


def _fields(*entries):
    return json.dumps({"fields": list(entries)}, ensure_ascii=False)


def _merged(facts, fields):
    result = pipe.merge_l1_layers(facts, fields)
    return {x["attribute"]: x for x in json.loads(result["claims_json"])["fields"]}, result


# ---------------------------------------------------------------- 并集

def test_merge_keeps_fact_only_field():
    """只在 1a 出现的事实必须进入账本（这正是 intended_brand 丢失的根因）。"""
    merged, _ = _merged(
        _facts({"attribute": "intended_brand", "value": "埃安", "basis": "fact",
                "record_channel": "外呼录音信息", "anchor": "我们这边是广汽埃安的"}),
        _fields({"attribute": "annual_income", "value": "10-20万", "basis": "inferred_low"}),
    )
    assert "intended_brand" in merged
    assert merged["intended_brand"]["value"] == "埃安"
    assert "annual_income" in merged


def test_merge_keeps_fields_only_field():
    """只在 1c 出现的推断字段必须保留，不被合并丢掉。"""
    merged, _ = _merged(
        _facts({"attribute": "intended_brand", "value": "埃安", "basis": "fact"}),
        _fields({"attribute": "communication_rhythm", "value": "当场答复为主",
                 "basis": "inferred_high"}),
    )
    assert "communication_rhythm" in merged


# ---------------------------------------------------------------- 同字段取舍

def test_merge_prefers_harder_basis():
    """同字段冲突时取更硬的一条：1a 的 fact 压过 1c 的 inferred_low。"""
    merged, _ = _merged(
        _facts({"attribute": "intended_brand", "value": "埃安", "basis": "fact"}),
        _fields({"attribute": "intended_brand", "value": "广汽埃安", "basis": "inferred_low"}),
    )
    assert merged["intended_brand"]["value"] == "埃安"
    assert merged["intended_brand"]["basis"] == "fact"


def test_merge_same_basis_prefers_fact_layer():
    """档位相同时以 1a 为准——它是钉死的事实层。"""
    merged, _ = _merged(
        _facts({"attribute": "age", "value": "35-39岁", "basis": "inferred_high"}),
        _fields({"attribute": "age", "value": "40-49岁", "basis": "inferred_high"}),
    )
    assert merged["age"]["value"] == "35-39岁"


def test_merge_harder_fields_wins_over_weaker_fact():
    """反向：1c 若比 1a 更硬，则保留 1c 的硬值。"""
    merged, _ = _merged(
        _facts({"attribute": "age", "value": "35-39岁", "basis": "inferred_low"}),
        _fields({"attribute": "age", "value": "40-49岁", "basis": "fact"}),
    )
    assert merged["age"]["value"] == "40-49岁"


# ---------------------------------------------------------------- 计数与边界

def test_merge_counts_are_reported():
    """留痕计数正确：from_facts_only 数只 1a 有的、overrode 数 1a 覆盖 1c 的。"""
    _, result = _merged(
        _facts({"attribute": "intended_brand", "value": "埃安", "basis": "fact"},
               {"attribute": "occupation", "value": "水果店经营者", "basis": "inferred_high"}),
        _fields({"attribute": "intended_brand", "value": "广汽埃安", "basis": "inferred_low"},
                {"attribute": "annual_income", "value": "10-20万", "basis": "inferred_low"}),
    )
    assert result["merged_count"] == 3
    assert result["facts_count"] == 2
    assert result["fields_count"] == 2
    assert result["from_facts_only"] == 1   # occupation
    assert result["overrode"] == 1          # intended_brand


# ------------------------------------------------- v3.1 主体继承

def test_subject_from_facts_layer_wins():
    """同档以 1a 为准时，其 ``subject`` 随条目一并生效。

    v3.1 改造二的机制面——1c 的输出示例默认给每条结论写 ``subject=customer``，
    靠 1a 的覆盖把「销售顾问的信息」纠正回 ``salesperson``，交投影层归属过滤拦下。
    """
    merged, _ = _merged(
        _facts({"attribute": "occupation", "value": "汽车销售", "basis": "fact",
                "subject": "salesperson"}),
        _fields({"attribute": "occupation", "value": "汽车销售", "basis": "inferred_high",
                 "subject": "customer"}),
    )
    assert merged["occupation"]["subject"] == "salesperson"


def test_subject_override_applies_when_fields_layer_wins():
    """1c 因更硬而胜出时，``subject`` 仍须回到 1a。

    这是覆盖逻辑**真正生效**的路径：同档时 1a 整条替换 1c，``subject`` 本就随之生效；
    只有 1c 更硬、保留了 1c 的取值时，才需要把 ``subject`` 单独纠回 1a 的判断。
    """
    merged, result = _merged(
        _facts({"attribute": "occupation", "value": "汽车销售", "basis": "inferred_low",
                "subject": "salesperson"}),
        _fields({"attribute": "occupation", "value": "汽车销售", "basis": "fact",
                 "subject": "customer"}),
    )
    assert merged["occupation"]["basis"] == "fact"             # 1c 的档更硬，取值胜出
    assert merged["occupation"]["subject"] == "salesperson"    # 但主体回到 1a
    assert result["subject_overrides"] == 1


def test_subject_not_forced_to_unknown_when_facts_lacks_field():
    """1a 没有该字段时保留 1c 的判断，**不**置 unknown。

    置空会让 1c 单独检出的身份字段被归属过滤整批拦下（实测 7 案 38 个字段，
    占 Notes 的 16.2%），把 v3 修好的覆盖又打回去。
    """
    merged, result = _merged(
        _facts(),
        _fields({"attribute": "age", "value": "35-39岁", "basis": "inferred_high",
                 "subject": "customer"}),
    )
    assert merged["age"]["subject"] == "customer"
    assert result["subject_overrides"] == 0


def test_subject_left_alone_when_facts_entry_declares_none():
    """1a 条目没写 ``subject`` 时不改动——不能把空串覆盖上去。"""
    merged, _ = _merged(
        _facts({"attribute": "age", "value": "35-39岁", "basis": "inferred_low"}),
        _fields({"attribute": "age", "value": "35-39岁", "basis": "fact",
                 "subject": "customer"}),
    )
    assert merged["age"]["subject"] == "customer"


def test_merge_handles_empty_inputs():
    """两边都空时不抛，输出空账本。"""
    merged, result = _merged(_facts(), _fields())
    assert merged == {}
    assert result["merged_count"] == 0


# ---------------------------------------------------------------- 节点接线

def test_localize_consumes_merge_not_fields():
    """接线回归：L1_LOCALIZE 的 claims_json 必须来自合并层，而非直连 1c。

    这是本 bug 的**接线**面——函数写对了但接线漏了，字段照样丢。故直接钉节点图。
    """
    nodes = {n.node_id: n for n in pipe.WORKFLOW.nodes}
    localize = nodes[pipe.L1_LOCALIZE]
    sources = {b.target: tuple(b.source) for b in localize.bindings}
    assert sources["claims_json"] == (pipe.L1_MERGE, "claims_json")

    merge = nodes[pipe.L1_MERGE]
    merge_sources = {b.target: tuple(b.source) for b in merge.bindings}
    assert merge_sources["facts_json"] == (pipe.L1_FACTS, "result")
    assert merge_sources["fields_json"] == (pipe.L1_FIELDS, "result")
    assert pipe.L1_MERGE in pipe.CODE_SPECS
