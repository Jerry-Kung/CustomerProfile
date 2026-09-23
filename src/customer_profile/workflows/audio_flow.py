"""录音类工作流（2 个）的共同结构与定义构造器。

试驾录音与外呼录音的图结构**逐节点相同**，只有渠道名（``data_source``）与提示词正文
不同，因此与截图类同样处理：一个构造器 + 两份参数。

图结构（节点 ID 沿用 DSL 原值，两个文件里是同一套）：

    start(customer_data, data_source)
      ├─ code 检查录音文件 → if-else 文件是否存在
      │     ├─ true 分支 ─┬─ code 数组格式转换 → if-else 是否存在多个文件
      │     │             │     ├─ true  → iteration(tool 录音抽取 → ...)
      │     │             │     └─ false → code 解析录音url → tool 录音抽取 → if-else(废弃的llm_model分支)
      │     │             │                                              └→ llm 录音文件分析
      │     │             └─ (true 分支的 llm 汇总略)
      │     └─ false 分支 → template-transform 无数据，输出默认信息
      └─ variable-aggregator → end

``llm_model`` 相关的两个 ``if-else``（DSL 的 ``条件分支 3``）按 §6.4.5 已废弃：
迁移后删除该分支，任选一支为默认路径——原值恒为 ``gemini``，两支差异仅是模型选择，
而模型在全项目已统一。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..definitions import WorkflowDef, make_node

# ---------------------------------------------------------------- 共用节点 ID

START = "1775641349605"
NO_DATA_TEMPLATE = "1775641360812"
CHECK_CODE = "1775702981484"
EXISTS_BRANCH = "1775703027681"
AGGREGATE = "1775703104830"
END = "1775703174717"
ARRAY_CODE = "1775705336479"
MULTI_BRANCH = "1775717005494"
PARSE_URL_CODE = "1775717470046"
ITERATION = "1775811095168"
ITERATION_START = "1775811095168start"
AGGREGATE_2 = "1775811233890"
JOIN_TEMPLATE = "1775811655657"
AUDIO_LLM = "17760451317570"
AUDIO_EXTRACT_SINGLE = "1776135353554"
AUDIO_EXTRACT_IN_ITERATION = "1776152239207"

# 条件分支 3（废弃的 llm_model 分支）：两个录音工作流各一个，节点 ID 不同
DEPRECATED_BRANCH_TEST_DRIVE = "1776763992027"
DEPRECATED_BRANCH_OUTBOUND = "1776764239626"
# 分支两侧的 llm 节点：迁移后只保留一支，另一支不再出现在定义中
LLM_AFTER_DEPRECATED_BRANCH_TEST_DRIVE = "17767640208110"
LLM_AFTER_DEPRECATED_BRANCH_OUTBOUND = "17767643097270"
# 结果聚合节点的两个不同 ID
AGGREGATE_LLM_TEST_DRIVE = "1776764121079"
AGGREGATE_LLM_OUTBOUND = "1776764354171"
# 被删除的 Gemini 侧 llm 节点（走 llm_model == gemini 那一支）
LLM_GEMINI_TEST_DRIVE = "1776149288520"
LLM_GEMINI_OUTBOUND = "1776149288520"

TRUE_BRANCH = "true"
ELSE_BRANCH = "false"

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
AUDIO_EXTRACT_WORKFLOW_ID = "audio_content_extract"


@dataclass(frozen=True, slots=True)
class AudioFlow:
    """一个录音流程的可变部分。"""

    slug: str
    display_name: str
    data_source: str
    workflow_id: str
    check_title: str
    exists_title: str
    llm_title: str
    aggregate_llm_id: str
    aggregate_llm_title: str
    deprecated_branch_id: str
    discarded_llm_id: str
    kept_llm_id: str


def template_name(slug: str, node_id: str) -> str:
    return f"{slug}__{node_id}"


def build_audio_workflow(flow: AudioFlow) -> WorkflowDef:
    """按共用图结构构造一个录音工作流的定义。"""
    slug = flow.slug
    template = lambda node_id: template_name(slug, node_id)  # noqa: E731

    nodes = (
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
            "无数据，输出默认信息",
            "template-transform",
            after=(EXISTS_BRANCH,),
            on_branch=(EXISTS_BRANCH, "false"),
            outputs=("output",),
            template=template(NO_DATA_TEMPLATE),
            original_node_id=NO_DATA_TEMPLATE,
        ),
        make_node(
            CHECK_CODE,
            flow.check_title,
            "code",
            after=(START,),
            inputs={
                "records": (START, "customer_data"),
                "data_source": (START, "data_source"),
            },
            outputs=("result",),
            function=f"{SHARED_CODE_MODULE}:extract_media_urls",
            original_node_id=CHECK_CODE,
        ),
        make_node(
            EXISTS_BRANCH,
            flow.exists_title,
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
            original_node_id=EXISTS_BRANCH,
        ),
        make_node(
            AGGREGATE,
            "变量聚合器",
            "variable-aggregator",
            after=(NO_DATA_TEMPLATE, AGGREGATE_2),
            variables=((NO_DATA_TEMPLATE, "output"), (AGGREGATE_2, "output")),
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
        make_node(
            ARRAY_CODE,
            "数组格式转换",
            "code",
            after=(EXISTS_BRANCH,),
            on_branch=(EXISTS_BRANCH, "true"),
            inputs={"files": (CHECK_CODE, "result")},
            outputs=("files", "count"),
            function=f"{SHARED_CODE_MODULE}:parse_file_array",
            original_node_id=ARRAY_CODE,
        ),
        make_node(
            MULTI_BRANCH,
            "是否存在多个文件",
            "if-else",
            after=(ARRAY_CODE,),
            inputs={"count": (ARRAY_CODE, "count")},
            branches=(
                {
                    "id": TRUE_BRANCH,
                    "condition": {"field": "count", "operator": "ne", "value": 1},
                },
            ),
            else_id=ELSE_BRANCH,
            original_node_id=MULTI_BRANCH,
        ),
        make_node(
            PARSE_URL_CODE,
            "解析录音url",
            "code",
            after=(MULTI_BRANCH,),
            on_branch=(MULTI_BRANCH, "false"),
            inputs={"files": (ARRAY_CODE, "files")},
            outputs=("file_url",),
            function=f"{SHARED_CODE_MODULE}:first_file_url",
            original_node_id=PARSE_URL_CODE,
        ),
        make_node(
            ITERATION,
            "迭代",
            "iteration",
            after=(MULTI_BRANCH,),
            on_branch=(MULTI_BRANCH, "true"),
            outputs=("output", "item"),
            iterator=(ARRAY_CODE, "files"),
            # 见 screenshot_flow 的说明：收集字段走 ``@output`` 绑定
            inputs={"@output": (AUDIO_EXTRACT_IN_ITERATION, "file_content")},
            body=(ITERATION_START, AUDIO_EXTRACT_IN_ITERATION),
            flatten_output=True,
            error_handle_mode="terminated",
            original_node_id=ITERATION,
        ),
        make_node(
            ITERATION_START,
            "",
            "iteration-start",
            outputs=("item", "index"),
            iteration_id=ITERATION,
            original_node_id=ITERATION_START,
        ),
        make_node(
            AUDIO_EXTRACT_IN_ITERATION,
            "录音文件内容抽取（纯识别，无加工）",
            "tool",
            after=(ITERATION_START,),
            inputs={"file_url": (ITERATION, "item")},
            outputs=("file_content",),
            **{
                "@workflow": AUDIO_EXTRACT_WORKFLOW_ID,
                "@inputs": {"file_url": "file_url"},
            },
            iteration_id=ITERATION,
            original_node_id=AUDIO_EXTRACT_IN_ITERATION,
        ),
        make_node(
            JOIN_TEMPLATE,
            "多文件内容聚合",
            "template-transform",
            after=(ITERATION,),
            inputs={"arr": (ITERATION, "output")},
            outputs=("output",),
            template=template(JOIN_TEMPLATE),
            original_node_id=JOIN_TEMPLATE,
        ),
        make_node(
            AUDIO_LLM,
            "录音文件分析",
            "llm",
            after=(JOIN_TEMPLATE,),
            # 正文引用 ``{{#1775811655657.output#}}``（多文件聚合结果）。必须绑定：
            # 未绑定时渲染期报「模板变量未提供」——该路径在 AUC 不可达期间从未被执行，
            # 因此直到 2026-09-23 真实录音冒烟才暴露。
            inputs={"arr": (JOIN_TEMPLATE, "output")},
            outputs=("text",),
            template=template(AUDIO_LLM),
            original_node_id=AUDIO_LLM,
        ),
        make_node(
            AUDIO_EXTRACT_SINGLE,
            "录音文件内容抽取（纯识别，无加工）",
            "tool",
            after=(PARSE_URL_CODE,),
            inputs={"file_url": (PARSE_URL_CODE, "file_url")},
            outputs=("file_content",),
            **{
                "@workflow": AUDIO_EXTRACT_WORKFLOW_ID,
                "@inputs": {"file_url": "file_url"},
            },
            original_node_id=AUDIO_EXTRACT_SINGLE,
        ),
        # 有意差异 W4：条件分支 3（llm_model 分支）已废弃，删除该 if-else，
        # 直接连到「非 gemini」那一支的 llm（两支差异仅模型选择）。
        make_node(
            flow.kept_llm_id,
            flow.llm_title,
            "llm",
            after=(AUDIO_EXTRACT_SINGLE,),
            # 正文引用 ``{{#1776135353554.file_content#}}``（单文件录音抽取结果）。
            # 与上面的 AUDIO_LLM 同因：未绑定即渲染失败。真实录音冒烟实测到
            # ``变量解析失败："模板变量 '1776135353554.file_content' 未提供；已提供：[]"``。
            inputs={"content": (AUDIO_EXTRACT_SINGLE, "file_content")},
            outputs=("text",),
            template=template(flow.kept_llm_id),
            original_node_id=flow.kept_llm_id,
        ),
        make_node(
            flow.aggregate_llm_id,
            flow.aggregate_llm_title,
            "variable-aggregator",
            after=(AUDIO_LLM, flow.kept_llm_id),
            variables=((AUDIO_LLM, "text"), (flow.kept_llm_id, "text")),
            outputs=("output",),
            original_node_id=flow.aggregate_llm_id,
        ),
        make_node(
            AGGREGATE_2,
            "变量聚合器 2",
            "variable-aggregator",
            after=(AUDIO_LLM, flow.aggregate_llm_id),
            variables=((AUDIO_LLM, "text"), (flow.aggregate_llm_id, "output")),
            outputs=("output",),
            original_node_id=AGGREGATE_2,
        ),
    )

    return WorkflowDef(
        workflow_id=flow.workflow_id,
        display_name=flow.display_name,
        source_dsl=f"{flow.display_name}.yml",
        entries=(START,),
        exits=(END,),
        outputs={"result": "output"},
        nodes=nodes,
    )


def default_inputs(
    flow: AudioFlow, customer_data: str, phone_number: str = ""
) -> dict[str, Any]:
    return {
        "phone_number": phone_number,
        "customer_data": customer_data,
        "data_source": flow.data_source,
    }
