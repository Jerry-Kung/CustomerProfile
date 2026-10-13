"""``录音文件内容抽取（纯识别，无加工）`` 的 Python 定义。

迁移前的定义基线：12 节点 / 11 边。被两个录音工作流各引用两次（单文件路径与迭代路径各一），
是子工作流复用最典型的例子。

链路：``start(file_url)`` → AUC 提交 → 取任务参数 → 轮询查询 → 识别结果提取 →
LLM 校对 → LLM 有效性判断 → 判断结果提取 →（有效 / 无效两支）→ end。

两条实现口径：

- **Q6 的轮询问题**已在迁移前的定义里澄清：``/query_analyze`` 的 ``max_retries: 5`` 是**单次
  请求内的重试**（既有 HTTP 客户端的重试语义），不是自建轮询循环。因此这里只按重试
  还原，不额外造轮询。
- AUC 服务无鉴权、``ssl_verify=false``（内网地址），由 ``@service`` 与
  ``@ssl_verify`` 承载。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "audio_content_extract"
DISPLAY_NAME = "录音文件内容抽取（纯识别，无加工）"
# 定义快照的 source_dsl 直接取这个显示名——原来那个指向 .yml 的追溯键已随
# 定义文件一并废弃，字段保留只为不动快照形状。

START = "1773112560827"
SUBMIT = "1773112855659"
PARSE_TASK = "17731967052740"
QUERY = "17731968880650"
EXTRACT_RESULT = "1773125494862"
PROOFREAD_LLM = "1773135369428"
VALIDITY_LLM = "17732152003520"
VALIDITY_CODE = "17732159935030"
VALIDITY_BRANCH = "1773215096599"
NO_CONTENT_TEMPLATE = "1776152619357"
END_VALID = "1773127141209"
END_INVALID = "1773216279486"
END_PURE = "1773112560828"

TRUE_BRANCH = "true"
ELSE_BRANCH = "false"

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
SLUG = "audio_content_extract"


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


PURE_WORKFLOW_ID = "audio_content_extract_pure"
"""v2 专用的纯识别变体：只跑 ASR 逐字识别，不做校对与有效性判断。

**为什么要一个变体而不是就地删节点**：本模块被基线臂的
``test_drive_audio`` / ``outbound_call_audio`` 复用（``audio_flow`` 的
``AUDIO_EXTRACT_SINGLE`` / ``AUDIO_EXTRACT_IN_ITERATION``）。就地删除两个 LLM 节点会让
基线臂的图结构改变，``definition_hash`` 漂移，AB 实验的可比性判据失效。变体作为**新的
workflow_id** 注册，与原件并存，回退只需改父图里的一个常量。

产物形状与原件对齐（同样交出 ``auc_result``），差别只在：

- **不做校对**：``file_content`` 直接给逐字原文，而不是「LLM 识别结果校对」的产物；
- **不做有效性判断**：因此不产出 ``is_valid``（那正是被关闭的那个节点的职责），
  也不会在内容判为无效时把原文替换成一句固定提示。
