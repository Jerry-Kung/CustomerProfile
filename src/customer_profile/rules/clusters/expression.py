"""表达簇：对客产出的形态。

这一簇的规则作用于**已写好的文案**，时机在成文之后。它们记 ``content_kind=hard`` 而
不是另设一类：表达约束也是**形态**约束（不写内部字段名、证据不得自指），与取值合法性
同层。执行器据此把它们排在语义判断之前，先把脏形态清掉。

三条规则的性质不同，不要混读：

- ``EXPR-01`` 管**正文里的枚举键**（``已定稿事实：power、appearance``）。判据是
  「是不是某个字段的合法枚举键」，不是「含不含英文字母」——后者会误伤 ``Model Y`` /
  ``BMW`` / ``max版`` 这类品牌与车型名（治理方案 §4.1）。
- ``EXPR-02`` 管**证据位的结论型措辞**（``推断为40-49岁``），只删那一项证据。
- ``EXPR-03`` 管**英文占位串**（``payment_method = unknown``），整条删除。与
  ``HARD-01`` 的中文占位串同表同动作，只是形态不同。
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from ... import internal_fields as inf
from ..actions import Action
from ..registry import Rule

RULES: tuple[Rule, ...] = (
    Rule(
        id="EXPR-01",
        version=1,
        title="对客正文不得出现英文枚举键",
        content_kind="hard",
        channels=("code", "prompt"),
        scope=("*",),
        stages=("gate", "express"),
        severity="repair",
        default_action="rewrite",
        message_internal=(
            "Notes 的 summary/steps 里出现枚举键（power、unmarried_no_child）："
            "按契约展示名改写为中文；取值与证据原文不动"
        ),
        message_customer="",
        owner="both",
        status="active",
        cluster="expression",
        tests=(
            "tests/rules/test_expression.py::test_enum_key_in_prose_is_rewritten",
            "tests/rules/test_expression.py::test_brand_name_in_prose_is_not_rewritten",
            "tests/test_pipeline_l1.py::test_review_rewrites_enum_keys_in_prose",
        ),
        legacy_codes=("english_in_prose",),
    ),
    Rule(
        id="EXPR-02",
        version=1,
        title="证据位不得出现结论型措辞",
        content_kind="hard",
        channels=("code",),
        scope=("*",),
        stages=("gate",),
        severity="repair",
        default_action="reject",
        message_internal="证据串是结论（「推断为…」）而不是原文：该条证据整条删除",
        message_customer="",
        owner="engineering",
        status="active",
        cluster="expression",
        tests=(
            "tests/test_pipeline_l1.py::test_review_flags_evidence_self_reference",
            "tests/test_projection.py::test_notes_reasoning_is_free_of_audit_language",
        ),
        legacy_codes=("evidence_self_reference",),
    ),
    Rule(
        id="EXPR-03",
        version=1,
        title="英文占位串整条去除",
        content_kind="hard",
        channels=("code",),
        scope=("*",),
        stages=("gate",),
        severity="block",
        default_action="reject",
        message_internal=(
            "枚举字段取值是 unknown/null/none 一类英文占位串（且不是本字段的合法键）："
            "该条 Notes 整条删除，不留在产物里占位"
        ),
        message_customer="",
        owner="engineering",
        status="active",
        cluster="expression",
        tests=(
            "tests/rules/test_expression.py::test_english_placeholder_is_rejected",
            "tests/rules/test_expression.py::test_legal_unknown_key_is_kept",
            "tests/test_pipeline_l1.py::test_review_removes_english_placeholder_entries",
        ),
        legacy_codes=("english_placeholder_removed",),
    ),
)


# ====================================================================
# 检查函数：只产出动作，不改任何状态
# ====================================================================

PROSE_FIELDS: tuple[str, ...] = ("summary", "steps")
"""可以改写的位置。

**``evidence`` 不在其列**：证据位要能指回客户原话（HARD-03 的判据就是「有没有原文」），
改写它会让锚点与原文对不上——治理英文残留不能以破坏可追溯为代价。
``confidence`` 是数值，同样不在其列。
"""

_ASCII_SPACE = " \t　"

_CHINESE_DELIMITERS = "：:、，,。；;！!？?「」『』（）()【】[]…·—\"'“”‘’"
"""允许紧贴枚举键的**中文标点**。

