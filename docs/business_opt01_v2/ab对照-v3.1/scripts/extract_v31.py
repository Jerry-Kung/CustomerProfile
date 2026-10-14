# -*- coding: utf-8 -*-
"""v3.1 对照抽取（只读）：把 A-pipeline-L8-v3.1-20261014 与基线同号 8 案抽成交付用 payload。

与 v3 版（``ab对照-v3-fix/scripts/extract_v3.py``）的差异：

- 数据源为 **v3.1 批次**（四项整改落地后） ``A-pipeline-L8-v3.1-20261014``，**8 案齐**
  （v3-fix 批因第 8 案超时只有 7 案）；
- 节点图新增 **``pipeline_l1_inject``（确定性注入，改造四）**，接在 ``L1_LOCALIZE`` 之后；
  ``pipeline_l1_merge``（合并层）增出 ``subject_overrides`` 读数（改造二）；
- payload 增 **v3.1 四类读数**：注入覆盖（phone_number / driver_license_status）、
  顾问词拒收（值形态门）、噪音锚点（寒暄语 / JSON 残片）、推演同集率（steps vs evidence）。

页面口径与 v3 版一致：Notes 按 ``attribute_key`` 求并集排两栏大表，缺侧留空高亮。
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]  # docs/business_opt01_v2/ab对照-v3.1/scripts -> repo root
A = REPO / "experiments/abexperiments/batches/A-pipeline-L8-v3.1-20261014/runs"
B = REPO / "experiments/batches/baseline-39-dsv41-noThink/runs"
OUT = HERE.parent / "data"

BATCH_A = "A-pipeline-L8-v3.1-20261014"
BATCH_B = "baseline-39-dsv41-noThink"

PHONES = ("13415100087 13435844484 13527911093 13530559591 "
          "13829156876 17119850673 18898883341 19820803068").split()

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

V31_FALLBACK_STEPS = frozenset({
    "由客户原话直接得出该结论",
    "经语义纠错归一并核对后得出该结论",
    "综合上述线索归纳得出该结论",
    "据现有记录归纳",
})
"""``_readable_steps`` 的**退路句**：产物里没有 ``reasoning_chain`` 时落到这几种非复述说明。
抽取脚本据它区分「真推导链」与「退路占位」，而不用 0% 同集率蒙混过关。"""


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


def notes_diff(v31_notes, base_notes):
    """按 attribute_key 求单边差异。返回按三类分组的字段列表（页面用 both/all 做并集）。"""
    v31m, bm = notes_map(v31_notes), notes_map(base_notes)
    v31k, bk = set(v31m), set(bm)
    both = sorted(v31k & bk)
    only_base = sorted(bk - v31k)
    only_v31 = sorted(v31k - bk)

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
        "both": brief(v31m, both),
        "only_base": brief(bm, only_base),
        # 键名沿用页面既有口径 ``only_v3``（app.js 读它；「v3」在这里指实验臂这一侧）。
        "only_v3": brief(v31m, only_v31),
    }


def _norm_set(items) -> set:
    """把 ``steps`` / ``evidence`` 归一成可比较集合（判两者是否雷同）。

    去「原始依据：/ 推断依据：」前缀、按分号拆分、逐条 strip——同一条依据在两栏里措辞
    略有包装差异时，不归一就会漏判雷同，把读数做假。
    """
    out = set()
    for item in items or []:
        text = str(item)
        for prefix in ("推断依据：", "推断依据:", "原始依据：", "原始依据:"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        for part in text.replace("；", "|").split("|"):
            part = part.strip()
            if part:
                out.add(part)
    return out


def steps_audit(notes):
    """v3.1 改造一读数：steps 与 evidence 是否同集，以及「真推导链 vs 退路占位」。"""
    total = same = chain = 0
    for n in notes or []:
        rd = n.get("reasoning_detail") or {}
        steps = rd.get("steps") or []
        evidence = rd.get("evidence") or []
        if steps and evidence:
            total += 1
            if _norm_set(steps) == _norm_set(evidence):
                same += 1
        if not (len(steps) == 1 and steps[0] in V31_FALLBACK_STEPS):
            chain += 1
    return {"fields": total, "same": same, "chain": chain}


def v31_readings(ia, loc, proj, inject, merge, notes):
    """v3.1 四项整改的逐案读数（全部从产物里读，不改判据口径）。"""
    # 改造四：确定性注入
    inj_phone = any(
        str(x.get("attribute")) == "phone_number"
        for x in (jload(inject.get("claims_json")) or {}).get("fields", []) or []
    )
    inj_license = any(
        str(x.get("attribute")) == "driver_license_status"
        and str(x.get("record_channel") or "") == "试驾与置换记录"
        for x in (jload(inject.get("claims_json")) or {}).get("fields", []) or []
    )
    # 改造二：值形态门拒收（顾问信息混入客户字段）
    salesperson = [
        {"attribute": r.get("attribute"), "value": r.get("value"), "rule": r.get("rule")}
        for r in (jload(proj.get("rejected_json")) or [])
        if str(r.get("rule")) == "a_salesperson_value"
    ]
    # 改造三：噪音锚点 / 信号缺失
    noisy, signal_mismatch = [], []
    for item in jload(loc.get("downgraded_detail")) or []:
        reason = str(item.get("reason") or "")
        if reason == "noisy_anchor":
            noisy.append({"attribute": item.get("attribute"), "anchor": item.get("anchor")})
        elif reason == "signal_mismatch":
            signal_mismatch.append({"attribute": item.get("attribute"), "anchor": item.get("anchor")})
    # 改造一：推导链
    return {
        "inject": {
            "added": inject.get("added"), "replaced": inject.get("replaced"),
            "injected": inject.get("injected"), "license_injected": inject.get("license_injected"),
            "phone_injected": inj_phone,
        },
        "merge": {
            "merged_count": merge.get("merged_count"), "facts_count": merge.get("facts_count"),
            "fields_count": merge.get("fields_count"),
            "from_facts_only": merge.get("from_facts_only"), "overrode": merge.get("overrode"),
            "subject_overrides": merge.get("subject_overrides"),
        },
        "salesperson": salesperson,
        "noisy_anchors": noisy,
        "signal_mismatch": signal_mismatch,
        "steps": steps_audit(notes),
    }


def main() -> int:
    cases = []
    for phone in PHONES:
        ra, va = payload_of(A, phone)
        rb, vb = payload_of(B, phone)
        aar = va.get("analysis_result") or {}
        bar = vb.get("analysis_result") or {}
        ia, ib = index(A, phone), index(B, phone)

        notes_v31 = aar.get("basic_notes_updates") or []
        notes_bs = bar.get("basic_notes_updates") or []

        facts_o = o1(ia, "pipeline_l1_facts")
        persona_o = o1(ia, "pipeline_l1_persona")
        fields_o = o1(ia, "pipeline_l1_fields")
        merge_o = o1(ia, "pipeline_l1_merge")
        loc = o1(ia, "pipeline_l1_localize")
        inject_o = o1(ia, "pipeline_l1_inject")
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

        extras_v31 = []
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
            extras_v31.append({"label": label, "text": txt})

        extras_base = []
        for node_id, label in BASE_EXTRA:
            o = o1(ib, node_id)
            if not o:
                continue
            txt = o.get("result")
            if txt is None:
                txt = json.dumps(o, ensure_ascii=False)
            extras_base.append({"label": label, "text": str(txt)})

        sec_v31 = {k: aar.get(k) for k in SEC}
        sec_bs = {k: bar.get(k) for k in SEC}

        v31_status, v31_error = run_status(A, phone)
        if v31_status != "succeeded":
            v31_error = failure_reason(A, phone) or v31_error

        facts_records = jload(facts_o.get("records"))
        if not isinstance(facts_records, list):
            facts_records = []

        readings = v31_readings(ia, loc, proj, inject_o, merge_o, notes_v31)

        v31 = {
            "status": v31_status,
            "error": v31_error,
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
            "merge": readings["merge"],
            "inject": readings["inject"],
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
            "v31": {
                "salesperson": readings["salesperson"],
                "noisy_anchors": readings["noisy_anchors"],
                "signal_mismatch": readings["signal_mismatch"],
                "steps": readings["steps"],
            },
            "notes": notes_v31,
            "sections": sec_v31,
            "sec_counts": {k: kind_of(sec_v31[k]) for k in SEC},
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
            "v31_extras": extras_v31,
            "base_extras": extras_base,
        }

        cases.append({
            "phone": phone,
            "v3": v31, "base": base, "raw": raw,
            "notes_diff": notes_diff(notes_v31, notes_bs),
        })

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "payload.json").write_text(
        json.dumps({"version": "v3.1", "cases": cases, "section_labels": SEC_LABEL,
                    "sections": SEC, "tier_label": TIER_LABEL,
                    "batch_a": BATCH_A, "batch_b": BATCH_B},
                   ensure_ascii=False),
        encoding="utf-8")

    tot_v31 = sum(len(c["v3"]["notes"]) for c in cases)
    tot_bs = sum(len(c["base"]["notes"]) for c in cases)
    print("payload:", format((OUT / "payload.json").stat().st_size, ","), "字节")
    print("案数:", len(cases))
    print("Notes 合计: v3.1=%d  基线=%d  差值=%+d" % (tot_v31, tot_bs, tot_v31 - tot_bs))
    print()
    print("%-13s %-6s %-6s %-6s %-6s %-8s %s" % ("手机号", "v3.1", "基线", "注入", "顾问拒", "同集", "状态"))
    for c in cases:
        v = c["v3"]
        inj = v["inject"]
        st = v["v31"]["steps"]
        print("%-13s %-6d %-6d %-6s %-6d %-8s %s" % (
            c["phone"], len(v["notes"]), len(c["base"]["notes"]),
            ("%s/%s" % ("P" if inj.get("phone_injected") else "-", "L" if inj.get("license_injected") else "-")),
            len(v["v31"]["salesperson"]),
            "%d/%d" % (st["same"], st["fields"]),
            v["status"]))
    print()
    print("=== v3.1 四项读数合计 ===")
    print("改造四 注入：phone_number 覆盖 %d/%d 案，驾照注入 %d 案" % (
        sum(1 for c in cases if c["v3"]["inject"].get("phone_injected")), len(cases),
        sum(1 for c in cases if c["v3"]["inject"].get("license_injected"))))
    print("改造二 值形态门拒收：%d 条" % sum(len(c["v3"]["v31"]["salesperson"]) for c in cases))
    print("改造三 噪音锚点：%d 条，信号缺失：%d 条" % (
        sum(len(c["v3"]["v31"]["noisy_anchors"]) for c in cases),
        sum(len(c["v3"]["v31"]["signal_mismatch"]) for c in cases)))
    tf = sum(c["v3"]["v31"]["steps"]["fields"] for c in cases)
    ts = sum(c["v3"]["v31"]["steps"]["same"] for c in cases)
    tc = sum(c["v3"]["v31"]["steps"]["chain"] for c in cases)
    print("改造一 推导链：有 steps/evidence 的字段 %d，两者同集 %d（%.1f%%），含真推导链 %d" % (
        tf, ts, (ts / tf * 100) if tf else 0.0, tc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
