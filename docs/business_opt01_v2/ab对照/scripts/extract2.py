# -*- coding: utf-8 -*-
"""只读提取（去重版）+ 内容质量排查 -> payload.json / findings.json

去重原则：``channel_bundle`` 已是「记录层」的原始输入，各渠道节点的单独输出若与其内容
重复则不再单列；只在 bundle 之外另有独立产出的节点（极光、盟识、企业分析、人工确认 Notes、
聊天记录、反馈）才单列。
"""
import json
import re
from pathlib import Path

REPO = Path("D:/KLH/Projects/dfms/CustomerProfile")
A = REPO / "experiments/abexperiments/batches/A-pipeline-L8-20261012/runs"
B = REPO / "experiments/batches/baseline-39-dsv41-noThink/runs"
TMP = Path("D:/KLH/DevTools/ClaudeCode/ClaudeTemp/opt01v2")

PHONES = "17119850673 13415100087 13527911093 19820803068 13435844484 18898883341 13530559591 13829156876".split()

SEC = ["profile_summary", "inferred_tags", "usage_scenarios", "communication_strategies",
       "customer_overview", "purchase_motivations", "product_preferences", "resistances",
       "emotion_state"]
SEC_LABEL = {
    "profile_summary": "画像摘要", "inferred_tags": "推断标签", "usage_scenarios": "用车场景",
    "communication_strategies": "沟通策略", "customer_overview": "客户概览",
    "purchase_motivations": "购车动机", "product_preferences": "产品偏好",
    "resistances": "抗性/顾虑", "emotion_state": "情绪状态",
}

# bundle 之外确有独立产出的节点
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
        except Exception:
            return v
    return v


def payload_of(root, phone):
    raw = json.loads((root / phone / "result.json").read_text(encoding="utf-8"))
    r1 = raw.get("result1")
    try:
        pay = json.loads(r1) if isinstance(r1, str) else r1
    except Exception:
        pay = None
    return raw, (pay or {})


def metrics(root, phone):
    at = json.loads((root / phone / "attempts.json").read_text(encoding="utf-8"))
    llm = [a for a in at if str(a.get("kind")) == "llm"]
    tok = 0
    by_node = {}
    for a in llm:
        u = a.get("usage_json")
        if isinstance(u, str):
            try:
                u = json.loads(u)
            except Exception:
                u = None
        n = int((u or {}).get("total_tokens") or 0) if isinstance(u, dict) else 0
        tok += n
        k = str(a.get("node_id"))
        by_node[k] = by_node.get(k, 0) + 1
    return {"llm_calls": len(llm), "http_calls": len(at) - len(llm), "tokens": tok, "by_node": by_node}


def flat_text(v):
    """把任意值拍成一段可搜索的文本。"""
    return json.dumps(v, ensure_ascii=False)


