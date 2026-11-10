"""底座组装与补缺：把「接口画像 A」与「回传体副本 B」合成上一版已定稿事实。

方案见 ``docs/feature-incremental-update/01`` §2.4。三条规则全是**纯代码判定**：

==============  ==========================  ==================================
情形            取值                        证据 / 置信度
==============  ==========================  ==================================
A 有值且 = B     A（等于 B）                 **沿用 B** 的 steps / evidence / 置信度
A 有值且 ≠ B    **A 为准**                  只有 A 的 summary；置信度留空，不臆造
A 没有该项      B                           沿用 B 的全部信息
==============  ==========================  ==================================

第二条是这套方案**唯一的质量让步**，写清楚在此：它发生在「上一版之后接口侧被别的
来源改过」的时刻（多数是人工上锁或人工修改），那时以 A 为准本来就是正确的业务选择。

为什么必须补缺（``01`` §2.3）：接口读回的证据信息不完整——792 次读回实测 ``basic_notes``
每个字段只有 ``value`` / ``reasoning_summary`` / ``is_locked`` / ``ai_feedback``，
**没有** ``steps``、``evidence``、``confidence``。只用 A 当底座，未被新数据影响的字段
回写时证据位会整体退化，而这不会报错。

输出形状与 ``localize_claims`` 一致（``{"fields": [...]}``），可直接喂 ``project_claims``；
来源名统一为 :data:`BASE_CHANNEL`，归「人工/系统登记」档。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

BASE_CHANNEL = "上一版画像"
"""底座断言的来源名。在 :data:`customer_profile.projection.CHANNEL_TIER` 里登记为
「人工/系统登记」档——上一版结论是我们自己发出去、销售看过的，可信度等同登记。"""

SECTIONS: tuple[str, ...] = (
    "profile_summary",
    "inferred_tags",
    "usage_scenarios",
    "communication_strategies",
    "customer_overview",
    "purchase_motivations",
    "product_preferences",
    "resistances",
    "emotion_state",
)
"""回传体里会被沿用的九板块（表达跳过时整段复用）。"""


def _payload(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None
    return value


def _norm(value: Any) -> str:
    """把取值归一成可比较的字符串。

    「逐字相同」按**序列化后的文本**判：结构化枚举在 A 里是 dict、在 B 里是 JSON 串，
    直接比对象形态会把同一条结论判成不同。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        # 结构化枚举在 A 里是对象、在 B 里是 JSON **字符串**：两边都解析后再比，
        # 否则「同一条结论」会被判成不同，证据白白丢掉。
        if text[:1] in ("{", "["):
            try:
                return _norm(json.loads(text))
            except ValueError:
                return text
        return text
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _note_value(note: Any) -> Any:
    if not isinstance(note, Mapping):
        return None
    return note.get("value")


def _locked(note: Any) -> bool:
    if not isinstance(note, Mapping):
        return False
    return note.get("is_locked") in (True, 1, "1")


def _summary(note: Any) -> str:
    if not isinstance(note, Mapping):
        return ""
    text = note.get("reasoning_summary")
    return str(text).strip() if text else ""


def _detail(entry: Any) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        return {}
    detail = entry.get("reasoning_detail")
    return detail if isinstance(detail, Mapping) else {}


def _hints(detail: Mapping[str, Any]) -> list[str]:
    evidence = detail.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    return [str(item).strip() for item in evidence if str(item).strip()]


def _steps(detail: Mapping[str, Any]) -> list[str]:
    steps = detail.get("steps") or []
    if isinstance(steps, str):
        steps = [steps]
    return [str(item).strip() for item in steps if str(item).strip()]


def _confidence(detail: Mapping[str, Any]) -> float | None:
    value = detail.get("confidence")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _entry(
    attribute: str,
    value: Any,
    *,
    basis: str,
    hints: Iterable[str] = (),
    steps: Iterable[str] = (),
    confidence: float | None = None,
    observed_at: str = "",
    origin: str = "",
) -> dict[str, Any]:
    return {
        "attribute": attribute,
        "value": value,
        "basis": basis,
        "subject": "customer",
        "record_channel": BASE_CHANNEL,
        "basis_of_inference": list(hints),
        "reasoning_chain": list(steps),
        "confidence": confidence,
        "observed_at": observed_at,
        "base_origin": origin,
    }


