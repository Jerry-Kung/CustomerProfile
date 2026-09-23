"""``（新）SubAgent - 用户人工Feedback数据提取`` 的 Python 定义。

DSL 基线：11 节点 / 12 边。链路是「取反馈原文 + 拉历史画像 → 拆点赞/点踩 → 判断有无反馈
→ 有则生成总结报告，无则给一句固定说明」：

    start(phone_number, customer_data, data_source)
      ├─ code 数据提取（取 channel 的 raw_payload）
      └─ http 获取历史数据&Feedback记录（mhero 画像）
           └─ code 代码执行 2（拆出 approved / rejected 两组字段）
                └─ code 代码执行 3（判断有无反馈）
                     └─ if-else 条件分支
                          ├─ true  → template 模板转换 2 → llm → 变量聚合器 → end
                          └─ else → template 模板转换 → 变量聚合器

两处口径：

- ``code`` 节点的两个解析辅助函数在本模块内同名（``_strip_code_fence`` 在两段 DSL
  正文里都有但实现不同），因此第二个被重命名为 ``_strip_code_fence__6946``；改名只动
  函数名，正文一字未改（见 ``tests/test_code_verbatim.py``）。
- 回写类接口不在此工作流内；这里只有一次 GET 读请求（mhero），按幂等重试。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef, make_node


# ---- DSL 节点 1779414084761 的正文（逐字符，仅函数名不同）
import json
import re


def _strip_code_fence(text: str) -> str:
    """
    兼容 LLM 输出中可能带有 ```json ... ``` 的情况。
    """
    text = text.strip()

    # 去掉 ```json / ```JSON / ``` 包裹
    text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    return text.strip()


def _parse_json_string(value):
    """
    兼容 Dify 上游传入 String / Object / None 三种情况。
    解析失败时返回空 dict，避免代码节点中断。
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        value = _strip_code_fence(value)

        if not value:
            return {}

        try:
            data = json.loads(value)
        except Exception:
            # 兼容部分上游把内部 JSON 字符串过度转义为 {\\"key\\": ...} 的情况
            try:
                fixed_value = value.replace('\\\\\"', '\\\"')
                data = json.loads(fixed_value)
            except Exception:
                return {}

        if isinstance(data, dict):
            return data

        return {}

    return {}


def _feedback_is_approved(value) -> bool:
    """
    兼容 ai_feedback 为 1 / '1' 的情况。
    """
    return value == 1 or value == "1"


def _feedback_is_rejected(value) -> bool:
    """
    兼容 ai_feedback 为 0 / '0' 的情况。
    None 表示未评论，不会被收集。
    """
    return value == 0 or value == "0"


def _build_feedback_item(field_name, field_content):
    """
    输出结构与原 locked_notes 的风格保持接近：

    {
      "字段名": {
        "value": 原字段值,
        "reasoning_summary": 原推理说明
      }
    }

    如果你只想保留 value，可以删除 reasoning_summary 相关部分。
    """
    item_content = {
        "value": field_content.get("value")
    }

    if "reasoning_summary" in field_content:
        item_content["reasoning_summary"] = field_content.get("reasoning_summary")

    return {
        field_name: item_content
    }


def split_feedback_notes(input_json) -> dict:
    """
    input_json: 上游传入的 JSON 字符串

    return:
      approved_feedback_json: 用户赞同字段汇总，JSON 字符串，不是 Object
      rejected_feedback_json: 用户反对字段汇总，JSON 字符串，不是 Object
    """

    data = _parse_json_string(input_json)

    basic_notes = data.get("basic_notes", {})
    if not isinstance(basic_notes, dict):
        basic_notes = {}

    approved_notes = []
    rejected_notes = []

    for field_name, field_content in basic_notes.items():
        if not isinstance(field_content, dict):
            continue

        feedback = field_content.get("ai_feedback")
        item = _build_feedback_item(field_name, field_content)

        if _feedback_is_approved(feedback):
            approved_notes.append(item)
        elif _feedback_is_rejected(feedback):
            rejected_notes.append(item)

    approved_result = {
        "approved_notes": approved_notes
    }

    rejected_result = {
        "rejected_notes": rejected_notes
    }

    # 关键：这里必须 json.dumps，确保输出是 String，而不是 Object
    return {
        "approved_feedback_json": json.dumps(approved_result, ensure_ascii=False),
        "rejected_feedback_json": json.dumps(rejected_result, ensure_ascii=False)
    }


