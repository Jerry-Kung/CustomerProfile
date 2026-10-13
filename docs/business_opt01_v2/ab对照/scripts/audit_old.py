# -*- coding: utf-8 -*-
"""老问题复检：按《基线画像质量诊断.md》§2–§3 的 13 类判据，同时审 v2 与基线这 8 案。

只读。输出 findings_old.json 与一份控制台汇总。
"""
import json
import re
from pathlib import Path

import sys
sys.path.insert(0, "D:/KLH/Projects/dfms/CustomerProfile/src")

from customer_profile import notes_contract as nc  # noqa: E402

REPO = Path("D:/KLH/Projects/dfms/CustomerProfile")
A = REPO / "experiments/abexperiments/batches/A-pipeline-L8-20261012/runs"
B = REPO / "experiments/batches/baseline-39-dsv41-noThink/runs"
TMP = Path("D:/KLH/DevTools/ClaudeCode/ClaudeTemp/opt01v2")

PHONES = "17119850673 13415100087 13527911093 19820803068 13435844484 18898883341 13530559591 13829156876".split()

SEC = ["profile_summary", "inferred_tags", "usage_scenarios", "communication_strategies",
       "customer_overview", "purchase_motivations", "product_preferences", "resistances",
       "emotion_state"]

SYNONYM_PAIRS = [
    ("comparison_brand", "comparison_brands", "compare_brand", "compare_brands", "对比品牌"),
    ("comparison_model", "comparison_models", "compare_model", "对比车型"),
    ("competitor_advantage", "competitor_advantages", "competitor_strength"),
    ("competitor_disadvantage", "competitor_disadvantages", "competitor_weakness"),
    ("brand_concern", "brand_concerns"),
    ("test_driven_models", "test_driven_model"),
    ("knowledge_of_erev", "knowledge_of_range_extender"),
]
SYNONYM_SETS = [set(p) for p in SYNONYM_PAIRS]

CHINESE = re.compile(r"[\u4e00-\u9fff]")
# 枚举值中允许出现中文的字段（text 类），这里只查枚举字段
PREDICTION_WORDS = ("可能", "推测", "预测", "表明", "估计", "大概率", "或许", "应该", "似乎")


def payload_of(root, phone):
    raw = json.loads((root / phone / "result.json").read_text(encoding="utf-8"))
    r1 = raw.get("result1")
    try:
        pay = json.loads(r1) if isinstance(r1, str) else r1
    except Exception:
        pay = None
    return (pay or {})


def notes_of(pay):
    return ((pay.get("analysis_result") or {}).get("basic_notes_updates")) or []


def sections_of(pay):
    ar = pay.get("analysis_result") or {}
    return {k: ar.get(k) for k in SEC}


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


def is_legal(attribute: str, value) -> bool:
    """枚举值是否合法键（含展示名反查）。与 customer_profile_l3.nc_is_legal 同口径。"""
    text = str(value or "").strip()
    if not text:
        return True
    return text in nc.legal_keys(attribute) or bool(nc.display_to_key(attribute, text))


def enum_label(arm, phone, notes, facts):
    hits = []
    facts = facts or {}
    for key, val in facts.items():
        if not nc.is_enum(key):
            continue
        items = val if isinstance(val, list) else [val]
        for it in items:
            if isinstance(it, dict):
                continue
            s = str(it or "")
            if s and not is_legal(key, s):
                hits.append({"field": key, "value": s, "why": "中文标签或非契约键" if CHINESE.search(s) else "非契约键"})
    for n in notes:
        key = str(n.get("attribute_key") or "")
        if not nc.is_enum(key):
            continue
        val = n.get("attribute_value")
        items = val if isinstance(val, list) else str(val or "").split("||")
        for it in items:
            s = str(it or "").strip()
            if s and not is_legal(key, s):
                hits.append({"field": key, "value": s, "why": "中文标签或非契约键（Notes）"})
    return hits


def synonym_split(notes):
    keys = {str(n.get("attribute_key")) for n in notes}
    hits = []
    for grp in SYNONYM_SETS:
        present = sorted(grp & keys)
        if len(present) > 1:
            hits.append(present)
    # 模板里根本不存在的 key
    unknown = sorted(k for k in keys if nc.by_key(k) is None)
    return hits, unknown


def purchase_coherence(notes, facts, sec_text):
    """purchase_type 与旧车信息的自洽。"""
    m = {str(n.get("attribute_key")): n.get("attribute_value") for n in notes}
    m.update({k: v for k, v in (facts or {}).items() if k not in m})
    pt = str(m.get("purchase_type") or "")
    old = str(m.get("old_car_model") or "")
    trade = str(m.get("trade_in_requirement") or "")
    out = []
    if "旧车" in sec_text and not old:
        out.append("卡片正文提到旧车，但 old_car_model 为空")
    if ("置换" in sec_text or "换购" in sec_text) and "replacement" not in pt and "换购" not in pt:
        out.append(f"正文提到置换/换购，但 purchase_type = {pt or '（空）'}")
    if "replacement" in pt and not old:
        out.append("purchase_type=replacement 但 old_car_model 为空（换购无证）")
    if pt.count("||") or (isinstance(m.get("purchase_type"), list) and len(m["purchase_type"]) > 1):
        out.append(f"purchase_type 多值：{pt}")
    return out


