"""``（新）SubAgent - 人设特征判断`` 的 Python 定义。

DSL 基线：16 节点 / 19 边。链路是「以证据线索推断人设特征」，三路并行产出后合并：

    start(phone_number, evidence_items)
      ├─ template 人设特征推断 Prompt → llm 人设特征推断 结果聚合 → end(profile_result)
      └─ template 消费者风格推断Prompt → llm ┐
         template 兴趣&关注点推断Prompt → llm ┴→ variable-aggregator 特征结果聚合(分组)
                                                    └─ code 代码执行 → end(hobby/style)

**本轮的关键判断：两套并行分支里，保留 ``tool`` 那一支，删除纯 ``llm`` 那一支。**

DSL 用两个 ``if-else``（``1776760124715`` / ``17767603231790``）按 ``llm_model == "gemini"``
在「纯 llm 节点」与「``gemini_retry_2_times`` 工具节点」之间二选一。两条实现口径叠加：

- 有意差异 W4：``llm_model`` 入参按 §6.4.5 删除，两个 ``if-else`` 因此必须归一；
- 有意差异 W2/W3：``gemini_retry_2_times`` 工具节点归一为一次 ``llm_call()``。

归一到哪一支**不是任意的**，实测证据有两条：

1. 证据汇总图给本流程传的是字面量 ``llm_model: "gemini"``，即生产环境走的是 **true 分支**；
2. 两支的提示词**并不等价**——工具支多出一整条业务约束：
   「由于当前的客户社媒数据查询服务不稳定…不能因此得出『该客户不使用该社交媒体』」。
   纯 llm 支没有这条，且其 user 正文开头被误复制了 system 那句
   ``You are a helpful AI assistant.``，属较早的版本。

因此保留工具支（转换为 ``llm`` 节点，输出字段仍叫 ``result``），删除两个 ``if-else``
与三个纯 ``llm`` 节点。被删节点与边记入 ``tests/test_ledger_crosscheck.py`` 的差异表。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "profile_features_analysis"
DISPLAY_NAME = "（新）SubAgent - 人设特征判断"
SOURCE_DSL = f"{DISPLAY_NAME}.yml"

START = "1776670030415"
END = "1776677585621"

PROFILE_PROMPT = "1777427777301"
PROFILE_LLM = "1777427788971"
PROFILE_AGGREGATE = "1776760253638"

STYLE_PROMPT = "1777427950125"
STYLE_LLM = "1777427931123"

HOBBY_PROMPT = "1777427955726"
HOBBY_LLM = "1777427938519"

FEATURE_AGGREGATE = "1776760747042"
SPLIT_CODE = "1776760868761"

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
SLUG = "profile_features_analysis"
SYSTEM_TEXT = "You are a helpful AI assistant."

# 有意差异 W4 + W2/W3 删除的节点（DSL 有、迁移后没有）
REMOVED_NODES = (
    "1776760124715",  # 条件分支（llm_model 判定）
    "17767603231790",  # 条件分支 (1)（llm_model 判定）
    "17767601866080",  # 人设特征推断（纯 llm 支）
    "17767606554420",  # 消费者风格推断（纯 llm 支）
    "17767606912710",  # 兴趣&关注点推断（纯 llm 支）
)


REMOVED_EDGES = (
    # —— 指向被删节点，或由被删节点发出
    ("1776670030415", "1776760124715"),
    ("1776760124715", "17767601866080"),
    ("17767601866080", "1776760253638"),
    ("1776760253638", "17767603231790"),
    ("17767603231790", "17767606554420"),
    ("17767603231790", "17767606912710"),
    ("17767606554420", "1776760747042"),
    ("17767606912710", "1776760747042"),
    ("1776760124715", "1777427777301"),
    ("17767603231790", "1777427950125"),
    ("17767603231790", "1777427955726"),
)
"""因 ``if-else`` 归一而消失的边（DSL 的 sourceHandle 是 ``true``/``false``，
台账里按 ``source → target`` 记录）。"""

ADDED_EDGES = (
    # 分支删除后，原由分支串起来的顺序改用直接依赖表达
    ("1776670030415", "1777427777301"),
    ("1776760253638", "1777427950125"),
    ("1776760253638", "1777427955726"),
)
"""分支归一后新增的边：把「分支按条件选一支执行」改成「上游直接连到该支的首节点」。"""


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=SOURCE_DSL,
    entries=(START,),
    exits=(END,),
    outputs={
        "profile_result": "output",
        "hobby_result": "hobby_result",
        "style_result": "consumption_result",
    },
    nodes=(
        make_node(
            START,
            "用户输入",
            "start",
            # 有意差异 W4：llm_model 入参按 §6.4.5 删除
            variables=("phone_number", "evidence_items"),
            required_phone_number=True,
            required_evidence_items=True,
        ),
        make_node(
            PROFILE_PROMPT,
            "人设特征推断 Prompt",
            "template-transform",
            after=(START,),
            on_branch=("1776760124715", "true"),
            inputs={"evidence_items": (START, "evidence_items")},
            outputs=("output",),
            template=template_name(PROFILE_PROMPT),
            original_node_id=PROFILE_PROMPT,
        ),
        make_node(
            PROFILE_LLM,
            "人设特征推断 - 工具",
            "llm",
            after=(PROFILE_PROMPT,),
            inputs={"input_prompt": (PROFILE_PROMPT, "output")},
            outputs=("result",),
            prompt_field="input_prompt",
            output="result",
            system_text=SYSTEM_TEXT,
            original_node_id=PROFILE_LLM,
        ),
        make_node(
            PROFILE_AGGREGATE,
            "人设特征推断 结果聚合",
            "variable-aggregator",
            after=(PROFILE_LLM,),
            variables=((PROFILE_LLM, "result"),),
            outputs=("output",),
            original_node_id=PROFILE_AGGREGATE,
        ),
        make_node(
            STYLE_PROMPT,
            "消费者风格推断Prompt",
            "template-transform",
            after=(PROFILE_AGGREGATE,),
            on_branch=("17767603231790", "true"),
            inputs={
                "evidence_items": (START, "evidence_items"),
                "output": (PROFILE_AGGREGATE, "output"),
            },
            outputs=("output",),
            template=template_name(STYLE_PROMPT),
            original_node_id=STYLE_PROMPT,
        ),
        make_node(
            STYLE_LLM,
            "消费者风格推断 - 工具",
            "llm",
            after=(STYLE_PROMPT,),
            inputs={"input_prompt": (STYLE_PROMPT, "output")},
            outputs=("result",),
            prompt_field="input_prompt",
            output="result",
            system_text=SYSTEM_TEXT,
            original_node_id=STYLE_LLM,
        ),
        make_node(
            HOBBY_PROMPT,
            "兴趣&关注点推断Prompt",
            "template-transform",
            after=(PROFILE_AGGREGATE,),
            on_branch=("17767603231790", "true"),
            inputs={
                "evidence_items": (START, "evidence_items"),
                "output": (PROFILE_AGGREGATE, "output"),
            },
            outputs=("output",),
            template=template_name(HOBBY_PROMPT),
            original_node_id=HOBBY_PROMPT,
        ),
        make_node(
            HOBBY_LLM,
            "兴趣&关注点推断 - 工具",
            "llm",
            after=(HOBBY_PROMPT,),
            inputs={"input_prompt": (HOBBY_PROMPT, "output")},
            outputs=("result",),
            prompt_field="input_prompt",
            output="result",
            system_text=SYSTEM_TEXT,
            original_node_id=HOBBY_LLM,
        ),
        make_node(
            FEATURE_AGGREGATE,
            "特征结果聚合",
            "variable-aggregator",
            after=(STYLE_LLM, HOBBY_LLM),
            outputs=("hobby", "consumption"),
            groups=(
                {
                    "group_name": "hobby",
                    "output_type": "string",
                    "variables": ((HOBBY_LLM, "result"),),
                },
                {
                    "group_name": "consumption",
                    "output_type": "string",
                    "variables": ((STYLE_LLM, "result"),),
                },
            ),
            original_node_id=FEATURE_AGGREGATE,
        ),
        make_node(
            SPLIT_CODE,
            "代码执行",
            "code",
            after=(FEATURE_AGGREGATE,),
            inputs={
                "hobby_result_object": (FEATURE_AGGREGATE, "hobby"),
                "consumption_result_object": (FEATURE_AGGREGATE, "consumption"),
            },
            outputs=("consumption_result", "hobby_result"),
            function=f"{SHARED_CODE_MODULE}:split_grouped_features",
            original_node_id=SPLIT_CODE,
        ),
        make_node(
            END,
            "输出",
            "end",
            # DSL 里 END 的直接前置只有「代码执行」；profile_result 由 END 的
            # value_selector 跨节点引用获取，不构成边。
            after=(SPLIT_CODE,),
            inputs={
                "profile_result": (PROFILE_AGGREGATE, "output"),
                "hobby_result": (SPLIT_CODE, "hobby_result"),
                "style_result": (SPLIT_CODE, "consumption_result"),
            },
            outputs=("profile_result", "hobby_result", "style_result"),
            original_node_id=END,
        ),
    ),
)


def default_inputs(evidence_items: str, phone_number: str = "") -> dict[str, Any]:
    return {"phone_number": phone_number, "evidence_items": evidence_items}
