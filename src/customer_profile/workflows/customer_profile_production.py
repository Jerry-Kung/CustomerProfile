"""``（新）SubAgent - 画像内容生成&回写（生产环境）（新）`` 的 Python 定义。

DSL 基线：41 节点 / 55 边，是全项目最大的工作流。它把三段输入（证据线索、人设画像、
原始客户数据）经 11 路 LLM 生成与 7 段 code 合并，拼成一份最终画像 JSON，再回写生产。

结构上分四层：

1. **预置层**：6 个无入参的 ``template-transform``（标签库、各类格式模板、产品卖点、
   邀约话术），把大段固定文本准备好；
2. **生成层**：13 个带引用的模板拼提示词 → 11 个 ``llm`` 节点产出各自片段；
3. **合并层**：7 个 ``code`` 节点逐级合并（录音证据 → 原始信息 → 人设卡 → 销售线索 →
   画像卡 → Notes → 最终数据）；
4. **回写层**：``GET /config/note-attributes`` 取 Notes 模板，``POST /callback/update-profile``
   写回最终 JSON。

两处口径：

- **有意差异 W2/W3**：11 个 ``gemini_retry_2_times`` 工具节点归一为 ``llm`` 节点，
  输出字段仍叫 ``result``；重试下沉到 ``llm_call()`` 内部。
- **回写默认关闭**：``POST /callback/update-profile`` 标记 ``@writeback``。缺省
  （``WRITEBACK_ENABLED=false``）时该节点**不发送请求**，只产出「已跳过」的可解释结果。
  测试与影子模式禁止生产回写（`Dify迁移任务说明.md` §9.3）。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "customer_profile_production"
DISPLAY_NAME = "（新）SubAgent - 画像内容生成&回写（生产环境）（新）"
SOURCE_DSL = f"{DISPLAY_NAME}.yml"

START = "1776754837871"
END = "1776759135563"

# —— 预置层（前 5 个无入参，INPUT_BUNDLE 有入参）
TAG_LIBRARY = "1776755019648"
EVIDENCE_FORMAT = "1776828906716"
PROFILE_FORMAT = "1776829070283"
PRODUCT_INFO = "1776853902103"
PRODUCT_INFO_SHORT = "1776855531118"
INVITE_STRATEGY = "1776934470389"
INPUT_BUNDLE = "1776828395715"

# —— code 合并层
AUDIO_EVIDENCE_CODE = "1776826087777"
ORIGINAL_INFO_CODE = "1777102704064"
PROFILE_CARD_CODE = "1776906696734"
SALES_LEAD_CODE = "1777020043726"
PROFILE_MERGE_CODE = "1776910309479"
NOTES_SPLIT_CODE = "1777260507680"
FINAL_MERGE_CODE = "1776911599997"

# —— HTTP
NOTES_TEMPLATE_HTTP = "1776911410253"
WRITEBACK_HTTP = "1776912964274"

# —— llm 节点（由 gemini_retry 工具节点归一而来）
DRIVE_EMOTION_PROMPT = "1777368178554"
DRIVE_EMOTION_LLM = "1777368717878"
SCRIPT_PROMPT = "1777371012576"
SCRIPT_LLM = "1777371252019"
SCENARIO_PROMPT = "1777371789490"
SCENARIO_LLM = "1777371804681"
TAG_FILTER_PROMPT = "1777374152638"
TAG_FILTER_LLM = "1777374158895"
TAGS_PROMPT = "1777371976537"
TAGS_LLM = "1777371986439"
OVERVIEW_PROMPT = "1777373490558"
OVERVIEW_LLM = "1777373496170"
LEAD_ANALYSIS_PROMPT = "1777373766807"
LEAD_ANALYSIS_LLM = "1777373760390"
LEVEL_PROMPT = "1777373071079"
LEVEL_LLM = "1777373077830"
LEAD_GEN_PROMPT = "1777373266562"
LEAD_GEN_LLM = "1777373257480"
NOTES_GEN_PROMPT = "1777372805721"
NOTES_GEN_LLM = "1777372801155"
NOTES_REVIEW_PROMPT = "1777372465294"
NOTES_REVIEW_LLM = "1777372470283"
NOTES_FIX_LLM = "1790051917170"

SLUG = "customer_profile_production"
SYSTEM_TEXT = "You are a helpful AI assistant."
NOTES_FIX_SYSTEM = "you are an useful assistant"
PROFILE_API_PREFIX = "/api/v1/remote/"


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


def _llm(
    node_id: str,
    title: str,
    prompt_id: str,
    *,
    system: str = SYSTEM_TEXT,
    after: Any = (),
):
    """构造一个由 ``gemini_retry`` 工具节点归一而来的 ``llm`` 节点。

    统一三件事：提示词来自上游模板节点的 ``output``、输出字段叫 ``result``
    （与 DSL 的 ``tool_parameters`` 消费方一致）、系统消息固定。收在一处，
    免得 11 份声明各写各的、日后改一处漏十处。
    """
    return make_node(
        node_id,
        title,
        "llm",
        after=after,
        inputs={"input_prompt": (prompt_id, "output")},
        outputs=("result",),
        prompt_field="input_prompt",
        output="result",
        system_text=system,
        original_node_id=node_id,
    )


# ====================================================================
# code 节点正文（逐字符取自 DSL，只把 def main 换成业务名；
# 跨节点重名的辅助函数加 __<节点后四位> 后缀，函数体一字未改）
# ====================================================================


# ---- DSL 节点 1776826087777 的正文（逐字符，仅函数名不同）
import json
import re


OUTBOUND_CALL_SOURCE = "外呼录音信息"
TEST_DRIVE_SOURCE = "试驾录音信息"


def _clean_json_string(value: str) -> str:
    """
    清理 LLM 可能输出的 ```json ... ``` 包裹。
    """
    value = value.strip()

    # 去掉 Markdown code fence
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*```$", "", value)

    return value.strip()


def _parse_json(value):
    """
    兼容 Dify 上游传入 String / Object 两种情况。
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        value = _clean_json_string(value)
        if not value:
            return {}
        return json.loads(value)

    raise ValueError(f"不支持的输入类型: {type(value)}")


