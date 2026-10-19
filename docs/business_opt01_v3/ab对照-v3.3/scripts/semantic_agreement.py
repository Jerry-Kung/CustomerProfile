# -*- coding: utf-8 -*-
"""语义一致率：把「表示差异」从「实质分歧」里剥出来。

**为什么必须做这一层**：直接按字符串比两臂取值，得到的「一致率 43.2%」是**假低**——
它把下面这些算成了「不一致」：

- 枚举表示不同但所指相同：新版写 ``commute``，基线写 ``通勤``（契约里 key↔展示名同义）；
- 列表分隔符不同：``commute||transport`` vs ``通勤、代步``；
- 数值写法不同：``40 ~ 49`` vs ``40-49岁``。

把这三类归一到契约口径后重算，才是「两臂到底在多少字段上说的一致」。
剩下的「不同」才值得人工看：那是真的取值分歧（如新版 ``first_time`` vs 基线 ``已到店``），
或口径深浅不同（如新版 ``2档收入`` vs 基线 ``8-15万``）。

归一方法：
1. 枚举字段：key 与展示名都映射到同一个**契约 token**（key 本身）；
2. 文本：去空白、统一分隔符为 ``|``、去「岁/万」等单位后缀、全角转半角；
3. 结构化枚举（``{key,input_value}``）：只取 ``key``。

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
OUT = HERE.parent / "review" / "semantic_agreement.json"
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src" / "customer_profile"))

SEP = re.compile(r"[|、,，/;；\n]+")
UNIT = re.compile(r"(岁|万元|万|元|档收入|档消费|档)")
FULLWIDTH = str.maketrans("０１２３４５６７８９（）－～", "0123456789()-~")


def _canon_tokens(key: str, value, nc) -> set[str]:
    """把一个字段取值归一成契约 token 集合。"""
    if value is None:
        return set()
    if isinstance(value, (list, tuple)):
        out: set[str] = set()
        for item in value:
            out |= _canon_tokens(key, item, nc)
        return out
    if isinstance(value, dict):
        inner = value.get("key") or value.get("value")
        base = _canon_tokens(key, inner, nc) if inner is not None else set()
        extra = value.get("input_value")
        if extra not in (None, "", []):
            base |= _canon_tokens(key, extra, nc)
        return base

    text = str(value).strip().translate(FULLWIDTH)
    if not text:
        return set()

    out: set[str] = set()
    for part in SEP.split(text):
        part = part.strip()
        if not part:
            continue
        part = UNIT.sub("", part).strip()
        if not part:
            continue
        # 枚举字段：key 与展示名都归到 key
        if nc is not None and nc.is_enum(key):
            legal = nc.legal_keys(key) or set()
            if part in legal:
                out.add(part)
                continue
            mapped = nc.display_to_key(key, part)
            if mapped:
                out.add(mapped)
                continue
        out.add(re.sub(r"[\s~]+", "", part).lower())
    return out


def _notes_map(side: dict) -> dict[str, object]:
    sec = (side or {}).get("sections") or {}
    val = sec.get("basic_notes_updates")
    if not isinstance(val, list):
        return {}
    out = {}
    for n in val:
        if isinstance(n, dict) and n.get("attribute_key"):
            out[str(n["attribute_key"])] = n.get("attribute_value")
    return out


def main() -> int:
    import notes_contract as nc
    packets = sorted(p for p in PACKETS.glob("*.json") if p.name != "_index.json")

    both = same = diff = setonly_new = setonly_base = 0
    diff_examples = defaultdict(list)
    per_case = []

    for p in packets:
        d = json.loads(p.read_text(encoding="utf-8"))
        nm, bm = _notes_map(d.get("new")), _notes_map(d.get("base"))
        row = {"phone": d["phone"], "both": 0, "same": 0, "diff": 0}
        for key in set(nm) | set(bm):
            a = _canon_tokens(key, nm.get(key), nc) if key in nm else None
            b = _canon_tokens(key, bm.get(key), nc) if key in bm else None
            a_ok, b_ok = bool(a), bool(b)
            if a_ok and b_ok:
                both += 1
                row["both"] += 1
                if a == b:
                    same += 1
                    row["same"] += 1
                else:
                    diff += 1
                    row["diff"] += 1
                    if len(diff_examples[key]) < 3:
                        diff_examples[key].append({
                            "phone": d["phone"],
                            "new": "/".join(sorted(a)), "base": "/".join(sorted(b)),
                        })
            elif a_ok:
                setonly_new += 1
            elif b_ok:
                setonly_base += 1
        per_case.append(row)

    print("== 语义一致率（枚举 key/展示名归一后）==")
    print("两臂都有取值 : %d 字段次" % both)
    print("  归一后一致  : %d（%.1f%%）" % (same, 100.0 * same / both if both else 0))
    print("  仍不同      : %d（%.1f%%）" % (diff, 100.0 * diff / both if both else 0))
    print("仅新版有      : %d 字段次" % setonly_new)
    print("仅基线有      : %d 字段次" % setonly_base)
    print()
    print("== 仍有分歧的字段（前 20，按分歧案数降序）==")
    for key, ex in sorted(diff_examples.items(), key=lambda x: -len(x[1])):
        e = ex[0]
        print("  %-24s 新版=%s  基线=%s   (如 %s)" % (key, e["new"][:30], e["base"][:30], e["phone"]))

    OUT.write_text(json.dumps({
        "totals": {"both": both, "same": same, "diff": diff,
                   "only_new": setonly_new, "only_base": setonly_base},
        "diff_examples": dict(diff_examples), "per_case": per_case,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n已写出 %s" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
