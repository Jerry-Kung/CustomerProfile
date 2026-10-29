"""枚举归一的第二道通路：**语义映射**（值有信息、只是没长成合法键）。

## 为什么需要这一层

``projection.normalize_enum`` 与 ``internal_fields.LEGACY_ENUM_ALIASES`` 都是**确定性**
匹配：合法键、契约展示名、旧版键。L1 在枚举字段上稳定给出契约之外的中文自由文本
（``已婚有子女`` / ``分期`` / ``初步了解``），这三者都不命中，于是：

    L2 保留原值 + 降级 → gate 放行（中文不是键形态）→ 回写出口救不回来 → 整条剔除

2026-10-28 六案实测被这样静默丢掉的 Notes 有 40 余条，其中包含 ``basis=fact``、
置信 1.0 的字段。补别名表追不动模型的措辞（``需上河南信阳牌照`` 这类带上下文的长短语
穷举不现实），因此对「查表查不到」的残余再做一次**批量语义映射**。

## 边界：这是「补」，不是「查」

与 checker（``rules.checker``）的分工必须写清，两者都是 code 节点调 LLM，但方向相反：

- checker 判「这条规则有没有被违反」，动作白名单**不含**产生取值，带 ``value`` 的裁决
  一律丢弃；
- 本模块的**唯一职责**就是产生取值（``原值 → 合法键``），因此不复用 checker 的动作集，
  也不挂在它的开关下。共用的只是「code 节点落地、异常全吞、关闭即零 token」这套骨架。

## 三道闸门，代码守住最后一道

1. **送检前过滤**：占位串（``暂无`` / ``未提供``）与键形态的废弃键（``replacement``）
   不送检——前者本来就没信息，后者归 ``ENUM-01`` 处理。把「没信息」送进映射，模型很
   容易把「暂无明确置换需求」读成「置换意向=否」，那是把没有说成有。
2. **契约缺口不猜**：``CONTRACT_GAP_VALUES`` 里的取值（``frequent_social_media`` 的
   ``微信``）在契约里**没有对应键**，属于契约缺口而非映射问题。送检模型只会得到一个
   看似相近的错误键（``微信`` → ``weibo``），因此就地阻断。该缺口已由业务方 2026-10-29
   定论**不加键**（微信是即时通信工具，不与纯社媒并列），此后的处置是终局而非待定。
3. **输出复核**：模型给的键必须 ∈ ``legal_keys(字段)``，且该字段必须在本次送检清单里、
   取值必须真的是送检过的那个。任何一条不过即按「无法映射」处理——模型输出只是**建议**，
   不把它当真值。

## 失败与开关

``ENUM_MAPPING_ENABLED`` 缺省 false，关闭时在**发起调用之前**返回，产物与今天逐字节
一致。调用异常、JSON 解析失败、返回为空，一律不抛：映射失败退回「保留原值 + 转确认」，
即今天的行为，管线不因此中断。

## 位置

挂在 ``L2_PROJECT → 本节点 → L2 兜底/审查/定级/回写`` 之间，而不是放在回写出口
（``callback_build``）：那里是全链路唯一的零 token 节点，且只救 Notes——``facts`` 里
仍是中文原值，画像卡（``_label`` 查不到展示名原样输出）与商机定级会继续拿到错形态。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

from . import internal_fields as inf
from . import text_utils as tut
from .rules.clusters import hard as hard_rules
from .rules.trace import RuleTrace

ENUM_MAP_RULE = "ENUM-02"
"""本条通路在登记表里的规则编号（映射了一条取值记一条 trace）。"""

UNMAPPED_REASON = "该字段的取值无法归入可选范围"
UNMAPPED_ACTION = "请确认后补填"
"""未映射条目的 ``to_confirm`` 文案。