def _normalize_source_list(evidence_source):
    """
    将 evidence_source 统一处理为 list[str]。
    兼容几种异常情况：
    - 正常情况：["微信主页截图信息", "外呼录音信息"]
    - 字符串情况："外呼录音信息"
    - 空值情况：None
    """
    if evidence_source is None:
        return []

    if isinstance(evidence_source, list):
        return [str(item).strip() for item in evidence_source if str(item).strip()]

    if isinstance(evidence_source, str):
        source = evidence_source.strip()
        return [source] if source else []

    return []


def extract_audio_evidence(json_str) -> dict:
    """
    输入参数：
    - json_str: JSON字符串，内部包含 evidence_items 数组

    输出：
    - outbound_call_evidence_items: 包含“外呼录音信息”的证据项数组
    - test_drive_evidence_items: 包含“试驾录音信息”的证据项数组
    - audio_evidence_items: 包含任一音频来源的证据项数组
    """

    data = _parse_json(json_str)

    evidence_items = data.get("evidence_items", [])

    if not isinstance(evidence_items, list):
        evidence_items = []

    outbound_call_evidence_items = []
    test_drive_evidence_items = []
    audio_evidence_items = []

    # 用于 audio_evidence_items 去重
    seen_audio_keys = set()

    for index, item in enumerate(evidence_items):
        if not isinstance(item, dict):
            continue

        evidence_source = _normalize_source_list(item.get("evidence_source"))

        has_outbound_call = OUTBOUND_CALL_SOURCE in evidence_source
        has_test_drive = TEST_DRIVE_SOURCE in evidence_source

        if has_outbound_call:
            outbound_call_evidence_items.append(item)

        if has_test_drive:
            test_drive_evidence_items.append(item)

        if has_outbound_call or has_test_drive:
            # 优先用 evidence_id 去重；没有 evidence_id 时用数组下标兜底
            evidence_id = item.get("evidence_id")
            dedup_key = evidence_id if evidence_id else f"__index_{index}"

            if dedup_key not in seen_audio_keys:
                audio_evidence_items.append(item)
                seen_audio_keys.add(dedup_key)

    return {
        "outbound_call_evidence_items": json.dumps(outbound_call_evidence_items, ensure_ascii=False),
        "test_drive_evidence_items": json.dumps(test_drive_evidence_items, ensure_ascii=False),
        "audio_evidence_items": json.dumps(audio_evidence_items, ensure_ascii=False)
    }


# ---- DSL 节点 1776906696734 的正文（逐字符，仅函数名不同）
import json
import re


def _strip_code_fence(text):
    """
    去掉 ```json ... ``` 包裹，兼容 LLM 产物。
    """
    if not isinstance(text, str):
        return text

    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json_input(value, expected_key, default_value):
    """
    解析上游传入的 JSON 字符串 / Object。
    只提取指定 key 的值。
    例如：
      profile_summary 输入: {"profile_summary": {...}}
      返回: {...}
    """
    if value is None or value == "":
        return default_value

    # 如果上游已经传来的是对象
    if isinstance(value, dict):
        return value.get(expected_key, default_value)

    # 如果是字符串，尝试按 JSON 解析
    if isinstance(value, str):
        cleaned = _strip_code_fence(value)
        if not cleaned:
            return default_value

        parsed = json.loads(cleaned)

        if isinstance(parsed, dict):
            return parsed.get(expected_key, default_value)

        return default_value

    return default_value


def build_profile_card_json(phone, batch_id, profile_summary, inferred_tags, usage_scenarios, communication_strategies) -> dict:
    # 解析四段 JSON 输入
    profile_summary_value = _parse_json_input(
        profile_summary,
        "profile_summary",
        {}
    )

    inferred_tags_value = _parse_json_input(
        inferred_tags,
        "inferred_tags",
        []
    )

    usage_scenarios_value = _parse_json_input(
        usage_scenarios,
        "usage_scenarios",
        []
    )

    communication_strategies_value = _parse_json_input(
        communication_strategies,
        "communication_strategies",
        []
    )

    # 组装最终 JSON
    result = {
        "phone": "" if phone is None else str(phone),
        "batch_id": "" if batch_id is None else str(batch_id),
        "analysis_result": {
            "profile_summary": profile_summary_value,
            "inferred_tags": inferred_tags_value,
            "usage_scenarios": usage_scenarios_value,
            "communication_strategies": communication_strategies_value
        }
    }

    # 输出为 JSON 字符串
    return {
        "result_json": json.dumps(result, ensure_ascii=False)
    }


