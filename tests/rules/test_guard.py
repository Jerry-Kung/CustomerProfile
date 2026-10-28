"""检索守卫：孤儿标记与未落地规则。

两条守卫的失败形态相反，因此各有一个正例一个反例：

- 孤儿标记 = 标记还在、登记表里没有这条（删规则删了一半）；
- 未落地规则 = 登记表里有、检索不到落点（文档里写了、代码里没做）。

另外钉住「三种标记形态」的识别：注释、成对标记、数据表并行键。裸查 ``rule:`` 会把
源码里大量的断言留痕字典当成落点，那是这条守卫最初写出来时就踩过的坑。
"""

from __future__ import annotations

from pathlib import Path

from customer_profile.rules import Rule, RuleRegistry
from customer_profile.rules import guard as gd


def make_rule(rule_id, **overrides):
    fields = dict(
        id=rule_id,
        version=1,
        title="t",
        content_kind="consistency",
        channels=("code",),
        scope=("annual_income",),
        stages=("project",),
        severity="repair",
        default_action="downgrade",
        message_internal="m",
        message_customer="c",
        owner="engineering",
        status="active",
        tests=("a", "b"),
    )
    fields.update(overrides)
    return Rule(**fields)


def table_of(*rules):
    table = RuleRegistry()
    for rule in rules:
        table.register(rule)
    return table


# ---------------------------------------------------------------- 形态识别

def test_comment_marker_is_found():
    assert gd.extract_ids("# rule: INCOME-01 收入只认收入标签") == ["INCOME-01"]


def test_text_block_markers_are_found():
    line = "<!-- rule: SPEC-02 begin -->"
    assert gd.extract_ids(line) == ["SPEC-02"]
    assert gd.extract_ids("<!-- rule: SPEC-02 end -->") == ["SPEC-02"]


def test_table_marker_is_found():
    line = '  {"tag": "预测手机价格", "rule": "INCOME-02", ...},'
    assert gd.extract_ids(line) == ["INCOME-02"]


def test_legacy_bare_codes_are_not_marks():
    """迁移期的旧裸串不是标记——它们由登记表的 legacy_codes 承接。"""
    assert gd.extract_ids('    "rule": "f_trade_in",') == []
    assert gd.extract_ids('"rule": "a_attribution"') == []


def test_arbitrary_rule_word_is_not_a_mark():
    assert gd.extract_ids("这条 rule 说的是另一回事") == []
    assert gd.extract_ids("三处落点，rule 编号见设计") == []


# ---------------------------------------------------------------- 扫描

def test_scan_finds_marks_in_py_and_txt(tmp_path: Path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.py").write_text(
        "# rule: INCOME-01 说明\ncode()\n", encoding="utf-8"
    )
    (source / "b.txt").write_text(
        "<!-- rule: SPEC-02 begin -->\n正文\n<!-- rule: SPEC-02 end -->\n",
        encoding="utf-8",
    )
    marks = gd.scan_marks([source], root=tmp_path)
    pairs = {(mark.rule_id, mark.kind) for mark in marks}
    assert ("INCOME-01", "code") in pairs
    assert ("SPEC-02", "text") in pairs
    # 开始行与结束行都要记：删规则时两行都得删。
    assert sum(1 for mark in marks if mark.rule_id == "SPEC-02") == 2


# ---------------------------------------------------------------- 孤儿标记

def test_orphan_detects_unregistered_id():
    marks = [gd.Mark("INCOME-99", "src/x.py", 3, "code")]
    assert gd.orphan_marks(marks, table_of(make_rule("INCOME-01"))) == marks


def test_orphan_detects_deprecated_rule_left_behind():
    """规则退役后残留标记即红——这是「删规则删了一半」的标准形态。"""
    marks = [gd.Mark("INCOME-01", "src/x.py", 3, "code")]
    table = table_of(make_rule("INCOME-01", status="deprecated"))
    assert len(gd.orphan_marks(marks, table)) == 1


def test_active_mark_is_clean():
    marks = [gd.Mark("INCOME-01", "src/x.py", 3, "code")]
    assert gd.orphan_marks(marks, table_of(make_rule("INCOME-01"))) == []


# ---------------------------------------------------------------- 未落地规则

def test_unlanded_detects_registered_rule_without_marks():
    table = table_of(make_rule("INCOME-01"), make_rule("INCOME-02"))
    marks = [gd.Mark("INCOME-01", "src/x.py", 3, "code")]
    assert gd.unlanded_rules(marks, table) == ["INCOME-02"]


def test_proposed_rule_need_not_land():
    table = table_of(make_rule("INCOME-01", status="proposed"))
    assert gd.unlanded_rules([], table) == []


# ---------------------------------------------------------------- 索引

def test_index_by_rule_lists_all_landing_points():
    marks = [
        gd.Mark("INCOME-01", "src/b.py", 9, "code"),
        gd.Mark("INCOME-01", "src/a.py", 2, "code"),
        gd.Mark("SPEC-02", "src/c.txt", 1, "text"),
    ]
    index = gd.index_by_rule(marks)
    assert list(index) == ["INCOME-01", "SPEC-02"]
    assert [mark.path for mark in index["INCOME-01"]] == ["src/a.py", "src/b.py"]
