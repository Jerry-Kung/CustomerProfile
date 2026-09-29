"""``（新）SubAgent - 手机号&企业信息分析`` 的 Python 定义。

DSL 基线：8 节点 / 8 边。本流程承接自 ``证据线索汇总``——原先那四个节点（取关联企业、
拆运营商/企业、两个 LLM）迁到这里独立成一个子流程，主图只留一个 ``tool`` 调用点。

    start(phone_number, customer_data, data_source=RelatedEnterpriseInfo)
      └─ code 代码执行 3（取 RelatedEnterpriseInfo 的 raw_payload）
           └─ code 结果解析（拆 operator_location / affiliated_company）
                ├─ llm 手机号特征分析 → ┐
                └─ llm 企业信息分析  → ┴→ variable-aggregator（分组）
                                            └─ code 聚合结果拆壳
                                                 └─ end(phonenumber_analysis_result,
                                                        company_analysis_result)

四处口径：

- **两个 LLM 的输出字段是 ``text`` 而不是 ``result``**：聚合器按 ``.text`` 取值，
  与 ``证据线索整理`` 那类 ``result`` 形态的 llm 节点不同，照抄会静默取空。
- **分组聚合器沿用 P3 实测口径**：每组包成 ``{"output": 值}``，因此下游必须有一个
  拆壳节点（``聚合结果拆壳``）取 ``["output"]``——这正是 DSL 里的做法。
- **有意差异 W4**：``start`` 的 ``llm_model`` 入参按 §6.4.5 删除。
- **有意差异 W50**：``结果解析`` 的取值键按真实载荷改写。DSL 原文取
  ``operator_location_json`` / ``affiliated_company_json``，而该渠道的真实载荷从不含这两个
  键（全部留痕实测出现 0 次），产出的两个字段恒为 ``{}``、企业数据被静默丢弃。现按真实
  载荷的两种形态取值并保留对原键名的兼容读取，理由见 ``ledger/differences.md`` W50。
"""

from __future__ import annotations

import json
from typing import Any

from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "phone_company_analysis"
DISPLAY_NAME = "（新）SubAgent - 手机号&企业信息分析"
SOURCE_DSL = f"{DISPLAY_NAME}.yml"

START = "1773748103758"
END = "1773748343861"

# —— 取数与解析
CHECK_CODE = "1776328382353"
PARSE_CODE = "1774941932987"

# —— 两个 LLM
PHONE_LLM = "1789984359173"
COMPANY_LLM = "1789984439065"

# —— 聚合与拆壳
AGGREGATE = "1774945047132"
UNWRAP_CODE = "1774946602973"

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
SLUG = "phone_company_analysis"
SYSTEM_TEXT = "You are a helpful AI assistant."

DATA_SOURCE = "RelatedEnterpriseInfo"
"""本流程默认取的渠道名，与 DSL ``start`` 的 ``data_source`` 默认值一致。"""


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


# ====================================================================
# code 节点正文（逐字符取自 DSL，只把 def main 换成业务名；函数体一字未改）
# ====================================================================


COMPANY_BLOCK_KEYS = (
    "company_main",
    "company_detail",
    "business_info",
    "enterprise_base_info",
    "shareholder_info",
)
"""载荷里承载「关联企业」内容的块名，按真实载荷的两种形态列出。

形态一（企业查询有结果）：``company_main`` / ``company_detail``；
形态二（天眼查）：``business_info`` / ``enterprise_base_info`` / ``shareholder_info``。
取哪几个由载荷自己决定，代码不预设。
"""


def has_content(value) -> bool:
    """判定一个字段是否携带实内容。

    ``None``、空串、空容器、以及字符串形态的 ``"false"`` / ``"null"`` 都算空——
    真实载荷里 ``have_company_info`` 是字符串 ``"false"``，``company_main`` 是 ``null``，
    照「非 None 即算有数据」的写法会把它们渲染成一张全是空字段的企业报告。
    """
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "null", "none")
    if isinstance(value, (dict, list, tuple, set)):
        return len(value) > 0
    return True


