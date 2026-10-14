# -*- coding: utf-8 -*-
"""v3 对照抽取（只读）：把 A-pipeline-L8-v3 与基线同号 8 案抽成交付用 payload。

与 v2.1 版（``ab对照-v21/scripts/extract_v21.py``）的差异：

- 数据源为 v3 修复批（L1 三层合并落地后） ``A-pipeline-L8-v3-20261014-fix``；
- 节点 ID 换成 v3 三分层后的新图：``pipeline_l1_facts`` / ``pipeline_l1_persona`` /
  ``pipeline_l1_fields`` 取代旧的 ``pipeline_l1_llm``，并新增 ``pipeline_l2_review``；
- 实验臂在 payload 里的键名由 ``v2`` 改为 ``v3``；
- Notes 单边差异仍按 ``attribute_key`` 求，但**页面不再把它做成独立模块**——页面把两臂
  按字段并集成一张大表，缺的一侧留空并高亮。此处保留 ``notes_diff`` 只为总览读数用。
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]  # docs/business_opt01_v2/ab对照-v3/scripts -> repo root
A = REPO / "experiments/abexperiments/batches/A-pipeline-L8-v3-20261014-fix/runs"
B = REPO / "experiments/batches/baseline-39-dsv41-noThink/runs"
OUT = HERE.parent / "data"

BATCH_A = "A-pipeline-L8-v3-20261014-fix"
BATCH_B = "baseline-39-dsv41-noThink"

PHONES = "17119850673 13415100087 13527911093 19820803068 13435844484 18898883341 13530559591".split()

SEC = ["profile_summary", "inferred_tags", "usage_scenarios", "communication_strategies",
       "customer_overview", "purchase_motivations", "product_preferences", "resistances",
       "emotion_state"]
SEC_LABEL = {
    "profile_summary": "画像摘要", "inferred_tags": "推断标签", "usage_scenarios": "用车场景",
    "communication_strategies": "沟通策略", "customer_overview": "客户概览",
    "purchase_motivations": "购车动机", "product_preferences": "产品偏好",
    "resistances": "抗性/顾虑", "emotion_state": "情绪状态",
}

EXTRA_NODES = [
    ("pipeline_jiguang", "极光第三方画像"),
    ("pipeline_mengshi_it", "盟识 IT 系统"),
    ("pipeline_phone_company", "手机号归属地 / 关联企业"),
    ("pipeline_locked_notes", "人工确认 Notes"),
    ("pipeline_chat_history", "聊天记录"),
    ("pipeline_feedback", "人工反馈"),
    ("pipeline_outbound_call", "外呼录音（纯识别变体产出）"),
    ("pipeline_test_drive", "试驾录音（纯识别变体产出）"),
]
BASE_EXTRA = [
    ("1777286551779", "极光第三方画像"),
    ("1778231153601", "盟识 IT 系统"),
    ("1776070114662", "手机号归属地 / 关联企业"),
    ("1776325681312", "聊天记录"),
]

TIER_LABEL = {"fact": "事实", "inferred_high": "强推断", "inferred_low": "弱推断"}


def nid(it):
    return it.get("node_id") or it.get("id")


def index(root, phone):
    nodes = json.loads((root / phone / "nodes.json").read_text(encoding="utf-8"))
    if isinstance(nodes, dict) and "nodes" in nodes:
        nodes = nodes["nodes"]
    d = {}
    for it in nodes:
        d.setdefault(nid(it), []).append(it.get("outputs") or it.get("output") or {})
    return d


def o1(d, k):
    a = d.get(k)
    return (a[0] if a else {}) or {}


def jload(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def payload_of(root, phone):
    raw = json.loads((root / phone / "result.json").read_text(encoding="utf-8"))
    r1 = raw.get("result1")
    try:
        pay = json.loads(r1) if isinstance(r1, str) else r1
    except ValueError:
        pay = None
    return raw, (pay or {})


def run_status(root, phone):
    """该号码本次运行的终态。失败案没有 result1，需据此在页面上标出。"""
    try:
        raw = json.loads((root / phone / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "missing", ""
    return str(raw.get("status") or "unknown"), str(raw.get("error") or "")


def failure_reason(root, phone):
    """从尝试记录里摘出失败原因的人话摘要。失败案没有 result1，只能看 attempts。"""
    try:
        att = json.loads((root / phone / "attempts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    for a in att:
        if str(a.get("kind")) == "llm" and a.get("error"):
            return str(a.get("error"))[:200]
    return ""


def metrics(root, phone):
    att = json.loads((root / phone / "attempts.json").read_text(encoding="utf-8"))
    llm = [a for a in att if str(a.get("kind")) == "llm"]
    tok = 0
    for a in llm:
        u = a.get("usage_json")
        if isinstance(u, str):
            try:
                u = json.loads(u)
            except ValueError:
                u = None
        if isinstance(u, dict):
            tok += int(u.get("total_tokens") or 0)
    return {"llm_calls": len(llm), "http_calls": len(att) - len(llm), "tokens": tok}


def kind_of(v):
    """section 内容形态：空 / 列表条数 / 值字典字段数。"""
    if v is None:
        return 0
    if isinstance(v, list):
        return len(v)
    if isinstance(v, dict):
        return sum(1 for x in v.values() if x not in (None, "", [], {}))
    return 0 if not str(v).strip() else 1


def notes_map(notes):
    out = {}
    for n in notes or []:
        k = n.get("attribute_key")
        if k:
            out[str(k)] = n
    return out


def notes_diff(v3_notes, base_notes):
    """按 attribute_key 求单边差异。返回按三类分组的字段列表（页面用 both/all 做并集）。"""
    v3m, bm = notes_map(v3_notes), notes_map(base_notes)
    v3k, bk = set(v3m), set(bm)
    both = sorted(v3k & bk)
    only_base = sorted(bk - v3k)
    only_v3 = sorted(v3k - bk)

    def brief(m, keys):
        out = []
        for k in keys:
            n = m[k]
            val = n.get("attribute_value")
            if isinstance(val, dict):
                val = val.get("input_value") or val.get("key") or json.dumps(val, ensure_ascii=False)
            if isinstance(val, list):
                val = " / ".join(str(x) for x in val)
            out.append({"key": k, "value": str(val or "")})
        return out

    return {
        "both": brief(v3m, both),
        "only_base": brief(bm, only_base),
        "only_v3": brief(v3m, only_v3),
    }


def main() -> int:
    cases = []
    for phone in PHONES:
        ra, va = payload_of(A, phone)
        rb, vb = payload_of(B, phone)
        aar = va.get("analysis_result") or {}
        bar = vb.get("analysis_result") or {}
        ia, ib = index(A, phone), index(B, phone)

        notes_v3 = aar.get("basic_notes_updates") or []
        notes_bs = bar.get("basic_notes_updates") or []

        facts_o = o1(ia, "pipeline_l1_facts")
        persona_o = o1(ia, "pipeline_l1_persona")
        fields_o = o1(ia, "pipeline_l1_fields")
        loc = o1(ia, "pipeline_l1_localize")
        proj = o1(ia, "pipeline_l2_project")
        rec = o1(ia, "pipeline_l0_records")
        tri = o1(ia, "pipeline_l4_triage")
        fb = o1(ia, "pipeline_l2_fallback")
        ap = o1(ia, "pipeline_l2_apply")
        rv = o1(ia, "pipeline_l2_review")

        bundle = o1(ia, "pipeline_channel_bundle").get("channels_json")
        bobj = jload(bundle) if isinstance(bundle, str) else {}
        if not isinstance(bobj, dict):
            bobj = {}
        bundle_blob = json.dumps(bobj, ensure_ascii=False)

        extras_v3 = []
        for node_id, label in EXTRA_NODES:
            o = o1(ia, node_id)
            if not o:
                continue
            txt = o.get("result")
            if txt is None:
                txt = json.dumps(o, ensure_ascii=False)
            txt = str(txt)
            if txt and txt in bundle_blob:
                continue
            extras_v3.append({"label": label, "text": txt})

        extras_base = []
        for node_id, label in BASE_EXTRA:
            o = o1(ib, node_id)
            if not o:
                continue
            txt = o.get("result")
            if txt is None:
                txt = json.dumps(o, ensure_ascii=False)
            extras_base.append({"label": label, "text": str(txt)})

        sec_v3 = {k: aar.get(k) for k in SEC}
        sec_bs = {k: bar.get(k) for k in SEC}

        v3_status, v3_error = run_status(A, phone)
        if v3_status != "succeeded":
            v3_error = failure_reason(A, phone) or v3_error

        facts_records = jload(facts_o.get("records"))
        if not isinstance(facts_records, list):
            facts_records = []

        v3 = {
            "status": v3_status,
            "error": v3_error,
            "metrics": metrics(A, phone),
            "tier": tri.get("tier"), "tier_reason": tri.get("tier_reason"),
            "usable_count": tri.get("usable_count"),
            "records_count": rec.get("records_count"),
            "exception_count": rec.get("exception_count"),
            "records": jload(rec.get("records_json")),
            "l1_input": str(o1(ia, "pipeline_l1_input").get("output") or ""),
            "facts": {
                "records": facts_records,
                "raw": jload(facts_o.get("result")),
                "dropped_lines": jload(facts_o.get("dropped_lines")),
            },
            "persona": jload(persona_o.get("parsed_json")) or jload(persona_o.get("result")),
            "fields": jload(fields_o.get("parsed_json")) or jload(fields_o.get("result")),
            "localize": {
                "kept": loc.get("kept"), "dropped": loc.get("dropped"),
                "downgraded": loc.get("downgraded"),
                "empty_claims": proj.get("empty_claims"),
                "empty_claim_names": jload(proj.get("empty_claim_names")),
                "dropped_detail": jload(loc.get("dropped_detail")),
                "downgraded_detail": jload(loc.get("downgraded_detail")),
                "claims": (jload(loc.get("claims_json")) or {}).get("fields") or [],
            },
            "project": {
                "facts": jload(proj.get("facts_json")) or {},
                "basis": jload(proj.get("basis_json")) or {},
                "to_confirm": jload(proj.get("to_confirm_json")),
                "rejected": jload(proj.get("rejected_json")),
                "candidates": jload(proj.get("candidates_json")),
                "notes_count": proj.get("notes_count"),
            },
            "fallback": {
                "result": fb.get("result"),
                "applied": ap.get("applied"),
                "out_of_bounds": ap.get("out_of_bounds"),
                "verdict": jload(ap.get("verdict_json")),
            },
            "review": {
                "issue_count": rv.get("issue_count"),
                "issues": jload(rv.get("issue_json")),
                "placeholder_removed": jload(rv.get("placeholder_removed")),
                "enum_illegal": jload(rv.get("enum_illegal")),
                "unbacked_fact": jload(rv.get("unbacked_fact")),
            },
            "notes": notes_v3,
            "sections": sec_v3,
            "sec_counts": {k: kind_of(sec_v3[k]) for k in SEC},
        }
        base = {
            "metrics": metrics(B, phone),
            "evidence_items": jload(o1(ib, "1777105195548").get("result")),
            "profile_analysis": o1(ib, "1776764933122"),
            "notes": notes_bs,
            "sections": sec_bs,
            "sec_counts": {k: kind_of(sec_bs[k]) for k in SEC},
        }
        raw = {
            "history_body": jload(bobj.get("history_body")),
            "channels": bobj.get("channels"),
            "transcripts": bobj.get("transcripts"),
            "reports": bobj.get("reports"),
            "v3_extras": extras_v3,
            "base_extras": extras_base,
        }

        cases.append({
            "phone": phone,
            "v3": v3, "base": base, "raw": raw,
            "notes_diff": notes_diff(notes_v3, notes_bs),
        })

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "payload.json").write_text(
        json.dumps({"cases": cases, "section_labels": SEC_LABEL, "sections": SEC,
                    "tier_label": TIER_LABEL, "batch_a": BATCH_A, "batch_b": BATCH_B},
                   ensure_ascii=False),
        encoding="utf-8")

    tot_v3 = sum(len(c["v3"]["notes"]) for c in cases)
    tot_bs = sum(len(c["base"]["notes"]) for c in cases)
    print("payload:", format((OUT / "payload.json").stat().st_size, ","), "字节")
    print("案数:", len(cases))
    print("Notes 合计: v3=%d  基线=%d  差值=%+d" % (tot_v3, tot_bs, tot_v3 - tot_bs))
    print()
    print("%-13s %-6s %-6s %-6s %s" % ("手机号", "v3", "基线", "状态", "仅基线有 / 仅v3有"))
    for c in cases:
        d = c["notes_diff"]
        print("%-13s %-6d %-6d %-8s %d / %d" % (
            c["phone"], len(c["v3"]["notes"]), len(c["base"]["notes"]),
            c["v3"]["status"], len(d["only_base"]), len(d["only_v3"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
