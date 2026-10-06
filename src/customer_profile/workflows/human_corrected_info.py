"""``人工确认信息提取`` 的 Python 定义。

迁移前的定义基线：4 节点 / 3 边，``start → http-request(GET) → code → end``。
它是 V0.2 用来验证「HTTP 读取与留痕」的载体。

改造口径：

- HTTP 节点保留方法与路径，**凭据从环境变量注入**（``MHERO`` 服务）。迁移前的定义里那是明文
  ``X-API-Key``，现在不再出现在
  任何定义文件中。
- ``timeout`` / ``retry_interval`` 不照搬迁移前的定义的取值（``timeout: 0``、``retry_interval: 100``），
  改走 ``Settings`` 的显式配置。
- ``code`` 节点逻辑与迁移前的定义逐字符一致。
- GET 幂等，按配置重试；这是读接口，不涉及回写副作用。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "human_corrected_info"
DISPLAY_NAME = "人工确认信息提取"
# 定义快照的 source_dsl 直接取这个显示名——原来那个指向 .yml 的追溯键已随
# 定义文件一并废弃，字段保留只为不动快照形状。

START_NODE = "1776649712409"
HTTP_NODE = "1776649722195"
CODE_NODE = "1776650937435"
END_NODE = "1776649751425"

MHERO_PATH_PREFIX = "/api/v1/remote/data/profile/"


# ====================================================================
# 节点 1776650937435「代码执行」的正文
# ====================================================================


def _strip_code_fence(text: str) -> str:
    """
    兼容 LLM 输出中可能带有 ```json ... ``` 的情况
    """
    text = text.strip()

    # 去掉 ```json / ```JSON / ``` 包裹
    text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    return text.strip()


def _parse_json_string(value):
    """
    兼容上游传入 String / Object 两种情况。
    但本节点最终输出仍然会强制转成 String。
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        value = _strip_code_fence(value)
        if not value:
            return {}
        return json.loads(value)

    raise ValueError(f"不支持的输入类型: {type(value)}")


def _is_locked(value) -> bool:
    """
    兼容 is_locked 为 1 / '1' / True 的情况。
    """
    return value == 1 or value == "1" or value is True


def extract_locked_notes(input_json) -> dict:
    """挑出被锁定的人工确认 Notes，输出 JSON 字符串。

    与迁移前的定义节点 ``1776650937435`` 的 ``main`` 函数体逐字符一致。
    ``locked_notes_json`` 必须是字符串而非对象——下游按字符串消费。
    """
    data = _parse_json_string(input_json)

    basic_notes = data.get("basic_notes", {})
    if not isinstance(basic_notes, dict):
        basic_notes = {}

    locked_notes = []

    for field_name, field_content in basic_notes.items():
        if not isinstance(field_content, dict):
            continue

        if _is_locked(field_content.get("is_locked")):
            locked_notes.append({
                field_name: {
                    "value": field_content.get("value")
                }
            })

    result = {
        "locked_notes": locked_notes
    }

    # 关键：这里必须 json.dumps，确保输出是 String，而不是 Object
    locked_notes_json = json.dumps(result, ensure_ascii=False)

    return {
        "locked_notes_json": locked_notes_json
    }


# ====================================================================
# 工作流定义
# ====================================================================

WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=DISPLAY_NAME,
    entries=(START_NODE,),
    exits=(END_NODE,),
    outputs={"result": "locked_notes_json"},
    nodes=(
        make_node(
            START_NODE,
            "用户输入",
            "start",
            variables=("phone_number",),
            required_phone_number=True,
        ),
        make_node(
            HTTP_NODE,
            "获取人工确认Notes",
            "http-request",
            after=(START_NODE,),
            inputs={"@path": (START_NODE, "phone_number")},
            outputs=("body", "status"),
            # 服务名决定 base_url 与凭据来源；凭据本身不进定义
            **{
                "@service": "mhero",
                "@method": "get",
                "@path": MHERO_PATH_PREFIX,
                "@auth": True,
                "@outputs": {"body": "$text", "status": "$status"},
            },
        ),
        make_node(
            CODE_NODE,
            "提取被锁定的人工确认字段",
            "code",
            after=(HTTP_NODE,),
            inputs={"input_json": (HTTP_NODE, "body")},
            outputs=("locked_notes_json",),
            function="customer_profile.workflows.human_corrected_info:extract_locked_notes",
        ),
        make_node(
            END_NODE,
            "输出",
            "end",
            after=(CODE_NODE,),
            inputs={"result": (CODE_NODE, "locked_notes_json")},
            outputs=("result",),
        ),
    ),
)


def profile_url(phone_number: str) -> str:
    """该工作流实际请求的 URL。用于构造固定响应 fixture。"""
    return f"{MHERO_PATH_PREFIX}{phone_number}"


def default_inputs(phone_number: str) -> dict[str, Any]:
    return {"phone_number": phone_number}
