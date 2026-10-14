# -*- coding: utf-8 -*-
"""1a 事实层 vs 1c 字段层 的合并覆盖率与兜底救援读数（只读，零 token）。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[4]
BATCH = ROOT / "experiments/abexperiments/batches/A-pipeline-L8-v3-20261013/runs"


def main() -> int:
    tot_1a = tot_1c = tot_union = tot_only1a = 0
    rescued = 0
    for ph in sorted(os.listdir(BATCH)):
        nodes = json.loads((BATCH / ph / "nodes.json").read_text(encoding="utf-8"))

        def g(nid):
            for n in nodes:
                if n.get("node_id") == nid:
                    return n
            return None

        f, fl = g("pipeline_l1_facts"), g("pipeline_l1_fields")
        recs = f["outputs"].get("records") or []
        if isinstance(recs, str):
            try:
                recs = json.loads(recs)
            except ValueError:
                recs = []
        dropped = f["outputs"].get("dropped_lines")
        pj = fl["outputs"].get("parsed_json")
        fields = (pj or {}).get("fields") if isinstance(pj, dict) else None
        if fields is None:
            r = fl["outputs"].get("result")
            try:
                fields = json.loads(r).get("fields") if isinstance(r, str) else (r or {}).get("fields")
            except ValueError:
                fields = []
        a = set(str(x.get("attribute")) for x in recs)
        b = set(str(x.get("attribute")) for x in (fields or []))
        # 1c 若无该字段，但 1a 有 → 走兜底补全才能回来
        only1a = a - b
        # 兜底实际采纳的字段
        ap = g("pipeline_l2_apply")
        vr = ap["outputs"].get("verdict_json")
        if isinstance(vr, str):
            try:
                vr = json.loads(vr)
            except ValueError:
                vr = []
        applied_keys = set(str(x.get("attribute_key")) for x in (vr or []))
        got = only1a & applied_keys
        rescued += len(got)
        tot_1a += len(a); tot_1c += len(b); tot_union += len(a | b); tot_only1a += len(only1a)
        print("%s settled=%s | 1a=%d 1c=%d 并集=%d 仅1a有=%d | 兜底救回=%d %s | 病行=%s"
              % (ph, f["outputs"].get("status") or "?", len(a), len(b), len(a | b),
                 len(only1a), len(got), sorted(got), (dropped if dropped not in (None, "", "[]") else "-")))
    print("\n合计：1a 字段 %d、1c 字段 %d、并集 %d、**仅 1a 有（1c 丢失）%d**、兜底救回 %d"
          % (tot_1a, tot_1c, tot_union, tot_only1a, rescued))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