def _b_entries(callback: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    analysis = (callback or {}).get("analysis_result")
    if not isinstance(analysis, Mapping):
        return {}
    updates = analysis.get("basic_notes_updates") or []
    if not isinstance(updates, (list, tuple)):
        return {}
    out: dict[str, Mapping[str, Any]] = {}
    for entry in updates:
        if not isinstance(entry, Mapping):
            continue
        attribute = str(entry.get("attribute_key") or "").strip()
        if not attribute:
            continue
        out[attribute] = entry
    return out


def _a_notes(profile: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    notes = (profile or {}).get("basic_notes")
    if not isinstance(notes, Mapping):
        return {}
    return {
        str(key): value
        for key, value in notes.items()
        if isinstance(value, Mapping)
    }


def _entry_value(entry: Mapping[str, Any]) -> Any:
    return entry.get("attribute_value")


def assemble_base_claims(
    profile_body: Any = None,
    callback: Mapping[str, Any] | None = None,
    *,
    updated_at_ms: int = 0,
) -> dict[str, Any]:
    """把 A（接口画像）与 B（回传体副本）合成底座断言。

    返回 ``{"fields_json", "facts", "sections", "stats"}``：

    - ``fields_json``：与 ``localize_claims`` 同形的断言 JSON，直接喂 ``project_claims``；
    - ``facts``：上一版事实表（表达跳过时用来判「事实有没有变」）；
    - ``sections``：上一版九板块（表达跳过时整段复用）；
    - ``stats``：三条规则各命中多少次，进运行台留痕。

    任何一步解析失败都**退回空底座**，调度层据此落 T3——宁可多跑一次全量，也不要拿
    半份底座往下叠。
    """
    profile = _payload(profile_body)
    if not isinstance(profile, Mapping):
        profile = {}
    callback = callback if isinstance(callback, Mapping) else {}
    analysis = callback.get("analysis_result")
    if not isinstance(analysis, Mapping):
        analysis = {}

    observed = _observed_at(updated_at_ms)
    a_notes = _a_notes(profile)
    b_entries = _b_entries(callback)

    fields: list[dict[str, Any]] = []
    same = 0
    differ = 0
    from_b_only = 0
    for attribute in sorted(set(a_notes) | set(b_entries)):
        note = a_notes.get(attribute)
        entry = b_entries.get(attribute)
        a_value = _note_value(note)
        a_text = _norm(a_value)
        b_value = _entry_value(entry) if entry is not None else None
        b_text = _norm(b_value)
        detail = _detail(entry)
        basis = "fact" if _locked(note) else "inferred_high"

        if a_text:
            if b_text and a_text == b_text:
                # 情形一：值没变，证据沿用 B——那时给出的依据对本条结论仍然准确。
                same += 1
                fields.append(
                    _entry(
                        attribute,
                        a_value,
                        basis=basis,
                        hints=_hints(detail),
                        steps=_steps(detail),
                        confidence=_confidence(detail),
                        observed_at=observed,
                        origin="A=B",
                    )
                )
                continue
            # 情形二：A 为准，只有它自己的 summary，不臆造证据与置信度。
            differ += 1
            summary = _summary(note)
            fields.append(
                _entry(
                    attribute,
                    a_value,
                    basis=basis,
                    hints=[summary] if summary else [],
                    observed_at=observed,
                    origin="A!=B" if b_text else "A_only",
                )
            )
            continue

        if b_text:
            # 情形三：A 没有这一项——B 的全部信息照用（含置信度与推导链）。
            from_b_only += 1
            fields.append(
                _entry(
                    attribute,
                    b_value,
                    basis="inferred_high",
                    hints=_hints(detail),
                    steps=_steps(detail),
                    confidence=_confidence(detail),
                    observed_at=observed,
                    origin="B_only",
                )
            )

    # 卡片字段：deal_level 不在 Notes 契约里，但定级锚点与表达都要它。
    level = _level_of(profile, analysis)
    if level:
        fields.append(
            _entry("deal_level", level, basis="inferred_high", observed_at=observed, origin="A!")
        )

    facts = _facts_from(analysis, fields)
    sections = {key: analysis[key] for key in SECTIONS if key in analysis}
    return {
        "fields": fields,
        "fields_json": json.dumps({"fields": fields}, ensure_ascii=False, default=str),
        "facts": facts,
        "facts_json": json.dumps(facts, ensure_ascii=False, default=str),
        "sections": sections,
        "sections_json": json.dumps(sections, ensure_ascii=False, default=str),
        "stats": {
            "same": same,
            "differ": differ,
            "from_b_only": from_b_only,
            "total": len(fields),
            "a_notes": len(a_notes),
            "b_notes": len(b_entries),
        },
    }


def _observed_at(updated_at_ms: int) -> str:
    if not updated_at_ms:
        return ""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(updated_at_ms / 1000).strftime("%Y-%m-%dT%H:%M:%S")


def _level_of(profile: Mapping[str, Any], analysis: Mapping[str, Any]) -> str:
    insight = (profile or {}).get("ai_insight")
    if isinstance(insight, Mapping):
        level = insight.get("purchase_intent_level")
        if isinstance(level, Mapping) and str(level.get("value") or "").strip():
            return str(level["value"]).strip()
    summary = analysis.get("profile_summary")
    if isinstance(summary, Mapping) and str(summary.get("deal_level") or "").strip():
        return str(summary["deal_level"]).strip()
    insight = analysis.get("ai_insight")
    if isinstance(insight, Mapping):
        level = insight.get("purchase_intent_level")
        if isinstance(level, Mapping) and str(level.get("value") or "").strip():
            return str(level["value"]).strip()
    return ""


def _facts_from(analysis: Mapping[str, Any], fields: list[dict[str, Any]]) -> dict[str, Any]:
    """上一版事实表：以断言取值为准，等级取 ``deal_level``。

    ``facts`` 只用于「表达跳过」的比对，不参与投影——投影读的是断言账本。取值统一按
    Notes 契约的落库形态（字符串），与 ``project_claims`` 产出的事实表可直接逐字节比较。
    """
    facts: dict[str, Any] = {}
    for entry in fields:
        attribute = entry["attribute"]
        value = entry.get("value")
        if value in (None, "", [], {}):
            continue
        facts[attribute] = value
    if "deal_level" not in facts and isinstance(analysis.get("profile_summary"), Mapping):
        level = analysis["profile_summary"].get("deal_level")
        if level:
            facts["deal_level"] = level
    return facts


def base_facts_signature(facts: Mapping[str, Any]) -> str:
    """事实表的逐字节签名（表达跳过用）。

    只比**取值**，不比证据与置信度：这些不改变卡片上写什么，比进来只会让跳过失效。
    """
    return json.dumps(
        {str(k): v for k, v in sorted((facts or {}).items())},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