# ---- DSL 节点 1779414516946 的正文（逐字符，仅函数名不同）
import json
import re


def _strip_code_fence__6946(text: str) -> str:
    """
    兼容上游可能传入 ```json ... ``` 的情况。
    """
    text = text.strip()

    text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    return text.strip()


def _parse_json_loose(value):
    """
    宽松解析 JSON 字符串。

    - None / 空字符串：返回 None
    - dict / list：直接返回
    - 合法 JSON 字符串：解析后返回
    - 非法 JSON 字符串：按空处理，返回 None
    """
    if value is None:
        return None

    if isinstance(value, (dict, list)):
        return value

    if isinstance(value, str):
        text = _strip_code_fence__6946(value)

        if not text:
            return None

        try:
            return json.loads(text)
        except Exception:
            return None

    return value


def _is_empty_value(value) -> bool:
    """
    递归判断一个 JSON 值是否为空。
    """
    if value is None:
        return True

    if isinstance(value, str):
        return value.strip() == ""

    if isinstance(value, list):
        if len(value) == 0:
            return True
        return all(_is_empty_value(item) for item in value)

    if isinstance(value, dict):
        if len(value) == 0:
            return True
        return all(_is_empty_value(v) for v in value.values())

    return False


def _has_content(value) -> bool:
    parsed_value = _parse_json_loose(value)
    return not _is_empty_value(parsed_value)


def detect_feedback_presence(feedback_content, approved_feedback_json, rejected_feedback_json) -> dict:
    has_feedback = any([
        _has_content(feedback_content),
        _has_content(approved_feedback_json),
        _has_content(rejected_feedback_json)
    ])

    return {
        # Dify Code 节点不支持 boolean 类型，所以用 number 输出
        # 1 = True，有反馈
        # 0 = False，无反馈
        "has_feedback": 1 if has_feedback else 0
    }


# ====================================================================
# 工作流定义
# ====================================================================

WORKFLOW_ID = "user_feedback_data"
DISPLAY_NAME = "（新）SubAgent - 用户人工Feedback数据提取"
SOURCE_DSL = f"{DISPLAY_NAME}.yml"

START = "1773748103758"
END = "1773748343861"
EXTRACT = "1776328382353"
HISTORY_HTTP = "1779088846431"
SPLIT_CODE = "1779414084761"
PRESENCE_CODE = "1779414516946"
BRANCH = "1779414671275"
NO_DATA_TEMPLATE = "1779414698011"
REPORT_TEMPLATE = "1779414703703"
AGGREGATE = "1779414750607"
REPORT_LLM = "1779419296890"

TRUE_BRANCH = "true"
ELSE_BRANCH = "false"

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
SLUG = "user_feedback_data"
MHERO_PROFILE_PREFIX = "/api/v1/remote/data/profile/"

