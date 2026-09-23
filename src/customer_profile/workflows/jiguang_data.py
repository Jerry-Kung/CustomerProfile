"""``（新）SubAgent - 极光数据（生产环境）`` 的 Python 定义。

DSL 基线：7 节点 / 7 边，是结构最简单的一类子流程——「取一段数据，取到就原样输出，
取不到给一句固定说明」。它也把迁移里的一条口径摆得最清楚：**空串是合法产出**。
分支判据是 ``result not empty``，而 falsy 的兜底分支同样会走到聚合器，因此聚合器必须
按「谁真正执行过」取值，不能按「值非空」取值（P4）。

    start(customer_data, data_source)
      └─ code 检查是否存在极光数据结果
           └─ if-else 条件分支
                ├─ true  → template 模板转换 (1)  ┐
                └─ else  → template 模板转换      ┴→ variable-aggregator → end
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "jiguang_data"
DISPLAY_NAME = "（新）SubAgent - 极光数据（生产环境）"
SOURCE_DSL = f"{DISPLAY_NAME}.yml"

START = "1776649712409"
END = "1776649751425"
NO_DATA_TEMPLATE = "1777286944456"
CHECK_CODE = "1777288063102"
BRANCH = "1777288172337"
PASS_TEMPLATE = "17772881985510"
AGGREGATE = "1777288211670"

TRUE_BRANCH = "true"
ELSE_BRANCH = "false"

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
SLUG = "jiguang_data"


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=SOURCE_DSL,
    entries=(START,),
    exits=(END,),
    outputs={"result": "output"},
    nodes=(
        make_node(
            START,
            "用户输入",
            "start",
            # 有意差异 W4：llm_model 入参按 §6.4.5 删除
            variables=("phone_number", "customer_data", "data_source"),
            required_phone_number=True,
            required_customer_data=True,
            required_data_source=False,
        ),
        make_node(
            NO_DATA_TEMPLATE,
            "模板转换",
            "template-transform",
            after=(BRANCH,),
            on_branch=(BRANCH, "false"),
            outputs=("output",),
            template=template_name(NO_DATA_TEMPLATE),
            original_node_id=NO_DATA_TEMPLATE,
        ),
        make_node(
            CHECK_CODE,
            "检查是否存在极光数据结果",
            "code",
            after=(START,),
            inputs={
                "records": (START, "customer_data"),
                "data_source": (START, "data_source"),
            },
            outputs=("result",),
            function=f"{SHARED_CODE_MODULE}:extract_jiguang_tags",
            original_node_id=CHECK_CODE,
        ),
        make_node(
            BRANCH,
            "条件分支",
            "if-else",
            after=(CHECK_CODE,),
            inputs={"result": (CHECK_CODE, "result")},
            outputs=("result",),
            branches=(
                {
                    "id": TRUE_BRANCH,
                    "condition": {"field": "result", "operator": "not_empty"},
                    "output": "result",
                },
            ),
            else_id=ELSE_BRANCH,
            original_node_id=BRANCH,
        ),
        make_node(
            PASS_TEMPLATE,
            "模板转换 (1)",
            "template-transform",
            after=(BRANCH,),
            on_branch=(BRANCH, "true"),
            inputs={"result": (CHECK_CODE, "result")},
            outputs=("output",),
            template=template_name(PASS_TEMPLATE),
            original_node_id=PASS_TEMPLATE,
        ),
        make_node(
            AGGREGATE,
            "变量聚合器",
            "variable-aggregator",
            after=(PASS_TEMPLATE, NO_DATA_TEMPLATE),
            variables=((PASS_TEMPLATE, "output"), (NO_DATA_TEMPLATE, "output")),
            outputs=("output",),
            original_node_id=AGGREGATE,
        ),
        make_node(
            END,
            "输出",
            "end",
            after=(AGGREGATE,),
            inputs={"result": (AGGREGATE, "output")},
            outputs=("result",),
            original_node_id=END,
        ),
    ),
)


def default_inputs(customer_data: str, phone_number: str = "") -> dict[str, Any]:
    return {
        "phone_number": phone_number,
        "customer_data": customer_data,
        "data_source": "jiguang",
    }
