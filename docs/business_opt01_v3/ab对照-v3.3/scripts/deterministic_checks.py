# -*- coding: utf-8 -*-
"""确定性体检：卡片正文里的「工程泄漏」与「填充率」。

**为什么必须用代码数，而不能靠阅读印象**：销售视角报告的核心论断是
「销售看不看得懂」。这个论断若只凭读几案的感觉，就成了印象；但其中一大块是
**可判定的**——卡片正文里有没有出现英文枚举键（``commute`` / ``first_time`` / ``Mr``）、
有没有出现链路术语（``inferred_low`` / 兜底 / 候选集 / 断言 / facts），
这些能逐字扫出来，并给出「39 案中多少案、共多少处」。

同样可数的是**填充率**：九段里每段实际有多少案给出了内容，而不是留空或写
「暂无法判断」。它回答「这张卡到底有多少信息量」，与「准不准」是两回事。

判据：
1. ``enum_key_leak``：正文出现契约枚举的**英文键**（非展示名）。
2. ``eng_term``：出现链路/工程术语。
3. ``placeholder``：整段是留白或「暂无法判断/暂无/待补充」型占位。
4. ``section_fill``：逐段的「有实质内容」率。

只读 packets，产出一份 JSON + 屏幕摘要。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKETS = HERE.parent / "review" / "packets"
OUT = HERE.parent / "review" / "deterministic.json"
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src" / "customer_profile"))

SECTIONS = (
    "profile_summary", "inferred_tags", "usage_scenarios", "communication_strategies",
    "customer_overview", "purchase_motivations", "product_preferences", "resistances",
    "emotion_state",
)

# 链路/工程术语：出现在给销售看的正文里即为泄漏
ENG_TERMS = (
    "inferred_low", "inferred_high", "record_channel", "basis_of_inference",
    "reasoning_chain", "anchor_kind", "value_signal", "claim_id", "attribute_key",
    "facts_json", "candidate", "候选集", "兜底", "断言账本", "投影层", "取证层",
    "分诊", "定坐标", "同源", "L1", "L2", "L3", "raw_payload", "content_kind",
    "空值占位", "占位串", "枚举键", "schema", "JSONL",
)

PLACEHOLDER_PATTERNS = (
    "暂无法判断", "暂无", "待补充", "信息不足", "无法判断", "资料尚待",
    "尚待补全", "未获取", "均未获取", "缺少依据",
)


def _as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False, default=str)


def _enum_keys():
    """契约里所有英文枚举键（含结构化的 input_value 键）。"""
    import notes_contract as nc
    keys = set()
    for entry in nc.RAW_CONFIGS:
        options = entry.get("enum_options")
        if not options:
            continue
        for k in (options.get("items") or {}):
            keys.add(str(k))
    return keys


def main() -> int:
    enum_keys = _enum_keys()
    packets = sorted(p for p in PACKETS.glob("*.json") if p.name != "_index.json")

    leak_cases = defaultdict(list)
    term_cases = defaultdict(list)
    fill = {s: {"filled": 0, "placeholder": 0, "empty": 0} for s in SECTIONS}
    case_rows = []

    for p in packets:
        d = json.loads(p.read_text(encoding="utf-8"))
        phone = d["phone"]
        secs = ((d.get("new") or {}).get("sections") or {})
        row = {"phone": phone, "leaks": 0, "terms": 0, "filled": 0, "placeholder": 0, "empty": 0}
        for s in SECTIONS:
            text = _as_text(secs.get(s))
            if not text or text in ("{}", "[]"):
                fill[s]["empty"] += 1
                row["empty"] += 1
                continue
            if any(pat in text for pat in PLACEHOLDER_PATTERNS):
                fill[s]["placeholder"] += 1
                row["placeholder"] += 1
            else:
                fill[s]["filled"] += 1
                row["filled"] += 1

        blob = " ".join(_as_text(secs.get(s)) for s in SECTIONS)
        for k in sorted(enum_keys):
            if len(k) < 3:
                continue
            # 词边界匹配，避免 honor 命中 honorable 之类
            if re.search(r"(?<![A-Za-z_])%s(?![A-Za-z_])" % re.escape(k), blob):
                leak_cases[k].append(phone)
                row["leaks"] += 1
        for t in ENG_TERMS:
            if t in blob:
                term_cases[t].append(phone)
                row["terms"] += 1
        case_rows.append(row)

    print("== 1. 英文枚举键泄漏到卡片正文 ==")
    if leak_cases:
        for k, phones in sorted(leak_cases.items(), key=lambda x: -len(x[1])):
            print("   %-22s %2d 案：%s" % (k, len(phones), " ".join(phones[:12])))
    else:
        print("   无")
    print()
    print("== 2. 链路/工程术语泄漏到卡片正文 ==")
    if term_cases:
        for t, phones in sorted(term_cases.items(), key=lambda x: -len(x[1])):
            print("   %-22s %2d 案：%s" % (t, len(phones), " ".join(phones[:10])))
    else:
        print("   无")
    print()
    print("== 3. 九段填充率（39 案）==")
    print("   %-24s %6s %8s %6s" % ("段落", "有内容", "占位", "整段空"))
    for s in SECTIONS:
        print("   %-24s %6d %8d %6d" % (s, fill[s]["filled"], fill[s]["placeholder"], fill[s]["empty"]))
    print()
    print("== 4. 逐案（有泄漏/术语/空段最多的排前）==")
    case_rows.sort(key=lambda r: (-(r["leaks"] + r["terms"]), -r["empty"]))
    print("   %-12s %5s %5s %6s %6s %5s" % ("案号", "泄漏", "术语", "有内容", "占位", "空"))
    for r in case_rows[:15]:
        print("   %-12s %5d %5d %6d %6d %5d" % (
            r["phone"], r["leaks"], r["terms"], r["filled"], r["placeholder"], r["empty"]))

    OUT.write_text(json.dumps({
        "enum_key_leak": {k: v for k, v in leak_cases.items()},
        "eng_term": {k: v for k, v in term_cases.items()},
        "section_fill": fill,
        "per_case": case_rows,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已写出 %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
