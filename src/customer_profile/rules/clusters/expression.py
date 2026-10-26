"""表达簇：对客产出的形态。

这一簇的规则作用于**已写好的文案**，时机在成文之后。它们记 ``content_kind=hard`` 而
不是另设一类：表达约束也是**形态**约束（不写内部字段名、证据不得自指），与取值合法性
同层。执行器据此把它们排在语义判断之前，先把脏形态清掉。

``EXPR-01``（英文表达与内部字段名）留到 M4 一并迁：它依赖一份一次性例外词表，而词表
的边界要先对着基线 8 案核过，否则会在 `Model Y`、`BMW` 这类合法英文上误伤。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..actions import Action
from ..registry import Rule

RULES: tuple[Rule, ...] = (
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
)


# ====================================================================
# 检查函数：只产出动作，不改任何状态
# ====================================================================


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
