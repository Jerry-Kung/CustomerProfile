"""知识库簇：在售车型清单对字段归属的硬约束。

本簇只有一条规则。它不是一个「口径」而是一条**结构性约束**：意向车型的取值域由
合作方的在售车型清单决定，清单变了取值域就跟着变——这正是知识库存在的意义。

放成规则而不是写死在代码里，是为了三件事：可单独关闭（``RULES_DISABLED=KB-02``）、
可留痕（每次改投或剔除都有 trace）、可统计（``scripts/rules_report.py`` 能看到它
到底有没有在生效）。
"""

from __future__ import annotations

from ..registry import Rule

CLUSTER = "knowledge"

RULES: tuple[Rule, ...] = (
    Rule(
        id="KB-02",
        version=1,
        title="意向车型必须取自本方在售清单",
        content_kind="hard",
        channels=("code",),
        scope=("intended_model",),
        stages=("project",),
        severity="repair",
        default_action="require_confirm",
        message_internal=(
            "客户提到的车型不在本方在售清单内：竞品改投对比车型位；本方未在售型号与"
            "完全陌生的称谓从意向位移出并转人工确认（不擅自并入竞品位）。"
        ),
        message_customer="客户提到的这台车不在当前在售车型中，建议确认它是意向车型、对比车型还是已有车辆",
        owner="business",
        status="active",
        cluster=CLUSTER,
        tests=(
            "tests/test_knowledge_intent.py::test_competitor_model_is_rerouted",
            "tests/test_knowledge_intent.py::test_unknown_name_is_not_forced_into_competing",
        ),
    ),
)
