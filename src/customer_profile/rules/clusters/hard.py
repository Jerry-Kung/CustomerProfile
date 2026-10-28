"""形态与占位簇：契约形态错误。

这簇的判据来自 ``review_notes`` 的四条机器检查，迁入后**补上动作执行**——原先只记
``issue``，判出违规照样进最终 Notes（蓝图 §2.2 称之为「能判但不拦」）。

三条规则共享一组判据函数，但动作不同：占位串整条删、非法枚举降级、无锚点 fact 降级
并转确认。三者都不是「删了没事」——只有占位串与内部字段名才是真该删的形态。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from ..actions import BASIS_FACT, BASIS_INFERRED_LOW, Action
from ..registry import Rule

RULES: tuple[Rule, ...] = (
    Rule(
        id="HARD-01",
        version=1,
        title="占位串整条去除",
        content_kind="hard",
        channels=("code",),
        scope=("*",),
        stages=("gate",),
        severity="block",
        default_action="reject",
        message_internal="取值为「未提供/暂无/无数据」一类占位串：整条删除，不留在 Notes 占位",
        message_customer="",
        owner="engineering",
        status="active",
        cluster="hard",
        tests=(
            "tests/test_pipeline_l1.py::test_review_removes_placeholder_values",
            "tests/test_pipeline_l1.py::test_review_keeps_clean_notes",
        ),
        legacy_codes=("placeholder_removed",),
    ),
    Rule(
        id="HARD-02",
        version=1,
        title="枚举取值必须是契约合法键",
        content_kind="hard",
        channels=("code",),
        scope=("*",),
        stages=("gate",),
        severity="repair",
        default_action="downgrade",
        message_internal="枚举字段取值归不到契约合法键、且未降级：降为 inferred_low 并转确认",
        message_customer="该字段取值不在可选范围内，请确认后更正",
        owner="engineering",
        status="active",
        cluster="hard",
        tests=(
            "tests/test_pipeline_l1.py::test_review_flags_illegal_enum_not_downgraded",
            "tests/test_pipeline_l1.py::test_review_allows_illegal_enum_when_downgraded",
        ),
        legacy_codes=("enum_illegal",),
    ),
    Rule(
        id="HARD-03",
        version=1,
        title="标为事实的字段必须指得出原文",
        content_kind="hard",
        channels=("code",),
        scope=("*",),
        stages=("gate",),
        severity="repair",
        default_action="downgrade",
        message_internal="basis=fact 但证据无原文切片、来源也不是登记档：降为 inferred_low，转确认",
        message_customer="该项缺少原文依据，建议核实",
        owner="engineering",
        status="active",
        cluster="hard",
        tests=(
            "tests/test_pipeline_l1.py::test_review_flags_unbacked_fact",
            "tests/test_pipeline_l1.py::test_review_accepts_fact_with_verbatim_quote",
        ),
        legacy_codes=("unbacked_fact",),
    ),
    Rule(
        id="ENUM-01",
        version=1,
        title="回写枚举取值必须是契约合法键",
        content_kind="hard",
        channels=("code",),
        scope=("*",),
        stages=("gate",),
        severity="block",
        default_action="reject",
        message_internal=(
            "枚举字段降级后取值仍归不到契约合法键：整条剔除，不把内部键回传给前端"
        ),
        message_customer="",
        owner="engineering",
        status="active",
        cluster="hard",
        tests=(
            "tests/rules/test_enum_legacy.py::test_illegal_enum_after_downgrade_is_removed",
            "tests/rules/test_enum_legacy.py::test_legal_enum_after_downgrade_is_kept",
            "tests/rules/test_enum_legacy.py::test_legacy_key_is_not_treated_as_illegal",
        ),
        legacy_codes=("enum_illegal_after_downgrade",),
    ),
    Rule(
        id="ENUM-02",
        version=1,
        title="非法枚举取值经语义映射归一",
        content_kind="hard",
        channels=("code",),
        scope=("*",),
        stages=("project",),
        severity="repair",
        default_action="rewrite",
        message_internal=(
            "枚举字段取值有信息但归不到合法键：经一次批量语义映射换成合法键，"
            "置信档仍为 inferred_low；映射不上的保留原值并转确认"
        ),
        message_customer="",
        owner="engineering",
        status="active",
        cluster="hard",
        tests=(
            "tests/test_enum_mapping.py::test_mapped_value_lands_in_facts_and_notes",
            "tests/test_enum_mapping.py::test_contract_gap_value_is_never_sent",
        ),
        legacy_codes=(),
    ),
)


# ====================================================================
# 检查函数：只产出动作，不改任何状态
# ====================================================================

PLACEHOLDER_VALUES: frozenset[str] = frozenset(
    {
        "未提供", "未提供。", "无", "暂无", "未知", "不详", "不明", "不清楚",
        "无数据", "未获取", "未获取到", "没有", "无信息", "未说明", "无内容",
        "-", "--", "——", "N/A", "n/a", "null", "None", "未知渠道",
    }
)
"""**占位串**：看着像有值、实际什么都没说的取值。