def occupation_from_evidence(notes):
    """基线侧口径：occupation 的 evidence 串里出现「第三方画像 / 预测」。"""
    for n in notes:
        if str(n.get("attribute_key")) != "occupation":
            continue
        ev = " ".join(str(x) for x in ((n.get("reasoning_detail") or {}).get("evidence") or []))
        if "第三方" in ev or "预测" in ev or "极光" in ev:
            return True
    return False


def occupation_source(claims):
    for c in claims or []:
        if str(c.get("attribute")) != "occupation":
            continue
        ch = str(c.get("record_channel") or "")
        if "第三方" in ch or "极光" in ch:
            return True
    return False


def level_conflict(secs):
    dl = ((secs.get("profile_summary") or {}) or {}).get("deal_level")
    bo = (((secs.get("customer_overview") or {}) or {}).get("business_opp_level") or {})
    if isinstance(bo, dict):
        bo = bo.get("value")
    dl = str(dl if not isinstance(dl, dict) else dl.get("value") or "")
    bo = str(bo or "")
    if dl and bo and dl.strip() and bo.strip() and dl.strip() != bo.strip():
        return f"deal_level={dl} vs business_opp_level={bo}"
    return None


def age_format(secs, notes):
    pa = ((secs.get("profile_summary") or {}) or {}).get("age")
    na = None
    for n in notes:
        if str(n.get("attribute_key")) == "age":
            na = n.get("attribute_value")
    if isinstance(pa, dict):
        pa = pa.get("value") or ""
    if isinstance(na, dict):
        na = na.get("input_value") or na.get("key") or ""
    pa, na = str(pa or ""), str(na or "")
    if pa and na and pa.replace(" ", "") != na.replace(" ", ""):
        return f"profile_summary.age=「{pa}」 vs Notes.age=「{na}」"
    return None


