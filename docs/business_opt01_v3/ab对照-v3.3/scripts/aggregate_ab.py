# -*- coding: utf-8 -*-
"""聚合比对：把 39 案的两臂产出对齐到「47 个契约字段」这一共同口径上做统计。

**为什么以契约为轴**：两臂的卡片段落文字口径不同，逐字比对会淹没在措辞差异里。
但两臂都往 Notes 里写同一份契约的字段（``basic_notes_updates``，键就是
``attribute_key``），因此按契约字段对齐是**唯一可比**的口径。

统计三件事：

1. **覆盖率**：每个契约字段，两臂各有多少案给出了取值（非空）。
2. **一致率**：两臂同时给出值的字段里，取值归一后相同的比例。
3. **各方案内矛盾**：同一案内，卡片某段说的与 Notes 同字段取值是否打架（粗判）。

另出「字段级胜负表」——哪些字段 v3.3 覆盖得更多、哪些更少。

只读 packets。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKETS = HERE.parent / "review" / "packets"
OUT = HERE.parent / "review" / "aggregate.json"

SRC = Path(__file__).resolve().parents[4] / "src" / "customer_profile"
sys.path.insert(0, str(SRC))


def _contract():
    import notes_contract as nc
    keys = [e["attribute_key"] for e in nc.RAW_CONFIGS]
    names = {e["attribute_key"]: e.get("attribute_name", "") for e in nc.RAW_CONFIGS}
    cats = {e["attribute_key"]: e.get("category", "") for e in nc.RAW_CONFIGS}
    enums = {e["attribute_key"]: nc.is_enum(e["attribute_key"]) for e in nc.RAW_CONFIGS}
    return keys, names, cats, enums


def _norm(value) -> str:
    """归一取值：用于两臂可比。去空白、统一分隔符、枚举取 key、大小写无关。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = text.strip()
    if not text:
        return ""
    text = text.replace("||", "|")
    text = re.sub(r"\s+", "", text)
    return text.lower()


def _notes_map(side: dict) -> dict[str, str]:
    sec = (side or {}).get("sections") or {}
    val = sec.get("basic_notes_updates")
    if not isinstance(val, list):
        return {}
    out = {}
    for n in val:
        if not isinstance(n, dict):
            continue
        key = str(n.get("attribute_key") or "")
        if key:
            out[key] = _norm(n.get("attribute_value"))
    return out


def _nonblank(text: str) -> bool:
    return bool(text) and text not in ("[]", "{}", "null", "none", "-", "--")


def main() -> int:
    keys, names, cats, enums = _contract()
    packets = sorted(p for p in PACKETS.glob("*.json") if p.name != "_index.json")

    new_cov = Counter()
    base_cov = Counter()
    both_same = Counter()
    both_diff = Counter()
    only_new_ex = defaultdict(list)
    only_base_ex = defaultdict(list)
    diff_ex = defaultdict(list)

    per_case = []
    for p in packets:
        d = json.loads(p.read_text(encoding="utf-8"))
        phone = d["phone"]
        nm = _notes_map(d.get("new"))
        bm = _notes_map(d.get("base"))
        row = {"phone": phone, "new": 0, "base": 0, "both": 0, "same": 0, "diff": 0}
        for k in keys:
            nv, bv = nm.get(k, ""), bm.get(k, "")
            n_ok, b_ok = _nonblank(nv), _nonblank(bv)
            if n_ok:
                new_cov[k] += 1
            if b_ok:
                base_cov[k] += 1
            if n_ok and b_ok:
                row["both"] += 1
                if nv == bv:
                    both_same[k] += 1
                    row["same"] += 1
                else:
                    both_diff[k] += 1
                    row["diff"] += 1
                    if len(diff_ex[k]) < 3:
                        diff_ex[k].append({"phone": phone, "new": nm[k][:80], "base": bm[k][:80]})
            elif n_ok:
                row["new"] += 1
                if len(only_new_ex[k]) < 3:
                    only_new_ex[k].append(phone)
            elif b_ok:
                row["base"] += 1
                if len(only_base_ex[k]) < 3:
                    only_base_ex[k].append(phone)
        per_case.append(row)

    total_new = sum(new_cov.values())
    total_base = sum(base_cov.values())
    total_both = sum(both_same.values()) + sum(both_diff.values())

    print("== 契约字段级聚合（39 案 × %d 字段）==" % len(keys))
    print("新版给出取值 : %d 字段次" % total_new)
    print("基线给出取值 : %d 字段次" % total_base)
    print("两臂都有取值 : %d 字段次（一致 %d / 不同 %d，一致率 %.1f%%）" % (
        total_both, sum(both_same.values()), sum(both_diff.values()),
        100.0 * sum(both_same.values()) / total_both if total_both else 0))

    print("\n== 字段级覆盖表（按新版覆盖降序；只列两臂不同的）==")
    print("%-26s %5s %5s %6s %6s %6s" % ("字段", "新版", "基线", "两臂", "一致", "不同"))
    rows = []
    for k in keys:
        rows.append((k, new_cov[k], base_cov[k], both_same[k] + both_diff[k],
                     both_same[k], both_diff[k]))
    rows.sort(key=lambda r: (-(r[1] - r[2]), -r[1]))
    for k, n, b, bo, s, df in rows:
        if n == b and s == 0:
            continue
        print("%-26s %5d %5d %6d %6d %6d" % (k, n, b, bo, s, df))

    print("\n== 逐案 ==")
    print("%-12s %5s %5s %5s %5s %5s" % ("案号", "新版", "基线", "两臂", "一致", "不同"))
    for r in per_case:
        print("%-12s %5d %5d %5d %5d %5d" % (
            r["phone"], r["new"], r["base"], r["both"], r["same"], r["diff"]))

    OUT.write_text(json.dumps({
        "fields": keys,
        "field_names": names,
        "field_categories": cats,
        "field_is_enum": enums,
        "new_coverage": dict(new_cov),
        "base_coverage": dict(base_cov),
        "both_same": dict(both_same),
        "both_diff": dict(both_diff),
        "only_new_examples": {k: v for k, v in only_new_ex.items()},
        "only_base_examples": {k: v for k, v in only_base_ex.items()},
        "diff_examples": {k: v for k, v in diff_ex.items()},
        "per_case": per_case,
        "totals": {
            "new": total_new, "base": total_base, "both": total_both,
            "same": sum(both_same.values()), "diff": sum(both_diff.values()),
        },
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已写出 %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