ASCII 空格不在其列：``smart 精灵`` 里的 ``smart`` 是品牌，``max 版`` 里的 ``max``
是版本名，用空格做左界会把它们当成枚举键替换掉。
"""


def _is_internal_key_shaped(token: str) -> bool:
    """键形态（``guard_against``）或英文占位串（``unknown``）才允许被空格包围时仍算键。"""
    return "_" in token or token.lower() in inf.ENGLISH_PLACEHOLDER_VALUES


def _replace_enum_keys(text: str, index: Mapping[str, str]) -> str:
    """把正文里的枚举键换成展示名；非键的英文原样保留。

    边界判据见 :data:`_CHINESE_DELIMITERS` 与 :func:`_is_internal_key_shaped`——
    这是「白名单成立」的前提：品牌名与车型名不受中文标点包裹时不会被改。
    """
    if not text or not index:
        return text
    out: list[str] = []
    position = 0
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9_]*", text):
        token = match.group(0)
        display = index.get(token)
        if display is None:
            continue
        start, end = match.span()
        left_ok = start == 0 or text[start - 1] in _CHINESE_DELIMITERS or (
            text[start - 1] in _ASCII_SPACE and _is_internal_key_shaped(token)
        )
        right_ok = end == len(text) or text[end] in _CHINESE_DELIMITERS or (
            text[end] in _ASCII_SPACE and _is_internal_key_shaped(token)
        )
        if not (left_ok and right_ok):
            continue
        out.append(text[position:start])
        out.append(display)
        position = end
    if not out:
        return text
    out.append(text[position:])
    return "".join(out)


def check_prose_keys(
    entries: Sequence[Any],
    *,
    display_index: Mapping[str, str] | None = None,
    is_placeholder: Any = None,
) -> list[Action]:
    """gate 阶段：把 Notes 正文里的枚举键改写成中文展示名。

    只改 ``reasoning_detail`` 的正文位，**不动 ``attribute_value``**——取值是
    契约枚举键，前端按 ``display_name`` 映射，改成中文会破坏回写（治理方案 §7）。

    ``is_placeholder`` 给出时，整条即将被 ``EXPR-03`` 删除的条目**跳过改写**：
    先改写再删除会留下两条留痕，把「删掉的条目」也算成一次英文命中。
    """
    # rule: EXPR-01 对客正文不得出现英文枚举键
    index = display_index if display_index is not None else inf.enum_display_index()
    actions: list[Action] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("attribute_key") or "")
        if is_placeholder is not None and is_placeholder(key, entry.get("attribute_value")):
            continue
        detail = entry.get("reasoning_detail")
        if not isinstance(detail, Mapping):
            continue
        replacement = dict(detail)
        changed = False
        for field in PROSE_FIELDS:
            value = detail.get(field)
            if isinstance(value, str):
                new_text = _replace_enum_keys(value, index)
                if new_text != value:
                    replacement[field] = new_text
                    changed = True
            elif isinstance(value, (list, tuple)):
                items = [
                    _replace_enum_keys(str(item), index) if isinstance(item, str) else item
                    for item in value
                ]
                if items != list(value):
                    replacement[field] = items
                    changed = True
        if not changed:
            continue
        actions.append(
            Action(
                rule="EXPR-01",
                attribute=key,
                action="rewrite",
                severity="repair",
                reason="正文含枚举键，已改写为中文展示名",
                stage="gate",
                target="note",
                payload={"reasoning_detail": replacement},
            )
        )
    return actions


def check_placeholders(
    entries: Sequence[Any], *, is_placeholder: Any
) -> list[Action]:
    """gate 阶段：英文占位串整条删除。

    ``is_placeholder`` 由调用方注入（``internal_fields.is_english_placeholder``）：
    判据要查合法枚举键，规则簇不自己抄一份契约知识。整条删除与 ``HARD-01`` 同口径——
    「付款方式：未知」对销售没有信息量，反而占位置。
    """
    # rule: EXPR-03 英文占位串整条去除
    actions: list[Action] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("attribute_key") or "")
        value = entry.get("attribute_value")
        if not is_placeholder(key, value):
            continue
        actions.append(
            Action(
                rule="EXPR-03",
                attribute=key,
                action="reject",
                severity="block",
                reason="取值为英文占位串，整条去除",
                stage="gate",
                target="note",
                value=value,
            )
        )
    return actions


def check_evidence(
    entries: Sequence[Any], *, is_conclusion: Any
) -> list[Action]:
    """gate 阶段：证据位不得出现结论型措辞。

    只删那一项证据、不动条目——证据位写脏了（把结论当依据），取值本身可能仍然成立。
    ``is_conclusion`` 由调用方注入：判定依赖散文句法（去掉结论部分后是否还有内容），
    实现留在投影层的文本工具里，规则簇只声明「这个位置上不该出现结论」。
    """
    # rule: EXPR-02 证据位不得出现结论型措辞
    actions: list[Action] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("attribute_key") or "")
        detail = entry.get("reasoning_detail")
        if not isinstance(detail, Mapping):
            continue
        for snippet in detail.get("evidence") or []:
            text = str(snippet)
            if is_conclusion(text) or "推断为" in text:
                actions.append(
                    Action(
                        rule="EXPR-02",
                        attribute=key,
                        action="reject",
                        severity="repair",
                        reason="证据位是结论而不是原文",
                        stage="gate",
                        target="evidence",
                        payload={"snippet": text},
                    )
                )
    return actions

