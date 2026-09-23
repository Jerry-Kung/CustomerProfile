"""``（新）SubAgent - 外呼录音信息提取`` 的 Python 定义。

DSL 基线：20 节点 / 21 边，与「外呼录音信息提取」**图结构逐节点相同**，只有渠道名
（``AIOutboundCallRecordingFile``）与提示词正文不同。结构复用
:mod:`customer_profile.workflows.audio_flow`。

录音文件直链来自 ``GET /api/v1/remote/data/history/{phone}`` 响应里
``channel == AIOutboundCallRecordingFile`` 那条记录的 ``media_urls``。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef
from .audio_flow import AudioFlow, build_audio_workflow

WORKFLOW_ID = "outbound_call_audio"
DISPLAY_NAME = "（新）SubAgent - 外呼录音信息提取"
SOURCE_DSL = f"{DISPLAY_NAME}.yml"
DATA_SOURCE = "AIOutboundCallRecordingFile"

FLOW = AudioFlow(
    slug=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    data_source=DATA_SOURCE,
    workflow_id=WORKFLOW_ID,
    check_title="检查外呼录音文件",
    exists_title="外呼录音文件是否存在",
    llm_title="录音文件分析",
    aggregate_llm_id="1776764354171",
    aggregate_llm_title="录音文件分析结果聚合",
    deprecated_branch_id="1776764239626",
    discarded_llm_id="1776149288520",
    kept_llm_id="17767643097270",
)

WORKFLOW: WorkflowDef = build_audio_workflow(FLOW)


def default_inputs(customer_data: str, phone_number: str = "") -> dict[str, Any]:
    """构造该工作流的典型入参，供测试与固定响应使用。"""
    return {
        "phone_number": phone_number,
        "customer_data": customer_data,
        "data_source": DATA_SOURCE,
    }