# ---- DSL 节点 1776910309479 的正文（逐字符，仅函数名不同）
def merge_profile_card_data(json_text1: str, json_text2: str, json_text3: str) -> dict:
    import json

    def parse_json(text: str, field_name: str) -> dict:
        if text is None:
            raise ValueError(f"{field_name} 为空")
        text = str(text).strip()
        if not text:
            raise ValueError(f"{field_name} 为空字符串")

        try:
            data = json.loads(text)
        except Exception as e:
            raise ValueError(f"{field_name} 不是合法 JSON: {str(e)}")

        if not isinstance(data, dict):
            raise ValueError(f"{field_name} 顶层必须是 JSON Object")
        return data

    def validate_and_extract(data: dict, field_name: str):
        phone = data.get("phone")
        batch_id = data.get("batch_id")
        analysis_result = data.get("analysis_result")

        if phone is None:
            raise ValueError(f"{field_name} 缺少字段 phone")
        if batch_id is None:
            raise ValueError(f"{field_name} 缺少字段 batch_id")
        if analysis_result is None:
            raise ValueError(f"{field_name} 缺少字段 analysis_result")
        if not isinstance(analysis_result, dict):
            raise ValueError(f"{field_name}.analysis_result 必须是 JSON Object")

        return phone, batch_id, analysis_result

    data1 = parse_json(json_text1, "json_text1")
    data2 = parse_json(json_text2, "json_text2")
    data3 = parse_json(json_text3, "json_text3")

    phone1, batch_id1, analysis1 = validate_and_extract(data1, "json_text1")
    phone2, batch_id2, analysis2 = validate_and_extract(data2, "json_text2")
    phone3, batch_id3, analysis3 = validate_and_extract(data3, "json_text3")

    # 校验 phone 和 batch_id 一致
    if not (phone1 == phone2 == phone3):
        raise ValueError("三个 JSON 的 phone 不一致，无法合并")
    if not (batch_id1 == batch_id2 == batch_id3):
        raise ValueError("三个 JSON 的 batch_id 不一致，无法合并")

    # 合并 analysis_result，若出现重复 key，则直接报错，避免静默覆盖
    merged_analysis = {}
    for idx, analysis in enumerate([analysis1, analysis2, analysis3], start=1):
        for k, v in analysis.items():
            if k in merged_analysis:
                raise ValueError(f"analysis_result 中存在重复字段: {k}（来源 json_text{idx}）")
            merged_analysis[k] = v

    merged_result = {
        "phone": phone1,
        "batch_id": batch_id1,
        "analysis_result": merged_analysis
    }

    return {
        "result": json.dumps(merged_result, ensure_ascii=False, indent=2)
    }


# ---- DSL 节点 1776911599997 的正文（逐字符，仅函数名不同）
import json


def _parse_json__9997(value):
    """
    兼容 Dify 上游传入 String / Object 两种情况
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return {}
        return json.loads(value)

    raise ValueError(f"不支持的输入类型: {type(value)}")


def merge_final_data(json_str_1, json_str_2) -> dict:
    # 解析两个输入
    data1 = _parse_json__9997(json_str_1)
    data2 = _parse_json__9997(json_str_2)

    # 确保 analysis_result 存在
    if "analysis_result" not in data1 or not isinstance(data1["analysis_result"], dict):
        data1["analysis_result"] = {}

    # 取出第二份 JSON 中的 basic_notes_updates
    basic_notes_updates = data2.get("basic_notes_updates", [])

    # 插入到第一份 JSON 的 analysis_result 下
    data1["analysis_result"]["basic_notes_updates"] = basic_notes_updates

    # 输出对象 + 字符串两种格式，按需选用
    return {
        "merged_json": json.dumps(data1, ensure_ascii=False, indent=2)
    }


# ---- DSL 节点 1777020043726 的正文（逐字符，仅函数名不同）
import json
import re


def _clean_json_string__3726(value: str) -> str:
    """
    清理 LLM 可能输出的 ```json ... ``` 包裹
    """
    value = value.strip()

    # 去掉 ```json / ``` 包裹
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)

    return value.strip()


def _parse_json__3726(value, field_name: str):
    """
    兼容 Dify 上游传入 String / Object / None
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        value = _clean_json_string__3726(value)

        if not value:
            return {}

        try:
            return json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(f"{field_name} 不是合法 JSON 字符串：{str(e)}")

    raise ValueError(f"{field_name} 类型不支持，当前类型为：{type(value)}")


def _safe_get_dict(data: dict, path: list):
    """
    安全读取嵌套 dict。
    若路径不存在，返回空 dict。
    """
    current = data

    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key, {})

    if isinstance(current, dict):
        return current

    return {}


def _safe_get_list(data: dict, path: list):
    """
    安全读取嵌套 list。
    若路径不存在，返回空 list。
    """
    current = data

    for key in path:
        if not isinstance(current, dict):
            return []
        current = current.get(key, [])

    if isinstance(current, list):
        return current

    return []


