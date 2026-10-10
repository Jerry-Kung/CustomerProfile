"""T4 验收探针：把 39 案既有字段按 L2 规则重算（0 token）。

`落地方案.md` 阶段二的验收是「基线回放，不花 token」，预期确定性修复 **97 处**。
本探针把既有 ``basic_notes_updates`` 重建成断言账本 → 跑 :func:`projection.project`
→ 逐类统计命中，并**如实报告与计划书预估的差异**。

一处必须说明的口径差异
----------------------
计划书的 97 处（含「枚举归一 38」「清除仅第三方身份断言 42」）来自当初的一次性诊断脚本，
本仓库没有那份脚本。探针按**当前代码里的谓词**复算，数字与预估**不完全相同**，这是
预期的：谓词只要写死在这里，结果就可复现、可回归，比对齐一个来路不明的数字更重要。
探针会**同时打印两边**，差异一目了然。

用法::

    python scripts/probe_pipeline_l2.py
    python scripts/probe_pipeline_l2.py --phone 13926073386   # 逐案细看
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile import notes_contract as nc  # noqa: E402
from customer_profile import projection as proj  # noqa: E402

DEFAULT_BATCH = REPO_ROOT / "experiments" / "batches" / "baseline-39-dsv41-noThink"

DEREGISTERED_MARKERS = ("注销", "吊销", "已注销")


def load_updates(case_dir: Path) -> list[dict[str, Any]] | None:
    """读一案产出的 ``basic_notes_updates``。"""
    path = case_dir / "result.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    payload = raw.get("result1")
    data = json.loads(payload) if isinstance(payload, str) else payload
    analysis = (data or {}).get("analysis_result") or {}
    return analysis.get("basic_notes_updates") or []


def build_context(updates: list[dict[str, Any]]) -> dict[str, Any]:
    """从证据文本里读投影所需的旁证（原文有「注销」等）。"""
    blob = json.dumps(updates, ensure_ascii=False)
    return {
        "company_deregistered": any(marker in blob for marker in DEREGISTERED_MARKERS),
        "has_old_car_evidence": ("旧车" in blob) or ("置换" in blob),
    }


def claims_of(updates: list[dict[str, Any]]) -> list[proj.Claim]:
    claims: list[proj.Claim] = []
    for entry in updates:
        claims.extend(proj.claims_from_notes_entry(entry))
    return claims


def classify(entry: dict[str, Any]) -> tuple[str, Any]:
    """判定一条既有字段的「现状」：是否枚举脏值 / 仅第三方 / 单选多值。"""
    key = str(entry.get("attribute_key") or "")
    value = entry.get("attribute_value")
    evidence = [str(x) for x in ((entry.get("reasoning_detail") or {}).get("evidence") or [])]
    tiers = set()
    for snippet in evidence:
        channel = proj._match_channel(snippet.split("：")[0].split(":")[0])
        tiers.add(proj.CHANNEL_TIER.get(channel, proj.SOCIAL))
    flags: list[str] = []
    if key in nc.IDENTITY_KEYS and evidence and tiers == {proj.PREDICTION}:
        flags.append("b_prediction_only")
    if nc.is_enum(key):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if text.strip().startswith("{"):
            flags.append("d_structured")
        else:
            import re

            if re.search(r"[一-鿿]", text):
                flags.append("d_chinese_key")
        if "||" in text and nc.is_single_value(key):
            flags.append("e_multi_on_single")
    return key, flags


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T4 验收探针：基线回放 L2 规则")
    parser.add_argument("--batch-dir", default=str(DEFAULT_BATCH))
    parser.add_argument("--phone", default=None)
    args = parser.parse_args(argv)

    runs = Path(args.batch_dir) / "runs"
    if not runs.is_dir():
        print(f"[FAIL] 找不到批次产物目录：{runs}")
        return 2

    cases = [args.phone] if args.phone else sorted(p.name for p in runs.iterdir() if p.is_dir())

    baseline_flags: Counter = Counter()
    projected_flags: Counter = Counter()
    level_conflicts: list[tuple[str, Any, Any]] = []
    age_mismatch = 0
    per_case: dict[str, dict[str, int]] = {}
    rejects: Counter = Counter()
    confirms: Counter = Counter()

    for phone in cases:
        updates = load_updates(runs / phone)
        if updates is None:
            continue
        counts: dict[str, int] = defaultdict(int)

        # —— 现状统计（基线）
        for entry in updates:
            _, flags = classify(entry)
            for flag in flags:
                baseline_flags[flag] += 1
                counts[flag] += 1

        # —— 等级两处是否一致
        raw = json.loads((runs / phone / "result.json").read_text(encoding="utf-8"))
        data = json.loads(raw["result1"]) if isinstance(raw.get("result1"), str) else raw.get("result1")
        analysis = (data or {}).get("analysis_result") or {}
        deal = (analysis.get("profile_summary") or {}).get("deal_level")
        opp = ((analysis.get("customer_overview") or {}).get("business_opp_level") or {}).get("value")
        if deal and opp and proj.normalize_level(deal) != proj.normalize_level(opp):
            level_conflicts.append((phone, deal, opp))
            counts["f_level"] += 1

        # —— 年龄两处是否一致
        notes_age = next(
            (e.get("attribute_value") for e in updates if e.get("attribute_key") == "age"), None
        )
        card_age = (analysis.get("profile_summary") or {}).get("age")
        if notes_age and card_age and proj.normalize_age(notes_age) != proj.normalize_age(card_age):
            age_mismatch += 1
            counts["f_age"] += 1

        # —— 投影
        result = proj.project(claims_of(updates), context=build_context(updates))
        for item in result.rejected:
            rejects[item["rule"]] += 1
            projected_flags[item["rule"]] += 1
        for item in result.to_confirm:
            confirms["all"] += 1

        per_case[phone] = dict(counts)
        if args.phone:
            print(f"=== {phone} ===")
            print("  基线 flags:", dict(counts))
            print("  facts:", json.dumps(result.facts, ensure_ascii=False)[:400])
            print("  to_confirm:", json.dumps(result.to_confirm, ensure_ascii=False)[:300])
            print("  rejected:", json.dumps(result.rejected, ensure_ascii=False)[:300])

    print()
    print(f"案数: {len(per_case)}")
    print("--- 基线现状（计划书口径对齐） ---")
    print(f"  中文当键（预估 38）      : {baseline_flags['d_chinese_key']}")
    print(f"  结构化对象字符串         : {baseline_flags['d_structured']}")
    print(f"  仅第三方撑腰的身份字段（预估 42）: {baseline_flags['b_prediction_only']}")
    print(f"  单选字段出现多值（预估 2）: {baseline_flags['e_multi_on_single']}")
    print(f"  等级两处不一致（预估 4）  : {len(level_conflicts)}  {[c[0] for c in level_conflicts]}")
    print(f"  年龄两处不一致（预估 8）  : {age_mismatch}")
    print()
    print("--- L2 实际动作 ---")
    for rule, count in sorted(rejects.items()):
        print(f"  {rule:<18} {count}")
    print(f"  to_confirm 合计          {confirms['all']}")
    print()
    print("说明：基线的「枚举脏值」含中文当键与结构化对象两类，L2 的 d_enum 拒绝计数")
    print("      只统计**归一失败**的（中文与结构化通常都能归一到合法键，故不计入拒绝）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
