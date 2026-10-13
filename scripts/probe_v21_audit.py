"""v2.1 整改的只读审计探针（0 token，不出网）。

用途：把 v2 首轮的三个问题量化成可复现的读数，并在整改后用同一套口径复测。
**只读批次产物，不修改任何东西，不调用模型。**

    python -I scripts/probe_v21_audit.py
    python -I scripts/probe_v21_audit.py --batch experiments/abexperiments/batches/A-pipeline-L8-20261012

三类读数：

1. Notes / section 覆盖：v2 与基线同号逐案对照（条数、字段键集合差）
2. L1→L2 漏斗：空值占位断言、L2 拒绝规则分布、分诊档
3. 可读性：`reasoning_detail` 里的审计词命中、「未知渠道」占位、证据来源形态
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

V2_DEFAULT = REPO / "experiments/abexperiments/batches/A-pipeline-L8-20261012/runs"
BASE_DEFAULT = REPO / "experiments/batches/baseline-39-dsv41-noThink/runs"

SEC = (
    "profile_summary", "inferred_tags", "usage_scenarios", "communication_strategies",
    "customer_overview", "purchase_motivations", "product_preferences", "resistances",
    "emotion_state",
)

# 面向销售的产出里不该出现的内部审计词（v2.1 硬约束，见 docs/business_opt01_v2/v2.1-整改设计.md §1）
AUDIT_WORDS = (
    "归属过滤", "来源分级", "枚举归一", "单多值约束", "契约校验",
    "未知渠道", "社交自呈现", "第三方预测", "一手交互", "公开登记查询", "人工/系统登记",
    "按来源分级投影得到", "断言",
)


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def analysis_of(raw: dict) -> dict:
    payload = raw.get("result1")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    return (payload or {}).get("analysis_result") or {}


def section_counts(analysis: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for key in SEC:
        value = (analysis or {}).get(key)
        if isinstance(value, list):
            out[key] = len(value)
        elif isinstance(value, dict):
            out[key] = sum(
                1 for item in value.values()
                if (isinstance(item, dict) and str(item.get("value") or "").strip())
                or (isinstance(item, str) and item.strip())
            )
        else:
            out[key] = 0
    return out


def note_keys(analysis: dict) -> list[str]:
    return [n.get("attribute_key") for n in (analysis.get("basic_notes_updates") or [])]


def json_load(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def nodes_index(path: Path) -> dict:
    nodes = load(path)
    if isinstance(nodes, dict) and "nodes" in nodes:
        nodes = nodes["nodes"]
    elif isinstance(nodes, dict):
        nodes = list(nodes.values())
    index: dict[str, dict] = {}
    for item in nodes or []:
        if not isinstance(item, dict):
            continue
        index.setdefault(item.get("node_id") or item.get("id"), item.get("outputs") or item.get("output") or {})
    return index


def compare_notes(v2_dir: Path, base_dir: Path) -> None:
    print("=== 1. Notes / section 覆盖（v2 vs 基线同号） ===")
    phones = sorted(p.name for p in v2_dir.iterdir() if p.is_dir())
    lost: Counter = Counter()
    gained: Counter = Counter()
    v2_sec: Counter = Counter()
    base_sec: Counter = Counter()
    v2_notes = base_notes = 0
    for phone in phones:
        v2 = analysis_of(load(v2_dir / phone / "result.json"))
        v2_s = section_counts(v2)
        v2_k = note_keys(v2)
        v2_notes += len(v2_k)
        for key in SEC:
            v2_sec[key] += v2_s[key]
        line = "%-13s V2 usage=%d notes=%2d" % (phone, v2_s["usage_scenarios"], len(v2_k))
        base_path = base_dir / phone / "result.json"
        if base_path.exists():
            base = analysis_of(load(base_path))
            base_s = section_counts(base)
            base_k = note_keys(base)
            base_notes += len(base_k)
            for key in SEC:
                base_sec[key] += base_s[key]
            line += " | BS usage=%d notes=%2d" % (base_s["usage_scenarios"], len(base_k))
            lost.update(set(base_k) - set(v2_k))
            gained.update(set(v2_k) - set(base_k))
        else:
            line += " | (无基线对照)"
        print(line)
    print("section 顺序 :", SEC)
    print("V2  section :", [v2_sec[k] for k in SEC])
    print("BS  section :", [base_sec[k] for k in SEC])
    print("Notes 合计  : V2=%d  基线=%d  比值=%.2f" % (v2_notes, base_notes, v2_notes / max(base_notes, 1)))
    print("基线有、v2 缺的字段键:", lost.most_common(25))
    print("v2 有、基线缺的字段键:", gained.most_common(15))
    print()


def funnel(v2_dir: Path) -> None:
    print("=== 2. L1→L2 漏斗与分诊档 ===")
    tiers: Counter = Counter()
    rejected: Counter = Counter()
    downgraded: Counter = Counter()
    empty_attrs: Counter = Counter()
    claims_total = empty_total = 0
    anchor_missing: Counter = Counter()
    for phone in sorted(p.name for p in v2_dir.iterdir() if p.is_dir()):
        index = nodes_index(v2_dir / phone / "nodes.json")

        def out(node_id: str) -> dict:
            return index.get(node_id) or {}

        tiers[str(out("pipeline_l4_triage").get("tier"))] += 1

        claims = json_load(out("pipeline_l1_localize").get("claims_json")) or {}
        claims = (claims.get("fields") if isinstance(claims, dict) else claims) or []
        claims_total += len(claims)
        for claim in claims:
            if str(claim.get("value") or "").strip() in ("", "[]"):
                empty_attrs[claim.get("attribute")] += 1
                empty_total += 1

        for item in json_load(out("pipeline_l2_project").get("rejected_json")) or []:
            rejected[item.get("rule")] += 1
        for item in json_load(out("pipeline_l1_localize").get("downgraded_detail")) or []:
            downgraded[item.get("reason")] += 1
            if item.get("reason") == "anchor_not_found":
                anchor_missing[item.get("attribute")] += 1

    print("分诊档:", dict(tiers))
    print("L1 断言 %d 条，其中空值占位 %d 条" % (claims_total, empty_total))
    print("空值占位字段 Top:", empty_attrs.most_common(15))
    print("L2 拒收规则:", rejected.most_common())
    print("L1 降级原因:", downgraded.most_common())
    print("anchor_not_found 字段:", anchor_missing.most_common(20))
    print()


def readability(v2_dir: Path) -> None:
    print("=== 3. 可读性（审计词 / 未知渠道 / 证据形态） ===")
    hits: Counter = Counter()
    unknown = 0
    notes_total = 0
    evidence_total = 0
    evidence_with_quote = 0
    for phone in sorted(p.name for p in v2_dir.iterdir() if p.is_dir()):
        analysis = analysis_of(load(v2_dir / phone / "result.json"))
        for note in analysis.get("basic_notes_updates") or []:
            notes_total += 1
            detail = note.get("reasoning_detail") or {}
            blob = json.dumps(
                {"summary": detail.get("summary"), "steps": detail.get("steps")},
                ensure_ascii=False,
            )
            for word in AUDIT_WORDS:
                if word in blob:
                    hits[word] += 1
            for snippet in detail.get("evidence") or []:
                evidence_total += 1
                text = str(snippet)
                if "未知渠道" in text:
                    unknown += 1
                if "：" in text or ":" in text:
                    evidence_with_quote += 1
    print("Notes 条数:", notes_total)
    print("审计词命中（人次）:", hits.most_common())
    print("证据串总数 %d，其中含「未知渠道」 %d，形如「渠道：原文」 %d" % (
        evidence_total, unknown, evidence_with_quote))
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v2.1 整改只读审计探针")
    parser.add_argument("--batch", default=str(V2_DEFAULT))
    parser.add_argument("--baseline", default=str(BASE_DEFAULT))
    args = parser.parse_args(argv)

    v2_dir = Path(args.batch)
    if not v2_dir.is_dir() and (v2_dir / "runs").is_dir():
        v2_dir = v2_dir / "runs"
    if not v2_dir.is_dir():
        print("[FAIL] 找不到 v2 批次：%s" % v2_dir)
        return 2

    if os.environ.get("PYTHONIOENCODING") is None:
        sys.stdout.reconfigure(encoding="utf-8")

    compare_notes(v2_dir, Path(args.baseline))
    funnel(v2_dir)
    readability(v2_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