def merge_sales_lead_data(phone, batch_id, business_level, customer_overview, customer_info) -> dict:
    # 1. 解析输入 JSON 字符串
    business_level_data = _parse_json__3726(business_level, "business_level")
    customer_overview_data = _parse_json__3726(customer_overview, "customer_overview")
    customer_info_data = _parse_json__3726(customer_info, "customer_info")

    # 2. 从 business_level 中提取 customer_overview
    business_customer_overview = _safe_get_dict(
        business_level_data,
        ["analysis_result", "customer_overview"]
    )

    business_opp_level = business_customer_overview.get("business_opp_level", {})
    closing_probability = business_customer_overview.get("closing_probability", {})

    # 3. 从 customer_overview 中提取 customer_overview
    customer_overview_obj = _safe_get_dict(
        customer_overview_data,
        ["analysis_result", "customer_overview"]
    )

    customer_type = customer_overview_obj.get("customer_type", {})
    current_stage = customer_overview_obj.get("current_stage", {})
    core_issue = customer_overview_obj.get("core_issue", {})
    breakthrough_point = customer_overview_obj.get("breakthrough_point", {})

    # 4. 从 customer_info 中提取三个数组
    purchase_motivations = _safe_get_list(
        customer_info_data,
        ["analysis_result", "purchase_motivations"]
    )

    product_preferences = _safe_get_list(
        customer_info_data,
        ["analysis_result", "product_preferences"]
    )

    resistances = _safe_get_list(
        customer_info_data,
        ["analysis_result", "resistances"]
    )

    # 5. 拼装最终 JSON
    merged_result = {
        "phone": phone or "",
        "batch_id": batch_id or "",
        "analysis_result": {
            "customer_overview": {
                "business_opp_level": business_opp_level,
                "closing_probability": closing_probability,
                "customer_type": customer_type,
                "current_stage": current_stage,
                "core_issue": core_issue,
                "breakthrough_point": breakthrough_point
            },
            "purchase_motivations": purchase_motivations,
            "product_preferences": product_preferences,
            "resistances": resistances
        }
    }

    # 6. 以 String 类型输出，避免 Dify 下游把它识别为 Object
    return {
        "result": json.dumps(merged_result, ensure_ascii=False)
    }


# ---- DSL 节点 1777102704064 的正文（逐字符，仅函数名不同）
import json
import re


def _clean_json_string__4064(value: str) -> str:
    """
    清理 LLM 可能输出的 ```json ... ``` 包裹
    """
    value = value.strip()

    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)

    return value.strip()


