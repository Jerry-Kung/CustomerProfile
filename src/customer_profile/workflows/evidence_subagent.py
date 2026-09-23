"""``（新）SubAgent - 证据线索汇总（生产环境）`` 的 Python 定义。

DSL 基线：24 节点 / 39 边，是全项目**扇出最宽**的图：一次取数之后 14 路并行取证。

    start(phone_number)
      └─ http 获取全量用户数据（GET /api/v1/remote/data/history/{phone}）
           ├─ tool 朋友圈截图      ├─ tool 微信手机号搜索   ├─ tool 微信主页
           ├─ tool 试驾录音        ├─ tool 外呼录音         ├─ tool 支付宝个人页
           ├─ tool 小红书个人页    ├─ tool 抖音个人页       ├─ tool 人工确认信息
           ├─ tool 极光数据        ├─ tool 猛士IT系统       ├─ tool 聊天记录
           ├─ tool 用户人工Feedback
           └─ code 代码执行 2 → code 代码执行 3 → llm 手机号特征分析 ┐
                                                                  ├→ template 客户数据聚合
                                              llm 企业信息分析 ────┘
      code 客户标准信息数据聚合（微信搜索 + 猛士IT）
           └─ template 证据线索整理Prompt → llm 证据线索整理 → end(result, original_data)

三处口径：

- **图片与数据的来源**：``GET /api/v1/remote/data/history/{phone}`` 的响应体作为
  ``customer_data`` 下发给各截图类子流程；子流程自己按 ``channel`` 取 ``media_urls``
  再逐张下载。**不是** mhero —— mhero 只提供人工确认 Notes 与 Feedback。
- **有意差异 W2/W3**：``gemini_retry_2_times`` 工具节点归一为一次 ``llm_call()``，
  输出字段仍叫 ``result``。
- **有意差异 W4**：传给子流程的 ``llm_model`` 参数（其中多处是字面量 ``"gemini"``）
  一并删除（§6.4.5）。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "evidence_subagent"
DISPLAY_NAME = "（新）SubAgent - 证据线索汇总（生产环境）"
SOURCE_DSL = f"{DISPLAY_NAME}.yml"

START = "1775638842380"
HISTORY_HTTP = "1775638961126"
END = "1775639065511"

# —— 14 路子流程（13 个真实子流程 + 1 个归一后的 llm）
MOMENTS = "1776068474385"
WECHAT_SEARCH = "1776070114662"
WECHAT_HOME = "1776070141016"
TEST_DRIVE = "1776150525082"
OUTBOUND_CALL = "1776322922950"
ALIPAY = "1776325681312"
XIAOHONGSHU = "1776325690049"
DOUYIN = "1776325696104"
LOCKED_NOTES = "1777286551779"
JIGUANG = "1777287016409"
MENGSHI_IT = "1778231153601"
CHAT_HISTORY = "1778809368635"
FEEDBACK = "1779089586906"
EVIDENCE_LLM = "1777366363196"

# —— 手机号 / 企业信息的解析与推断
RELATED_CODE = "1789984908249"
SPLIT_CODE = "1789985056439"
PHONE_LLM = "1789985109360"
COMPANY_LLM = "1789985119700"

# —— 聚合与整理
DATA_AGGREGATE = "1776069633580"
STANDARD_AGGREGATE = "1778232215760"
EVIDENCE_PROMPT = "1777366329758"

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
SLUG = "evidence_subagent"
SYSTEM_TEXT = "You are a helpful AI assistant."
HISTORY_PATH = "/api/v1/remote/data/history/"

LITERAL = "@literal:"


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


def _tool(
    node_id: str,
    title: str,
    workflow: str,
    data_source: str | None,
    *,
    after: Any = (HISTORY_HTTP,),
    outputs: tuple[str, ...] = ("result",),
):
    """构造一个下发给子流程的 ``tool`` 节点。

    13 路子流程的调用形态高度一致：入参都是 ``customer_data``（全量历史数据响应体）、
    固定的 ``data_source``（渠道名）与 ``phone_number``。差异只有渠道名与子流程 ID，
    因此收在一个构造函数里，避免 13 份重复声明各写各的。
    """
    inputs: dict[str, Any] = {
        "customer_data": (HISTORY_HTTP, "body"),
        "phone_number": (START, "phone_number"),
    }
    mapping: dict[str, str] = {
        "customer_data": "customer_data",
        "phone_number": "phone_number",
    }
    if data_source is not None:
        # 渠道名是常量，不是来自父图的数据：只在 @inputs 里登记字面量，
        # **不**建立绑定——绑一个手机号给 data_source 会凭空造出一条数据依赖。
        mapping["data_source"] = f"{LITERAL}{data_source}"
    return make_node(
        node_id,
        title,
        "tool",
        after=after,
        inputs=inputs,
        outputs=outputs,
        **{"@workflow": workflow, "@inputs": mapping},
        original_node_id=node_id,
    )


# ====================================================================
# code 节点正文（逐字符取自 DSL，只把 def main 换成业务名；
# 跨节点重名的辅助函数加 __<节点后四位> 后缀，函数体一字未改）
# ====================================================================


# ---- DSL 节点 1778232215760 的正文（逐字符，仅函数名不同）
import json


def _parse_json(value):
    """
    兼容 Dify 上游传入 String / Object 两种情况。
    解析失败、空字符串、None、非 JSON 对象时，统一返回空 dict。
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        text = value.strip()

        if not text:
            return {}

        # 兼容 LLM 可能输出的 ```json 包裹
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()

        try:
            data = json.loads(text)
        except Exception:
            return {}

        if isinstance(data, dict):
            return data

        return {}

    return {}


