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
    """一条规则只作用于自己声明的字段：别的字段不得出现它的正文。

    2026-10-28 起 ``scope`` 允许挂多个字段（SPEC-17/18）：断言由「作用域恰好是这一个
    字段」放宽为「该字段在作用域内」。正文仍只在声明过的字段下渲染。
    """
    by_id = {rule.id: rule for rule in spec_rules.RULES}
    for field, text in notes.items():
        owners = [rule for rule in by_id.values() if text in rule.message_internal]
        assert len(owners) == 1, f"{text[:12]}… 同时被 {len(owners)} 条规则声明"
        assert field in owners[0].scope


# ---------------------------------------------------------------- 迁移一致性

def test_spec_rules_cover_exactly_the_migrated_fields(notes, registry):
    """迁入字段与登记表里的 SPEC 规则一一对应，不多不少。

    多字段规则（SPEC-17）的正文只在**主字段**下渲染一次，其余适用字段由
    :func:`render.field_note_extra_scopes` 以「本条口径同样适用于」一行带出。
    因此比对集合是「有正文的字段 ∪ 被同样适用标注的字段」。

    ``scope=("*",)`` 的全局规则（``SPEC-19``）不参与：它没有归属字段，正文由
    :func:`render.global_notes` 单独成段，不会出现在任何字段行下。比对时一并排除，
    与 :func:`render.field_notes` 的排除口径保持一致。
    """
    extra = render.field_note_extra_scopes(stage="extract")
    covered = set(notes) | {field for fields in extra.values() for field in fields}
    registered = {
        field
        for rule in registry.active()
        if rule.cluster == "spec" and rule.enables_channel("prompt")
        for field in rule.scope
        if field != "*"
    }
    assert registered == covered


def test_spec_fields_are_extraction_fields(notes):
    """字段必须是**真取证字段**：字段改名或下线后仍留着规则，会静默失效。"""
    from customer_profile import internal_fields as inf

    assert set(notes) <= set(inf.EXTRACTION_KEYS)


def test_deposit_phrases_never_land_in_ownership_fields():
    """SPEC-17/18 与 CONSIST-04 的同一口径：小订/大定是下单动作，不是拥车。

    代码通道（CONSIST-04）在投影层拒收这类断言，提示词通道（SPEC-17）告诉 L1 别
    这么读。两通道的对接口径在这里钉一次，任一侧改了词表或字段集合就红。
    """
    from customer_profile.rules.clusters import consistency as consistency_rules

    assert {"小订", "小定", "大订", "大定"} <= set(consistency_rules.DEPOSIT_PHRASES)
    spec17 = next(rule for rule in spec_rules.RULES if rule.id == "SPEC-17")
    assert set(spec17.scope) - {"intended_model"} <= set(
        consistency_rules.OWNERSHIP_ATTRIBUTES
    ) | {"current_vehicle_info"}


def test_trade_in_note_states_signal_requirement(notes):
    """``SPEC-21`` 的四条口径都要进提示词，一条不能少。

    这四条各自对应一类实障，缺任何一条都会让错误重新长回来：

    1. 置换要有明确的置换类信号（业务方报障的根因）；
    2. 有旧车 / 增购 **都不是**置换；
    3. 多台旧车甄别不出时写「待确认」，不得并列写入；
    4. 意向 / 对比 / 试驾车型不得混进置换车型。
    """
    text = notes["trade_in_requirement"]
    assert "以旧换新" in text and "换购" in text
    assert "客户有旧车不等于客户要置换" in text
    assert "增购也不等于置换" in text
    assert "待确认" in text
    assert "意向车型、对比车型、试驾过的车型" in text
    # 「没提到」不等于「不置换」：不得据此写「无」
    assert "不得**写「无」" in text


def test_trade_in_note_states_exclusion_and_indirect_rules(notes):
    """回放验证补的两条口径：排他信号优先，间接信号可作低置信信源。

    两条各自对应一个实测缺陷，缺任何一条都会重现：

    1. ``13797120317``「客户刚卖了二手车，问一下补贴」——车已卖出，没有可置换的旧车。
       旧产物判 ``no`` 是对的，新提示词却因为看到「置换」二字判成了有需求，两轮重放复现。
    2. ``13986119717``「购置税全款，然后置换补贴也没有」——在抱怨拿不到补贴，不是要置换。
    业务口径（2026-11-04）：第 2 类可以作**低置信**信源，只在没有排他信号时成立。
    """
    text = notes["trade_in_requirement"]
    assert "已经卖掉、过户、报废或已处置" in text
    assert "这条排除优先于其它一切信号" in text
    assert "置换补贴" in text and "间接" in text
    assert "标低置信" in text
    assert "仅有间接信号时**不得**写置换车型" in text


