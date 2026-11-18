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

from typing import Any, Iterable, Mapping

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

# ====================================================================
# 推导链的四段结构：段名、切分与「替换而非追加」
# ====================================================================

STAGE_SOURCE = "信源整合"
STAGE_GAP = "信息缺位"
STAGE_CONCLUSION = "结论推断"
STAGE_GUARD = "检查核对"
STAGE_ORDER: tuple[str, ...] = (STAGE_SOURCE, STAGE_GAP, STAGE_CONCLUSION, STAGE_GUARD)
"""四段的**规范顺序**。

提示词要求「标签直接写在句首」（``信源整合：…``），前端按前缀识别并着色。因此任何
往推导链里加话的代码都必须带段标签、并且落在规范位置上——不带标签的追加会打断
前端的分段，这就是 2026-11-17 UAT 核查里「五段式」551 条的成因。
"""

STAGES: tuple[str, ...] = STAGE_ORDER

STEPS_LIMIT = 4
"""一条推导链最多呈现的句数（与出口 ``_readable_steps`` 的截断一致）。"""

_STAGE_COLONS: tuple[str, ...] = ("：", ":")

LEGACY_FALLBACK_STEPS: tuple[str, ...] = (
    "客户资料中有明确记录，据此直接确定",
    "客户资料中有明确原文，据此直接确定",
    "客户本人有明确表述，据此直接确定",
    "销售在沟通中提及，据此确定",
    "综合上述几条线索归纳得出",
    "客户原话经口误与品牌名核对后确定",
    "根据客户资料中已有信息归纳",
    "根据客户资料中已有信息归纳得出",
)
"""投影层 2026-11-17 之前产出的**兜底句**。

它们不是推导，只是把「没有推导链」包装成了一句推导（``projection._readable_steps``
现已不再产出）。回放既有批次的 Notes 里仍存着这些句子；往这样的链上补新句之前
要把它们摘掉，否则链上永远留着一条冒充推导的尾巴。"""

_LEGACY_STEP_KEYS: tuple[str, ...] = tuple(
    item.strip("。") for item in LEGACY_FALLBACK_STEPS
)


def is_legacy_fallback_step(text: Any) -> bool:
    """这条句子是不是旧的出口兜底句（按去句末标点后的整句比对）。"""
    return str(text or "").strip().strip("。") in _LEGACY_STEP_KEYS


def stage_of(text: Any) -> str:
    """取一条推导链句子的段标签；没有标签、或标签不在四段之内返回空串。"""
    body = str(text or "").strip()
    for colon in _STAGE_COLONS:
        head, sep, _ = body.partition(colon)
        if sep and head.strip() in STAGE_ORDER:
            return head.strip()
    return ""


def is_stage_tagged(text: Any) -> bool:
    """这条句子是不是带四段标签的写法。"""
    return bool(stage_of(text))


def _clean_steps(steps: Iterable[Any]) -> list[str]:
    """归一成句子列表，并摘掉旧的出口兜底句（见 :data:`LEGACY_FALLBACK_STEPS`）。"""
    return [
        str(item).strip()
        for item in (steps or ())
        if str(item).strip() and not is_legacy_fallback_step(item)
    ]


def merge_stage(steps: Iterable[Any], stage: str, sentence: str) -> list[str]:
    """把一句「某一段」的话写进推导链：**同段已有则替换，没有则按四段顺序插入**。

    为什么必须替换而不是追加（2026-11-17 UAT 核查问题二）：``merge_l2_verdict`` 兜底
    改判取值后，把 L2 的理由 ``append`` 到已有四段之后，交付里就出现 551 条「四段 +
    一条无标签尾巴」。前端按前缀着色，尾巴会被当成第五段，结构直接乱掉。L2 兜底的
    定位是「只改取值与置信度」，要改结论就整段换掉结论，其余段沿用 L1 的。

    链上**一个段标签都没有**（旧写法）时保留原链、把新句补在末尾：旧链无从判断哪一句
    是结论推断，删掉它等于丢掉当时唯一的一句推导；链满 ``STEPS_LIMIT`` 句时替换末句
    ——旧写法的末句通常就是那时的结论。
    """
    text = str(sentence or "").strip()
    out = _clean_steps(steps)
    if not text:
        return out
    if stage_of(text) != stage:
        text = f"{stage}：{text}"
    if not any(stage_of(item) for item in out):
        if len(out) >= STEPS_LIMIT:
            return [*out[: STEPS_LIMIT - 1], text]
        return [*out, text]

    replaced = False
    result: list[str] = []
    for item in out:
        if stage_of(item) != stage:
            result.append(item)
            continue
        if not replaced:
            result.append(text)
            replaced = True
    if not replaced:
        rank = STAGE_ORDER.index(stage)
        index = next(
            (
                position
                for position, item in enumerate(result)
                if stage_of(item) in STAGE_ORDER
                and STAGE_ORDER.index(stage_of(item)) > rank
            ),
            len(result),
        )
        result.insert(index, text)
    return result


def append_stage_note(steps: Iterable[Any], stage: str, note: str) -> list[str]:
    """把一句**补充说明**并入某一段：该段已在则接在其后，否则新起一段。

    与 :func:`merge_stage` 的区别：后者说的是「这一段该写什么」（替换），本函数说的是
    「这一段还要补一句」（合并）。规则层的加注（PAY-03 的「未确认」提示）属于后者——
    它不该把 L1 已经写好的「检查核对」整段吞掉。
    """
    text = str(note or "").strip()
    out = _clean_steps(steps)
    if not text:
        return out
    for index, item in enumerate(out):
        if stage_of(item) == stage:
            if text in item:
                return out
            merged = list(out)
            merged[index] = f"{item.rstrip('。；; ')}；{text}"
            return merged
    return [*out, f"{stage}：{text}"]


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
