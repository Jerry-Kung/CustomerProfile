"""``（新）SubAgent - 猛士IT系统数据信息`` 的 Python 定义。

DSL 基线（`dify_dsl_data/（新）SubAgent - 猛士IT系统数据信息.yml`）：4 节点 / 3 边，
两个 ``code`` 节点一条直线，无外部依赖。它是 V0.2 用来验证「确定性逻辑等价」的载体。

迁移口径（规划 §6.2）：``code`` 节点的逻辑**直接提取**，只剥离 Dify 的入参加载与
返回值包装，不在迁移中顺手重构。两个函数体与 DSL 的 ``main()`` 逐字符一致
（见 ``tests/test_mengshi_it_system_data.py`` 的正文比对断言）。

有意差异（W4）：``start`` 的 ``llm_model`` 入参按 §6.4.5 删除，不再作为工作流入参。
"""

from __future__ import annotations

import json
from typing import Any

from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "mengshi_it_system_data"
DISPLAY_NAME = "（新）SubAgent - 猛士IT系统数据信息"
SOURCE_DSL = f"{DISPLAY_NAME}.yml"

START_NODE = "1773748103758"
EXTRACT_NODE = "1776328382353"
INTEGRATE_NODE = "1778230548931"
END_NODE = "1773748343861"


# ====================================================================
# 节点 1776328382353「数据提取」的正文
# 与 DSL 逐字符一致；Dify 的 def main(...) 改名为业务化名字，签名与函数体不动。
# ====================================================================

DATA_SOURCE_DEFAULT = "MengShiITSystemData"


def extract_channel_payload(records: str, data_source: str) -> dict:
    """从 ``customer_data`` 里取出 ``channel == data_source`` 那一项的载荷。

    与 DSL 节点 ``1776328382353`` 的 ``main`` 函数体逐字符一致。
    """
    import json

    # 兜底：输入为空时直接返回空字符串
    if records is None:
        return {"result": ""}

    records = records.strip()
    if not records:
        return {"result": ""}

    # 解析 JSON 字符串
    try:
        data = json.loads(records)
    except Exception:
        return {"result": ""}

    # 顶层必须是数组
    if not isinstance(data, list):
        return {"result": ""}

    # 遍历数组元素，查找 channel == data_source
    for item in data:
        if not isinstance(item, dict):
            continue

        if item.get("channel") == data_source:
            raw_payload = item.get("raw_payload")

            # 若找到目标元素，但 media_urls 为空/null，则返回空字符串
            if raw_payload is None:
                return {"result": ""}

            # 若 media_urls 本身就是字符串，直接返回
            if isinstance(raw_payload, str):
                return {"result": raw_payload}

            # 若 media_urls 是数组/对象，转成 JSON 字符串返回
            return {"result": json.dumps(raw_payload, ensure_ascii=False)}

    # 没找到目标元素
    return {"result": ""}


# ====================================================================
# 节点 1778230548931「数据整合」的正文
# ====================================================================

# 原始字段列表：保证输出字段顺序稳定
ORIGINAL_FIELDS = [
    "customer_name",
    "customer_nick",
    "customer_phone",
    "customer_remark",
    "customer_phone_brand",
    "customer_register_time",
    "sales_consultant_account",
    "customer_phone_device_model",
    "customer_address",
    "customer_channel",
    "customer_wechat_id",
    "customer_stage",
    "customer_leads_status",
    "customer_last_follow_content",
    "customer_lead_rating",
]


# 客户线索渠道枚举
CUSTOMER_CHANNEL_MAP = {
    "1": "app",
    "2": "百度",
}


# 客户线索阶段枚举
CUSTOMER_STAGE_MAP = {
    "1": "线索",
    "2": "商机",
}


# 客户线索状态枚举
# 来源：用户提供的客户线索状态参考表截图
CUSTOMER_LEADS_STATUS_MAP = {
    "100401": "待处理",
    "100402": "有效",
    "100403": "无效",
    "100404": "待外呼清洗",
    "100405": "培育中",
    "100406": "待下发",
    "100407": "下发失败",
    "100408": "待分配",
    "100409": "待建档",
    "100410": "跟进中",
    "100411": "已锁单",
    "100412": "已暂败",
    "100413": "关闭",
    "100414": "首次跟进",
    "100415": "已预订",
}


# system_customer_info 中需要展示的字段、中文标签、可选值转换器
INFO_FIELD_LABELS = [
    ("customer_name", "客户姓名", None),
    ("customer_nick", "客户昵称", None),
    ("customer_phone", "客户手机号码", None),
    ("customer_remark", "我方销售人员手动添加的客户备注信息", None),
    ("customer_phone_brand", "客户使用的手机品牌", None),
    ("customer_phone_device_model", "客户使用的手机型号", None),
    ("customer_register_time", "客户在销售系统中的数据注册时间", None),
    ("customer_address", "客户地址", None),
    (
        "customer_channel",
        "客户线索渠道",
        lambda value: _map_value(value, CUSTOMER_CHANNEL_MAP),
    ),
    ("customer_wechat_id", "客户微信号", None),
    (
        "customer_stage",
        "客户线索阶段（线索or商机）",
        lambda value: _map_value(value, CUSTOMER_STAGE_MAP),
    ),
    (
        "customer_leads_status",
        "客户线索状态",
        lambda value: _map_value(value, CUSTOMER_LEADS_STATUS_MAP),
    ),
    ("customer_last_follow_content", "销售顾问对该客户的最新跟进内容", None),
    (
        "customer_lead_rating",
        "销售顾问对该客户的初始线索评级（ABC三档，A为优质线索，B为一般线索，C为劣质线索）",
        None,
    ),
]


