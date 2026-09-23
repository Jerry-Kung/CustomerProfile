"""``录音文件内容抽取（纯识别，无加工）`` 的 Python 定义。

DSL 基线：12 节点 / 11 边。被两个录音工作流各引用两次（单文件路径与迭代路径各一），
是子工作流复用最典型的例子（见规划 §1.2）。

链路：``start(file_url)`` → AUC 提交 → 取任务参数 → 轮询查询 → 识别结果提取 →
LLM 校对 → LLM 有效性判断 → 判断结果提取 →（有效 / 无效两支）→ end。

两条实现口径：

- **Q6 的轮询问题**已在 DSL 里澄清：``/query_analyze`` 的 ``max_retries: 5`` 是**单次
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
SOURCE_DSL = f"{DISPLAY_NAME}.yml"

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

TRUE_BRANCH = "true"
ELSE_BRANCH = "false"

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
SLUG = "audio_content_extract"


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=SOURCE_DSL,
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
            coords=(80.0, 282.0),
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
                # 请求体逐字符取自 DSL：引用会被替换成带引号的字符串
                "@body_template": '{ "file_url": {{#1773112560827.file_url#}} }',
                "@ssl_verify": False,
                "@outputs": {"body": "$text", "status": "$status"},
            },
            original_node_id=SUBMIT,
        ),
        make_node(
            PARSE_TASK,
            "获取任务参数",
            "code",
            after=(SUBMIT,),
            inputs={"body": (SUBMIT, "body")},
            outputs=("task_id", "x_tt_logid"),
            function=f"{SHARED_CODE_MODULE}:parse_task_params",
            original_node_id=PARSE_TASK,
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
                # DSL 原值 max_retries: 5 —— 单次请求内的重试，非轮询循环（Q6）
                "@retry_max": 5,
                "@ssl_verify": False,
                "@outputs": {"body": "$text", "status": "$status"},
            },
            original_node_id=QUERY,
        ),
        make_node(
            EXTRACT_RESULT,
            "识别结果提取",
            "code",
            after=(QUERY,),
            inputs={"body": (QUERY, "body")},
            outputs=("auc_result", "poll_count"),
            function=f"{SHARED_CODE_MODULE}:extract_auc_result",
            original_node_id=EXTRACT_RESULT,
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
            original_node_id=PROOFREAD_LLM,
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
            original_node_id=VALIDITY_LLM,
        ),
        make_node(
            VALIDITY_CODE,
            "判断结果提取",
            "code",
            after=(VALIDITY_LLM,),
            inputs={"result": (VALIDITY_LLM, "text")},
            outputs=("is_valid",),
            function=f"{SHARED_CODE_MODULE}:extract_validity",
            original_node_id=VALIDITY_CODE,
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
            original_node_id=VALIDITY_BRANCH,
        ),
        make_node(
            NO_CONTENT_TEMPLATE,
            "模板转换",
            "template-transform",
            after=(VALIDITY_BRANCH,),
            on_branch=(VALIDITY_BRANCH, "false"),
            outputs=("output",),
            template=template_name(NO_CONTENT_TEMPLATE),
            original_node_id=NO_CONTENT_TEMPLATE,
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
            },
            outputs=("is_valid", "file_content"),
            original_node_id=END_VALID,
        ),
        make_node(
            END_INVALID,
            "输出 2",
            "end",
            after=(NO_CONTENT_TEMPLATE,),
            inputs={
                "is_valid": (VALIDITY_CODE, "is_valid"),
                "file_content": (NO_CONTENT_TEMPLATE, "output"),
            },
            outputs=("is_valid", "file_content"),
            original_node_id=END_INVALID,
        ),
    ),
)


def default_inputs(file_url: str) -> dict[str, Any]:
    return {"file_url": file_url}
