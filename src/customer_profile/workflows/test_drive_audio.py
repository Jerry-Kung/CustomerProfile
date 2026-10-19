"""``试驾录音信息提取`` 的 Python 定义。

迁移前的定义基线：20 节点 / 21 边，与「外呼录音信息提取」**图结构逐节点相同**，只有渠道名
（``TestDriveRecordingFile``）与提示词正文不同。结构复用
:mod:`customer_profile.workflows.audio_flow`。

录音文件直链来自 ``GET /api/v1/remote/data/history/{phone}`` 响应里
``channel == TestDriveRecordingFile`` 那条记录的 ``media_urls``。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef
from .audio_flow import AudioFlow, build_audio_workflow, build_pure_workflow

WORKFLOW_ID = "test_drive_audio"
DISPLAY_NAME = "试驾录音信息提取"

DATA_SOURCE = "TestDriveRecordingFile"

FLOW = AudioFlow(
    slug=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    data_source=DATA_SOURCE,
    workflow_id=WORKFLOW_ID,
    check_title="检查试驾录音文件",
    exists_title="试驾录音文件是否存在",
    llm_title="录音文件分析",
    aggregate_llm_id="1776764121079",
    aggregate_llm_title="录音文件分析结果聚合",
    deprecated_branch_id="1776763992027",
    discarded_llm_id="1776149288520",
    kept_llm_id="17767640208110",
)

WORKFLOW: WorkflowDef = build_audio_workflow(FLOW)
"""迁移前的定义基线（18 节点）。**基线臂在用，逐字不得改动。**"""

PURE_WORKFLOW_ID = "test_drive_audio_pure"
"""v2 纯识别变体的工作流 ID。见 ``build_pure_workflow`` 与
:func:`customer_profile.workflows.audio_flow.build_audio_workflow`。"""

PURE_WORKFLOW: WorkflowDef = build_pure_workflow(
    FLOW, pure_flow_id=PURE_WORKFLOW_ID, pure_display_name=f"{DISPLAY_NAME}（纯识别）"
)
"""v2 纯识别变体（15 节点、零 LLM）。事实账本管线的「试驾录音信息提取」在用它。

实测依据（R1b 13 案）：本图的「录音文件分析」节点 421,030 token，且其内部发起了
「LLM 识别结果校对」1,261,865 + 「LLM 内容有效性判断」420,881。管线的记录层把这
一路的 ``result`` 当作**兜底报告**用，而 20 条录音记录里 0 条走到该兜底。
"""

SUBWORKFLOWS: tuple[WorkflowDef, ...] = (PURE_WORKFLOW,)
"""本模块导出的子工作流。``load_all`` 会把它们一并注册。"""


def default_inputs(customer_data: str, phone_number: str = "") -> dict[str, Any]:
    """构造该工作流的典型入参，供测试与固定响应使用。"""
    return {
        "phone_number": phone_number,
        "customer_data": customer_data,
        "data_source": DATA_SOURCE,
    }
