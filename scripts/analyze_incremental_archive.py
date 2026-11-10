#!/usr/bin/env python
"""归档侧统计：底座补缺会补多少，以及 token/时延按节点怎么分布。

两件事都只需读归档，不调用模型：

1. ``supplement``（T0-5）：把「上一版回传体」与「本轮接口画像」按手机号配对，
   统计逐字段补缺率、证据可用率、``ai_insight`` 缺失率。这是 P2 补缺逻辑的预期值，
   实现之后拿它核对——数字对不上说明配对口径或取值规则有偏差。
2. ``cost``（T0-3 第二项）：按节点汇总 LLM token 与时延占比，作为成本指标的分母。

配对口径：``GET /data/profile`` 是某次运行**开始前**读到的画像；上一版回传体是该号码
**上一次运行**发出的 ``analysis_result``。两者才构成「底座 A + 副本 B」的真实关系。
同一运行的 profile 与 callback 不能配对——那是「读之前」与「写之后」，字段集合天然不同。

用法::

    python scripts/analyze_incremental_archive.py supplement --source uat-traces-20261102-20261103
    python scripts/analyze_incremental_archive.py cost --source uat-traces-20261102-20261103
    python scripts/analyze_incremental_archive.py all --markdown experiments/incremental/archive-stats.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

BATCH_ROOT = REPO_ROOT / "experiments" / "uat_data"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _http(run_dir: Path, marker: str) -> dict | None:
    for row in _load(run_dir / "attempts.json"):
        if row.get("kind") == "llm":
            continue
        url = str((row.get("request_json") or {}).get("url") or row.get("target") or "")
        if marker in url and row.get("status_code") == 200:
            return row
    return None


def profile_of(run_dir: Path) -> dict | None:
    row = _http(run_dir, "/data/profile/")
    if row is None:
        return None
    try:
        payload = json.loads(row.get("response_text") or "")
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def callback_of(run_dir: Path) -> dict | None:
    row = _http(run_dir, "update-profile")
    if row is None:
        return None
    body = (row.get("request_json") or {}).get("request_body")
    return body if isinstance(body, dict) else None


def runs_by_phone(source: Path) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = defaultdict(list)
    for entry in sorted((source / "runs").iterdir()):
        if not entry.is_dir():
            continue
        result_path = entry / "result.json"
        if not result_path.is_file():
            continue
        result = _load(result_path)
        if str(result.get("status") or "") != "succeeded":
            continue
        out[entry.name.split("__")[0]].append(entry)
    for items in out.values():
        items.sort(key=lambda p: int(_load(p / "result.json").get("started_at_ms") or 0))
    return out


# ---------------------------------------------------------------- T0-5

def _b_fields(callback: dict) -> dict[str, dict]:
    """回传体里的 Notes 项：``{attribute_key: {value, steps, evidence, confidence}}``。"""
    out: dict[str, dict] = {}
    for item in (callback.get("analysis_result") or {}).get("basic_notes_updates") or []:
        key = str(item.get("attribute_key") or "")
        if not key:
            continue
        detail = item.get("reasoning_detail") or {}
        out[key] = {
            "value": "" if item.get("attribute_value") is None else str(item["attribute_value"]).strip(),
            "has_steps": bool(detail.get("steps")),
            "has_evidence": bool(detail.get("evidence")),
            "has_confidence": isinstance(detail.get("confidence"), (int, float)),
            "has_summary": bool(str(detail.get("summary") or "").strip()),
        }
    return out


def _a_fields(profile: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for key, item in (profile.get("basic_notes") or {}).items():
        if not isinstance(item, dict):
            continue
        out[str(key)] = {
            "value": "" if item.get("value") is None else str(item["value"]).strip(),
            "locked": bool(item.get("is_locked")),
            "has_summary": bool(str(item.get("reasoning_summary") or "").strip()),
        }
    return out


def supplement_stats(source: Path) -> dict[str, Any]:
    runs = runs_by_phone(source)
    pairs = 0
    no_ai_insight = 0
    profile_reads = 0
    empty_notes = 0
    missing_counts: list[int] = []
    field_total = 0
    field_missing = 0
    per_field_missing: Counter[str] = Counter()
    per_field_total: Counter[str] = Counter()
    same_value = 0
    diff_value = 0
    b_only_evidence = 0  # A 有值且与 B 相同 → 只有 B 带证据/置信
    a_only_summary = 0  # A 与 B 不同 → 只剩 A 的 summary

    for phone, items in runs.items():
        for index in range(1, len(items)):
            prev_cb = callback_of(items[index - 1])
            cur_profile = profile_of(items[index])
            if prev_cb is None or cur_profile is None:
                continue
            profile_reads += 1
            pairs += 1
            if not (cur_profile.get("ai_insight") or {}).get("purchase_intent_level"):
                no_ai_insight += 1
            a = _a_fields(cur_profile)
            if not a:
                empty_notes += 1
            b = _b_fields(prev_cb)
            b_keys = {key for key, item in b.items() if item["value"]}
            a_keys = {key for key, item in a.items() if item["value"]}
            missing = b_keys - a_keys
            missing_counts.append(len(missing))
            field_total += len(b_keys)
            field_missing += len(missing)
            for key in b_keys:
                per_field_total[key] += 1
            for key in missing:
                per_field_missing[key] += 1
            for key in b_keys & a_keys:
                if a[key]["value"].strip() == b[key]["value"].strip():
                    same_value += 1
                    if b[key]["has_evidence"] or b[key]["has_confidence"]:
                        b_only_evidence += 1
                else:
                    diff_value += 1
                    if a[key]["has_summary"] and not b[key]["has_evidence"]:
                        a_only_summary += 1

    per_field_rate = {
        key: round(per_field_missing[key] / per_field_total[key], 3)
        for key in per_field_total
        if per_field_total[key] >= 5
    }
    return {
        "pairs": pairs,
        "profile_reads": profile_reads,
        "no_ai_insight_rate": round(no_ai_insight / profile_reads, 3) if profile_reads else None,
        "empty_basic_notes_rate": round(empty_notes / profile_reads, 3) if profile_reads else None,
        "missing_per_pair_median": statistics.median(missing_counts) if missing_counts else None,
        "missing_per_pair_max": max(missing_counts) if missing_counts else None,
        "field_missing_rate": round(field_missing / field_total, 3) if field_total else None,
        "pairs_with_any_missing": sum(1 for n in missing_counts if n > 0),
        "same_value_pairs": same_value,
        "b_only_evidence": b_only_evidence,
        "diff_value_pairs": diff_value,
        "a_only_summary": a_only_summary,
        "per_field_missing_rate": dict(
            sorted(per_field_rate.items(), key=lambda item: -item[1])
        ),
    }


# ---------------------------------------------------------------- T0-3 成本

def cost_stats(source: Path) -> dict[str, Any]:
    by_node: dict[str, dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "duration_ms": 0}
    )
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "duration_ms": 0}
    per_case_tokens: list[int] = []
    per_case_duration: list[int] = []

    for entry in sorted((source / "runs").iterdir()):
        if not entry.is_dir():
            continue
        result_path = entry / "result.json"
        if not result_path.is_file():
            continue
        result = _load(result_path)
        if str(result.get("status") or "") != "succeeded":
            continue
        case_tokens = 0
        for row in _load(entry / "attempts.json"):
            if row.get("kind") != "llm":
                continue
            node = str(row.get("node_id") or "unknown")
            usage = row.get("usage_json") or {}
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            duration = int(row.get("duration_ms") or 0)
            bucket = by_node[node]
            bucket["calls"] += 1
            bucket["prompt_tokens"] += prompt
            bucket["completion_tokens"] += completion
            bucket["duration_ms"] += duration
            totals["calls"] += 1
            totals["prompt_tokens"] += prompt
            totals["completion_tokens"] += completion
            totals["duration_ms"] += duration
            case_tokens += prompt + completion
        if case_tokens:
            per_case_tokens.append(case_tokens)
            per_case_duration.append(int(result.get("duration_ms") or 0))

    total_tokens = totals["prompt_tokens"] + totals["completion_tokens"]
    rows = []
    for node, bucket in by_node.items():
        node_tokens = bucket["prompt_tokens"] + bucket["completion_tokens"]
        rows.append(
            {
                "node_id": node,
                "calls": bucket["calls"],
                "tokens": node_tokens,
                "token_share": round(node_tokens / total_tokens, 4) if total_tokens else 0,
                "duration_ms": bucket["duration_ms"],
                "duration_share": round(bucket["duration_ms"] / totals["duration_ms"], 4)
                if totals["duration_ms"]
                else 0,
            }
        )
    rows.sort(key=lambda item: -item["tokens"])
    return {
        "cases": len(per_case_tokens),
        "totals": totals,
        "total_tokens": total_tokens,
        "median_tokens_per_case": statistics.median(per_case_tokens) if per_case_tokens else None,
        "median_duration_ms": statistics.median(per_case_duration) if per_case_duration else None,
        "by_node": rows,
    }


# ---------------------------------------------------------------- 渲染

def render_supplement(stats: dict[str, Any]) -> str:
    lines = ["## 底座补缺分布（T0-5）", ""]
    lines.append(f"- 配对样本：{stats['pairs']} 对（上一版回传体 + 本轮接口画像）")
    lines.append(f"- 读回完全没有 `ai_insight.purchase_intent_level`：{_p(stats['no_ai_insight_rate'])}")
    lines.append(f"- `basic_notes` 整个为空：{_p(stats['empty_basic_notes_rate'])}")
    lines.append(f"- 每对缺字段数：中位 {stats['missing_per_pair_median']}，最多 {stats['missing_per_pair_max']}")
    lines.append(
        f"- 字段级补缺率：{_p(stats['field_missing_rate'])}"
        f"（{stats['pairs_with_any_missing']}/{stats['pairs']} 对至少缺一项）"
    )
    lines.append("")
    lines.append("| 情形 | 计数 | 含义 |")
    lines.append("|---|---|---|")
    lines.append(
        f"| A 有值且与 B 相同 | {stats['same_value_pairs']} | 沿用副本证据"
        f"（其中 {stats['b_only_evidence']} 项副本确实带证据或置信度） |"
    )
    lines.append(
        f"| A 有值且与 B 不同 | {stats['diff_value_pairs']} | 以 A 为准，"
        f"{stats['a_only_summary']} 项只剩 A 的 summary |"
    )
    lines.append("")
    if stats["per_field_missing_rate"]:
        lines.append("逐字段补缺率（样本 ≥5 的字段）：")
        lines.append("")
        lines.append("| 字段 | 补缺率 |")
        lines.append("|---|---|")
        for key, rate in list(stats["per_field_missing_rate"].items())[:40]:
            lines.append(f"| `{key}` | {_p(rate)} |")
        lines.append("")
    return "\n".join(lines)


def render_cost(stats: dict[str, Any]) -> str:
    lines = ["## token 与时延按节点（T0-3 第二项）", ""]
    lines.append(f"- 样本：{stats['cases']} 个成功运行")
    lines.append(f"- 单案 token 中位：{stats['median_tokens_per_case']}")
    lines.append(f"- 单案根运行耗时中位：{stats['median_duration_ms']} ms")
    lines.append("")
    lines.append("| 节点 | 调用 | token | 占比 | 耗时 | 耗时占比 |")
    lines.append("|---|---|---|---|---|---|")
    for row in stats["by_node"]:
        lines.append(
            f"| {row['node_id']} | {row['calls']} | {row['tokens']} | "
            f"{_p(row['token_share'])} | {row['duration_ms']} ms | {_p(row['duration_share'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def _p(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="归档侧统计：补缺分布与成本占比")
    parser.add_argument("mode", choices=["supplement", "cost", "all"])
    parser.add_argument("--source", default="uat-traces-20261102-20261103")
    parser.add_argument("--markdown", default="", help="把报告写到这个路径")
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.is_dir():
        source = BATCH_ROOT / args.source
    if not source.is_dir():
        raise SystemExit(f"归档批次不存在：{source}")

    parts: list[str] = [f"# 归档统计：{source.name}", ""]
    payload: dict[str, Any] = {"source": source.name}
    if args.mode in ("supplement", "all"):
        stats = supplement_stats(source)
        payload["supplement"] = stats
        parts.append(render_supplement(stats))
    if args.mode in ("cost", "all"):
        stats = cost_stats(source)
        payload["cost"] = stats
        parts.append(render_cost(stats))

    text = "\n".join(parts)
    print(text)
    if args.markdown:
        path = Path(args.markdown)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        path.with_suffix(".json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
