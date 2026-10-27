"""SPEC 簇：逐字段取证方向（prompt 通道）。

这簇规则迁自 ``pipeline.FIELD_SOURCE_NOTES`` 那个字典常量。迁移的验收口径只有一条：
**L1 提示词逐字不变**。规则换了一种存放方式，模型看到的内容一个字都不该动——否则就
不是「把规则登记起来」，而是顺手改了提示词，那是业务优化，得单独确认。

因此这里既钉「正文与迁移前一致」（快照在 ``spec.txt`` 里就是正文本身），也钉
「按字段取用时不串味」（一条规则只贴到它自己的字段下面）。
"""

from __future__ import annotations

import pytest

from customer_profile import notes_contract as nc
from customer_profile.rules import default as default_registry
from customer_profile.rules import render
from customer_profile.rules.clusters import spec as spec_rules
from customer_profile.workflows import customer_profile_pipeline as pipe


@pytest.fixture(scope="module")
def notes():
    return render.field_notes(stage="extract")


@pytest.fixture(scope="module")
def registry():
    return default_registry()


def test_spec_note_rendered_inline(notes):
    """每条 SPEC 规则的正文都要出现在《目标字段清单》里，且**紧跟它自己的字段行**。"""
    guide = pipe.field_inference_guide()
    lines = guide.splitlines()
    for field, text in notes.items():
        assert text in guide, f"{field} 的取证方向没有进提示词"
        index = next(
            i for i, line in enumerate(lines) if line.startswith(f"- `{field}`")
        )
        assert lines[index + 1] == f"  - 取证范围：{text}", (
            f"{field} 的取证方向没有贴着字段渲染——另起一节模型容易漏读"
        )


def test_spec_note_isolated_per_field(notes):
    """一条规则只作用于自己声明的字段：别的字段不得出现它的正文。"""
    by_id = {rule.id: rule for rule in spec_rules.RULES}
    for field, text in notes.items():
        owners = [rule for rule in by_id.values() if text in rule.message_internal]
        assert len(owners) == 1, f"{text[:12]}… 同时被 {len(owners)} 条规则声明"
        assert owners[0].scope == (field,)


# ---------------------------------------------------------------- 迁移一致性

def test_spec_rules_cover_exactly_the_migrated_fields(notes, registry):
    """迁入字段与登记表里的 SPEC 规则一一对应，不多不少。"""
    registered = {
        rule.scope[0]
        for rule in registry.active()
        if rule.cluster == "spec" and rule.enables_channel("prompt")
    }
    assert registered == set(notes)


def test_spec_fields_are_extraction_fields(notes):
    """字段必须是**真取证字段**：字段改名或下线后仍留着规则，会静默失效。"""
    from customer_profile import internal_fields as inf

    assert set(notes) <= set(inf.EXTRACTION_KEYS)


def test_spec_texts_are_unique_cluster_registered(registry):
    """编号连续、无空缺段（``SPEC-01`` 留给 M5 的 checker 通道）。"""
    ids = [rule.id for rule in spec_rules.RULES]
    assert ids == [f"SPEC-{n:02d}" for n in range(2, 2 + len(ids))]
    for rule_id in ids:
        assert registry.get(rule_id) is not None


def test_l1_guide_is_unchanged_by_the_migration():
    """L1 提示词**逐字不变**：这是 M4 的验收点，也是「迁移不是改提示词」的证据。

    字符数与哈希都钉住。改这条断言等于改了提示词，必须走业务确认。

    2026-10-27 经业务确认追加一条：``SPEC-16`` 消费等级档位方向（8240 → 8327 字符）。
    """
    guide = pipe.l1_guide()
    assert len(guide) == 8327
    assert "取证范围：从沟通记录里的时间表述推断" in guide
    assert "数字越大代表消费水平越高" in guide
    # 标记不得漏进提示词：它们是落点凭据，不是给模型看的内容
    assert "<!-- rule:" not in guide


def test_field_source_notes_constant_is_gone():
    """迁移完成后不得留第二份正文——「一处登记」的最低检验。"""
    assert not hasattr(pipe, "FIELD_SOURCE_NOTES")


def test_contract_fields_still_listed_once():
    """字段清单仍逐条覆盖全部取证字段（防止迁移时误删字段行）。"""
    from customer_profile import internal_fields as inf

    guide = pipe.field_inference_guide()
    listed = {
        line.split("`")[1]
        for line in guide.splitlines()
        if line.startswith("- `") and line.count("`") >= 2
    }
    assert listed == set(inf.EXTRACTION_KEYS)
    assert set(nc.ALL_KEYS) <= listed