def _parse_json(json_string):
    """
    解析输入 JSON 字符串。
    解析失败、空字符串、非 dict 类型，统一返回 None。
    """
    if json_string is None:
        return None

    if isinstance(json_string, dict):
        return json_string

    if not isinstance(json_string, str):
        return None

    text = json_string.strip()
    if not text:
        return None

    # 兼容 LLM 可能输出的 ```json 包裹
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()

    try:
        data = json.loads(text)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    return data


def _is_empty(value):
    """
    判断字段是否为空。
    空字符串、纯空格、None 视为空。
    数字 0、False 不视为空。
    """
    if value is None:
        return True

    if isinstance(value, str) and value.strip() == "":
        return True

    return False


def _to_display_text(value):
    """
    将非空字段转成普通字符串。
    如果是 dict/list，则转成 JSON 字符串，避免直接输出 Python 表示法。
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)

    return str(value)


def _map_value(value, mapping):
    """
    将枚举码值转换为可读文本。
    若遇到未收录的枚举值，保留原始值并标记为未知，避免静默丢失信息。
    """
    value_text = _to_display_text(value).strip()
    return mapping.get(value_text, f"{value_text}（未知）")


def _build_system_customer_info(data):
    """
    构造 system_customer_info。
    只输出有值的数据行。
    如果没有任何可输出数据行，则输出默认空内容。
    """
    lines = []

    for field_name, label, converter in INFO_FIELD_LABELS:
        value = data.get(field_name, "")
        if not _is_empty(value):
            display_value = converter(value) if converter else _to_display_text(value)
            lines.append(f"{label}：{display_value}")

    if not lines:
        return "客户留存在销售数据系统中的信息记录为空。"

    return "客户留存在销售数据系统中的信息记录：\n" + "\n".join(lines)


def _build_system_customer_json_data(data):
    """
    构造 system_customer_json_data。
    字段重命名为 ITsystem_ + 原字段名。
    若字段缺失，则设置为空字符串。
    若原字段存在且为 null，则保留 None，最终 JSON 中表现为 null。
    """
    result = {}

    for field_name in ORIGINAL_FIELDS:
        new_field_name = f"ITsystem_{field_name}"

        if field_name in data:
            result[new_field_name] = data[field_name]
        else:
            result[new_field_name] = ""

    return json.dumps(result, ensure_ascii=False)


def integrate_customer_data(json_string) -> dict:
    """把销售系统 JSON 整理成文本画像与结构化 JSON 两个输出。

    与 DSL 节点 ``1778230548931`` 的 ``main`` 函数体逐字符一致。
    """
    data = _parse_json(json_string)

    # JSON 为空、格式异常、解析失败时：
    # system_customer_info 输出默认异常内容
    # system_customer_json_data 输出全字段空值
    if data is None:
        empty_data = {field: "" for field in ORIGINAL_FIELDS}

        return {
            "system_customer_info": "客户留存在销售数据系统中的信息记录为空。",
            "system_customer_json_data": _build_system_customer_json_data(empty_data)
        }

    return {
        "system_customer_info": _build_system_customer_info(data),
        "system_customer_json_data": _build_system_customer_json_data(data)
    }


# ====================================================================
# 工作流定义
# ====================================================================

WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=SOURCE_DSL,
    entries=(START_NODE,),
    exits=(END_NODE,),
    outputs={
        "IT_customer_info": "system_customer_info",
        "IT_json_data": "system_customer_json_data",
    },
    nodes=(
        make_node(
            START_NODE,
            "用户输入",
            "start",
            # 有意差异 W4：llm_model 入参已按 §6.4.5 删除
            variables=("phone_number", "customer_data", "data_source"),
            required_phone_number=True,
            required_customer_data=True,
            required_data_source=False,
            coords=(80.0, 282.0),
        ),
        make_node(
            EXTRACT_NODE,
            "数据提取",
            "code",
            after=(START_NODE,),
            inputs={
                "records": (START_NODE, "customer_data"),
                "data_source": (START_NODE, "data_source"),
            },
            outputs=("result",),
            function="customer_profile.workflows.mengshi_it_system_data:extract_channel_payload",
            original_node_id=EXTRACT_NODE,
            coords=(430.50293012833276, 303.0),
        ),
        make_node(
            INTEGRATE_NODE,
            "数据整合",
            "code",
            after=(EXTRACT_NODE,),
            inputs={"json_string": (EXTRACT_NODE, "result")},
            outputs=("system_customer_info", "system_customer_json_data"),
            function="customer_profile.workflows.mengshi_it_system_data:integrate_customer_data",
            original_node_id=INTEGRATE_NODE,
            coords=(823.619487938305, 303.0),
        ),
        make_node(
            END_NODE,
            "输出",
            "end",
            after=(INTEGRATE_NODE,),
            inputs={
                "IT_customer_info": (INTEGRATE_NODE, "system_customer_info"),
                "IT_json_data": (INTEGRATE_NODE, "system_customer_json_data"),
            },
            outputs=("IT_customer_info", "IT_json_data"),
            original_node_id=END_NODE,
            coords=(1234.3781676281988, 303.0),
        ),
    ),
)


def default_inputs(
    customer_data: str, data_source: str = DATA_SOURCE_DEFAULT, phone_number: str = ""
) -> dict[str, Any]:
    """构造该工作流的典型入参，供测试与回放使用。"""
    return {
        "phone_number": phone_number,
        "customer_data": customer_data,
        "data_source": data_source,
    }
