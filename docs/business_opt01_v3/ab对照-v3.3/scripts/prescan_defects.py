# -*- coding: utf-8 -*-
"""机器预扫：对 39 案逐一跑确定性判据，产出「可疑点清单」，供人工审核聚焦。

**为什么先跑机器预扫**：39 案 × 两臂 ≈ 12.66 MB 文本，逐字读一遍才能判「准不准」，
但其中一类问题不需要人眼——「卡片里出现的数字在 facts 里找不到出处」「Notes 取值不在
facts 里」「枚举取值非法」「Notes 说 A、卡片说 B」都是可判定的。先把这类挑出来，
人工只读机器挑不出的语义问题。

判据（每条都对应一个真实错误类型，不是通用 lint）：

1. ``notes_not_in_facts``：Notes 的 ``attribute_key`` 不在 L2 facts 里（L3 校验判的
   error，此处独立复核）。
2. ``enum_illegal``：facts 里枚举字段取值非法（排除已降为 inferred_low 的客户原话）。
3. ``unbacked_fact``：``basis=fact`` 却既无 ``quote`` 也非登记档来源——「编造的确定性」。
4. ``card_number_unsourced``：卡片文本里带单位（万元/%/岁/万）的量在 facts 中找不到出处。
5. ``low_conf_stated_as_fact``：Notes 置信度 ≤0.45（兜底补全的软值）却在卡片里被当作
   确定信息陈述。
6. ``section_empty``：九段中某段整段为空。
7. ``cross_arm_enum_conflict``：同一字段两臂取值互斥（枚举语义冲突，非措辞差异）。
8. ``arm_agreement``：两臂在 47 个契约字段上的取值，按「归一后字符串相等」比对，
   统计一致/单边/冲突——这是「v3.3 相对基线改了多少」的可数口径。

产出：
- ``review/prescan.json``：逐案判据结果；
- ``review/prescan.md``：人读摘要（哪些案有几个可疑点）。

只读 packets，不触碰批次目录。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKETS = HERE.parent / "review" / "packets"
OUT_JSON = HERE.parent / "review" / "prescan.json"
OUT_MD = HERE.parent / "review" / "prescan.md"

REGISTRY_CHANNELS = frozenset({
    "销售人员人工确认的客户Notes信息",
    "客户留资手机号",
    "试驾与置换记录",
    "客户留存在我方销售数据系统中的信息记录",
    "销售人员对当前客户历史画像数据的反馈记录",
})

LIST_SECTIONS = (
    "inferred_tags", "usage_scenarios", "communication_strategies",
    "purchase_motivations", "product_preferences", "resistances",
)
OBJECT_SECTIONS = ("profile_summary", "customer_overview", "emotion_state")

# 带单位的量最可能是「编出来的事实」
UNIT_MARKERS = ("万元", "%", "万", "岁")


def _loads(v, default=None):
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        try:
            return json.loads(v.replace("\n", "\\n")) if isinstance(v, str) else default
        except (ValueError, TypeError):
            return default


def _facts_of(chain: dict) -> dict:
    proj = chain.get("l2_project") or {}
    facts = _loads(proj.get("facts_json"), {})
    return facts if isinstance(facts, dict) else {}


def _basis_of(chain: dict) -> dict:
    proj = chain.get("l2_project") or {}
    basis = _loads(proj.get("basis_json"), {})
    return basis if isinstance(basis, dict) else {}


def _notes_of(new_side: dict) -> list:
    sec = (new_side or {}).get("sections") or {}
    val = sec.get("basic_notes_updates")
    return val if isinstance(val, list) else []


def _claims_of(chain: dict) -> list:
    loc = chain.get("localize") or {}
    inj = chain.get("inject") or {}
    raw = inj.get("claims_json") or loc.get("claims_json")
    data = _loads(raw, {})
    if isinstance(data, dict):
        data = data.get("fields") or data.get("claims") or []
    return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []


def _as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, default=str)


def scan_case(packet: dict) -> dict:
    phone = packet["phone"]
    chain = packet.get("chain") or {}
    new = packet.get("new") or {}
    base = packet.get("base") or {}
    facts = _facts_of(chain)
    basis = _basis_of(chain)
    notes = _notes_of(new)
    claims = _claims_of(chain)
    l3 = chain.get("l3") or {}

    findings: list[dict] = []

    # ---- 1 Notes 不在 facts 里
    facts_keys = set(facts)
    drift = [
        {"attribute": n.get("attribute_key"), "value": n.get("attribute_value")}
        for n in notes
        if n.get("attribute_key") and n.get("attribute_key") not in facts_keys
    ]
    if drift:
        findings.append({"rule": "notes_not_in_facts", "count": len(drift), "detail": drift[:10]})

    # ---- 2 枚举非法（未降级）
    try:
        import notes_contract as nc  # type: ignore
    except ImportError:
        nc = None
    if nc is not None:
        illegal = []
        for key, value in facts.items():
            if not nc.is_enum(key):
                continue
            if str(basis.get(key) or "") == "inferred_low":
                continue
            items = value if isinstance(value, list) else (
                [p.strip() for p in value.split("||")] if isinstance(value, str) and "||" in value
                else ([value.get("key")] if isinstance(value, dict) else [value]))
            for item in items:
                if item in (None, ""):
                    continue
                text = str(item)
                if text in nc.legal_keys(key) or nc.display_to_key(key, text):
                    continue
                illegal.append({"attribute": key, "value": text})
        if illegal:
            findings.append({"rule": "enum_illegal", "count": len(illegal), "detail": illegal[:10]})

    # ---- 3 无锚点 fact
    unbacked = []
    for c in claims:
        if str(c.get("basis") or "") != "fact":
            continue
        if str(c.get("quote") or "").strip():
            continue
        if str(c.get("record_channel") or "").strip() in REGISTRY_CHANNELS:
            continue
        unbacked.append({"attribute": c.get("attribute"), "value": c.get("value"),
                         "channel": c.get("record_channel")})
    if unbacked:
        findings.append({"rule": "unbacked_fact", "count": len(unbacked), "detail": unbacked[:10]})

    # ---- 4 卡片里无出处的带单位量
    haystack = " ".join(_as_text(v) for v in facts.values())
    new_secs = new.get("sections") or {}
    unsourced = []
    for sec in LIST_SECTIONS + OBJECT_SECTIONS:
        text = _as_text(new_secs.get(sec))
        if not text:
            continue
        for marker in UNIT_MARKERS:
            idx = text.find(marker)
            if idx < 0:
                continue
            window = text[max(0, idx - 12): idx + len(marker)].strip()
            if window and window not in haystack and not any(
                    tok and tok in haystack for tok in [window[-6:], window[:6]]):
                unsourced.append({"section": sec, "excerpt": window})
            break
    if unsourced:
        findings.append({"rule": "card_number_unsourced", "count": len(unsourced),
                         "detail": unsourced[:10]})

    # ---- 5 软值被当确定信息
    soft_keys = {str(n.get("attribute_key")) for n in notes
                 if float((n.get("reasoning_detail") or {}).get("confidence") or 1) <= 0.45}
    stated = []
    for key in soft_keys:
        val = _as_text(facts.get(key))
        if not val:
            continue
        for sec in LIST_SECTIONS + OBJECT_SECTIONS:
            if val and val in _as_text(new_secs.get(sec)):
                stated.append({"attribute": key, "value": val, "section": sec})
                break
    if stated:
        findings.append({"rule": "low_conf_stated_as_fact", "count": len(stated), "detail": stated})

    # ---- 6 空段
    empty = [s for s in LIST_SECTIONS + OBJECT_SECTIONS if not new_secs.get(s)]
    if empty:
        findings.append({"rule": "section_empty", "count": len(empty), "detail": empty})

    # ---- 7 两臂取值差异（比对两臂 Notes 同键取值）
    base_notes = {str(n.get("attribute_key")): _as_text(n.get("attribute_value"))
                  for n in _notes_of(base)}
    new_notes = {str(n.get("attribute_key")): _as_text(n.get("attribute_value"))
                 for n in notes}
    both, only_new, only_base, conflict = 0, 0, 0, []
    for key in set(base_notes) | set(new_notes):
        a, b = new_notes.get(key), base_notes.get(key)
        if a and b:
            both += 1
            if a != b:
                conflict.append({"attribute": key, "new": a, "base": b})
        elif a:
            only_new += 1
        else:
            only_base += 1
    if conflict:
        # 差异不等于是错误（口径可以不同），故只作为「值得人工看」的线索，序在前
        findings.append({"rule": "notes_value_differs", "count": len(conflict),
                         "detail": conflict[:15]})

    return {
        "phone": phone,
        "findings": findings,
        "notes_both": both,
        "notes_only_new": only_new,
        "notes_only_base": only_base,
        "notes_conflict": len(conflict),
        "l3_suspects": l3.get("new_fact_suspects"),
        "l3_sections_missing": _loads(l3.get("sections_missing"), []),
        "issue_count": (chain.get("l2_review") or {}).get("issue_count"),
    }


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src" / "customer_profile"))
    packets = sorted(PACKETS.glob("*.json"))
    packets = [p for p in packets if p.name != "_index.json"]
    results = []
    for p in packets:
        packet = json.loads(p.read_text(encoding="utf-8"))
        results.append(scan_case(packet))

    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = ["# 机器预扫结果（39 案）", "",
             "| 案号 | 可疑点 | 契约字段：两臂同有/仅新/仅基/取值不同 | L3 无出处量 | 空段 |",
             "|---|---|---|---|---|"]
    for r in results:
        rules = "、".join("%s×%d" % (f["rule"], f["count"]) for f in r["findings"]) or "—"
        lines.append("| %s | %s | %d/%d/%d/%d | %s | %s |" % (
            r["phone"], rules, r["notes_both"], r["notes_only_new"],
            r["notes_only_base"], r["notes_conflict"],
            r["l3_suspects"] if r["l3_suspects"] is not None else "—",
            "、".join(r["l3_sections_missing"]) if r["l3_sections_missing"] else "—",
        ))
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    total = sum(len(r["findings"]) for r in results)
    print("已写出 %s / %s" % (OUT_JSON, OUT_MD))
    print("案数 %d，判据命中合计 %d 类次" % (len(results), total))
    for r in results:
        rules = "、".join("%s×%d" % (f["rule"], f["count"]) for f in r["findings"]) or "—"
        print("  %-12s %s" % (r["phone"], rules))
    return 0


if __name__ == "__main__":
    sys.exit(main())
