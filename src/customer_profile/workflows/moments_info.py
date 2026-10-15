"""``朋友圈信息提取`` 的 Python 定义。

迁移前的定义基线：18 节点 / 18 边，与其余 6 个截图流程**图结构逐节点相同**，只有渠道名
（``WeChatMomentsScreenshot``）与提示词正文不同。因此这里只给出参数，结构复用
:mod:`customer_profile.workflows.screenshot_flow`。

渠道名来自迁移前的定义 ``start`` 节点由父图传入的 ``data_source`` 字面量；图片直链来自
``GET /api/v1/remote/data/history/{phone}`` 响应里 ``channel == WeChatMomentsScreenshot``
那条记录的 ``media_urls``。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef
from .screenshot_flow import ScreenshotFlow, build_screenshot_workflow

WORKFLOW_ID = "moments_info"
DISPLAY_NAME = "朋友圈信息提取"

DATA_SOURCE = "WeChatMomentsScreenshot"

FLOW = ScreenshotFlow(
    slug=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    data_source=DATA_SOURCE,
    workflow_id=WORKFLOW_ID,
)

WORKFLOW: WorkflowDef = build_screenshot_workflow(FLOW)
"""迁移前的定义基线（18 节点）。**基线臂在用，逐字不得改动。**"""

PURE_WORKFLOW_ID = "moments_info_pure"
"""v2 纯识别变体的工作流 ID。"""

PURE_FLOW = ScreenshotFlow(
    slug=WORKFLOW_ID,
    display_name=f"{DISPLAY_NAME}（纯识别）",
    data_source=DATA_SOURCE,
    workflow_id=PURE_WORKFLOW_ID,
    summary_llm=False,
)
"""纯识别变体的参数：模板 slug 与原件相同（复用同一批模板资源），只关掉多图总结。"""

PURE_WORKFLOW: WorkflowDef = build_screenshot_workflow(PURE_FLOW)
"""v2 纯识别变体（17 节点、零消费压缩的 LLM）。事实账本管线的「朋友圈信息提取」在用它。

实测（R1b 13 案 / 基線 39 案）：本流的「多图片内容总结」节点 **13/13、39/39 全部 skipped**
——13 案的朋友圈渠道一条图片也没有（``moments_images=None``），多图分支从未被走到。
因此本变体关闭它是**零质量风险**的（该节点本就未产出任何被消费的东西），
价值在于：日后真有数据时，逐图解析结果**逐字**进入 L1，而不是先被汇总成一份有损报告。
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
