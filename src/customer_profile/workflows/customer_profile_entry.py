"""``（新）客户初始画像（生产环境）`` —— 全流程主入口的 Python 定义。

DSL 基线：6 节点 / 6 边。它是**编排图**：自己不做事，只按顺序调用三个子流程。

    start(phone_number, batch_id)
      └─ tool 证据线索汇总          → 得到 result（证据线索）与 original_data
           └─ tool 人设特征判断      → 得到 profile / hobby / style 三段画像
                └─ tool 画像内容生成&回写 → 得到最终 result1
                     └─ end(result1)

**有意差异 W1（已决策）**：DSL 里 ``画像内容生成&回写`` 有两个节点（``1777278748069``
与 ``1777361789855``），后者只由前者的 ``fail-branch`` 触发，两者参数完全相同。
Q3 的答复是「只保留一个节点，不执行两次」，因此删除 ``1777361789855`` 及其两条边，
入口图由 6 节点 / 6 边降为 **5 节点 / 4 边**。

**有意差异 W4**：传给子流程的 ``llm_model`` 参数一并删除。其中一处是字面量
``"gemini"``，另一处取自 ``start.llm_model``——都是同一处废弃参数的残留（§6.4.5）。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "customer_profile_entry"
DISPLAY_NAME = "（新）客户初始画像（生产环境）"
SOURCE_DSL = f"{DISPLAY_NAME}.yml"

START = "1776764752943"
EVIDENCE = "1777105195548"
PROFILE = "1776764933122"
PRODUCTION = "1777278748069"
END = "1776765012890"

# 有意差异 W1：冗余的第二个「画像内容生成&回写」节点
REMOVED_NODES = ("1777361789855",)
REMOVED_EDGES = (
    (PRODUCTION, "1777361789855"),
    ("1777361789855", END),
)

EVIDENCE_WORKFLOW = "evidence_subagent"
PROFILE_WORKFLOW = "profile_features_analysis"
PRODUCTION_WORKFLOW = "customer_profile_production"


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=SOURCE_DSL,
    entries=(START,),
    exits=(END,),
    outputs={"result1": "result1"},
    nodes=(
        make_node(
            START,
            "用户输入",
            "start",
            # 有意差异 W4：llm_model 入参按 §6.4.5 删除
            variables=("phone_number", "batch_id"),
            required_phone_number=True,
            required_batch_id=False,
            coords=(80.0, 282.0),
        ),
        make_node(
            EVIDENCE,
            "（新）SubAgent - 证据线索汇总（生产环境）",
            "tool",
            after=(START,),
            inputs={"phone_number": (START, "phone_number")},
            outputs=("result", "original_data"),
            **{
                "@workflow": EVIDENCE_WORKFLOW,
                "@inputs": {"phone_number": "phone_number"},
            },
            original_node_id=EVIDENCE,
            coords=(420.0, 282.0),
        ),
        make_node(
            PROFILE,
            "（新）SubAgent - 人设特征判断",
            "tool",
            after=(EVIDENCE,),
            inputs={
                "evidence_items": (EVIDENCE, "result"),
                "phone_number": (START, "phone_number"),
            },
            outputs=("profile_result", "hobby_result", "style_result"),
            **{
                "@workflow": PROFILE_WORKFLOW,
                "@inputs": {
                    "evidence_items": "evidence_items",
                    "phone_number": "phone_number",
                },
            },
            original_node_id=PROFILE,
            coords=(760.0, 282.0),
        ),
        make_node(
            PRODUCTION,
            "（新）SubAgent - 画像内容生成&回写（生产环境）（新）",
            "tool",
            after=(PROFILE,),
            inputs={
                "phone_number": (START, "phone_number"),
                "batch_id": (START, "batch_id"),
                "evidence_items": (EVIDENCE, "result"),
                "original_cus_data": (EVIDENCE, "original_data"),
                "profile_analysis": (PROFILE, "profile_result"),
                "hobby_analysis": (PROFILE, "hobby_result"),
                "consumption_style_analysis": (PROFILE, "style_result"),
            },
            outputs=("result1",),
            **{
                "@workflow": PRODUCTION_WORKFLOW,
                "@inputs": {
                    "phone_number": "phone_number",
                    "batch_id": "batch_id",
                    "evidence_items": "evidence_items",
                    "original_cus_data": "original_cus_data",
                    "profile_analysis": "profile_analysis",
                    "hobby_analysis": "hobby_analysis",
                    "consumption_style_analysis": "consumption_style_analysis",
                },
            },
            original_node_id=PRODUCTION,
            coords=(1100.0, 282.0),
        ),
        make_node(
            END,
            "输出",
            "end",
            after=(PRODUCTION,),
            inputs={"result1": (PRODUCTION, "result1")},
            outputs=("result1",),
            original_node_id=END,
            coords=(1440.0, 282.0),
        ),
    ),
)


def default_inputs(phone_number: str, batch_id: str = "") -> dict[str, Any]:
    """该工作流的对外入参契约：``phone_number`` 必填，``batch_id`` 可选。"""
    return {"phone_number": phone_number, "batch_id": batch_id}
