"""截图类工作流（7 个）的共同结构与定义构造器。

七个截图流程——朋友圈、微信主页、微信手机号搜索、抖音、小红书、支付宝——的**图结构
逐节点相同**，只有各自的渠道名（``data_source``）、提示词正文与显示名不同。DSL 里它们
是七份各自独立的导出文件，迁移后应当是**一个构造器 + 七份参数**，而不是七份复制粘贴的
定义：那样任何一处修正都得改七遍。

图结构（节点 ID 沿用 DSL 原值，七个文件里也是同一套 ID）：

    start(customer_data, data_source)
      ├─ code 检查文件 → if-else 文件是否存在
      │     ├─ true 分支 ─┬─ code 数组格式转换 → if-else 是否存在多个文件
      │     │             │     ├─ true  → iteration(逐张取图 → vision 解析)
      │     │             │     └─ false → code 解析图片url → http 获取图片 → vision 解析
      │     │             └─ ...
      │     └─ false 分支 → template-transform 无数据，输出默认信息
      └─ variable-aggregator → end

``模板转换 多张图片内容聚合`` 与 ``多图片内容总结`` 只挂在迭代侧。
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
VISION_SINGLE = "1775717096663"
PARSE_URL_CODE = "1775717470046"
FETCH_IMAGE = "1775807707663"
ITERATION = "1775811095168"
ITERATION_START = "1775811095168start"
VISION_IN_ITERATION = "1775811114840"
FETCH_IMAGE_IN_ITERATION = "1775811132789"
AGGREGATE_2 = "1775811233890"
JOIN_TEMPLATE = "1775811655657"
SUMMARY_LLM = "17760451317570"

# 分支 ID：DSL 的 if-else 只有 true 一支，其余走 else
TRUE_BRANCH = "true"
ELSE_BRANCH = "false"

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
TEMPLATE_PREFIX = "customer_profile.templates"


@dataclass(frozen=True, slots=True)
class ScreenshotFlow:
    """一个截图流程的可变部分。"""

    slug: str
    """模块与模板资源用的稳定标识，对应 ``scripts/export_templates.py`` 的 SLUGS。"""

    display_name: str
    """中文显示名，与 DSL 文件名一致。"""

    data_source: str
    """渠道名。传给「检查文件」节点，决定从 history 响应里取哪一条记录。"""

    workflow_id: str
    """Python 侧的工作流 ID（英文）。"""


def template_name(slug: str, node_id: str) -> str:
    """模板资源文件名（不含扩展名）。与 ``scripts/export_templates.py`` 的命名一致。"""
    return f"{slug}__{node_id}"


def build_screenshot_workflow(flow: ScreenshotFlow) -> WorkflowDef:
    """按共用图结构构造一个截图工作流的定义。"""
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
            coords=(80.0, 423.0),
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
            "检查文件",
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
            "文件是否存在",
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
            inputs={"images": (CHECK_CODE, "result")},
            outputs=("images", "count"),
            function=f"{SHARED_CODE_MODULE}:parse_media_url_array",
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
            VISION_SINGLE,
            "内容解析",
            "llm",
            after=(FETCH_IMAGE,),
            outputs=("text",),
            template=template(VISION_SINGLE),
            images_selector=(FETCH_IMAGE, "files"),
            image_detail="high",
            original_node_id=VISION_SINGLE,
        ),
        make_node(
            PARSE_URL_CODE,
            "解析图片url",
            "code",
            after=(MULTI_BRANCH,),
            on_branch=(MULTI_BRANCH, "false"),
            inputs={"images": (ARRAY_CODE, "images")},
            outputs=("image_url",),
            function=f"{SHARED_CODE_MODULE}:first_image_url",
            original_node_id=PARSE_URL_CODE,
        ),
        make_node(
            FETCH_IMAGE,
            "获取图片",
            "http-request",
            after=(PARSE_URL_CODE,),
            inputs={"@url": (PARSE_URL_CODE, "image_url")},
            outputs=("files", "body", "status"),
            **{
                "@method": "get",
                "@url": "",
                "@auth": False,
                "@ssl_verify": True,
                "@outputs": {"files": "$files", "body": "$text", "status": "$status"},
            },
            original_node_id=FETCH_IMAGE,
        ),
        make_node(
            ITERATION,
            "迭代",
            "iteration",
            after=(MULTI_BRANCH,),
            on_branch=(MULTI_BRANCH, "true"),
            # ``item`` 供循环体内的 http 节点取当前项；``output`` 是聚合结果
            outputs=("output", "item"),
            # 有意差异 P1：is_parallel=false 是权威信号，逐项串行
            iterator=(ARRAY_CODE, "images"),
            # 每项收集哪个字段：必须以 ``@output`` **绑定**的形式声明，
            # ``config["output"]`` 是聚合结果的字段名，两者不是一回事。
            inputs={"@output": (VISION_IN_ITERATION, "text")},
            body=(ITERATION_START, FETCH_IMAGE_IN_ITERATION, VISION_IN_ITERATION),
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
            VISION_IN_ITERATION,
            "内容解析",
            "llm",
            after=(FETCH_IMAGE_IN_ITERATION,),
            outputs=("text",),
            template=template(VISION_IN_ITERATION),
            images_selector=(FETCH_IMAGE_IN_ITERATION, "files"),
            image_detail="high",
            iteration_id=ITERATION,
            original_node_id=VISION_IN_ITERATION,
        ),
        make_node(
            FETCH_IMAGE_IN_ITERATION,
            "获取图片",
            "http-request",
            after=(ITERATION_START,),
            inputs={"@url": (ITERATION, "item")},
            outputs=("files", "body", "status"),
            **{
                "@method": "get",
                "@url": "",
                "@auth": False,
                "@ssl_verify": False,
                "@outputs": {"files": "$files", "body": "$text", "status": "$status"},
            },
            iteration_id=ITERATION,
            original_node_id=FETCH_IMAGE_IN_ITERATION,
        ),
        make_node(
            AGGREGATE_2,
            "变量聚合器 2",
            "variable-aggregator",
            after=(SUMMARY_LLM, VISION_SINGLE),
            variables=((SUMMARY_LLM, "text"), (VISION_SINGLE, "text")),
            outputs=("output",),
            original_node_id=AGGREGATE_2,
        ),
        make_node(
            JOIN_TEMPLATE,
            "多张图片内容聚合",
            "template-transform",
            after=(ITERATION,),
            inputs={"arr": (ITERATION, "output")},
            outputs=("output",),
            template=template(JOIN_TEMPLATE),
            original_node_id=JOIN_TEMPLATE,
        ),
        make_node(
            SUMMARY_LLM,
            "多图片内容总结",
            "llm",
            after=(JOIN_TEMPLATE,),
            # 提示词正文里有 ``{{#1775811655657.output#}}``（多图聚合结果），必须绑定，
            # 否则渲染期报「模板变量未提供」。绑定用 ``@`-less 的普通名即可——
            # 渲染时变量表按 **来源** ``node_id.field`` 建键，目标名不参与匹配。
            inputs={"arr": (JOIN_TEMPLATE, "output")},
            outputs=("text",),
            template=template(SUMMARY_LLM),
            original_node_id=SUMMARY_LLM,
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
    flow: ScreenshotFlow, customer_data: str, phone_number: str = ""
) -> dict[str, Any]:
    """构造典型入参，供测试与固定响应使用。"""
    return {
        "phone_number": phone_number,
        "customer_data": customer_data,
        "data_source": flow.data_source,
    }
