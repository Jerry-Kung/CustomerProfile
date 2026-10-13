# -*- coding: utf-8 -*-
"""只读探针：把 ⑦/⑨/① 三个退化项的真实样例打出来，供 HTML 页面写结论用。"""
import json
import re
from pathlib import Path

REPO = Path("D:/KLH/Projects/dfms/CustomerProfile")
A = REPO / "experiments/abexperiments/batches/A-pipeline-L8-20261012/runs"
B = REPO / "experiments/batches/baseline-39-dsv41-noThink/runs"

PHONES = "17119850673 13415100087 13527911093 19820803068 13435844484 18898883341 13530559591 13829156876".split()
SEC = ["profile_summary", "inferred_tags", "usage_scenarios", "communication_strategies",
       "customer_overview", "purchase_motivations", "product_preferences", "resistances",
       "emotion_state"]


def payload_of(root, phone):
    raw = json.loads((root / phone / "result.json").read_text(encoding="utf-8"))
    r1 = raw.get("result1")
    try:
        pay = json.loads(r1) if isinstance(r1, str) else r1
    except Exception:
        pay = None
    return pay or {}


def secs(pay):
    ar = pay.get("analysis_result") or {}
    return {k: ar.get(k) for k in SEC}


def ng(s):
    s = re.sub(r"\s+", "", str(s))
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


print("=" * 78)
print("⑨ summary 与 steps 语义零重叠的真实样例（v2）")
print("=" * 78)
n = 0
for phone in PHONES:
    S = secs(payload_of(A, phone))

    def walk(node, path):
        global n
        if isinstance(node, dict):
            if "summary" in node and ("steps" in node or "evidence" in node):
                a = ng(node.get("summary"))
                b = ng("".join(map(str, (node.get("steps") or []) + (node.get("evidence") or []))))
                if a and b and not (a & b):
                    n += 1
                    if n <= 6:
                        print()
                        print("  [%s] %s" % (phone, path))
                        print("    summary : %s" % str(node.get("summary"))[:150])
                        for i, s in enumerate((node.get("steps") or [])[:3]):
                            print("    step%-3d: %s" % (i + 1, str(s)[:130]))
            for k, v in node.items():
                walk(v, path + "." + str(k) if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, "%s[%d]" % (path, i))

    for k in SEC:
        walk(S.get(k), k)
print()
print("  合计 %d 个块命中" % n)

print()
print("=" * 78)
print("⑦ 同一份卡片里 age 的两种写法（v2）")
print("=" * 78)
for phone in PHONES:
    S = secs(payload_of(A, phone))
    pa = (S.get("profile_summary") or {}).get("age")
    if isinstance(pa, dict):
        pa = pa.get("value")
    notes = (payload_of(A, phone).get("analysis_result") or {}).get("basic_notes_updates") or []
    na = None
    for x in notes:
        if str(x.get("attribute_key")) == "age":
            na = x.get("attribute_value")
    if isinstance(na, dict):
        na = na.get("input_value") or na.get("key")
    print("  %-13s 卡片 profile_summary.age = 「%s」    Notes.age = 「%s」" % (phone, pa, na))

print()
print("=" * 78)
print("① v2 枚举字段落进非契约值 / unknown 的全部位置")
print("=" * 78)
for phone in PHONES:
    pay = payload_of(A, phone)
    ar = pay.get("analysis_result") or {}
    notes = ar.get("basic_notes_updates") or []
    for x in notes:
        k = str(x.get("attribute_key"))
        v = x.get("attribute_value")
        s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v or "")
        if "unknown" in s or re.search(r"[\u4e00-\u9fff]", s):
            print("  %-13s %-24s = %s" % (phone, k, s[:80]))

print()
print("=" * 78)
print("section 空缺（两臂逐案）")
print("=" * 78)
print("  %-13s %-38s %s" % ("手机号", "基线缺", "v2 缺"))
for phone in PHONES:
    sb = secs(payload_of(B, phone))
    sa = secs(payload_of(A, phone))
    mb = [k for k in SEC if not sb.get(k)]
    ma = [k for k in SEC if not sa.get(k)]
    print("  %-13s %-38s %s" % (phone, ",".join(mb) or "—", ",".join(ma) or "—"))