def evidence_self_ref(secs):
    """卡片证据锚点中引用「画像/人设/标签」等中间产物。"""
    hits = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "evidence" and isinstance(v, list):
                    for s in v:
                        if any(w in str(s) for w in ("画像", "人设", "个人特征", "第三方画像")):
                            hits.append({"path": path, "anchor": str(s)[:90]})
                else:
                    walk(v, path + "." + str(k) if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    for k in SEC:
        walk(secs.get(k), k)
    return hits


def summary_steps_desync(secs):
    """summary 与 steps+evidence 语义零重叠（按字符 bigram 近似）。"""
    bad = 0
    total = 0

    def ng(s):
        s = re.sub(r"\s+", "", str(s))
        return {s[i:i + 2] for i in range(len(s) - 1)} or {s}

    def walk(node):
        nonlocal bad, total
        if isinstance(node, dict):
            if "summary" in node and ("steps" in node or "evidence" in node):
                total += 1
                a = ng(node.get("summary"))
                b = ng("".join(map(str, (node.get("steps") or []) + (node.get("evidence") or []))))
                if a and b and not (a & b):
                    bad += 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for k in SEC:
        walk(secs.get(k))
    return bad, total


def dirty_names(notes, phone):
    out = []
    for n in notes:
        k = str(n.get("attribute_key"))
        v = n.get("attribute_value")
        if k == "customer_name":
            s = str(v or "")
            if len(s) <= 1 or re.search(r"客户\d{3,}", s):
                out.append(f"customer_name=「{s}」")
        if k == "wechat_id":
            s = str(v or "")
            if s == phone:
                out.append(f"wechat_id 等于手机号（{s}）")
            elif s in ("未知", ""):
                out.append(f"wechat_id=「{s}」")
    return out


def main():
    rows = []
    for phone in PHONES:
        pa, pb = payload_of(A, phone), payload_of(B, phone)
        na, nb = notes_of(pa), notes_of(pb)
        sa, sb = sections_of(pa), sections_of(pb)
        dl_va = index(A, phone)
        dl_vb = index(B, phone)
        fa = json.loads(o1(dl_va, "pipeline_l2_project").get("facts_json") or "{}")
        ca = (json.loads(o1(dl_va, "pipeline_l1_localize").get("claims_json") or "{}")).get("fields") or []
        ta = json.dumps({k: sa.get(k) for k in SEC}, ensure_ascii=False)
        tb = json.dumps({k: sb.get(k) for k in SEC}, ensure_ascii=False)

        rec = {"phone": phone, "v2": {}, "base": {}}
        rec["v2"]["enum_labels"] = enum_label("v2", phone, na, fa)
        rec["v2"]["synonym_split"], rec["v2"]["unknown_keys"] = synonym_split(na)
        rec["v2"]["purchase"] = purchase_coherence(na, fa, ta)
        rec["v2"]["occupation_third_party"] = occupation_source(ca)
        rec["v2"]["level_conflict"] = level_conflict(sa)
        rec["v2"]["age_format"] = age_format(sa, na)
        rec["v2"]["evidence_self_ref"] = evidence_self_ref(sa)
        rec["v2"]["desync"] = summary_steps_desync(sa)
        rec["v2"]["dirty"] = dirty_names(na, phone)

        rec["base"]["enum_labels"] = enum_label("base", phone, nb, None)
        rec["base"]["synonym_split"], rec["base"]["unknown_keys"] = synonym_split(nb)
        rec["base"]["purchase"] = purchase_coherence(nb, None, tb)
        rec["base"]["occupation_third_party"] = occupation_from_evidence(nb)
        rec["base"]["level_conflict"] = level_conflict(sb)
        rec["base"]["age_format"] = age_format(sb, nb)
        rec["base"]["evidence_self_ref"] = evidence_self_ref(sb)
        rec["base"]["desync"] = summary_steps_desync(sb)
        rec["base"]["dirty"] = dirty_names(nb, phone)
        rows.append(rec)

    (TMP / "old_problems.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    def tot(arm, key, sub=None):
        n = 0
        for r in rows:
            v = r[arm].get(key)
            if sub:
                n += 1 if v else 0
            elif isinstance(v, list):
                n += len(v)
            elif isinstance(v, tuple):
                n += v[0]
            elif isinstance(v, bool):
                n += 1 if v else 0
            else:
                n += 1 if v else 0
        return n

    print("%-34s%-16s%s" % ("老问题类别", "基线 8 案", "v2 8 案"))
    print("-" * 66)
    print("%-34s%-16s%s" % ("①枚举用了中文标签/非契约值", tot("base", "enum_labels"), tot("v2", "enum_labels")))
    print("%-34s%-16s%s" % ("②同义 key 分裂（组数）", tot("base", "synonym_split"), tot("v2", "synonym_split")))
    print("%-34s%-16s%s" % ("③模板外 key", tot("base", "unknown_keys"), tot("v2", "unknown_keys")))
    print("%-34s%-16s%s" % ("④购买类型与旧车矛盾", tot("base", "purchase"), tot("v2", "purchase")))
    print("%-34s%-16s%s" % ("⑤职业仅靠第三方预测", tot("base", "occupation_third_party"), tot("v2", "occupation_third_party")))
    print("%-34s%-16s%s" % ("⑥等级字段打架", tot("base", "level_conflict"), tot("v2", "level_conflict")))
    print("%-34s%-16s%s" % ("⑦age 跨节格式不一致", tot("base", "age_format"), tot("v2", "age_format")))
    print("%-34s%-16s%s" % ("⑧证据锚点引用画像/人设", tot("base", "evidence_self_ref"), tot("v2", "evidence_self_ref")))
    dbad = sum(r["base"]["desync"][0] for r in rows)
    dtot = sum(r["base"]["desync"][1] for r in rows)
    vbad = sum(r["v2"]["desync"][0] for r in rows)
    vtot = sum(r["v2"]["desync"][1] for r in rows)
    print("%-34s%-16s%s" % ("⑨summary 与 steps 脱节", "%d/%d" % (dbad, dtot), "%d/%d" % (vbad, vtot)))
    print("%-34s%-16s%s" % ("⑩姓名/微信号脏值", tot("base", "dirty"), tot("v2", "dirty")))
    print()
    print("=== ⑨ 明细 ===")
    for r in rows:
        print("  %-13s 基线 %s  v2 %s" % (r["phone"], r["base"]["desync"], r["v2"]["desync"]))
    print()
    print("=== ④ 明细 ===")
    for r in rows:
        if r["base"]["purchase"] or r["v2"]["purchase"]:
            print("  %-13s 基线 %s" % (r["phone"], r["base"]["purchase"] or "—"))
            print("  %-13s v2   %s" % ("", r["v2"]["purchase"] or "—"))
    print()
    print("=== ⑦ 明细 ===")
    for r in rows:
        if r["base"]["age_format"] or r["v2"]["age_format"]:
            print("  %-13s 基线 %s | v2 %s" % (r["phone"], r["base"]["age_format"] or "一致", r["v2"]["age_format"] or "一致"))
    print()
    print("=== ① 明细 ===")
    for r in rows:
        print("  %-13s 基线 %2d 处  v2 %2d 处" % (r["phone"], len(r["base"]["enum_labels"]), len(r["v2"]["enum_labels"])))
        for h in r["v2"]["enum_labels"][:4]:
            print("        v2: %s=%s (%s)" % (h["field"], h["value"], h["why"]))
    print()
    print("=== ②③⑧⑩ 明细 ===")
    for r in rows:
        print("  %-13s ②基线%s v2%s  ③基线%s v2%s  ⑧基线%d v2%d  ⑩基线%s v2%s" % (
            r["phone"],
            r["base"]["synonym_split"] or "无", r["v2"]["synonym_split"] or "无",
            r["base"]["unknown_keys"] or "无", r["v2"]["unknown_keys"] or "无",
            len(r["base"]["evidence_self_ref"]), len(r["v2"]["evidence_self_ref"]),
            r["base"]["dirty"] or "无", r["v2"]["dirty"] or "无"))


if __name__ == "__main__":
    main()
