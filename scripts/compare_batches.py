#!/usr/bin/env python
"""两个批次的逐案对比：新代码跑出的结果 vs 归档批次的结果。

用途：改动管线后回答「结果有没有劣化」。只比对**结果面**（result1 与节点状态），
不重跑模型、不重放投影——两边各自是真实运行的真实产物。

口径：

1. **按号码配对**，同一号码在旧批次里有多个运行（``__run2``）时只取第一份，
   并在报告里标出来——重跑一次只有一个运行，数量上就不可比。
2. **耗时不做跨批次结论**。两批并发度不同（旧批 UAT 快照、新批 4 并发），耗时只列出来
   供参考，不参与「劣化」判定。
3. **判定分四类**，按可信度从高到低：运行状态（硬）、Notes 覆盖（硬）、取值变化（软）、
   文本措辞（最软）。软的只列，不下结论。

用法：

    python scripts/compare_batches.py --old uat-18-20261026 --new uat-18-20261026-rerun
    python scripts/compare_batches.py --old <id> --new <id> --markdown out.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

BATCH_ROOT = REPO_ROOT / "experiments" / "uat_data"

PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "未提供", "未提供。", "无", "暂无", "未知", "不详", "不明", "不清楚",
        "无数据", "未获取", "未获取到", "没有", "无信息", "未说明", "无内容",
        "-", "--", "——", "N/A", "n/a", "null", "None", "未知渠道",
    }
)

PIPELINE = "customer_profile_pipeline"


# ---------------------------------------------------------------- 读取

def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _result1(run_dir: Path) -> dict | None:
    path = run_dir / "result.json"
    if not path.is_file():
        return None
    payload = _load_json(path)
    raw = payload.get("result1")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def _notes_of(analysis: dict) -> dict[str, str]:
    """Notes 更新项 → ``{属性键: 取值}``。空值不入表——「留白」与「有值」是两件事。"""
    out: dict[str, str] = {}
    for item in analysis.get("basic_notes_updates") or []:
        key = str(item.get("attribute_key") or "")
        value = item.get("attribute_value")
        if not key:
            continue
        text = "" if value is None else str(value).strip()
        if text:
            out[key] = text
    return out


def _confidence_of(analysis: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in analysis.get("basic_notes_updates") or []:
        key = str(item.get("attribute_key") or "")
        detail = item.get("reasoning_detail") or {}
        value = detail.get("confidence")
        if key and isinstance(value, (int, float)):
            out[key] = float(value)
    return out


def _evidence_less(analysis: dict) -> list[str]:
    """置信不低却没有一条证据的 Notes 项。

    这里只在 ``confidence >= 0.7`` 时算——低置信本就允许无据，那是「降级保留」的正常形态。
    """
    out: list[str] = []
    for item in analysis.get("basic_notes_updates") or []:
        detail = item.get("reasoning_detail") or {}
        confidence = detail.get("confidence")
        evidence = detail.get("evidence") or []
        if isinstance(confidence, (int, float)) and confidence >= 0.7 and not evidence:
            out.append(str(item.get("attribute_key") or ""))
    return [key for key in out if key]


def _placeholders(notes: dict[str, str]) -> list[str]:
    return [key for key, value in notes.items() if value.strip() in PLACEHOLDERS]


def _nodes(run_dir: Path) -> list[dict]:
    path = run_dir / "nodes.json"
    return _load_json(path) if path.is_file() else []


def _attempts(run_dir: Path) -> list[dict]:
    path = run_dir / "attempts.json"
    return _load_json(path) if path.is_file() else []


def _rule_hits(nodes: list[dict]) -> Counter:
    hits: Counter = Counter()
    for node in nodes:
        if node.get("workflow_id") != PIPELINE:
            continue
        outputs = node.get("outputs") or {}
        raw = outputs.get("rule_trace_json")
        if not raw:
            continue
        try:
            traces = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        for trace in traces or []:
            if isinstance(trace, dict) and trace.get("rule_id"):
                hits[str(trace["rule_id"])] += 1
    return hits


def _pipeline_nodes(nodes: list[dict]) -> dict[str, str]:
    return {
        node["node_id"]: node.get("status", "")
        for node in nodes
        if node.get("workflow_id") == PIPELINE and node.get("call_path") in (None, "", [])
    }


def case_profile(run_dir: Path) -> dict[str, Any]:
    payload = _load_json(run_dir / "result.json")
    analysis = (_result1(run_dir) or {}).get("analysis_result") or {}
    notes = _notes_of(analysis)
    nodes = _nodes(run_dir)
    attempts = _attempts(run_dir)
    llm = [item for item in attempts if item.get("kind") == "llm"]
    prompt_tokens = sum((item.get("usage_json") or {}).get("prompt_tokens") or 0 for item in llm)
    completion_tokens = sum((item.get("usage_json") or {}).get("completion_tokens") or 0 for item in llm)
    pipeline = _pipeline_nodes(nodes)
    level = ((analysis.get("ai_insight") or {}).get("purchase_intent_level") or {}).get("value")
    decisions = analysis.get("purchase_decisions") or []
    decision_items = sum(len(item.get("items") or []) for item in decisions if isinstance(item, dict))
    return {
        "run_id": payload.get("run_id"),
        "status": payload.get("status"),
        "duration_ms": payload.get("duration_ms"),
        "definition_hash": (payload.get("definition") or {}).get("definition_hash"),
        "notes": notes,
        "confidence": _confidence_of(analysis),
        "level": level,
        "sections": len([k for k in analysis if k != "basic_notes_updates"]),
        "decision_items": decision_items,
        "psychology": len(analysis.get("decision_psychology") or []),
        "placeholders": _placeholders(notes),
        "evidence_less": _evidence_less(analysis),
        "node_counts": dict(Counter(node.get("status") for node in nodes)),
        "pipeline": pipeline,
        "skipped_pipeline": sorted(k for k, v in pipeline.items() if v == "skipped"),
        "failed_pipeline": sorted(k for k, v in pipeline.items() if v == "failed"),
        "llm_calls": len(llm),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "rule_hits": dict(_rule_hits(nodes)),
        "has_rule_layer": any("rule" in node.get("node_id", "") for node in nodes),
    }


# ---------------------------------------------------------------- 配对

def case_dirs(batch: Path) -> dict[str, Path]:
    """号码 → 运行目录。同号码多个运行时取第一份，并返回被忽略的份数。"""
    root = batch / "runs"
    picked: dict[str, Path] = {}
    duplicates: dict[str, int] = {}
    for path in sorted(root.iterdir() if root.is_dir() else []):
        if not path.is_dir():
            continue
        phone = path.name.split("__")[0]
        if phone in picked:
            duplicates[phone] = duplicates.get(phone, 1) + 1
            continue
        picked[phone] = path
    return picked, duplicates


# ---------------------------------------------------------------- 对比

def compare(old_dir: Path, new_dir: Path, control_dir: Path | None = None) -> dict[str, Any]:
    old_cases, old_dups = case_dirs(old_dir)
    new_cases, new_dups = case_dirs(new_dir)
    control_cases: dict[str, Path] = {}
    if control_dir is not None:
        control_cases, _ = case_dirs(control_dir)
    phones = sorted(set(old_cases) | set(new_cases) | set(control_cases))
    rows: list[dict[str, Any]] = []
    for phone in phones:
        old_path = old_cases.get(phone)
        new_path = new_cases.get(phone)
        control_path = control_cases.get(phone)
        old = case_profile(old_path) if old_path else None
        new = case_profile(new_path) if new_path else None
        control = case_profile(control_path) if control_path else None
        row: dict[str, Any] = {"phone": phone, "old": old, "new": new, "control": control}
        if old and new:
            row["lost"] = sorted(set(old["notes"]) - set(new["notes"]))
            row["gained"] = sorted(set(new["notes"]) - set(old["notes"]))
            row["changed"] = sorted(
                key for key in set(old["notes"]) & set(new["notes"])
                if old["notes"][key] != new["notes"][key]
            )
        if control and new:
            row["ctrl_vs_new_notes"] = len(new["notes"]) - len(control["notes"])
            row["ctrl_vs_new_level"] = (control["level"], new["level"])
        rows.append(row)
    return {
        "old_batch": old_dir.name,
        "new_batch": new_dir.name,
        "control_batch": control_dir.name if control_dir else None,
        "phones": phones,
        "old_duplicates": old_dups,
        "new_duplicates": new_dups,
        "rows": rows,
    }


# ---------------------------------------------------------------- 渲染

def _fmt(value: Any, default: str = "—") -> str:
    return default if value is None or value == "" else str(value)


def render(report: dict[str, Any]) -> str:
    rows = report["rows"]
    lines: list[str] = []
    lines.append(f"# 批次对比：{report['old_batch']} → {report['new_batch']}")
    lines.append("")
    paired = [row for row in rows if row["old"] and row["new"]]
    missing = [row for row in rows if not row["old"] or not row["new"]]
    lines.append(f"- 配对数：{len(paired)} / 旧批 {sum(1 for r in rows if r['old'])} / "
                 f"新批 {sum(1 for r in rows if r['new'])}")
    if missing:
        lines.append(f"- 未配对：{', '.join(row['phone'] for row in missing)}")
    if report["new_duplicates"]:
        lines.append(f"- 新批同号码多次运行：{report['new_duplicates']}")
    lines.append("")

    lines.append("## 1. 运行状态与规模")
    lines.append("")
    has_control = report.get("control_batch") is not None
    head = "| 号码 | 旧状态 | 新状态 | Notes 旧→新 | 商机等级 旧→新 | 节点 旧→新 | 出网LLM 旧→新 |"
    if has_control:
        head += " Notes 对照→新 |"
    lines.append(head)
    lines.append("|---" * (8 if has_control else 7) + "|")
    for row in rows:
        old, new = row["old"], row["new"]
        control = row.get("control")
        if not old or not new:
            lines.append(f"| {row['phone']} | {_fmt(old and old['status'])} | "
                         f"{_fmt(new and new['status'])} | 未配对 | | | |"
                         + (" 未配对 |" if has_control else ""))
            continue
        line = (
            f"| {row['phone']} | {old['status']} | {new['status']} | "
            f"{len(old['notes'])} → {len(new['notes'])} | "
            f"{_fmt(old['level'])} → {_fmt(new['level'])} | "
            f"{sum(old['node_counts'].values())} → {sum(new['node_counts'].values())} | "
            f"{old['llm_calls']} → {new['llm_calls']} |"
        )
        if has_control:
            line += f" {len(control['notes']) if control else '—'} → {len(new['notes'])} |"
        lines.append(line)
    lines.append("")

    lines.append("## 2. 劣化信号（硬指标）")
    lines.append("")
    signals = 0
    for row in paired:
        old, new = row["old"], row["new"]
        control = row.get("control")
        issues: list[str] = []
        if new["status"] != "succeeded":
            issues.append(f"新批运行状态 {new['status']}")
        if new["failed_pipeline"]:
            issues.append("失败节点 " + ", ".join(new["failed_pipeline"]))
        if old["status"] == "succeeded" and len(new["notes"]) < len(old["notes"]):
            # 与对照组同向的差异不算本次代码改动的账——那是上游数据变了。
            if control is not None and len(control["notes"]) == len(new["notes"]):
                issues.append(
                    f"Notes 减少 {len(old['notes'])} → {len(new['notes'])}，"
                    f"但**未改代码的对照组同样是 {len(control['notes'])}** → 归因上游数据，非本次改动"
                )
            else:
                issues.append(f"Notes 减少 {len(old['notes'])} → {len(new['notes'])}")
        if len(new["placeholders"]) > len(old["placeholders"]):
            issues.append(f"占位值增加 {old['placeholders']} → {new['placeholders']}")
        if len(new["evidence_less"]) > len(old["evidence_less"]):
            issues.append(f"缺证据的高置信项增加 {old['evidence_less']} → {new['evidence_less']}")
        if len(new["skipped_pipeline"]) > len(old["skipped_pipeline"]):
            added = sorted(set(new["skipped_pipeline"]) - set(old["skipped_pipeline"]))
            issues.append("新增跳过节点 " + ", ".join(added))
        if issues:
            signals += 1
            lines.append(f"- **{row['phone']}**：" + "；".join(issues))
    if not signals:
        lines.append("未发现硬指标劣化。")
    lines.append("")

    lines.append("## 3. Notes 取值变化")
    lines.append("")
    for row in paired:
        lost, gained, changed = row["lost"], row["gained"], row["changed"]
        if not (lost or gained or changed):
            continue
        lines.append(f"**{row['phone']}**：")
        if lost:
            lines.append(f"- 丢失：{', '.join(lost)}")
        if gained:
            lines.append(f"- 新增：{', '.join(gained)}")
        if changed:
            lines.append(f"- 改值：{', '.join(changed)}")
        lines.append("")

    lines.append("## 4. 规则层命中（仅新批）")
    lines.append("")
    totals: Counter = Counter()
    for row in paired:
        new = row["new"]
        if new["has_rule_layer"]:
            totals.update(new["rule_hits"])
    if totals:
        for rule_id, count in totals.most_common():
            lines.append(f"- {rule_id}: {count}")
    else:
        lines.append("新批次未产生任何规则命中（含降级/拒收/说明）。")
    lines.append("")
    return "\n".join(lines)


def _resolve(value: str, root: str | None) -> Path:
    """批次可以按名字（本仓库 batches 下）给，也可以直接给目录路径。

    直接给路径是为了对照跑在 `git worktree` 里的批次——那是另一棵树，
    不在本仓库的 `experiments/` 下。
    """
    path = Path(value)
    if path.is_dir():
        return path
    return Path(root or BATCH_ROOT) / value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="两个批次的逐案对比")
    parser.add_argument("--old", required=True, help="旧批次名或目录路径")
    parser.add_argument("--new", required=True, help="新批次名或目录路径")
    parser.add_argument(
        "--control", default=None,
        help="对照批次：**未改代码**的同一输入跑出的结果（见报告 §5 归因方法）",
    )
    parser.add_argument("--root", default=None, help="按名字查找时的 batches 根目录")
    parser.add_argument("--markdown", default=None, help="把报告写到这个路径（UTF-8）")
    args = parser.parse_args(argv)

    old_dir = _resolve(args.old, args.root)
    new_dir = _resolve(args.new, args.root)
    control_dir = _resolve(args.control, args.root) if args.control else None
    for path in (old_dir, new_dir):
        if not path.is_dir():
            raise SystemExit(f"批次目录不存在：{path}")

    report = compare(old_dir, new_dir, control_dir)
    text = render(report)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
