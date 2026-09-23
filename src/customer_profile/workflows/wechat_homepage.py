"""``微信主页信息提取`` 的 Python 定义。

DSL 基线：18 节点 / 18 边，与其余 6 个截图流程**图结构逐节点相同**，只有渠道名
（``WeChatHomepageScreenshot``）与提示词正文不同。因此这里只给出参数，结构复用
:mod:`customer_profile.workflows.screenshot_flow`。

渠道名来自 DSL ``start`` 节点由父图传入的 ``data_source`` 字面量；图片直链来自
``GET /api/v1/remote/data/history/{phone}`` 响应里 ``channel == WeChatHomepageScreenshot``
那条记录的 ``media_urls``。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef
from .screenshot_flow import ScreenshotFlow, build_screenshot_workflow

WORKFLOW_ID = "wechat_homepage"
DISPLAY_NAME = "（新）SubAgent - 微信主页信息提取"
SOURCE_DSL = f"{DISPLAY_NAME}.yml"
DATA_SOURCE = "WeChatHomepageScreenshot"

FLOW = ScreenshotFlow(
    slug=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    data_source=DATA_SOURCE,
    workflow_id=WORKFLOW_ID,
)

WORKFLOW: WorkflowDef = build_screenshot_workflow(FLOW)


def default_inputs(customer_data: str, phone_number: str = "") -> dict[str, Any]:
    """构造该工作流的典型入参，供测试与固定响应使用。"""
    return {
        "phone_number": phone_number,
        "customer_data": customer_data,
        "data_source": DATA_SOURCE,
    }