DATA_SOURCE = "UserFeedbackInformation"


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=SOURCE_DSL,
    entries=(START,),
    exits=(END,),
    outputs={"result": "output"},
    nodes=(
        make_node(
            START,
            "用户输入",
            "start",
            # 有意差异 W4：llm_model 入参按 §6.4.5 删除
            variables=("phone_number", "customer_data", "data_source"),
            required_phone_number=True,
            required_customer_data=True,
            required_data_source=False,
        ),
        make_node(
            EXTRACT,
            "数据提取",
            "code",
            after=(START,),
            inputs={
                "records": (START, "customer_data"),
                "data_source": (START, "data_source"),
            },
            outputs=("result",),
            function=f"{SHARED_CODE_MODULE}:extract_raw_payload",
            original_node_id=EXTRACT,
        ),
        make_node(
            HISTORY_HTTP,
            "获取历史数据&Feedback记录",
            "http-request",
            after=(START,),
            inputs={"@path": (START, "phone_number")},
            outputs=("body", "status"),
            **{
                "@service": "mhero",
                "@method": "get",
                "@path": MHERO_PROFILE_PREFIX,
                "@auth": True,
                "@outputs": {"body": "$text", "status": "$status"},
            },
            original_node_id=HISTORY_HTTP,
        ),
        make_node(
            SPLIT_CODE,
            "代码执行 2",
            "code",
            after=(HISTORY_HTTP,),
            inputs={"input_json": (HISTORY_HTTP, "body")},
            outputs=("approved_feedback_json", "rejected_feedback_json"),
            function=f"customer_profile.workflows.{SLUG}:split_feedback_notes",
            original_node_id=SPLIT_CODE,
        ),
        make_node(
            PRESENCE_CODE,
            "代码执行 3",
            "code",
            after=(SPLIT_CODE, EXTRACT),
            inputs={
                "feedback_content": (EXTRACT, "result"),
                "approved_feedback_json": (SPLIT_CODE, "approved_feedback_json"),
                "rejected_feedback_json": (SPLIT_CODE, "rejected_feedback_json"),
            },
            outputs=("has_feedback",),
            function=f"customer_profile.workflows.{SLUG}:detect_feedback_presence",
            original_node_id=PRESENCE_CODE,
        ),
        make_node(
            BRANCH,
            "条件分支",
            "if-else",
            after=(PRESENCE_CODE,),
            inputs={"has_feedback": (PRESENCE_CODE, "has_feedback")},
            branches=(
                {
                    "id": TRUE_BRANCH,
                    # DSL: has_feedback = 1（number）。code 节点用 number 表达布尔
                    # （Dify 不支持 boolean 输出），因此按文本比较即可。
                    "condition": {"field": "has_feedback", "operator": "eq", "value": "1"},
                },
            ),
            else_id=ELSE_BRANCH,
            original_node_id=BRANCH,
        ),
        make_node(
            NO_DATA_TEMPLATE,
            "模板转换",
            "template-transform",
            after=(BRANCH,),
            on_branch=(BRANCH, "false"),
            outputs=("output",),
            template=template_name(NO_DATA_TEMPLATE),
            original_node_id=NO_DATA_TEMPLATE,
        ),
        make_node(
            REPORT_TEMPLATE,
            "模板转换 2",
            "template-transform",
            after=(BRANCH,),
            on_branch=(BRANCH, "true"),
            inputs={
                "history_result": (HISTORY_HTTP, "body"),
                "feedback_content": (EXTRACT, "result"),
                "approved_feedback_json": (SPLIT_CODE, "approved_feedback_json"),
                "rejected_feedback_json": (SPLIT_CODE, "rejected_feedback_json"),
            },
            outputs=("output",),
            template=template_name(REPORT_TEMPLATE),
            original_node_id=REPORT_TEMPLATE,
        ),
        make_node(
            REPORT_LLM,
            "Gemini（异常输出重试版）",
            "llm",
            # 有意差异 W2/W3：DSL 的 gemini_retry_2_times 工具节点归一为一次 llm_call
            after=(REPORT_TEMPLATE,),
            inputs={"input_prompt": (REPORT_TEMPLATE, "output")},
            outputs=("result",),
            prompt_field="input_prompt",
            output="result",
            system_text="You are a helpful AI assistant.",
            original_node_id=REPORT_LLM,
        ),
        make_node(
            AGGREGATE,
            "变量聚合器",
            "variable-aggregator",
            after=(NO_DATA_TEMPLATE, REPORT_LLM),
            variables=((REPORT_LLM, "result"), (NO_DATA_TEMPLATE, "output")),
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
    ),
)


def history_url(phone_number: str) -> str:
    """该工作流实际请求的画像 URL。用于构造固定响应 fixture。"""
    return f"{MHERO_PROFILE_PREFIX}{phone_number}"


def default_inputs(customer_data: str, phone_number: str = "") -> dict[str, Any]:
    return {
        "phone_number": phone_number,
        "customer_data": customer_data,
        "data_source": DATA_SOURCE,
    }


# ====================================================================
# code 节点 -> 函数名映射
#
# 迁移只允许改函数名，不许改函数体。跨节点重名的辅助函数（同一份 DSL 里不同
# 节点各写了一份 ``_parse_json`` 之类）必须改名，否则后一份会覆盖前一份。
# 这里如实记录每个节点用了什么名字，``tests/test_code_verbatim.py`` 按它把
# DSL 原文里的旧名换成新名后再逐字符比对——差异因此只剩下「名字」。
# ====================================================================

CODE_SPECS: dict[str, dict] = {
    '1779414084761': {'main': 'split_feedback_notes', 'renames': {}},
    '1779414516946': {'main': 'detect_feedback_presence', 'renames': {"_strip_code_fence": "_strip_code_fence__6946"}},
}
