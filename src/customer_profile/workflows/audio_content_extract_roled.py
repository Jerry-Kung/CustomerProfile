"""``录音内容角色标注`` —— 带说话方角色标注的录音抽取子流程（v2 专用）。

与 :mod:`audio_content_extract` 的关系：图的前半段（提交 → 查询 → 识别结果提取）与本
变体完全相同，差别只在末尾多了一次 LLM 调用与一次代码展开：

    start(file_url)
      → AUC 提交 → 取任务参数 → 查询 → 识别结果提取
        → 角色标注准备（纯代码：切块参数、渠道规则、块输入）
          → LLM 角色标注与勘误
            → 标注展开（纯代码：分段展开、覆盖校验、勘误附着）
              → end

**为什么不在 ``audio_content_extract_pure`` 里加节点。** 那个变体的语义是「零 LLM、
只交逐字原文」，``tests/test_pipeline_l0_pure.py`` 用节点数与零 LLM 钉住它。就地加节点
会让「关掉的压缩」与「新加的角色标注」混在同一个 workflow_id 下，日后想回退角色标注
就分不清该关哪个节点。新开一个 workflow_id，回退只需把管线里的常量改回纯识别变体。

**为什么角色标注放在文件级而不是管线级。** 一次调用正好对应一个录音文件，文件边界就是
上下文边界；放到管线里会把多个文件的转写拼在一起再标，上下文就串了。

**产出**：``transcript_bundle``（JSON 字符串，含标注后正文 ``content`` 与六字段原文
``raw``）、``annotated``、``role_summary``。管线取 ``content`` 进 L0，取 ``raw`` 留作
对照——勘误与角色都可能出错，原文是唯一能回查的东西。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef, make_node
from . import role_labelling as rl

WORKFLOW_ID = "audio_content_extract_roled"
DISPLAY_NAME = "录音内容角色标注"

# 前半段节点 ID 与 ``audio_content_extract`` 一致——它们是同一批子流程入参的固定绑定，
# 沿用原 ID 让留痕与既有子流程可直接对照。
START = "1773112560827"
SUBMIT = "1773112855659"
PARSE_TASK = "17731967052740"
QUERY = "17731968880650"
EXTRACT_RESULT = "1773125494862"

PREPARE = "roled_prepare"
ROLE_LLM = "roled_llm"
EXPAND = "roled_expand"
END = "roled_end"

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
ROLE_CODE_MODULE = "customer_profile.workflows.role_labelling"
SLUG = "audio_content_extract_roled"


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


def build_workflow() -> WorkflowDef:
    """构造角色标注子流程（8 节点、1 个 LLM）。"""
    return WorkflowDef(
        workflow_id=WORKFLOW_ID,
        display_name=DISPLAY_NAME,
        source_dsl=DISPLAY_NAME,
        entries=(START,),
        exits=(END,),
        outputs={
            "transcript_bundle": "transcript_bundle",
            "annotated": "annotated",
            "role_summary": "role_summary",
        },
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
                PREPARE,
                "角色标注准备",
                "code",
                after=(EXTRACT_RESULT,),
                inputs={
                    "auc_result": (EXTRACT_RESULT, "auc_result"),
                    "file_url": (START, "file_url"),
                },
                outputs=("total", "prior", "channel_rule", "utterances"),
                function=f"{ROLE_CODE_MODULE}:prepare_prompt",
            ),
            make_node(
                ROLE_LLM,
                "LLM 角色标注与勘误",
                "llm",
                after=(PREPARE,),
                inputs={
                    "total": (PREPARE, "total"),
                    "prior": (PREPARE, "prior"),
                    "channel_rule": (PREPARE, "channel_rule"),
                    "auc_result": (PREPARE, "utterances"),
                },
                outputs=("text", "parsed_json"),
                template=template_name(ROLE_LLM),
                output="text",
                expect_json=True,
                system_text="你是一个有用的AI助手",
            ),
            make_node(
                EXPAND,
                "角色标注展开",
                "code",
                after=(ROLE_LLM,),
                inputs={
                    "payload": (ROLE_LLM, "text"),
                    "auc_result": (EXTRACT_RESULT, "auc_result"),
                    "file_url": (START, "file_url"),
                },
                outputs=("transcript_bundle", "annotated", "role_summary", "model_text"),
                function=f"{ROLE_CODE_MODULE}:expand_annotation",
            ),
            make_node(
                END,
                "输出",
                "end",
                after=(EXPAND,),
                inputs={
                    "transcript_bundle": (EXPAND, "transcript_bundle"),
                    "annotated": (EXPAND, "annotated"),
                    "role_summary": (EXPAND, "role_summary"),
                },
                outputs=("transcript_bundle", "annotated", "role_summary"),
            ),
        ),
    )


WORKFLOW: WorkflowDef = build_workflow()

SUBWORKFLOWS: tuple[WorkflowDef, ...] = ()
"""本模块只导出主定义。``load_all`` 会先收 ``WORKFLOW`` 再收 ``SUBWORKFLOWS``，
这里再列一次会让同一个 ``workflow_id`` 注册两遍而报「工作流 ID 重复」。"""


def default_inputs(file_url: str) -> dict[str, Any]:
    return {"file_url": file_url}