def test_trade_in_signal_levels_are_ordered():
    """三档判定：排他 > 否定 > 间接 > 直接。

    判序是这条逻辑的全部内容——顺序换一下，「已经卖了旧车想置换」就会被读成有需求，
    「置换补贴也没有」会被读成没有需求。
    """
    from customer_profile.workflows.customer_profile_pipeline import (
        TRADE_SIGNAL_DIRECT,
        TRADE_SIGNAL_INDIRECT,
        TRADE_SIGNAL_NONE,
        _trade_in_signal_level,
    )

    # 排他：车不在了，压过同一句里的「置换」「以旧换新」
    assert _trade_in_signal_level("客户刚卖了二手车，问一下补贴的问题") == TRADE_SIGNAL_NONE
    assert _trade_in_signal_level("已经卖了旧车，现在想置换新车") == TRADE_SIGNAL_NONE
    assert _trade_in_signal_level("那台车撞报废了") == TRADE_SIGNAL_NONE
    assert _trade_in_signal_level("旧车已过户给父亲") == TRADE_SIGNAL_NONE
    # 否定：客户说的是没有
    assert _trade_in_signal_level("暂无明确置换需求") == TRADE_SIGNAL_NONE
    # 间接：只说明在问价
    assert _trade_in_signal_level("买车购置税全款，然后置换补贴也没有") == TRADE_SIGNAL_INDIRECT
    assert _trade_in_signal_level("置换政策怎么样") == TRADE_SIGNAL_INDIRECT
    # 直接：客户明说要交出去
    assert _trade_in_signal_level("我就想把现在这台置换掉") == TRADE_SIGNAL_DIRECT
    assert _trade_in_signal_level("想以旧换新") == TRADE_SIGNAL_DIRECT
    # 纯拥车叙述不是任何信号
    assert _trade_in_signal_level("客户有旧车一台") == TRADE_SIGNAL_NONE


def test_trade_in_note_rendered_under_requirement_field(notes):
    """正文挂在 ``trade_in_requirement`` 行下，``trade_in_model`` 以「同样适用于」带出。

    两个字段是一件事的两面，登记为一条规则；渲染层对多字段规则只在第一个字段下写正文。
    """
    from customer_profile.rules import render as rule_render

    extra = rule_render.field_note_extra_scopes(stage="extract")
    owners = [rule for rule in spec_rules.RULES if rule.id == "SPEC-21"]
    assert len(owners) == 1
    assert owners[0].scope == ("trade_in_requirement", "trade_in_model")
    assert "trade_in_model" in extra["trade_in_requirement"]
    # SPEC-17 已退出置换字段，否则两个 scope 会争同一行
    spec17 = next(rule for rule in spec_rules.RULES if rule.id == "SPEC-17")
    assert "trade_in_model" not in spec17.scope


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
    2026-10-28 经业务确认追加两条：``SPEC-17`` 小订/大定不是拥车、``SPEC-18`` 小订/
    大定的看车进度；同日业务确认多字段规则只渲染一次，长度 8327 → 8688 字符。
    2026-10-30 经业务确认追加一条：``SPEC-19`` 多来源冲突取舍次序（8688 → 8944 字符）。
    它是**全局规则**，单列一段放在《合法取值清单》之后。
    2026-11-03 经业务确认追加一条：``SPEC-20`` 付款方式的取证范围（8944 → 9135 字符）。
    业务背景：贷款判定被补贴/砍价/收入档一类价格信号带偏，真实分布约九成全款。
    2026-11-04 经业务确认追加一条：``SPEC-21`` 置换需求与置换车型的取证范围
    （9135 → 9504 字符），同时把 ``SPEC-17`` 的 scope 从五个字段收窄到三个拥车字段——
    它与新规则争同一个字段，而渲染层对同一字段是「先到先得」，不收窄就会在启动期报错。
    业务背景：把「客户有旧车」当成了「客户要置换」。
    同日后回放验证补两条口径（9504 → 9729 字符）：已卖掉/报废/过户的车不构成置换需求
    （优先级最高）；「置换补贴」一类只说明在问价，属间接信号，可作低置信信源但不得据此
    写置换车型。业务背景：``13797120317``（客户刚卖了二手车）两轮重放都误判为有置换需求。
    """
    guide = pipe.l1_guide()
    assert len(guide) == 9729
    assert "取证范围：从沟通记录里的时间表述推断" in guide
    assert "数字越大代表消费水平越高" in guide
    assert "第三方标签可以降档，但不允许整条丢掉" in guide
    # 标记不得漏进提示词：它们是落点凭据，不是给模型看的内容
    assert "<!-- rule:" not in guide


def test_field_source_notes_constant_is_gone():
    """迁移完成后不得留第二份正文——「一处登记」的最低检验。"""
    assert not hasattr(pipe, "FIELD_SOURCE_NOTES")


def test_global_rule_rendered_separately():
    """``SPEC-19`` 正例：全局规则单独成段，且真的进了 L1 提示词。

    这条测试存在的理由就是「写了但到不了模型」——参考标准表那一列只做展示，
    规则正文若不经过渲染就永远影响不到输出。
    """
    text = render.global_notes(stage="extract")
    assert "客户原话" in text and "第三方画像标签" in text
    assert "不得" in text  # 必须写明「不得留空」这一条，否则模型会整批丢弃第三方字段
    assert text in pipe.l1_guide()


def test_global_rule_not_folded_into_field_notes():
    """``SPEC-19`` 反例：全局规则**不得**被当成某个字段的取证口径贴进行内。

    ``scope=("*",)`` 的语义是「不限于某字段」，不是「适用于每个字段」。若它混进
    ``field_notes``，字段清单里每一行下面都会多出一段与字段无关的话。
    """
    notes = render.field_notes(stage="extract")
    assert all("多来源冲突" not in text for text in notes.values())
    extra = render.field_note_extra_scopes(stage="extract")
    assert all("*" not in fields for fields in extra.values())


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
