"""**事实可读化渲染**：把机器形态的 ``facts`` 翻成中文「字段名 → 取值」清单。

``facts`` 天生是机器形态——键是英文字段键（``planned_purchase_time``），枚举值是
英文键（``within_3_months``）。这套形态只适合代码消费；一旦原样 ``json.dumps`` 喂给
LLM，模型就会把英文键写进对客产物（2026-10-20 实测：「已定稿事实显示牌照状态为
needs_license、付款方式为loan」）。因此**凡是把事实喂给模型的装配点，都必须过这里的
渲染**，而不是 dump 原文。

反例：``L1-1c`` 分析投影层的输入**刻意保留**英文键——它输出的就是枚举键，喂中文名
反而会诱发模型输出中文值。渲染只用于「事实作为背景资料」的场合，不用于「事实作为
输出模板」的场合。
"""

from __future__ import annotations

from typing import Any, Mapping

from . import internal_fields as inf

MULTI_JOIN = "、"
"""多值之间的连接符（与 Notes 的 ``||`` 区分：这里只求可读）。"""

NESTED_KEY_NAMES: dict[str, str] = {
    # ``deal_level_detail``（:func:`customer_profile.workflows.customer_profile_pipeline.apply_level`
    # 写入）的键名表。嵌套结构不走契约，只能在这里登记。
    "summary": "结论",
    "steps": "推理链",
    "evidence": "依据",
    "confidence": "置信度",
    "level": "等级",
    "stage": "阶段",
    "time_window": "时间窗口",
    "dimensions": "维度",
    "want_to_buy": "购车需求",
    "can_to_buy": "购买条件",
    "when_to_buy": "购车时间",
    "homework_evidence": "推进动作",
    "value": "结论",
}
"""嵌套结构里的键名。

查不到的键保留原键——宁可露出一个英文键，也不静默丢内容（渲染层不做有损裁剪）。
"""

EMPTY_TEXT = "（暂无已定稿事实）"
"""事实为空时的占位：让模型看到「确实没有」，而不是看到一段空白自行发挥。"""


def readable_key(attribute: str) -> str:
    """字段键 → 中文名（契约字段读 ``attribute_name``，编外字段读登记表）。"""
    key = str(attribute)
    name = inf.name_of(key)
    if name != key:
        return name
    # 既不在契约也不在编外登记表里的内部键（定级环节写入的 ``deal_level_detail``、
    # ``business_opp_stage`` 等）。它们只在这里有中文名，不补就会原样露出英文键。
    nested = NESTED_KEY_NAMES.get(key)
    if nested:
        return nested
    return FALLBACK_FIELD_NAMES.get(key, key)


FALLBACK_FIELD_NAMES: dict[str, str] = {
    "business_opp_stage": "业务阶段",
    "deal_level_detail": "商机定级详情",
}
"""契约与编外登记表都没有、但确实会进 ``facts`` 的字段。

新增这类键时必须在此登记——漏登记的表现不是报错，而是提示词里悄悄出现一个英文键。
"""


def _enum_display(attribute: str, text: str) -> str:
    """单个枚举键 → 展示名；不是合法键则原样返回（客户原话不得被改写）。

    一处例外：**英文占位串**（``unknown`` 一类）不原样透出。它是 L1 在无线索时填的
    空值标记，不是客户说的话，映射不到展示名时给空串让整行省略——「付款方式：unknown」
    对销售没有信息量，反而让内部取值形态漏进画像卡（英文残留治理方案 §4.3）。
    """
    display = inf.display_map(attribute)
    mapped = display.get(text)
    if mapped:
        return str(mapped)
    if inf.is_english_placeholder(attribute, text):
        return ""
    return text


def readable_value(attribute: str, value: Any) -> str:
    """把单个字段的取值渲染成中文可读文本。

    四种形态与 ``projection.serialize_value`` 的出口形态一一对应：

    - 结构化枚举项（``{"key": ..., "input_value": ...}``）→「需家人同意（配偶）」；
    - 多选枚举（``a||b``）→ 逐项翻展示名后用顿号连接；
    - 列表 → 逐项递归；
    - 其它自由文本 → 原样透传（客户原话是中文，不改写）。
    """
    if value is None:
        return ""
    if isinstance(value, Mapping):
        key = str(value.get("key") or "").strip()
        if key or "input_value" in value:
            name = _enum_display(attribute, key) if key else ""
            extra = str(value.get("input_value") or "").strip()
            if name and extra:
                return f"{name}（{extra}）"
            return name or extra
        parts = []
        for nested_key, nested_value in value.items():
            name = NESTED_KEY_NAMES.get(str(nested_key), readable_key(str(nested_key)))
            rendered = readable_value(str(nested_key), nested_value)
            if rendered:
                parts.append(f"{name}：{rendered}")
        return "；".join(parts)
    if isinstance(value, (list, tuple)):
        parts = [readable_value(attribute, item) for item in value]
        return MULTI_JOIN.join(part for part in parts if part)
    text = str(value).strip()
    if not text:
        return ""
    if inf.is_enum(attribute) and "||" in text:
        items = [part.strip() for part in text.split("||") if part.strip()]
        return MULTI_JOIN.join(_enum_display(attribute, item) for item in items)
    if inf.is_enum(attribute):
        return _enum_display(attribute, text)
    return text


def readable_facts(facts: Any, *, empty_text: str = EMPTY_TEXT) -> str:
    """整份 ``facts`` → 逐行「中文字段名：中文取值」。

    键按字典序排列，保证同一份事实每次渲染出同样的文本（提示词缓存友好，
    也让测试能稳定断言）。
    """
    if not isinstance(facts, Mapping) or not facts:
        return empty_text
    lines: list[str] = []
    for key in sorted(facts, key=str):
        rendered = readable_value(str(key), facts[key])
        if not rendered:
            continue
        lines.append(f"{readable_key(str(key))}：{rendered}")
    return "\n".join(lines) or empty_text