原在 ``pipeline``，M3 随 HARD-01 迁入——它是这条规则的数据表，放回规则身边，
「改规则只改一处」才成立。``pipeline`` 保留同名别名供既有调用点沿用。
"""

QUOTE_MARKERS: tuple[str, ...] = ("：", "「", "“")
"""证据串里「这是原文」的标记。

投影层产出的证据只有三种写法：``渠道：原文``、``原文「…」（经纠错归一）``、
``第三方画像标签“…”给出“…”``；注入字段（手机号）同样带全角冒号。裸渠道名与
推断依据都不含这些标记，因此不能当作「指得出原文」。
"""

REGISTRY_CHANNELS: frozenset[str] = frozenset(
    {
        "销售人员人工确认的客户Notes信息",
        "客户留存在我方销售数据系统中的信息记录",
        "销售人员对当前客户历史画像数据的反馈记录",
    }
)
"""登记档渠道：由系统直接给出的事实，本就不需要原文锚点。

``review_notes``（Notes 侧）与 ``validate_l3``（断言侧）共用这一份——两处各写一份
正是 HARD-03 之前「同名不同义」的成因。
"""


def is_placeholder(value: Any) -> bool:
    """取值本身是占位串（含空白归零后）时返回真。"""
    if isinstance(value, str):
        return value.strip() in PLACEHOLDER_VALUES
    return False


def has_quote_marker(text: str) -> bool:
    return any(marker in text for marker in QUOTE_MARKERS)


def is_unbacked_fact(has_quote: bool, channel: str) -> bool:
    """HARD-03 的唯一判据：标为事实、却既指不出原文也不是登记档。

    两个调用点的**输入来源不同**（Notes 侧读证据串里有没有原文标记；断言侧读
    ``claim.quote``），但判据完全相同，因此收敛到这一个函数——
    这正是 M3 要合并的那份重复实现。
    """
    return not has_quote and str(channel or "").strip() not in REGISTRY_CHANNELS


def enum_values(value: Any) -> list[str]:
    """把枚举字段的取值拆成待校验的字符串列表（多选用 ``||``，结构化取 ``key``）。

    **结构化串先解析成 JSON 对象再取 ``key``**（2026-10-28）：Notes 出口的结构化枚举
    是 JSON **字符串**（``'{"key":"yes","input_value":""}'``），整串既不等于合法键
    也不等于展示名，直接判会把一个完全合规的取值当成非法枚举降级。回写出口有同一处
    修复（``callback_build._enum_items``），两处必须同形，否则「gate 说违规、出口说
    正常」——同一份取值两套结论。
    """
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(enum_values(item))
        return out
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                key = str(parsed.get("key") or "").strip()
                return [key] if key else []
        if "||" in text:
            return [part.strip() for part in text.split("||") if part.strip()]
        return [text] if text else []
    if isinstance(value, dict):
        return [str(value.get("key"))] if value.get("key") else []
    return [str(value)] if value not in (None, "") else []


def check_notes(
    entries: Sequence[Any],
    *,
    basis: Mapping[str, str],
    is_conclusion: Any,
    enum_known: Any,
    legal_keys: Any,
    display_to_key: Any,
    is_enum_key: Any,
) -> list[Action]:
    """gate 阶段：占位串、非法枚举、无锚点 fact。

    三个判据按原 ``review_notes`` 的顺序逐条执行，且**占位串整条跳过后续检查**——
    一条「未提供」没有校验取值的意义。

    枚举与归一的实现由调用方注入（``internal_fields`` 的那几个函数），规则簇不自己
    抄一份契约知识：合法键的全部来源是契约快照，抄一份就会在下一次契约变动时漂移。
    ``is_enum_key`` 判「取值看起来是不是枚举键」，ENUM-01 靠它把废弃内部键与客户原话
    分开（``replacement`` 删、``说不清`` 留）。
    """
    actions: list[Action] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("attribute_key") or "")
        value = entry.get("attribute_value")

        # rule: HARD-01 占位串整条去除
        # ---- HARD-01 占位串整条去除
        # 「未知」对姓名字段是**明确的业务结论**（查遍记录也没有可靠姓名），不是占位串。
        if key != "customer_name" and (
            key in PLACEHOLDER_VALUES or is_placeholder(value)
        ):
            actions.append(
                Action(
                    rule="HARD-01",
                    attribute=key,
                    action="reject",
                    severity="block",
                    reason="取值为占位串，整条去除",
                    stage="gate",
                    target="note",
                    value=value,
                )
            )
            continue

        detail = entry.get("reasoning_detail")
        if not isinstance(detail, Mapping):
            detail = {}

        # rule: HARD-02 枚举取值必须是契约合法键
        # ---- HARD-02 枚举取值必须是契约合法键（已降级的不重复拦）
        if enum_known(key) and str(basis.get(key) or "") != BASIS_INFERRED_LOW:
            for item in enum_values(value):
                if legal_keys(key) and item not in legal_keys(key):
                    if not display_to_key(key, item):
                        actions.append(
                            Action(
                                rule="HARD-02",
                                attribute=key,
                                action="downgrade",
                                severity="repair",
                                reason="枚举取值不在契约内且未降级",
                                stage="gate",
                                target="note",
                                value=item,
                            )
                        )

        # rule: ENUM-01 回写枚举取值必须是契约合法键
        # ---- ENUM-01 回写枚举取值必须是契约合法键
        # 与 HARD-02 的分工：HARD-02 管「L1 给了非法值**且尚未降级**」，动作是降级转确认
        # （保留取值，让销售看到并核）；ENUM-01 管「**降级后**取值仍然是**键形态**」，
        # 动作是剔除——前端点开看到一个 ``replacement`` 就是不可读的内部串。
        #
        # **判据必须带形态**：只按「不在合法键集合」判，会把客户原话（``说不清``）一起删掉，
        # 那是 P3 规则 d 刻意保留的东西。``looks_like_enum_key`` 是这里的唯一分界线。
        if (
            enum_known(key)
            and str(basis.get(key) or "") == BASIS_INFERRED_LOW
            and legal_keys(key)
        ):
            for item in enum_values(value):
                if item in legal_keys(key) or display_to_key(key, item):
                    continue
                if not is_enum_key(item):
                    continue
                actions.append(
                    Action(
                        rule="ENUM-01",
                        attribute=key,
                        action="reject",
                        severity="block",
                        reason="枚举取值降级后仍是废弃的内部键，整条剔除",
                        stage="gate",
                        target="note",
                        value=item,
                    )
                )
                break

        # rule: HARD-03 标为事实的字段必须指得出原文
        # ---- HARD-03 标为事实的字段必须指得出原文
        if str(basis.get(key) or "") == BASIS_FACT:
            evidence = [str(item) for item in (detail.get("evidence") or [])]
            has_quote = any(has_quote_marker(item) for item in evidence)
            channel = str(entry.get("record_channel") or "")
            if is_unbacked_fact(has_quote, channel):
                actions.append(
                    Action(
                        rule="HARD-03",
                        attribute=key,
                        action="downgrade",
                        severity="repair",
                        reason="标为事实但指不出原文，也不是登记档来源",
                        stage="gate",
                        target="note",
                        value=value,
                    )
                )
    return actions
