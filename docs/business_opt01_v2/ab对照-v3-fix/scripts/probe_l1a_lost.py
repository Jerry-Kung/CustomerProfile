# -*- coding: utf-8 -*-
"""量化「1a 已检出、最终 Notes 为空」的字段（只读，零 token）。

用法：python -X utf8 scripts/probe_l1a_lost.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
BATCH = ROOT / "experiments/abexperiments/batches/A-pipeline-L8-v3-20261013/runs"

try:
    from customer_profile import notes_contract as nc
    CONTRACT = set(str(e["attribute_key"]) for e in nc.RAW_CONFIGS)
except Exception as exc:  # noqa: BLE001
    CONTRACT = set()
    print("契约加载失败:", exc)

# 身份类字段：由 L2 投影的身份白名单单独处置，不计入「内容字段丢失」
IDENTITY = {"phone_number", "wechat_id", "customer_name"}


def main() -> int:
    grand_lost = []
    for ph in sorted(os.listdir(BATCH)):
        nodes = json.loads((BATCH / ph / "nodes.json").read_text(encoding="utf-8"))

        def g(nid):
            for n in nodes:
                if n.get("node_id") == nid:
                    return n
            return None

        recs = g("pipeline_l1_facts")["outputs"].get("records") or []
        if isinstance(recs, str):
            recs = json.loads(recs)
        facts_map = {str(x.get("attribute")): x for x in recs}

        pj = g("pipeline_l1_fields")["outputs"].get("parsed_json")
        fields = (pj or {}).get("fields") if isinstance(pj, dict) else None
        if fields is None:
            r = g("pipeline_l1_fields")["outputs"].get("result")
            fields = json.loads(r).get("fields") if isinstance(r, str) else []
        c_keys = set(str(x.get("attribute")) for x in (fields or []))

        # 最终 Notes 键
        ru = g("pipeline_l2_review")["outputs"].get("notes_updates_json")
        if isinstance(ru, str):
            ru = json.loads(ru)
        notes_keys = set(str(u.get("attribute_key")) for u in (ru or []))

        lost = []
        for k, f in facts_map.items():
            if k in c_keys:
                continue          # 1c 复述了，不算丢
            if k in notes_keys:
                continue          # 最终 Notes 里回来了（兜底救回）
            if k in IDENTITY:
                continue
            lost.append((k, f.get("value"), f.get("basis"), f.get("anchor_kind")))
        in_contract = [x for x in lost if x[0] in CONTRACT]
        grand_lost += in_contract
        print("%s  1a检出但最终Notes为空：%d 条（其中契约内容字段 %d）" % (ph, len(lost), len(in_contract)))
        for k, v, b, ak in in_contract:
            print("     %-24s = %-14s basis=%s anchor=%s" % (k, str(v)[:14], b, ak))

    print("\n===== 合计：1a 已检出、最终 Notes 为空、且属契约内容字段：%d 条 =====" % len(grand_lost))
    # 字段出现频次
    from collections import Counter
    for k, n in Counter(x[0] for x in grand_lost).most_common():
        print("   %-24s %d 案" % (k, n))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