**不得出现规则编号、内部字段键或占位串**（``docs/README.md`` 的可读性硬约束）：
``to_confirm`` 会随回传体下发，销售看到的是这两句中文，不是 ``ENUM-02``。"""

PLACEHOLDER_PREFIXES: tuple[str, ...] = (
    "暂无",
    "未明确",
    "未提供",
    "未说明",
    "不清楚",
    "不详",
    "不明",
    "无相关",
    "无明确",
)
"""**「本来就没信息」的取值前缀**，与 ``HARD-01`` 的精确表互补。

精确表收 ``暂无`` 本身，收不到 ``暂无明确家庭结构信息`` 这种长句。两者都不该送映射：
它说的是「客户没提」，不是「客户说了另一种格式」。
"""

CONTRACT_GAP_VALUES: Mapping[str, frozenset[str]] = {
    "frequent_social_media": frozenset({"微信"}),
}
"""**契约缺口**：真实高频出现、但契约里没有对应键的取值。

``frequent_social_media`` 的合法集只有微博 / 抖音 / 头条 / 小红书，而「微信」是这批
客户里最常见的社交平台。这不是映射问题——硬塞进 ``weibo`` 等于凭空造一个客户没用过的
平台。业务方 2026-10-29 定论：微信的定位是**即时通信工具**，不与抖音、小红书这类纯社媒
并列，因此**不加键、契约维持现状**。该值按「无法映射」走 ``to_confirm``，这是终局处置。

**不送检而不是送检后拒收**：送检就会得到一个键，而相邻键的展示名离得太近，模型必然
给出 ``weibo``；拒绝的成本必须由代码承担，不能指望提示词。
"""

MAX_VALUES = 400
"""单次送检的取值上限。超出只取前 N 条，其余按未映射处理。

按实测非法值数量（单批几十条）这是宽松上限；设它是为了防止历史回放时一次把几万条
塞进提示词。
"""

_CACHE: dict[tuple[str, tuple[str, ...], str], str | None] = {}
"""``(字段, 合法键集合, 原值) -> 键 | None`` 的进程内缓存。