# ---- DSL 节点 1774941932987 的正文（**有意差异 W50**，理由见下）
def split_operator_and_company(body) -> dict:
    """把 ``RelatedEnterpriseInfo`` 的 ``raw_payload`` 拆成归属地与企业两块。

    **有意差异 W50**：DSL 原文按 ``operator_location_json`` / ``affiliated_company_json``
    两个键取值，而该渠道的真实载荷从来不含这两个键——全部留痕实测（5 个批次、8 份
    响应）里它们出现次数为 0。后果是产出的两个字段恒为 ``{}``，企业信息分析 LLM 只能
    回「输入数据为空」，真实的企业数据被静默丢弃。本函数改为按真实载荷的两种形态取值，
    并保留对 DSL 原键名的兼容读取。
    """
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

    # 归属地：新形态把字段摊在 ``operator_location`` 下，旧形态用 ``operator_location_json``。
    # 两者都认，取到哪个用哪个；都取不到时退回空对象（下游据此输出「无数据」）。
    operator_location = data.get("operator_location")
    if operator_location is None:
        operator_location = data.get("operator_location_json", {})

    # 关联企业：逐个收集**有实内容**的块，空的（None / 空容器 / "false"）一律不收。
    affiliated_company = {}
    for key in COMPANY_BLOCK_KEYS:
        if has_content(data.get(key)):
            affiliated_company[key] = data[key]
    if not affiliated_company and has_content(data.get("affiliated_company_json")):
        # 旧形态的兼容读取：整块就是企业内容本身，不再包一层
        affiliated_company = data["affiliated_company_json"]

    return {
        "operator_location": json.dumps(
            operator_location,
            ensure_ascii=False
        ),
        "affiliated_company": json.dumps(
            affiliated_company,
            ensure_ascii=False
        )
    }


# ---- DSL 节点 1774946602973 的正文（逐字符，仅函数名不同）
def unwrap_grouped_analysis(company_result_object, phonenumber_result_object) -> dict:
    return {
        "company_result": company_result_object["output"],
        "phonenumber_result": phonenumber_result_object["output"]
    }


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=SOURCE_DSL,
    entries=(START,),
    exits=(END,),
    outputs={
        "phonenumber_analysis_result": "phonenumber_result",
        "company_analysis_result": "company_result",
    },
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
            CHECK_CODE,
            "代码执行 3",
            "code",
            after=(START,),
            inputs={
                "records": (START, "customer_data"),
                "data_source": (START, "data_source"),
            },
            outputs=("result",),
            function=f"{SHARED_CODE_MODULE}:extract_raw_payload",
            original_node_id=CHECK_CODE,
        ),
        make_node(
            PARSE_CODE,
            "结果解析",
            "code",
            after=(CHECK_CODE,),
            inputs={"body": (CHECK_CODE, "result")},
            outputs=("operator_location", "affiliated_company"),
            function=f"customer_profile.workflows.{SLUG}:split_operator_and_company",
            original_node_id=PARSE_CODE,
        ),
        make_node(
            PHONE_LLM,
            "手机号特征分析",
            "llm",
            after=(PARSE_CODE,),
            inputs={
                "phone_number": (START, "phone_number"),
                "operator_location": (PARSE_CODE, "operator_location"),
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
            after=(PARSE_CODE,),
            inputs={"affiliated_company": (PARSE_CODE, "affiliated_company")},
            outputs=("text",),
            template=template_name(COMPANY_LLM),
            system_text=SYSTEM_TEXT,
            original_node_id=COMPANY_LLM,
        ),
        make_node(
            AGGREGATE,
            "变量聚合器",
            "variable-aggregator",
            after=(PHONE_LLM, COMPANY_LLM),
            outputs=("phonenumber_result", "company_result"),
            groups=(
                {
                    "group_name": "company_result",
                    "output_type": "string",
                    "variables": ((COMPANY_LLM, "text"),),
                },
                {
                    "group_name": "phonenumber_result",
                    "output_type": "string",
                    "variables": ((PHONE_LLM, "text"),),
                },
            ),
            original_node_id=AGGREGATE,
        ),
        make_node(
            UNWRAP_CODE,
            "聚合结果拆壳",
            "code",
            after=(AGGREGATE,),
            inputs={
                "company_result_object": (AGGREGATE, "company_result"),
                "phonenumber_result_object": (AGGREGATE, "phonenumber_result"),
            },
            outputs=("company_result", "phonenumber_result"),
            function=f"customer_profile.workflows.{SLUG}:unwrap_grouped_analysis",
            original_node_id=UNWRAP_CODE,
        ),
        make_node(
            END,
            "输出",
            "end",
            after=(UNWRAP_CODE,),
            inputs={
                "phonenumber_analysis_result": (UNWRAP_CODE, "phonenumber_result"),
                "company_analysis_result": (UNWRAP_CODE, "company_result"),
            },
            outputs=("phonenumber_analysis_result", "company_analysis_result"),
            original_node_id=END,
        ),
    ),
)


def default_inputs(customer_data: str, phone_number: str = "") -> dict[str, Any]:
    return {
        "phone_number": phone_number,
        "customer_data": customer_data,
        "data_source": DATA_SOURCE,
    }


# ====================================================================
# code 节点 -> 函数名映射（供 tests/test_code_verbatim.py 定位比对）
# ====================================================================

CODE_SPECS: dict[str, dict] = {
    '1774941932987': {'main': 'split_operator_and_company', 'renames': {}},
    '1774946602973': {'main': 'unwrap_grouped_analysis', 'renames': {}},
}