def _parse_json__4064(value, field_name: str = "json_str"):
    """
    兼容 Dify 上游传入 String / Object / None
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        value = _clean_json_string__4064(value)

        if not value:
            return {}

        try:
            data = json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(f"{field_name} 不是合法 JSON 字符串：{str(e)}")

        # 防止传入的是 JSON 数组、字符串、数字等非 Object 类型
        if not isinstance(data, dict):
            return {}

        return data

    raise ValueError(f"{field_name} 类型不支持，当前类型为：{type(value)}")


def organize_original_customer_info(json_str) -> dict:
    # 1. 解析输入 JSON
    # 如果输入不是合法 JSON 字符串，直接返回异常提示
    try:
        data = _parse_json__4064(json_str)
    except Exception:
        return {
            "result": "客户原始信息数据异常，请勿使用"
        }

    # 2. 读取 wechat_profile_info
    profile_info = data.get("wechat_profile_info", {})
    if not isinstance(profile_info, dict):
        profile_info = {}

    # 3. 读取 IT_system_data
    it_system_info = data.get("IT_system_data", {})
    if not isinstance(it_system_info, dict):
        it_system_info = {}

    # 4. 组装新的 JSON
    result_obj = {
        "客户昵称备注": it_system_info.get("ITsystem_customer_nick", ""),
        "微信昵称（wechat_nickname）": profile_info.get("nickname", ""),
        "性别（gender）": profile_info.get("gender", ""),
        "微信号（wechat_id）": profile_info.get("wechat_id", "")
    }

    # 5. 输出为 String 类型 JSON
    return {
        "result": json.dumps(result_obj, ensure_ascii=False)
    }


# ---- DSL 节点 1777260507680 的正文（逐字符，仅函数名不同）
import json
import re


def _clean_json_string__7680(value: str) -> str:
    """
    清理 LLM 可能输出的 ```json ... ``` 包裹
    """
    value = value.strip()

    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)

    return value.strip()


def _parse_json__7680(value, field_name: str = "json_str"):
    """
    兼容 Dify 上游传入 String / Object / None
    """
    if value is None:
        return {}

    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        value = _clean_json_string__7680(value)

        if not value:
            return {}

        try:
            return json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(f"{field_name} 不是合法 JSON 字符串：{str(e)}")

    raise ValueError(f"{field_name} 类型不支持，当前类型为：{type(value)}")


def split_notes_result(json_str) -> dict:
    # 1. 解析输入 JSON 字符串
    data = _parse_json__7680(json_str)

    # 2. 提取两个字段
    problem_list = data.get("problem_list", [])
    basic_notes_updates = data.get("basic_notes_updates", [])

    # 3. 类型兜底
    if not isinstance(problem_list, list):
        problem_list = []

    if not isinstance(basic_notes_updates, list):
        basic_notes_updates = []

    # 4. 输出时保留原始字段标签
    problem_list_result = {
        "problem_list": problem_list
    }

    basic_notes_updates_result = {
        "basic_notes_updates": basic_notes_updates
    }

    # 5. 分别输出为 String 类型 JSON 字符串
    return {
        "problem_list": json.dumps(problem_list_result, ensure_ascii=False),
        "basic_notes_updates": json.dumps(basic_notes_updates_result, ensure_ascii=False)
    }


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=SOURCE_DSL,
    entries=(START,),
    exits=(END,),
    outputs={"result1": "merged_json"},
    nodes=(
        make_node(
            START,
            "用户输入",
            "start",
            variables=(
                "phone_number",
                "evidence_items",
                "profile_analysis",
                "hobby_analysis",
                "consumption_style_analysis",
                "batch_id",
                "original_cus_data",
            ),
            required_phone_number=True,
            required_evidence_items=True,
            required_profile_analysis=True,
            required_hobby_analysis=True,
            required_consumption_style_analysis=True,
            required_batch_id=False,
            required_original_cus_data=False,
            coords=(80.0, 282.0),
        ),
        # ---------------------------------------------------------- 预置层
        make_node(
            TAG_LIBRARY,
            "标签库预置",
            "template-transform",
            after=(START,),
            outputs=("output",),
            template=template_name(TAG_LIBRARY),
            original_node_id=TAG_LIBRARY,
        ),
        make_node(
            EVIDENCE_FORMAT,
            "证据线索数据格式预置",
            "template-transform",
            after=(START,),
            outputs=("output",),
            template=template_name(EVIDENCE_FORMAT),
            original_node_id=EVIDENCE_FORMAT,
        ),
        make_node(
            PROFILE_FORMAT,
            "特征画像数据格式预置",
            "template-transform",
            after=(START,),
            outputs=("output",),
            template=template_name(PROFILE_FORMAT),
            original_node_id=PROFILE_FORMAT,
        ),
        make_node(
            PRODUCT_INFO,
            "产品信息及卖点预置",
            "template-transform",
            after=(START,),
            outputs=("output",),
            template=template_name(PRODUCT_INFO),
            original_node_id=PRODUCT_INFO,
        ),
        make_node(
            PRODUCT_INFO_SHORT,
            "产品信息及卖点预置（速查简化版）",
            "template-transform",
            after=(START,),
            outputs=("output",),
            template=template_name(PRODUCT_INFO_SHORT),
            original_node_id=PRODUCT_INFO_SHORT,
        ),
        make_node(
            INVITE_STRATEGY,
            "邀约策略&话术预置",
            "template-transform",
            after=(START,),
            outputs=("output",),
            template=template_name(INVITE_STRATEGY),
            original_node_id=INVITE_STRATEGY,
        ),
        # ---------------------------------------------------------- code 首段
        make_node(
            AUDIO_EVIDENCE_CODE,
            "录音类证据提取",
            "code",
            after=(
                TAG_LIBRARY,
                EVIDENCE_FORMAT,
                PROFILE_FORMAT,
                PRODUCT_INFO,
                PRODUCT_INFO_SHORT,
                INVITE_STRATEGY,
                ORIGINAL_INFO_CODE,
            ),
            inputs={"json_str": (START, "evidence_items")},
            outputs=(
                "audio_evidence_items",
                "outbound_call_evidence_items",
                "test_drive_evidence_items",
            ),
            function=f"customer_profile.workflows.{SLUG}:extract_audio_evidence",
            original_node_id=AUDIO_EVIDENCE_CODE,
        ),
        make_node(
            ORIGINAL_INFO_CODE,
            "客户原始信息整理",
            "code",
            after=(START,),
            inputs={"json_str": (START, "original_cus_data")},
            outputs=("result",),
            function=f"customer_profile.workflows.{SLUG}:organize_original_customer_info",
            original_node_id=ORIGINAL_INFO_CODE,
        ),
        make_node(
            INPUT_BUNDLE,
            "信息输入包整合",
            "template-transform",
            after=(AUDIO_EVIDENCE_CODE,),
            inputs={
                "evidence_items": (START, "evidence_items"),
                "profile_analysis": (START, "profile_analysis"),
                "hobby_analysis": (START, "hobby_analysis"),
                "consumption_style_analysis": (START, "consumption_style_analysis"),
            },
            outputs=("output",),
            template=template_name(INPUT_BUNDLE),
            original_node_id=INPUT_BUNDLE,
        ),
        # ---------------------------------------------------------- 生成层
        make_node(
            DRIVE_EMOTION_PROMPT,
            "客户试驾情绪分析prompt",
            "template-transform",
            after=(INPUT_BUNDLE,),
            inputs={
                "evidence_output": (EVIDENCE_FORMAT, "output"),
                "phone_number": (START, "phone_number"),
                "batch_id": (START, "batch_id"),
                "evidence_items": (START, "evidence_items"),
                "message_output": (PRODUCT_INFO_SHORT, "output"),
            },
            outputs=("output",),
            template=template_name(DRIVE_EMOTION_PROMPT),
            original_node_id=DRIVE_EMOTION_PROMPT,
        ),
        _llm(
            DRIVE_EMOTION_LLM,
            "客户试驾情绪分析 - 工具",
            DRIVE_EMOTION_PROMPT,
            after=(DRIVE_EMOTION_PROMPT,),
        ),
        make_node(
            SCRIPT_PROMPT,
            "客户沟通话术生成Prompt",
            "template-transform",
            after=(INPUT_BUNDLE,),
            inputs={
                "evidence_output": (EVIDENCE_FORMAT, "output"),
                "output": (PROFILE_FORMAT, "output"),
                "output_1": (INPUT_BUNDLE, "output"),
                "output_3": (PRODUCT_INFO, "output"),
                "output_4": (INVITE_STRATEGY, "output"),
            },
            outputs=("output",),
            template=template_name(SCRIPT_PROMPT),
            original_node_id=SCRIPT_PROMPT,
        ),
        _llm(SCRIPT_LLM, " 客户沟通话术生成 - 工具", SCRIPT_PROMPT, after=(SCRIPT_PROMPT,)),
        make_node(
            SCENARIO_PROMPT,
            "客户用车场景生成Prompt",
            "template-transform",
            after=(INPUT_BUNDLE,),
            inputs={
                "output": (EVIDENCE_FORMAT, "output"),
                "output_1": (PROFILE_FORMAT, "output"),
                "output_2": (INPUT_BUNDLE, "output"),
            },
            outputs=("output",),
            template=template_name(SCENARIO_PROMPT),
            original_node_id=SCENARIO_PROMPT,
        ),
        _llm(SCENARIO_LLM, "客户用车场景生成 - 工具", SCENARIO_PROMPT, after=(SCENARIO_PROMPT,)),
        make_node(
            TAG_FILTER_PROMPT,
            "客户标签初筛Prompt",
            "template-transform",
            after=(INPUT_BUNDLE,),
            inputs={
                "output": (EVIDENCE_FORMAT, "output"),
                "output_1": (PROFILE_FORMAT, "output"),
                "output_2": (INPUT_BUNDLE, "output"),
                "result": (ORIGINAL_INFO_CODE, "result"),
                "output_3": (TAG_LIBRARY, "output"),
            },
            outputs=("output",),
            template=template_name(TAG_FILTER_PROMPT),
            original_node_id=TAG_FILTER_PROMPT,
        ),
        _llm(TAG_FILTER_LLM, "客户标签初筛 - 工具", TAG_FILTER_PROMPT, after=(TAG_FILTER_PROMPT,)),
        make_node(
            TAGS_PROMPT,
            "客户标签生成Prompt",
            "template-transform",
            after=(TAG_FILTER_LLM,),
            inputs={
                "output": (EVIDENCE_FORMAT, "output"),
                "output_1": (PROFILE_FORMAT, "output"),
                "output_2": (INPUT_BUNDLE, "output"),
                "text": (TAG_FILTER_LLM, "result"),
            },
            outputs=("output",),
            template=template_name(TAGS_PROMPT),
            original_node_id=TAGS_PROMPT,
        ),
        _llm(TAGS_LLM, "客户标签生成 - 工具", TAGS_PROMPT, after=(TAGS_PROMPT,)),
        make_node(
            OVERVIEW_PROMPT,
            "客户总体信息生成Prompt",
            "template-transform",
            after=(TAG_FILTER_LLM,),
            inputs={
                "phone_number": (START, "phone_number"),
                "output": (EVIDENCE_FORMAT, "output"),
                "output_1": (PROFILE_FORMAT, "output"),
                "output_2": (INPUT_BUNDLE, "output"),
                "result": (ORIGINAL_INFO_CODE, "result"),
                "text": (TAG_FILTER_LLM, "result"),
                "output_3": (PRODUCT_INFO_SHORT, "output"),
            },
            outputs=("output",),
            template=template_name(OVERVIEW_PROMPT),
            original_node_id=OVERVIEW_PROMPT,
        ),
        _llm(OVERVIEW_LLM, "客户总体信息生成 - 工具", OVERVIEW_PROMPT, after=(OVERVIEW_PROMPT,)),
        make_node(
            LEAD_ANALYSIS_PROMPT,
            "客户销售线索分析Prompt",
            "template-transform",
            after=(INPUT_BUNDLE,),
            inputs={
                "output": (EVIDENCE_FORMAT, "output"),
                "output_1": (PROFILE_FORMAT, "output"),
                "output_2": (INPUT_BUNDLE, "output"),
                "output_3": (PRODUCT_INFO_SHORT, "output"),
            },
            outputs=("output",),
            template=template_name(LEAD_ANALYSIS_PROMPT),
            original_node_id=LEAD_ANALYSIS_PROMPT,
        ),
        _llm(LEAD_ANALYSIS_LLM, "客户销售线索分析 - 工具", LEAD_ANALYSIS_PROMPT, after=(LEAD_ANALYSIS_PROMPT,)),
        make_node(
            LEVEL_PROMPT,
            "客户等级及成交概率计算Prompt",
            "template-transform",
            after=(LEAD_ANALYSIS_LLM,),
            inputs={
                "output": (EVIDENCE_FORMAT, "output"),
                "output_1": (PROFILE_FORMAT, "output"),
                "output_2": (INPUT_BUNDLE, "output"),
                "text": (LEAD_ANALYSIS_LLM, "result"),
                "output_3": (PRODUCT_INFO_SHORT, "output"),
            },
            outputs=("output",),
            template=template_name(LEVEL_PROMPT),
            original_node_id=LEVEL_PROMPT,
        ),
        _llm(LEVEL_LLM, "客户等级及成交概率计算 - 工具", LEVEL_PROMPT, after=(LEVEL_PROMPT,)),
        make_node(
            LEAD_GEN_PROMPT,
            "客户销售线索生成Prompt",
            "template-transform",
            after=(LEAD_ANALYSIS_LLM, TAG_FILTER_LLM),
            inputs={
                "output": (PRODUCT_INFO_SHORT, "output"),
                "text": (LEAD_ANALYSIS_LLM, "result"),
                "text_1": (TAG_FILTER_LLM, "result"),
                "output_1": (INPUT_BUNDLE, "output"),
                "output_2": (EVIDENCE_FORMAT, "output"),
                "output_3": (PROFILE_FORMAT, "output"),
            },
            outputs=("output",),
            template=template_name(LEAD_GEN_PROMPT),
            original_node_id=LEAD_GEN_PROMPT,
        ),
        _llm(LEAD_GEN_LLM, "客户销售线索生成 - 工具", LEAD_GEN_PROMPT, after=(LEAD_GEN_PROMPT,)),
        # ---------------------------------------------------------- 合并层
        make_node(
            PROFILE_CARD_CODE,
            "人设画像卡信息合并",
            "code",
            after=(SCRIPT_LLM, SCENARIO_LLM, TAGS_LLM, OVERVIEW_LLM),
            inputs={
                "phone": (START, "phone_number"),
                "batch_id": (START, "batch_id"),
                "profile_summary": (OVERVIEW_LLM, "result"),
                "inferred_tags": (TAGS_LLM, "result"),
                "usage_scenarios": (SCENARIO_LLM, "result"),
                "communication_strategies": (SCRIPT_LLM, "result"),
            },
            outputs=("result_json",),
            function=f"customer_profile.workflows.{SLUG}:build_profile_card_json",
            original_node_id=PROFILE_CARD_CODE,
        ),
        make_node(
            SALES_LEAD_CODE,
            "销售线索数据合并",
            "code",
            after=(LEVEL_LLM, LEAD_GEN_LLM),
            inputs={
                "phone": (START, "phone_number"),
                "batch_id": (START, "batch_id"),
                "business_level": (LEVEL_LLM, "result"),
                "customer_overview": (LEAD_GEN_LLM, "result"),
                "customer_info": (LEAD_ANALYSIS_LLM, "result"),
            },
            outputs=("result",),
            function=f"customer_profile.workflows.{SLUG}:merge_sales_lead_data",
            original_node_id=SALES_LEAD_CODE,
        ),
        make_node(
            PROFILE_MERGE_CODE,
            "客户画像卡数据合并",
            "code",
            after=(PROFILE_CARD_CODE, SALES_LEAD_CODE, DRIVE_EMOTION_LLM),
            inputs={
                "json_text1": (PROFILE_CARD_CODE, "result_json"),
                "json_text2": (SALES_LEAD_CODE, "result"),
                "json_text3": (DRIVE_EMOTION_LLM, "result"),
            },
            outputs=("result",),
            function=f"customer_profile.workflows.{SLUG}:merge_profile_card_data",
            original_node_id=PROFILE_MERGE_CODE,
        ),
        make_node(
            NOTES_TEMPLATE_HTTP,
            "获取Notes模版",
            "http-request",
            after=(OVERVIEW_LLM,),
            outputs=("body", "status"),
            **{
                "@service": "profile",
                "@method": "get",
                "@path": f"{PROFILE_API_PREFIX}config/note-attributes",
                "@auth": True,
                "@outputs": {"body": "$text", "status": "$status"},
            },
            original_node_id=NOTES_TEMPLATE_HTTP,
        ),
        make_node(
            NOTES_GEN_PROMPT,
            "Notes生成Prompt",
            "template-transform",
            after=(NOTES_TEMPLATE_HTTP,),
            inputs={
                "output": (EVIDENCE_FORMAT, "output"),
                "output_1": (PROFILE_FORMAT, "output"),
                "body": (NOTES_TEMPLATE_HTTP, "body"),
                "output_2": (INPUT_BUNDLE, "output"),
                "result": (ORIGINAL_INFO_CODE, "result"),
                "output_3": (PRODUCT_INFO_SHORT, "output"),
            },
            outputs=("output",),
            template=template_name(NOTES_GEN_PROMPT),
            original_node_id=NOTES_GEN_PROMPT,
        ),
        _llm(NOTES_GEN_LLM, "Notes生成 - 工具", NOTES_GEN_PROMPT, after=(NOTES_GEN_PROMPT,)),
        make_node(
            NOTES_REVIEW_PROMPT,
            "Notes审核Prompt",
            "template-transform",
            after=(PROFILE_MERGE_CODE, NOTES_GEN_LLM),
            inputs={
                "output": (EVIDENCE_FORMAT, "output"),
                "body": (NOTES_TEMPLATE_HTTP, "body"),
                "text": (NOTES_GEN_LLM, "result"),
                "output_1": (INPUT_BUNDLE, "output"),
                "result_json": (PROFILE_CARD_CODE, "result_json"),
                "result": (ORIGINAL_INFO_CODE, "result"),
                "output_2": (PRODUCT_INFO_SHORT, "output"),
            },
            outputs=("output",),
            template=template_name(NOTES_REVIEW_PROMPT),
            original_node_id=NOTES_REVIEW_PROMPT,
        ),
        _llm(NOTES_REVIEW_LLM, "Notes审核 - 工具", NOTES_REVIEW_PROMPT, after=(NOTES_REVIEW_PROMPT,)),
        make_node(
            NOTES_FIX_LLM,
            "LLM",
            "llm",
            after=(NOTES_REVIEW_LLM,),
            inputs={"result": (NOTES_REVIEW_LLM, "result")},
            outputs=("text",),
            template=template_name(NOTES_FIX_LLM),
            system_text=NOTES_FIX_SYSTEM,
            # 下游 code 节点对它的输出做 JSON 解析 → 按消费方语义要求 JSON（§6.4.2 / M2）
            expect_json=True,
            original_node_id=NOTES_FIX_LLM,
        ),
        make_node(
            NOTES_SPLIT_CODE,
            "Notes结果拆分",
            "code",
            after=(NOTES_FIX_LLM,),
            inputs={"json_str": (NOTES_FIX_LLM, "text")},
            outputs=("basic_notes_updates", "problem_list"),
            function=f"customer_profile.workflows.{SLUG}:split_notes_result",
            original_node_id=NOTES_SPLIT_CODE,
        ),
        make_node(
            FINAL_MERGE_CODE,
            "最终数据合并",
            "code",
            after=(PROFILE_MERGE_CODE, NOTES_SPLIT_CODE),
            inputs={
                "json_str_1": (PROFILE_MERGE_CODE, "result"),
                "json_str_2": (NOTES_SPLIT_CODE, "basic_notes_updates"),
            },
            outputs=("merged_json",),
            function=f"customer_profile.workflows.{SLUG}:merge_final_data",
            original_node_id=FINAL_MERGE_CODE,
        ),
        # ---------------------------------------------------------- 回写层
        make_node(
            WRITEBACK_HTTP,
            "HTTP 请求 2",
            "http-request",
            after=(FINAL_MERGE_CODE,),
            outputs=("body", "status"),
            **{
                "@service": "profile",
                "@method": "post",
                "@path": f"{PROFILE_API_PREFIX}callback/update-profile",
                "@auth": True,
                # 请求体逐字符取自 DSL；引用位于 JSON 字符串**内部**，因此只转义、不补引号
                "@body_template": (
                    '{"data": [{"id": "key-value-149", "key": "", "type": "text", '
                    '"value": "{{#1776911599997.merged_json#}}"}], "type": "json"}'
                ),
                # 生产回写：WRITEBACK_ENABLED=false 时不发送（§9.3）
                "@writeback": True,
                "@outputs": {"body": "$text", "status": "$status"},
            },
            original_node_id=WRITEBACK_HTTP,
        ),
        make_node(
            END,
            "输出",
            "end",
            after=(WRITEBACK_HTTP,),
            inputs={"result1": (FINAL_MERGE_CODE, "merged_json")},
            outputs=("result1",),
            original_node_id=END,
        ),
    ),
)


def default_inputs(
    *,
    phone_number: str,
    evidence_items: str,
    profile_analysis: str,
    hobby_analysis: str,
    consumption_style_analysis: str,
    batch_id: str = "",
    original_cus_data: str = "",
) -> dict[str, Any]:
    """该工作流的入参契约：前五项必填，后两项可选。"""
    return {
        "phone_number": phone_number,
        "evidence_items": evidence_items,
        "profile_analysis": profile_analysis,
        "hobby_analysis": hobby_analysis,
        "consumption_style_analysis": consumption_style_analysis,
        "batch_id": batch_id,
        "original_cus_data": original_cus_data,
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
    '1776826087777': {'main': 'extract_audio_evidence', 'renames': {}},
    '1776906696734': {'main': 'build_profile_card_json', 'renames': {}},
    '1776910309479': {'main': 'merge_profile_card_data', 'renames': {}},
    '1776911599997': {'main': 'merge_final_data', 'renames': {"_parse_json": "_parse_json__9997"}},
    '1777020043726': {'main': 'merge_sales_lead_data', 'renames': {"_clean_json_string": "_clean_json_string__3726", "_parse_json": "_parse_json__3726"}},
    '1777102704064': {'main': 'organize_original_customer_info', 'renames': {"_clean_json_string": "_clean_json_string__4064", "_parse_json": "_parse_json__4064"}},
    '1777260507680': {'main': 'split_notes_result', 'renames': {"_clean_json_string": "_clean_json_string__7680", "_parse_json": "_parse_json__7680"}},
}