def audit_case(phone, v2, base):
    """六类判据的逐案排查，返回 findings 列表。"""
    F = []

    def add(kind, severity, title, detail, where=""):
        F.append({"kind": kind, "severity": severity, "title": title, "detail": detail, "where": where})

    notes = v2["notes"]
    secs = v2["sections"]
    facts = v2["project"]["facts"] or {}
    basis = v2["project"]["basis"] or {}
    claims = v2["localize"]["claims"] or []
    notes_map = {n.get("attribute_key"): n for n in notes}

    # --- 1 卡片 × Notes 同源：卡片里出现的确定值是否在 facts 里
    card_blob = flat_text({k: secs.get(k) for k in SEC})
    for field, label in (("age", "年龄"), ("annual_income", "年收入"), ("intended_brand", "意向品牌"),
                         ("intended_model", "意向车型"), ("old_car_model", "旧车车型")):
        val = notes_map.get(field, {}).get("attribute_value")
        if isinstance(val, dict):
            val = val.get("input_value") or val.get("key")
        if not val:
            continue
        core = str(val).replace(" ", "").replace("　", "")
        # 允许区间写法差异：取数字与关键词比对
        nums = re.findall(r"\d+", core)
        hit = core in card_blob.replace(" ", "")
        if not hit and nums:
            hit = all(n in card_blob for n in nums[:2])
        if not hit:
            add("card_note_gap", "warn", f"{label}：Notes 有值但卡片中找不到",
                f"Notes「{label}」= {val}；在其 9 个 section 的文本里检索不到该取值。",
                field)

    # --- 2 卡片内部矛盾：同一指标在多个 section 出现不同值
    age_hits = set()
    for m in re.finditer(r"(\d{2})\s*[-~到至]\s*(\d{2})\s*岁", card_blob):
        age_hits.add(m.group(0).replace(" ", ""))
    if len(age_hits) > 1:
        add("card_internal", "error", "卡片内部年龄区间不一致", "同一份卡片出现多个年龄段：" + "、".join(sorted(age_hits)))

    inc_hits = set()
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[-~到至]\s*(\d+(?:\.\d+)?)\s*万", card_blob):
        inc_hits.add(m.group(0).replace(" ", ""))
    if len(inc_hits) > 1:
        add("card_internal", "warn", "卡片内部收入区间不一致", "同一份卡片出现多个收入区间：" + "、".join(sorted(inc_hits)))

    # --- 3 空值入叙述：卡片里出现空字段渲染痕迹
    for pat, desc in (("：\\s*[,，}]", "疑似空值入叙述"), ("\"\\s*\"", "空字符串字段"),
                      ("null", "null 字段"), ("undefined", "undefined")):
        n = len(re.findall(pat, card_blob))
        if n:
            add("empty_render", "warn", desc, f"卡片文本中匹配到 {n} 处「{pat}」。")

    # --- 4 未知渠道：Notes 的证据串退化
    unk = 0
    samples = []
    for n in notes:
        ev = (n.get("reasoning_detail") or {}).get("evidence") or []
        for s in ev:
            if "未知渠道" in str(s):
                unk += 1
                if len(samples) < 3:
                    samples.append(f"{n.get('attribute_key')}: {s}")
    if unk:
        add("unknown_channel", "error", f"{unk} 条证据的渠道标注退化为「未知渠道」",
            "证据串只剩来源档、看不出值来自哪个渠道，审计链断掉。样例：" + "；".join(samples))

    # --- 5 无依据确定值 / 中间档缺失
    fact_claims = [c for c in claims if str(c.get("basis")) == "fact"]
    low = [k for k, v in basis.items() if str(v) == "inferred_low"]
    if fact_claims and not any(str(v) == "fact" for v in basis.values()):
        add("basis_collapse", "error", "三分类中间档与 fact 档在字段级全部塌成 inferred_low",
            f"L1 断言 fact {len(fact_claims)} 条，但投影后 {len(basis)} 个降级字段全部是 inferred_low，无一 fact。")

    # --- 6 section 空缺
    empty = [SEC_LABEL[s] for s in SEC if not secs.get(s)]
    if empty:
        # 判断有无原料
        have = []
        for s in SEC:
            if secs.get(s):
                continue
            rel = [k for k in facts if facts.get(k) not in (None, "", [], {})]
            have.append(SEC_LABEL[s])
        add("section_gap", "warn", f"{len(empty)} 个 section 为空：" + "、".join(empty),
            "表达层未给出这些 section，装配层以空形态占位（未编造）。")

    # --- 7 多值丢值：与基线同字段比
    bnotes = {n.get("attribute_key"): n.get("attribute_value") for n in base["notes"]}
    lost = []
    for k, v in notes_map.items():
        bv = bnotes.get(k)
        if bv is None or v is None:
            continue
        av = v.get("attribute_value")
        ba = bv if isinstance(bv, list) else str(bv).split("||")
        aa = av if isinstance(av, list) else str(av or "").split("||")
        ba = [x for x in ba if str(x).strip()]
        aa = [x for x in aa if str(x).strip()]
        if len(ba) > 1 and len(aa) < len(ba):
            lost.append(f"{k}: {ba} → {aa}")
    if lost:
        add("multi_loss", "warn", f"{len(lost)} 个多值字段取值减少",
            "；".join(lost[:6]))

    # --- 8 空值占位断言
    blanks = [c.get("attribute") for c in claims
              if str(c.get("value") or "").strip() in ("", "[]") or c.get("value") == []]
    if blanks:
        add("empty_claim", "info", f"{len(blanks)} 条 L1 断言是空值占位",
            "断言存在但取值为空：" + "、".join(sorted(set(blanks))[:10]))

    return F


