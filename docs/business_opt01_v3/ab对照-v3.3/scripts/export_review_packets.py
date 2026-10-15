# -*- coding: utf-8 -*-
"""审核用数据包导出：把每一案的「输入 → 链路 → 两臂产出」打成一份独立 JSON。

**用途**：供人工/多视角审核逐案阅读。它回答的是「这一案的输入是什么、
链路怎么走过来的、最终说了什么」——而不是「页面怎么渲染」。

**为什么以 ``pipeline_l1_input.output`` 为输入口径**：那正是 v3.3 管线实际消费的
证据包正文（已含逐字转写、按来源档排序、带记录编号），对照它就是对照模型真正看到的
东西。基线臂看到的等价物是「客户数据聚合」模板渲染的 15 渠道全量包，两者底层数据同源。

导出结构（每案一个文件）::

    {
      phone,
      input:  { l1_input, l1_sections, tier, tier_reason },
      chain:  { facts_1a, persona_1b, fields_1c, merge, localize,
                l2_facts, l2_fallback, l2_issues, l3_suspects },
      new:    { sections:{10}, notes:[...] },
      base:   { sections:{10}, notes:[...] },
      meta:   { new_llm, new_tok, base_llm, base_tok, new_dur, base_dur }
    }

只读两侧批次目录，产出写到 ``review/packets/``。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
NEW_BATCH = REPO_ROOT / "experiments/batches/pipeline-v3.3-39"
BASE_BATCH = REPO_ROOT / "experiments/batches/baseline-39-dsv41-noThink"
OUT = HERE.parent / "review" / "packets"

SECTIONS = (
    "profile_summary", "inferred_tags", "usage_scenarios", "communication_strategies",
    "customer_overview", "purchase_motivations", "product_preferences", "resistances",
    "emotion_state", "basic_notes_updates",
)

# 链路里值得逐案看的节点，键是导出名，值是 node_id
CHAIN_NODES = {
    "triage": "pipeline_l4_triage",
    "records": "pipeline_l0_records",
    "facts_1a": "pipeline_l1_facts",
    "persona_1b": "pipeline_l1_persona",
    "fields_1c": "pipeline_l1_fields",
    "merge": "pipeline_l1_merge",
    "localize": "pipeline_l1_localize",
    "inject": "pipeline_l1_inject",
    "l2_project": "pipeline_l2_project",
    "l2_fallback": "pipeline_l2_fallback",
    "l2_apply": "pipeline_l2_apply",
    "l2_review": "pipeline_l2_review",
    "l3": "pipeline_production",
}

# 只导出这些节点输出的字段（避免把整棵树塞进包）
CHAIN_FIELDS = {
    "triage": ("tier", "tier_reason", "usable_count"),
    "records": ("records_json", "records_count", "exception_count"),
    "facts_1a": ("result",),
    "persona_1b": ("result",),
    "fields_1c": ("result",),
    "merge": ("merged_count", "facts_count", "fields_count",
              "from_facts_only", "overrode", "subject_overrides"),
    "localize": ("kept", "dropped", "downgraded", "downgraded_detail", "parse_ok"),
    "inject": ("added", "replaced", "injected", "license_injected"),
    "l2_project": ("facts_json", "notes_updates_json", "to_confirm_json",
                   "rejected_json", "candidates_json", "basis_json",
                   "notes_count", "empty_claims", "empty_claim_names", "tier"),
    "l2_fallback": ("result",),
    "l2_apply": ("notes_count", "applied", "out_of_bounds", "verdict_json"),
    "l2_review": ("notes_count", "issue_json", "issue_count", "placeholder_removed",
                  "enum_illegal", "unbacked_fact"),
    "l3": ("section_count", "sections_missing", "sections_hoisted",
           "new_fact_suspects", "suspects_json"),
}


def _load_result(batch: Path, phone: str) -> dict | None:
    p = batch / "runs" / phone / "result.json"
    if not p.is_file():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    r1 = raw.get("result1")
    analysis: dict = {}
    if isinstance(r1, str) and r1:
        try:
            analysis = (json.loads(r1) or {}).get("analysis_result") or {}
        except ValueError:
            analysis = {}
    elif isinstance(r1, dict):
        analysis = r1.get("analysis_result") or {}
    return {
        "status": raw.get("status"),
        "duration_ms": raw.get("duration_ms"),
        "sections": {k: analysis.get(k) for k in SECTIONS},
    }


L1_INPUT_MARKER = "## 目标字段清单"
"""L1 输入包的起始标记（``l1_guide()`` 的第一行）。