def _merge_wrapped_json(base_data, source_value, target_field_name):
    """
    将一个 JSON 输入解析后，用指定字段名包装，并合并到 base_data 中。
    """
    parsed_data = _parse_json(source_value)
    base_data[target_field_name] = parsed_data
    return base_data


def aggregate_customer_standard_info(
    json_string_1,
    json_string_2,
    json_string_3=None,
    json_string_4=None
) -> dict:
    # 1. 解析主 JSON
    base_data = _parse_json(json_string_1)

    # 2. 定义待合并 JSON 与目标包装字段名
    merge_rules = [
        {
            "source_value": json_string_2,
            "target_field_name": "IT_system_data"
        },

        # 后续如需扩展，打开下面这些配置即可
        # {
        #     "source_value": json_string_3,
        #     "target_field_name": "extra_data_1"
        # },
        # {
        #     "source_value": json_string_4,
        #     "target_field_name": "extra_data_2"
        # },
    ]

    # 3. 执行合并
    for rule in merge_rules:
        source_value = rule.get("source_value")
        target_field_name = rule.get("target_field_name")

        if target_field_name:
            base_data = _merge_wrapped_json(
                base_data=base_data,
                source_value=source_value,
                target_field_name=target_field_name
            )

    # 4. 输出为 String 类型 JSON
    return {
        "merged_json_string": json.dumps(base_data, ensure_ascii=False)
    }


# ---- DSL 节点 1789985056439 的正文（逐字符，仅函数名不同）
import json