键里带合法键集合的摘要是为了**契约一变缓存自动失效**：同一个「已婚有子女」在契约
改了 ``family_structure`` 的选项之后不能继续沿用旧结论。跨案复用收益明确——同一批
几十个案子里「已婚有子女」出现十几次，只该问一次。
"""


def reset_cache() -> None:
    """清空映射缓存（测试用）。"""
    _CACHE.clear()


def _text(value: Any) -> str:
    return tut.as_text(value)


def _is_placeholder(item: str) -> bool:
    return item in hard_rules.PLACEHOLDER_VALUES or item.startswith(PLACEHOLDER_PREFIXES)


def _is_contract_gap(attribute: str, item: str) -> bool:
    return item in (CONTRACT_GAP_VALUES.get(attribute) or frozenset())


def _needs_mapping(attribute: str, item: str) -> bool:
    """取值是否真的需要送映射。**已在本地能定的都不送**，与「能写代码的不交给模型」同口径。"""
    if not item or _is_placeholder(item) or _is_contract_gap(attribute, item):
        return False
    legal = inf.legal_keys(attribute)
    if not legal or item in legal:
        return False
    if inf.display_to_key(attribute, item) or inf.legacy_alias(attribute, item):
        return False
    # 键形态的废弃键（``replacement`` / ``commute``）归 ``ENUM-01`` 剔除，不归映射——
    # 把它们交给模型，等于让模型有机会把废弃键塞回另一个字段的合法键里。
    if inf.looks_like_enum_key(item):
        return False
    # 换字段能救的（``purchase_type=首购`` → ``purchase_nature=first``）在投影层已经
    # 搬走了，走到这里说明搬不走——仍不送语义映射：它本来就该按「换字段」处理。
    if inf.legacy_cross_field(attribute, item):
        return False
    return True


def _value_items(attribute: str, value: Any) -> list[str]:
    """把取值摊成待映射的单项（多选 ``||``、结构化取 ``key``、列表逐项）。"""
    return hard_rules.enum_values(value)


def collect(
    facts: Mapping[str, Any], notes: Sequence[Any]
) -> list[dict[str, Any]]:
    """汇总本批次「有信息但格式不对」的枚举取值，按字段分组去重。

    输入是**已经投影过的** facts 与 notes，因此只会有投影层留下的「归不了合法键」残留。
    同一字段内相同取值只出现一次；字段用中文名送检（与 checker 的硬边界同口径：模型
    不该看见内部字段键）。
    """
    pending: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()

    def _scan(attribute: str, value: Any) -> None:
        if not inf.is_enum(attribute):
            return
        for item in _value_items(attribute, value):
            key = (attribute, item)
            if key in seen or not _needs_mapping(attribute, item):
                continue
            seen.add(key)
            pending.setdefault(attribute, []).append(item)

    for attribute, value in facts.items():
        _scan(str(attribute), value)
    for entry in notes:
        if isinstance(entry, Mapping):
            _scan(_text(entry.get("attribute_key")), entry.get("attribute_value"))

    groups: list[dict[str, Any]] = []
    budget = MAX_VALUES
    for attribute, values in pending.items():
        if budget <= 0:
            break
        picked = values[:budget]
        budget -= len(picked)
        groups.append(
            {
                "attribute": attribute,
                "name": inf.name_of(attribute),
                "multiple": inf.is_multi_value(attribute),
                "options": [
                    {"key": key, "display": display}
                    for key, display in inf.display_map(attribute).items()
                    if display
                ],
                "values": picked,
            }
        )
    return groups


def prompt_view(groups: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """送检清单的**模型可见形态**：剥掉内部字段键，只留中文字段名。"""
    return [
        {
            "field": group["name"],
            "multiple": group["multiple"],
            "options": group["options"],
            "values": group["values"],
        }
        for group in groups
    ]


def _parse_pairs(verdict: Any) -> list[dict[str, Any]]:
    data = tut.as_payload(verdict)
    if isinstance(data, Mapping):
        data = data.get("mappings") or data.get("items") or []
    return [item for item in data if isinstance(item, Mapping)] if isinstance(data, (list, tuple)) else []


def review(
    verdict: Any, groups: Sequence[Mapping[str, Any]]
) -> tuple[dict[tuple[str, str], str], list[dict[str, Any]]]:
    """复核模型输出。返回 ``({(字段, 原值): 键}, 丢弃明细)``。

    四条复核，逐条对应一种可预见的胡来：字段必须在本次送检清单里、取值必须真的送过、
    键必须是该字段的合法键、多选字段的每个分项各自映射不得回填整串。
    """
    by_name = {str(group["name"]): group for group in groups}
    accepted: dict[tuple[str, str], str] = {}
    dropped: list[dict[str, Any]] = []
    for item in _parse_pairs(verdict):
        name = _text(item.get("field") or item.get("字段"))
        value = _text(item.get("value") or item.get("取值"))
        key = _text(item.get("key") or item.get("键"))
        group = by_name.get(name)
        if group is None:
            dropped.append({"reason": "字段不在本次送检清单", "field": name})
            continue
        attribute = str(group["attribute"])
        if value not in (group.get("values") or []):
            dropped.append({"reason": "取值不是送检过的取值", "field": name, "value": value})
            continue
        if not key:
            # 显式「无法映射」：不是错误，记一条供统计，不产生取值。
            dropped.append({"reason": "模型判定无法映射", "field": name, "value": value})
            continue
        if key not in inf.legal_keys(attribute):
            dropped.append(
                {"reason": "键不在该字段合法集内", "field": name, "value": value, "key": key}
            )
            continue
        accepted[(attribute, value)] = key
    return accepted, dropped


def _rewrite(attribute: str, value: Any, mapping: Mapping[str, str]) -> tuple[Any, list[str]]:
    """按映射改写一个取值，返回 ``(新值, 本次改写的原值列表)``。

    保持原形态：多选仍是 ``||`` 串、结构化仍写回 ``key``、列表仍是列表——
    ``attribute_value`` 的形状是契约，不能因为修取值就改形状。
    """
    used: list[str] = []

    def _one(item: str) -> str:
        target = mapping.get(item)
        if target:
            used.append(item)
            return target
        return item

    if isinstance(value, (list, tuple)):
        return [_one(_text(item)) for item in value], used
    if isinstance(value, Mapping):
        patched = dict(value)
        patched["key"] = _one(_text(value.get("key")))
        return patched, used
    text = _text(value)
    if not text:
        return value, used
    structured = _as_json_object(text)
    if structured is not None:
        structured["key"] = _one(_text(structured.get("key")))
        return json.dumps(structured, ensure_ascii=False), used
    if "||" in text:
        return "||".join(_one(part.strip()) for part in text.split("||") if part.strip()), used
    return _one(text), used


def _as_json_object(text: str) -> dict | None:
    if not text.startswith("{"):
        return None
    parsed = tut.as_payload(text)
    return dict(parsed) if isinstance(parsed, Mapping) else None


def apply_mappings(
    facts: Mapping[str, Any],
    notes: Sequence[Any],
    to_confirm: Sequence[Any],
    candidates: Sequence[Any],
    accepted: Mapping[tuple[str, str], str],
    *,
    stage: str = "project",
) -> dict[str, Any]:
    """把通过复核的映射落到 facts / notes，并清理已被解决的转确认与候选。

    **必须同时清 ``candidates``**：L2 兜底的候选集里带着 ``current_value``（中文原值），
    若只改 facts 与 notes，兜底仍会按候选集把原值写回去——修了上游、下游又填回来。
    """
    by_attribute: dict[str, dict[str, str]] = {}
    for (attribute, value), key in accepted.items():
        by_attribute.setdefault(attribute, {})[value] = key

    new_facts = dict(facts)
    resolved_attributes: set[str] = set()
    applied: list[dict[str, Any]] = []
    traces: list[RuleTrace] = []

    for attribute, mapping in by_attribute.items():
        if attribute not in new_facts:
            continue
        rewritten, used = _rewrite(attribute, new_facts[attribute], mapping)
        if not used:
            continue
        new_facts[attribute] = rewritten
        applied.extend({"attribute": attribute, "from": item, "to": mapping[item]} for item in used)
        # rule: ENUM-02 非法枚举取值经语义映射归一
        traces.append(
            RuleTrace(
                rule_id=ENUM_MAP_RULE,
                stage=stage,
                attribute=attribute,
                action="rewrite",
                severity="repair",
                reason="枚举取值经语义映射归一到合法键",
                source="llm_enum_map",
                before=[item for item in used],
                after=[mapping[item] for item in used],
            )
        )
        # 整个字段的非法项都被映射走了，才算「已解决」——还有残留的仍留在转确认里。
        remaining = [
            item
            for item in _value_items(attribute, rewritten)
            if item not in inf.legal_keys(attribute)
        ]
        if not remaining:
            resolved_attributes.add(attribute)

    new_notes: list[dict[str, Any]] = []
    for entry in notes:
        if not isinstance(entry, Mapping):
            continue
        patched = dict(entry)
        attribute = _text(patched.get("attribute_key"))
        mapping = by_attribute.get(attribute)
        if mapping:
            rewritten, used = _rewrite(attribute, patched.get("attribute_value"), mapping)
            if used:
                patched["attribute_value"] = rewritten
        new_notes.append(patched)

    new_confirm = [
        entry
        for entry in to_confirm
        if not (
            isinstance(entry, Mapping)
            and _text(entry.get("attribute_key")) in resolved_attributes
        )
    ]
    new_candidates = [
        entry
        for entry in candidates
        if not (
            isinstance(entry, Mapping)
            and _text(entry.get("attribute_key")) in resolved_attributes
        )
    ]
    return {
        "facts": new_facts,
        "notes": new_notes,
        "to_confirm": new_confirm,
        "candidates": new_candidates,
        "applied": applied,
        "traces": traces,
    }


def unresolved_report(groups: Sequence[Mapping[str, Any]], accepted: Mapping[tuple[str, str], str]) -> list[dict[str, Any]]:
    """仍未映射的取值，供 ``to_confirm`` 与批次报告使用（**不含**规则编号）。"""
    out: list[dict[str, Any]] = []
    for group in groups:
        attribute = str(group["attribute"])
        leftovers = [
            value for value in group.get("values") or [] if (attribute, value) not in accepted
        ]
        if leftovers:
            out.append({"attribute": attribute, "name": group["name"], "values": leftovers})
    return out


def build_prompt(template_name: str, groups: Sequence[Mapping[str, Any]], templates: Any) -> str:
    from .templates import TemplateRepository

    repository = templates or TemplateRepository()
    return repository.render(
        template_name,
        {"arg1": json.dumps(prompt_view(groups), ensure_ascii=False, indent=2)},
    )


def load_cached(groups: Sequence[Mapping[str, Any]]) -> tuple[dict[tuple[str, str], str], list[dict[str, Any]]]:
    """先用进程内缓存满足一部分，返回 ``(命中, 尚未决定的取值)``。

    命中的直接进 ``accepted``，且**从送检清单里剔除**——同一批里「已婚有子女」出现十几次，
    缓存命中后这次调用就不必再问它。
    """
    accepted: dict[tuple[str, str], str] = {}
    remaining: list[dict[str, Any]] = []
    for group in groups:
        attribute = str(group["attribute"])
        legal = inf.legal_keys(attribute)
        undecided: list[str] = []
        for value in group.get("values") or []:
            if (attribute, legal, value) in _CACHE:
                cached = _CACHE[(attribute, legal, value)]
                if cached:
                    accepted[(attribute, value)] = cached
                continue
            undecided.append(value)
        if undecided:
            remaining.append({**group, "values": undecided})
    return accepted, remaining


def remember(
    groups: Sequence[Mapping[str, Any]], accepted: Mapping[tuple[str, str], str]
) -> None:
    """把本次结论写进缓存（含「无法映射」的否定结论，避免同一值反复送检）。"""
    for group in groups:
        attribute = str(group["attribute"])
        legal = inf.legal_keys(attribute)
        for value in group.get("values") or []:
            _CACHE[(attribute, legal, value)] = accepted.get((attribute, value))


def enabled(explicit: bool | None = None) -> bool:
    """``ENUM_MAPPING_ENABLED`` 的运行时取值。读配置失败按**关闭**处理。"""
    if explicit is not None:
        return bool(explicit)
    try:
        from .settings import get_settings

        return bool(get_settings().enum_mapping_enabled)
    except Exception:
        return False


def to_confirm_entries(groups: Iterable[Mapping[str, Any]], accepted: Mapping[tuple[str, str], str]) -> list[dict[str, Any]]:
    """给仍未映射的取值造 ``to_confirm`` 条目（对客可读，不含编号与内部键）。"""
    entries: list[dict[str, Any]] = []
    for item in unresolved_report(list(groups), accepted):
        entries.append(
            {
                "attribute_key": item["attribute"],
                "candidates": item["values"],
                "reason": "该字段的取值无法归入可选范围",
                "suggested_action": "请确认后补填",
            }
        )
    return entries


__all__ = [
    "ENUM_MAP_RULE",
    "UNMAPPED_REASON",
    "UNMAPPED_ACTION",
    "CONTRACT_GAP_VALUES",
    "PLACEHOLDER_PREFIXES",
    "collect",
    "prompt_view",
    "review",
    "apply_mappings",
    "unresolved_report",
    "build_prompt",
    "load_cached",
    "remember",
    "enabled",
    "reset_cache",
    "to_confirm_entries",
]