"""


def build_workflow(pure_recognition: bool = False) -> WorkflowDef:
    """构造录音抽取定义。

    ``pure_recognition=False``（缺省）产出迁移前的定义基线：12 节点 / 11 边，
    「识别结果提取 → LLM 校对 → LLM 有效性判断 → 判断结果提取 → 条件分支 → 两支 end」。
    这一支是**基线臂**在用的，逐字不得改动。

    ``pure_recognition=True`` 产出 v2 的纯识别变体：只保留
    ``start → 提交 → 取任务参数 → 查询 → 识别结果提取 → end``，**零 LLM 节点**。
    它交出的 ``auc_result`` 与原件逐字节相同（同一个「识别结果提取」节点产出），
    因此关闭那两个 LLM 节点**不改变任何被消费的产出**。
    """
    if pure_recognition:
        return _build_pure_workflow()
    return WorkflowDef(
        workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=DISPLAY_NAME,
    entries=(START,),
    exits=(END_VALID, END_INVALID),
    outputs={"is_valid": "is_valid", "file_content": "file_content"},
    nodes=(
        make_node(
            START,
            "用户输入",
            "start",
            variables=("file_url",),
            required_file_url=True,
        ),
        make_node(
            SUBMIT,
            "AUC语音大模型任务提交",
            "http-request",
            after=(START,),
            outputs=("body", "status"),
            **{
                "@service": "auc",
                "@method": "post",
                "@path": "/submit_analyze",
                "@headers": {"Content-Type": "application/json"},
                # 请求体逐字符取自迁移前的定义：引用会被替换成带引号的字符串
                "@body_template": '{ "file_url": {{#1773112560827.file_url#}} }',
                "@ssl_verify": False,
                "@outputs": {"body": "$text", "status": "$status"},
            },
        ),
        make_node(
            PARSE_TASK,
            "获取任务参数",
            "code",
            after=(SUBMIT,),
            inputs={"body": (SUBMIT, "body")},
            outputs=("task_id", "x_tt_logid"),
            function=f"{SHARED_CODE_MODULE}:parse_task_params",
        ),
        make_node(
            QUERY,
            "轮询语音识别结果",
            "http-request",
            after=(PARSE_TASK,),
            outputs=("body", "status"),
            **{
                "@service": "auc",
                "@method": "post",
                "@path": "/query_analyze",
                "@headers": {"Content-Type": "application/json"},
                "@body_template": (
                    '{"task_id": {{#17731967052740.task_id#}},'
                    ' "x_tt_logid": {{#17731967052740.x_tt_logid#}}}'
                ),
                # 迁移前的定义的取值 max_retries: 5 —— 单次请求内的重试，非轮询循环
                "@retry_max": 5,
                "@ssl_verify": False,
                "@outputs": {"body": "$text", "status": "$status"},
            },
        ),
        make_node(
            EXTRACT_RESULT,
            "识别结果提取",
            "code",
            after=(QUERY,),
            inputs={"body": (QUERY, "body")},
            outputs=("auc_result", "poll_count"),
            function=f"{SHARED_CODE_MODULE}:extract_auc_result",
        ),
        make_node(
            PROOFREAD_LLM,
            "LLM 识别结果校对",
            "llm",
            after=(EXTRACT_RESULT,),
            inputs={"auc_result": (EXTRACT_RESULT, "auc_result")},
            outputs=("text",),
            template=template_name(PROOFREAD_LLM),
            system_text="你是一个有用的AI助手",
        ),
        make_node(
            VALIDITY_LLM,
            "LLM 内容有效性判断",
            "llm",
            after=(PROOFREAD_LLM,),
            inputs={"text": (PROOFREAD_LLM, "text")},
            outputs=("text",),
            template=template_name(VALIDITY_LLM),
            system_text="你是一个有用的AI助手",
            # 下游 code 节点对它做 JSON 解析 → 按消费方语义要求 JSON（§6.4.2 / M2）
            expect_json=True,
        ),
        make_node(
            VALIDITY_CODE,
            "判断结果提取",
            "code",
            after=(VALIDITY_LLM,),
            inputs={"result": (VALIDITY_LLM, "text")},
            outputs=("is_valid",),
            function=f"{SHARED_CODE_MODULE}:extract_validity",
        ),
        make_node(
            VALIDITY_BRANCH,
            "条件分支",
            "if-else",
            after=(VALIDITY_CODE,),
            inputs={"is_valid": (VALIDITY_CODE, "is_valid")},
            branches=(
                {
                    "id": TRUE_BRANCH,
                    "condition": {"field": "is_valid", "operator": "is_true"},
                },
            ),
            else_id=ELSE_BRANCH,
        ),
        make_node(
            NO_CONTENT_TEMPLATE,
            "无有效内容提示",
            "template-transform",
            after=(VALIDITY_BRANCH,),
            on_branch=(VALIDITY_BRANCH, "false"),
            outputs=("output",),
            template=template_name(NO_CONTENT_TEMPLATE),
        ),
        make_node(
            END_VALID,
            "输出",
            "end",
            after=(VALIDITY_BRANCH,),
            on_branch=(VALIDITY_BRANCH, "true"),
            inputs={
                "is_valid": (VALIDITY_CODE, "is_valid"),
                "file_content": (PROOFREAD_LLM, "text"),
                # 逐字原文（ASR 六字段数组文本）。取值自「识别结果提取」，**不经校对 LLM**。
                # 供 L0 记录层留存原文用：`file_content` 是校对后的产物，且内容判为无效时
                # 会被替换成一句固定提示，逐字原话在那条路径上会整个丢失。
                "auc_result": (EXTRACT_RESULT, "auc_result"),
            },
            outputs=("is_valid", "file_content", "auc_result"),
        ),
        make_node(
            END_INVALID,
            "输出（无有效内容）",
            "end",
            after=(NO_CONTENT_TEMPLATE,),
            inputs={
                "is_valid": (VALIDITY_CODE, "is_valid"),
                "file_content": (NO_CONTENT_TEMPLATE, "output"),
                # 同上：内容判无效也要留下原文，否则「输入有录音、输出却说无录音」无法纠正。
                "auc_result": (EXTRACT_RESULT, "auc_result"),
            },
            outputs=("is_valid", "file_content", "auc_result"),
        ),
    ),
    )


def _build_pure_workflow() -> WorkflowDef:
    """纯识别变体：零 LLM 节点，只产出逐字原文。

    ``file_content`` 与 ``auc_result`` 同源（都取「识别结果提取」的产出）：
    前者供 ``audio_flow`` 的父节点消费（它按 ``file_content`` 取录音正文），
    后者供事实账本管线消费（它按 ``auc_result`` 取逐字原文）。
    **两者在此处指向同一份逐字文本**，正是「关掉的是压缩、不是产出」的体现。
    """
    return WorkflowDef(
        workflow_id=PURE_WORKFLOW_ID,
        display_name=f"{DISPLAY_NAME}（纯识别）",
        source_dsl=DISPLAY_NAME,
        entries=(START,),
        exits=(END_PURE,),
        outputs={"file_content": "file_content", "auc_result": "auc_result"},
        nodes=(
            make_node(
                START,
                "用户输入",
                "start",
                variables=("file_url",),
                required_file_url=True,
            ),
            make_node(
                SUBMIT,
                "AUC语音大模型任务提交",
                "http-request",
                after=(START,),
                outputs=("body", "status"),
                **{
                    "@service": "auc",
                    "@method": "post",
                    "@path": "/submit_analyze",
                    "@headers": {"Content-Type": "application/json"},
                    "@body_template": '{ "file_url": {{#1773112560827.file_url#}} }',
                    "@ssl_verify": False,
                    "@outputs": {"body": "$text", "status": "$status"},
                },
            ),
            make_node(
                PARSE_TASK,
                "获取任务参数",
                "code",
                after=(SUBMIT,),
                inputs={"body": (SUBMIT, "body")},
                outputs=("task_id", "x_tt_logid"),
                function=f"{SHARED_CODE_MODULE}:parse_task_params",
            ),
            make_node(
                QUERY,
                "轮询语音识别结果",
                "http-request",
                after=(PARSE_TASK,),
                outputs=("body", "status"),
                **{
                    "@service": "auc",
                    "@method": "post",
                    "@path": "/query_analyze",
                    "@headers": {"Content-Type": "application/json"},
                    "@body_template": (
                        '{"task_id": {{#17731967052740.task_id#}},'
                        ' "x_tt_logid": {{#17731967052740.x_tt_logid#}}}'
                    ),
                    "@retry_max": 5,
                    "@ssl_verify": False,
                    "@outputs": {"body": "$text", "status": "$status"},
                },
            ),
            make_node(
                EXTRACT_RESULT,
                "识别结果提取",
                "code",
                after=(QUERY,),
                inputs={"body": (QUERY, "body")},
                outputs=("auc_result", "poll_count"),
                function=f"{SHARED_CODE_MODULE}:extract_auc_result",
            ),
            make_node(
                END_PURE,
                "输出",
                "end",
                after=(EXTRACT_RESULT,),
                inputs={
                    "file_content": (EXTRACT_RESULT, "auc_result"),
                    "auc_result": (EXTRACT_RESULT, "auc_result"),
                },
                outputs=("file_content", "auc_result"),
            ),
        ),
    )


WORKFLOW: WorkflowDef = build_workflow(False)
"""迁移前的定义基线（12 节点）。**基线臂在用，逐字不得改动。**"""

PURE_WORKFLOW: WorkflowDef = build_workflow(True)
"""v2 纯识别变体（6 节点、零 LLM）。事实账本管线的录音链在用它。"""

SUBWORKFLOWS: tuple[WorkflowDef, ...] = (PURE_WORKFLOW,)
"""本模块导出的子工作流。``load_all`` 会把它们一并注册。"""


def default_inputs(file_url: str) -> dict[str, Any]:
    return {"file_url": file_url}