def split_operator_and_company(body) -> dict:
    # 兼容 HTTP 节点 body 可能是字符串，也可能已经是对象
    if isinstance(body, str):
        body = body.strip()

        # 关键修改：空字符串直接按空 JSON 对象处理，避免 json.loads("") 报错
        if body == "":
            data = {}
        else:
            data = json.loads(body)

    elif isinstance(body, dict):
        data = body

    elif body is None:
        data = {}

    else:
        raise ValueError(f"Unsupported body type: {type(body)}")

    return {
        "operator_location": json.dumps(
            data.get("operator_location_json", {}),
            ensure_ascii=False
        ),
        "affiliated_company": json.dumps(
            data.get("affiliated_company_json", {}),
            ensure_ascii=False
        )
    }


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=SOURCE_DSL,
    entries=(START,),
    exits=(END,),
    outputs={"result": "result", "original_data": "merged_json_string"},
    nodes=(
        make_node(
            START,
            "用户输入",
            "start",
            # 有意差异 W4：llm_model 入参按 §6.4.5 删除
            variables=("phone_number",),
            required_phone_number=True,
            coords=(80.0, 282.0),
        ),
        make_node(
            HISTORY_HTTP,
            "获取全量用户数据",
            "http-request",
            after=(START,),
            inputs={"@path": (START, "phone_number")},
            outputs=("body", "status"),
            **{
                "@service": "profile",
                "@method": "get",
                # 图片直链与各渠道原始数据都由这一份响应提供（Context 中已实测）
                "@path": HISTORY_PATH,
                "@auth": True,
                "@outputs": {"body": "$text", "status": "$status"},
            },
            original_node_id=HISTORY_HTTP,
        ),
        # ---------------------------------------------------------- 13 路子流程
        _tool(MOMENTS, "（新）SubAgent - 朋友圈信息提取", "moments_info", "WeChatMomentsScreenshot"),
        _tool(
            WECHAT_SEARCH,
            "（新）SubAgent - 微信手机号搜索截图信息提取",
            "wechat_search_homepage",
            "WeChatPhoneSearchScreenshot",
        ),
        _tool(WECHAT_HOME, "（新）SubAgent - 微信主页信息提取", "wechat_homepage", "WeChatHomepageScreenshot"),
        _tool(TEST_DRIVE, "（新）SubAgent - 试驾录音信息提取", "test_drive_audio", "TestDriveRecordingFile"),
        _tool(
            OUTBOUND_CALL,
            "（新）SubAgent - 外呼录音信息提取",
            "outbound_call_audio",
            "AIOutboundCallRecordingFile",
        ),
        _tool(ALIPAY, "（新）SubAgent - 支付宝个人页信息提取", "alipay_homepage", "AlipayPersonalPageScreenshot"),
        _tool(
            XIAOHONGSHU,
            "（新）SubAgent - 小红书个人页信息提取",
            "xiaohongshu_homepage",
            "XiaohongshuPersonalPageScreenshot",
        ),
        _tool(DOUYIN, "（新）SubAgent - 抖音个人页信息提取", "douyin_homepage", "DouyinPersonalPageScreenshot"),
        # 人工确认信息只需要手机号（走 mhero，与 history 无关）
        make_node(
            LOCKED_NOTES,
            "（新）SubAgent - 人工确认信息提取（生产环境）",
            "tool",
            after=(HISTORY_HTTP,),
            inputs={"phone_number": (START, "phone_number")},
            outputs=("result",),
            **{
                "@workflow": "human_corrected_info",
                "@inputs": {"phone_number": "phone_number"},
            },
            original_node_id=LOCKED_NOTES,
        ),
        _tool(JIGUANG, "（新）SubAgent - 极光数据（生产环境）", "jiguang_data", "jiguang"),
        _tool(
            MENGSHI_IT,
            "（新）SubAgent - 猛士IT系统数据信息",
            "mengshi_it_system_data",
            "MengShiITSystemData",
            outputs=("IT_customer_info", "IT_json_data"),
        ),
        _tool(
            CHAT_HISTORY,
            "（新）SubAgent - 聊天记录数据信息",
            "chat_history_data",
            "MengShiCustomerChatInfo",
        ),
        _tool(
            FEEDBACK,
            "（新）SubAgent - 用户人工Feedback数据提取",
            "user_feedback_data",
            "UserFeedbackInformation",
        ),
        # ---------------------------------------------------------- 手机号 / 企业信息
        make_node(
            RELATED_CODE,
            "代码执行 2",
            "code",
            after=(HISTORY_HTTP,),
            inputs={"records": (HISTORY_HTTP, "body")},
            outputs=("result",),
            function=f"{SHARED_CODE_MODULE}:extract_related_enterprise",
            original_node_id=RELATED_CODE,
        ),
        make_node(
            SPLIT_CODE,
            "代码执行 3",
            "code",
            after=(RELATED_CODE,),
            inputs={"body": (RELATED_CODE, "result")},
            outputs=("affiliated_company", "operator_location"),
            function=f"customer_profile.workflows.{SLUG}:split_operator_and_company",
            original_node_id=SPLIT_CODE,
        ),
        make_node(
            PHONE_LLM,
            "手机号特征分析",
            "llm",
            after=(SPLIT_CODE,),
            inputs={
                "phone_number": (START, "phone_number"),
                "operator_location": (SPLIT_CODE, "operator_location"),
            },
            outputs=("text",),
            template=template_name(PHONE_LLM),
            system_text=SYSTEM_TEXT,
            original_node_id=PHONE_LLM,
        ),
        make_node(
            COMPANY_LLM,
            "企业信息分析",
            "llm",
            after=(SPLIT_CODE,),
            inputs={"affiliated_company": (SPLIT_CODE, "affiliated_company")},
            outputs=("text",),
            template=template_name(COMPANY_LLM),
            system_text=SYSTEM_TEXT,
            original_node_id=COMPANY_LLM,
        ),
        # ---------------------------------------------------------- 聚合与整理
        make_node(
            DATA_AGGREGATE,
            "客户数据聚合",
            "template-transform",
            after=(
                MOMENTS, WECHAT_SEARCH, WECHAT_HOME, TEST_DRIVE, OUTBOUND_CALL, ALIPAY,
                XIAOHONGSHU, DOUYIN, LOCKED_NOTES, JIGUANG, MENGSHI_IT, CHAT_HISTORY,
                FEEDBACK, PHONE_LLM, COMPANY_LLM,
            ),
            inputs={
                "moments_result": (MOMENTS, "result"),
                "wechat_search_homepage_result": (WECHAT_SEARCH, "result"),
                "wechat_homepage_result": (WECHAT_HOME, "result"),
                "test_drive_result": (TEST_DRIVE, "result"),
                "outbound_call_result": (OUTBOUND_CALL, "result"),
                "alipay_homepage_result": (ALIPAY, "result"),
                "xiaohongshu_homepage_result": (XIAOHONGSHU, "result"),
                "douyin_homepage_result": (DOUYIN, "result"),
                "phone_result": (PHONE_LLM, "text"),
                "company_result": (COMPANY_LLM, "text"),
                "locked_notes_result": (LOCKED_NOTES, "result"),
                "it_system_result": (MENGSHI_IT, "IT_customer_info"),
                "chat_history_result": (CHAT_HISTORY, "result"),
                "feedback_result": (FEEDBACK, "result"),
            },
            outputs=("output",),
            template=template_name(DATA_AGGREGATE),
            original_node_id=DATA_AGGREGATE,
        ),
        make_node(
            STANDARD_AGGREGATE,
            "客户标准信息数据聚合",
            "code",
            after=(WECHAT_SEARCH, MENGSHI_IT),
            inputs={
                "json_string_1": (WECHAT_SEARCH, "result"),
                "json_string_2": (MENGSHI_IT, "IT_json_data"),
            },
            outputs=("merged_json_string",),
            function=f"customer_profile.workflows.{SLUG}:aggregate_customer_standard_info",
            original_node_id=STANDARD_AGGREGATE,
        ),
        make_node(
            EVIDENCE_PROMPT,
            "证据线索整理Prompt",
            "template-transform",
            after=(DATA_AGGREGATE, STANDARD_AGGREGATE),
            inputs={"arg1": (DATA_AGGREGATE, "output")},
            outputs=("output",),
            template=template_name(EVIDENCE_PROMPT),
            original_node_id=EVIDENCE_PROMPT,
        ),
        make_node(
            EVIDENCE_LLM,
            "证据线索整理 - 工具",
            "llm",
            # 有意差异 W2/W3：DSL 的 gemini_retry_2_times 工具节点归一为一次 llm_call
            after=(EVIDENCE_PROMPT,),
            inputs={"input_prompt": (EVIDENCE_PROMPT, "output")},
            outputs=("result",),
            prompt_field="input_prompt",
            output="result",
            system_text=SYSTEM_TEXT,
            original_node_id=EVIDENCE_LLM,
        ),
        make_node(
            END,
            "输出",
            "end",
            after=(EVIDENCE_LLM,),
            inputs={
                "result": (EVIDENCE_LLM, "result"),
                "original_data": (STANDARD_AGGREGATE, "merged_json_string"),
            },
            outputs=("result", "original_data"),
            original_node_id=END,
        ),
    ),
)


def history_url(phone_number: str) -> str:
    """该工作流实际请求的全量历史数据 URL。用于构造固定响应 fixture。"""
    return f"{HISTORY_PATH}{phone_number}"


def default_inputs(phone_number: str) -> dict[str, Any]:
    return {"phone_number": phone_number}


# ====================================================================
# code 节点 -> 函数名映射
#
# 迁移只允许改函数名，不许改函数体。跨节点重名的辅助函数（同一份 DSL 里不同
# 节点各写了一份 ``_parse_json`` 之类）必须改名，否则后一份会覆盖前一份。
# 这里如实记录每个节点用了什么名字，``tests/test_code_verbatim.py`` 按它把
# DSL 原文里的旧名换成新名后再逐字符比对——差异因此只剩下「名字」。
# ====================================================================

CODE_SPECS: dict[str, dict] = {
    '1778232215760': {'main': 'aggregate_customer_standard_info', 'renames': {}},
    '1789985056439': {'main': 'split_operator_and_company', 'renames': {}},
}