def main():
    cases = []
    findings_all = []
    for phone in PHONES:
        va, vb = index(A, phone), index(B, phone)

        rec = o1(va, "pipeline_l0_records")
        tri = o1(va, "pipeline_l4_triage")
        loc = o1(va, "pipeline_l1_localize")
        proj = o1(va, "pipeline_l2_project")
        ap = o1(va, "pipeline_l2_apply")
        fb = o1(va, "pipeline_l2_fallback")
        _, v2pay = payload_of(A, phone)
        _, bpay = payload_of(B, phone)
        var = v2pay.get("analysis_result") or {}
        bar = bpay.get("analysis_result") or {}

        bundle = o1(va, "pipeline_channel_bundle").get("channels_json")
        bobj = jload(bundle) if isinstance(bundle, str) else {}
        if not isinstance(bobj, dict):
            bobj = {}

        bundle_blob = flat_text(bobj)
        extras_v2 = []
        for node_id, label in EXTRA_NODES:
            o = o1(va, node_id)
            if not o:
                continue
            txt = o.get("result")
            if txt is None:
                txt = json.dumps(o, ensure_ascii=False)
            txt = str(txt)
            if txt and txt in bundle_blob:
                continue
            extras_v2.append({"label": label, "text": txt})

        extras_base = []
        for node_id, label in BASE_EXTRA:
            o = o1(vb, node_id)
            if not o:
                continue
            txt = o.get("result")
            if txt is None:
                txt = json.dumps(o, ensure_ascii=False)
            extras_base.append({"label": label, "text": str(txt)})

        v2 = {
            "metrics": metrics(A, phone),
            "tier": tri.get("tier"), "tier_reason": tri.get("tier_reason"),
            "usable_count": tri.get("usable_count"),
            "records_count": rec.get("records_count"), "exception_count": rec.get("exception_count"),
            "records": jload(rec.get("records_json")),
            "l1_input": str(o1(va, "pipeline_l1_input").get("output") or ""),
            "l1_llm": jload(o1(va, "pipeline_l1_llm").get("result")),
            "localize": {
                "kept": loc.get("kept"), "dropped": loc.get("dropped"),
                "downgraded": loc.get("downgraded"),
                "dropped_detail": jload(loc.get("dropped_detail")),
                "downgraded_detail": jload(loc.get("downgraded_detail")),
                "claims": (jload(loc.get("claims_json")) or {}).get("fields") or [],
            },
            "project": {
                "facts": jload(proj.get("facts_json")), "basis": jload(proj.get("basis_json")),
                "to_confirm": jload(proj.get("to_confirm_json")),
                "rejected": jload(proj.get("rejected_json")),
                "candidates": jload(proj.get("candidates_json")),
                "notes_count": proj.get("notes_count"),
            },
            "fallback": {"result": fb.get("result"), "applied": ap.get("applied"),
                         "out_of_bounds": ap.get("out_of_bounds"), "verdict": jload(ap.get("verdict_json"))},
            "notes": var.get("basic_notes_updates") or [],
            "sections": {k: var.get(k) for k in SEC},
        }
        base = {
            "metrics": metrics(B, phone),
            "evidence_items": jload(o1(vb, "1777105195548").get("result")),
            "profile_analysis": o1(vb, "1776764933122"),
            "notes": bar.get("basic_notes_updates") or [],
            "sections": {k: bar.get(k) for k in SEC},
        }
        raw = {
            "history_body": jload(bobj.get("history_body")),
            "channels": bobj.get("channels"),
            "transcripts": bobj.get("transcripts"),
            "reports": bobj.get("reports"),
            "v2_extras": extras_v2,
            "base_extras": extras_base,
        }

        fs = audit_case(phone, v2, base)
        for f in fs:
            f["phone"] = phone
        findings_all.extend(fs)

        cases.append({"phone": phone, "v2": v2, "base": base, "raw": raw,
                      "finding_count": len(fs)})

    (TMP / "payload.json").write_text(
        json.dumps({"cases": cases, "section_labels": SEC_LABEL, "sections": SEC}, ensure_ascii=False),
        encoding="utf-8")
    (TMP / "findings.json").write_text(json.dumps(findings_all, ensure_ascii=False, indent=1), encoding="utf-8")

    print("payload:", format((TMP / "payload.json").stat().st_size, ","), "字节")
    print("案数:", len(cases))
    print()
    print("=== 排查发现汇总 ===")
    from collections import Counter
    c = Counter(f["kind"] for f in findings_all)
    for k, v in c.most_common():
        print("  %-18s%d" % (k, v))
    print()
    err = [f for f in findings_all if f["severity"] == "error"]
    print("error 级 %d 条 / warn 级 %d 条 / info 级 %d 条" % (
        len([f for f in findings_all if f["severity"] == "error"]),
        len([f for f in findings_all if f["severity"] == "warn"]),
        len([f for f in findings_all if f["severity"] == "info"])))
    print()
    for f in err:
        print("  [%s] %s | %s" % (f["phone"], f["title"], f["detail"][:100]))


if __name__ == "__main__":
    main()