用它从 LLM 提示词里切回证据包正文：``build_l1_input`` 的输出是
``f"{guide}\\n\\n---\\n\\n{body}"``，而该输出被整段嵌进提示词的 user 消息。
因此「从首个标记起到消息末尾」就是证据包本身。
"""


def _recover_l1_input(phone: str) -> str:
    """从 ``attempts.json`` 里取回被 ``nodes.json`` 截断的 L1 证据包全文。

    **为什么需要这一步**：``nodes.json`` 对超大节点产出做了截断，只留
    ``{_truncated, bytes, sha256, preview}``。39 案里有 3 案（``13926073386`` /
    ``15818891947`` / ``18675756738``）命中——而这三案恰恰是**输入最长**的案子，
    审核「有没有漏信息」最需要的就是它们的原文。把它们跳过，等于对最该看的三案失明。

    ``attempts.json`` 不截断 ``request_json``：一次 LLM 尝试的提示词原文（含证据包）
    完整留存。故这里从 ``pipeline_l1_facts`` 那次尝试的 user 消息里切回证据包。

    **校验**：切出的正文必须以标记开头。对不上返回空串，让上游如实显示「无」，
    而不是把一段来路不明的文本塞进证据位。
    """
    path = NEW_BATCH / "runs" / phone / "attempts.json"
    if not path.is_file():
        return ""
    try:
        attempts = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return ""
    for attempt in attempts:
        if attempt.get("node_id") != "pipeline_l1_facts" or attempt.get("kind") != "llm":
            continue
        request = attempt.get("request_json")
        if isinstance(request, str):
            try:
                request = json.loads(request)
            except ValueError:
                continue
        if not isinstance(request, dict):
            continue
        for message in request.get("messages") or []:
            content = message.get("content")
            if not isinstance(content, str):
                continue
            index = content.find(L1_INPUT_MARKER)
            if index >= 0:
                return content[index:]
    return ""


def _load_chain(phone: str) -> dict:
    p = NEW_BATCH / "runs" / phone / "nodes.json"
    if not p.is_file():
        return {}
    nodes = json.loads(p.read_text(encoding="utf-8"))
    by_id = {}
    for n in nodes:
        nid = n.get("node_id")
        if nid in CHAIN_NODES.values() and n.get("depth") == 0:
            by_id[nid] = n
    out: dict = {}
    for alias, nid in CHAIN_NODES.items():
        node = by_id.get(nid)
        if node is None:
            continue
        outs = node.get("outputs") or {}
        keep = CHAIN_FIELDS.get(alias, ())
        out[alias] = {k: outs.get(k) for k in keep if k in outs}
        if alias == "triage":
            out[alias]["l1_input_chars"] = (node.get("inputs") or {})
    # L1 输入包正文（模型实际看到的证据）
    for n in nodes:
        if n.get("node_id") == "pipeline_l1_input" and n.get("depth") == 0:
            out["l1_input_output"] = (n.get("outputs") or {}).get("output")
            break
    return out


def _llm_metrics(phone: str) -> dict:
    import sqlite3
    db = NEW_BATCH / "trace.db"
    if not db.is_file():
        return {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT run_id FROM workflow_runs WHERE parent_run_id IS NULL AND business_ref=?",
            (phone,),
        ).fetchone()
        if not row:
            return {}
        seen, frontier = {row[0]}, {row[0]}
        while frontier:
            rows = conn.execute(
                "SELECT run_id FROM workflow_runs WHERE parent_run_id IN (%s)"
                % ",".join("?" * len(frontier)), tuple(frontier)).fetchall()
            nxt = {r[0] for r in rows} - seen
            if not nxt:
                break
            seen |= nxt
            frontier = nxt
        ids = sorted(seen)
        marks = ",".join("?" * len(ids))
        llm = conn.execute(
            f"SELECT COUNT(*) FROM request_attempts WHERE kind='llm' AND run_id IN ({marks})",
            ids).fetchone()[0]
        tok = conn.execute(
            f"SELECT SUM(json_extract(usage_json,'$.total_tokens')) FROM request_attempts "
            f"WHERE run_id IN ({marks})", ids).fetchone()[0]
        return {"llm": llm, "tok": tok or 0}
    finally:
        conn.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    phones = sorted(
        p.parent.name for p in (NEW_BATCH / "runs").glob("*/result.json")
    )
    written = 0
    index = []
    for phone in phones:
        new = _load_result(NEW_BATCH, phone)
        base = _load_result(BASE_BATCH, phone)
        chain = _load_chain(phone)
        metrics = _llm_metrics(phone)

        l1_input = chain.pop("l1_input_output", "")
        truncated = not isinstance(l1_input, str)
        if truncated:
            # nodes.json 截断了超长产出，改从 attempts.json 取回全文（见 _recover_l1_input）。
            l1_input = _recover_l1_input(phone)

        packet = {
            "phone": phone,
            "input": {
                "l1_input": l1_input,
                "l1_input_recovered": truncated,
            },
            "chain": chain,
            "new": new,
            "base": base,
            "meta": {
                "new_status": (new or {}).get("status"),
                "base_status": (base or {}).get("status"),
                "new_duration_ms": (new or {}).get("duration_ms"),
                "base_duration_ms": (base or {}).get("duration_ms"),
                "new_llm": metrics.get("llm"),
                "new_tok": metrics.get("tok"),
            },
        }
        path = OUT / f"{phone}.json"
        path.write_text(json.dumps(packet, ensure_ascii=False, indent=1), encoding="utf-8")
        size = path.stat().st_size
        written += 1
        index.append({"phone": phone, "bytes": size})
        print("%s  %8d 字节" % (phone, size))
    total = sum(i["bytes"] for i in index)
    print("\n共 %d 案，合计 %s 字节 ≈ %.2f MB" % (
        written, format(total, ","), total / 1048576))
    (OUT / "_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
