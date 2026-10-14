# -*- coding: utf-8 -*-
"""追一个字段在 v3 管线各层的生死（只读，零 token）。

用法：python -X utf8 scripts/probe_field_trace.py <phone> <attribute_key>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[4]
BATCH = ROOT / "experiments/abexperiments/batches/A-pipeline-L8-v3-20261013/runs"


def load(phone: str):
    p = BATCH / phone / "nodes.json"
    return json.loads(p.read_text(encoding="utf-8"))


def node_of(nodes, node_id):
    for n in nodes:
        if n.get("node_id") == node_id:
            return n
    return None


def out(n, key, default=None):
    return (n or {}).get("outputs", {}).get(key, default)


def show(title, items):
    print("\n===== %s =====" % title)
    if not items:
        print("  (空)")
    for x in items:
        print(" ", json.dumps(x, ensure_ascii=False)[:400])


def main() -> int:
    phone = sys.argv[1]
    key = sys.argv[2]
    nodes = load(phone)

    # 1a 事实层
    facts_node = node_of(nodes, "pipeline_l1_facts")
    recs = out(facts_node, "records") or []
    if isinstance(recs, str):
        try:
            recs = json.loads(recs)
        except ValueError:
            recs = []
    hit_recs = [r for r in recs if key in json.dumps(r, ensure_ascii=False)]
    show("1a 事实层 records 命中（共 %d 条事实）" % len(recs), hit_recs)

    # 1c 分析投影层
    fnode = node_of(nodes, "pipeline_l1_fields")
    raw = out(fnode, "result")
    pj = out(fnode, "parsed_json")
    fields = None
    for cand in (pj, raw):
        if isinstance(cand, dict) and "fields" in cand:
            fields = cand["fields"]
            break
    if fields is None and isinstance(raw, str):
        try:
            fields = json.loads(raw).get("fields")
        except ValueError:
            fields = None
    hit_fields = [f for f in (fields or []) if str(f.get("attribute")) == key]
    show("1c 分析投影层 fields 命中（共 %d 条）" % len(fields or []), hit_fields)

    # localize
    lnode = node_of(nodes, "pipeline_l1_localize")
    kept = out(lnode, "claims_json")
    if isinstance(kept, str):
        try:
            kept = json.loads(kept).get("fields", [])
        except ValueError:
            kept = []
    hit_kept = [x for x in (kept or []) if str(x.get("attribute")) == key]
    show("localize 保留 claims 命中（kept=%s dropped=%s downgraded=%s）" % (
        out(lnode, "kept"), out(lnode, "dropped"), out(lnode, "downgraded")), hit_kept)

    # L2 project
    pnode = node_of(nodes, "pipeline_l2_project")
    facts = out(pnode, "facts_json")
    if isinstance(facts, str):
        facts = json.loads(facts)
    hit_facts = [f for f in (facts or []) if key in json.dumps(f, ensure_ascii=False)]
    show("L2 project facts_json 命中", hit_facts)

    nu = out(pnode, "notes_updates_json")
    if isinstance(nu, str):
        nu = json.loads(nu)
    hit_nu = [u for u in (nu or []) if str(u.get("attribute_key")) == key]
    show("L2 project notes_updates 命中（共 %d）" % len(nu or []), hit_nu)
    print("  empty_claim_names:", out(pnode, "empty_claim_names"))
    print("  notes_count:", out(pnode, "notes_count"), "tier:", out(pnode, "tier"))

    # candidates
    cands = out(pnode, "candidates_json")
    if isinstance(cands, str):
        cands = json.loads(cands)
    hit_c = [c for c in (cands or []) if str(c.get("attribute_key")) == key or str(c.get("attribute")) == key]
    show("L2 project candidates 命中（共 %d）" % len(cands or []), hit_c)

    basis = out(pnode, "basis_json")
    if isinstance(basis, str):
        basis = json.loads(basis)
    print("  basis[%s] = %r" % (key, (basis or {}).get(key)))

    # L2 fallback
    fb = node_of(nodes, "pipeline_l2_fallback")
    vr = out(fb, "result")
    show("L2 兜底 verdict 原文（前 1500 字）", [str(vr)[:1500]])

    # L2 apply
    an = node_of(nodes, "pipeline_l2_apply")
    au = out(an, "notes_updates_json")
    if isinstance(au, str):
        au = json.loads(au)
    hit_au = [u for u in (au or []) if str(u.get("attribute_key")) == key]
    show("L2 apply notes_updates 命中（applied=%s out_of_bounds=%s）" % (
        out(an, "applied"), out(an, "out_of_bounds")), hit_au)

    # L2 review
    rn = node_of(nodes, "pipeline_l2_review")
    ru = out(rn, "notes_updates_json")
    if isinstance(ru, str):
        ru = json.loads(ru)
    hit_ru = [u for u in (ru or []) if str(u.get("attribute_key")) == key]
    show("L2 review 出口 notes_updates 命中", hit_ru)
    print("  review issues:", out(rn, "issue_json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
