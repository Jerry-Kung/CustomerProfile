"""跑批验证的量化对比：数量与质量，逐案 × 逐时点。

## 为什么单独写一个脚本

`compare_incremental.py` 回答的是「两批之间有多少字段变了」，那是**稳定性**口径。
本次验收要回答的是另外两件事：

1. **数量**：产出是否变少（Notes 条数、空值、契约覆盖率）。
2. **质量**：每条结论是否有像样的依据（证据、推导步骤、置信度），等级与板块是否完整。

两者都会「静默劣化」——条数少两条、证据全空，运行照样 succeeded。所以必须逐项列出来，
而不是看一眼汇总。

## 比较组

- `--truth`：归档里的原始 `result1.json`（真值参照，不是标尺——模型随机性使然）。
- `--arm`：待验证的一批。
- `--control`：同一夹具、同一时点跑现行全量管线的对照。

口径写死在常量里，每期验收引用同一份定义。

用法::

    python scripts/verify_batch.py --arm experiments/uat_data/armB-incremental-20261109/t2 \\
        --control experiments/uat_data/armC-control-full-20261109/t2 \\
        --truth experiments/incremental/sequences --markdown out.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile import notes_contract as nc  # noqa: E402

CONTRACT_KEYS = tuple(d["attribute_key"] for d in nc.RAW_CONFIGS)
"""契约字段全集（45 项）。覆盖率的分母。"""

SECTIONS = (
    "profile_summary",
    "inferred_tags",
    "usage_scenarios",
    "communication_strategies",
    "customer_overview",
    "purchase_motivations",
    "product_preferences",
    "resistances",
    "emotion_state",
)
"""表达层九个板块（`customer_profile_l3.SECTION_KEYS` 的对应位置）。"""


# ---------------------------------------------------------------- 读取

def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _analysis(run_dir: Path) -> dict[str, Any]:
    """从 `result.json` 取 `analysis_result`。取不到返回空字典，不抛。"""
    path = run_dir / "result.json"
    if not path.is_file():
        return {}
    payload = _load(path)
    raw = payload.get("result1")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return (raw or {}).get("analysis_result") or {}


def _notes(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in (analysis.get("basic_notes_updates") or []) if isinstance(item, dict)]


def _usage(run_dir: Path) -> dict[str, int]:
    path = run_dir / "attempts.json"
    if not path.is_file():
        return {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    calls = prompt = completion = 0
    for item in _load(path):
        if item.get("kind") != "llm":
            continue
        calls += 1
        usage = item.get("usage_json") or {}
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
    return {"llm_calls": calls, "prompt_tokens": prompt, "completion_tokens": completion}


def sections_of(run_dir: Path) -> dict[str, Any]:
    """表达层九节的完成情况，取自 `l3_assemble` 节点出参。

    读不到时返回 `sections=None`（而不是 0）：0 会被读成「表达层全塌了」，
    而实际可能只是这次运行没走到表达层（T0 原样回传就没有 L3 节点）。
    """
    path = run_dir / "nodes.json"
    if not path.is_file():
        return {"sections": None, "sections_missing": None}
    try:
        nodes = _load(path)
    except ValueError:
        return {"sections": None, "sections_missing": None}
    for node in nodes:
        if not isinstance(node, dict) or node.get("node_id") != "l3_assemble":
            continue
        outputs = node.get("outputs") or {}
        if not isinstance(outputs, dict):
            continue
        try:
            count = int(outputs.get("section_count"))
        except (TypeError, ValueError):
            count = None
        missing = outputs.get("sections_missing")
        return {
            "sections": count,
            "sections_missing": list(missing) if isinstance(missing, list) else None,
        }
    return {"sections": None, "sections_missing": None}


def _status(run_dir: Path) -> str:
    path = run_dir / "result.json"
    if not path.is_file():
        return "missing"
    return str(_load(path).get("status") or "")


def _case_dirs(root: Path) -> dict[str, Path]:
    """`root/runs/<phone>` → {phone: dir}。多份运行取第一份。"""
    picked: dict[str, Path] = {}
    runs = root / "runs"
    if not runs.is_dir():
        return picked
    for path in sorted(runs.iterdir()):
        if path.is_dir():
            picked.setdefault(path.name.split("__")[0], path)
    return picked


# ---------------------------------------------------------------- 指标

def measure(run_dir: Path) -> dict[str, Any]:
    """一个号码一次的产出测量。数量与质量都从这里出。"""
    analysis = _analysis(run_dir)
    notes = _notes(analysis)
    values = {str(n.get("attribute_key") or ""): n for n in notes}

    non_empty = 0
    with_evidence = 0
    with_steps = 0
    confidences: list[float] = []
    for note in notes:
        detail = note.get("reasoning_detail") or {}
        if str(note.get("attribute_value") or "").strip():
            non_empty += 1
        if detail.get("evidence"):
            with_evidence += 1
        if detail.get("steps"):
            with_steps += 1
        try:
            confidences.append(float(detail.get("confidence")))
        except (TypeError, ValueError):
            pass

    level = str(((analysis.get("ai_insight") or {}).get("purchase_intent_level") or {}).get("value") or "")
    level_detail = (analysis.get("ai_insight") or {}).get("purchase_intent_level") or {}
    contract_hit = sum(1 for key in CONTRACT_KEYS if key in values)

    return {
        "status": _status(run_dir),
        "notes": len(notes),
        "non_empty": non_empty,
        "contract_hit": contract_hit,
        "contract_coverage": round(contract_hit / len(CONTRACT_KEYS), 3),
        "with_evidence": with_evidence,
        "with_steps": with_steps,
        "evidence_rate": round(with_evidence / len(notes), 3) if notes else None,
        "confidence_mean": round(sum(confidences) / len(confidences), 3) if confidences else None,
        "level": level,
        "level_has_steps": bool(level_detail.get("steps")),
        "level_has_summary": bool(level_detail.get("summary")),
        "level_reverted": str(level_detail.get("reverted") or ""),
        # 表达层九节只在 L3 装配节点里，**不在**根 result1：后者是回传体契约（六键）。
        # 从 result.json 数会恒得 0，必须读节点留痕。
        **sections_of(run_dir),
        "usage": _usage(run_dir),
        "keys": sorted(values),
    }


def compare_case(arm: dict, control: dict | None, truth: dict | None) -> dict[str, Any]:
    """逐案对比。`control` / `truth` 缺失时只报自身数字，不做差。"""
    row: dict[str, Any] = {"arm": arm}
    if control is not None:
        row["control"] = control
        row["notes_delta"] = arm["notes"] - control["notes"]
        row["coverage_delta"] = round(arm["contract_coverage"] - control["contract_coverage"], 3)
        row["evidence_delta"] = (
            round(arm["evidence_rate"] - control["evidence_rate"], 3)
            if arm["evidence_rate"] is not None and control["evidence_rate"] is not None
            else None
        )
        row["sections_delta"] = (
            arm["sections"] - control["sections"]
            if arm["sections"] is not None and control["sections"] is not None
            else None
        )
        row["level_same"] = arm["level"] == control["level"]
        row["arm_only_keys"] = sorted(set(arm["keys"]) - set(control["keys"]))
        row["control_only_keys"] = sorted(set(control["keys"]) - set(arm["keys"]))
        row["llm_calls_delta"] = arm["usage"]["llm_calls"] - control["usage"]["llm_calls"]
    if truth is not None:
        row["truth"] = truth
        row["notes_vs_truth"] = arm["notes"] - truth["notes"]
        row["coverage_vs_truth"] = round(arm["contract_coverage"] - truth["contract_coverage"], 3)
    return row


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 3) if clean else None


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


# ---------------------------------------------------------------- 呈现

def render(report: dict[str, Any]) -> str:
    lines = [f"# 跑批验证：{report['arm_dir']}", ""]
    if report.get("control_dir"):
        lines.append(f"对照组：`{report['control_dir']}`")
    if report.get("truth_dir"):
        lines.append(f"真值参照：`{report['truth_dir']}`")
    lines.append("")

    lines += [
        "| 号码 | 状态 | Notes | 契约覆盖 | 有证据 | 置信均值 | 等级 | 板块 | LLM |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for phone, row in report["rows"].items():
        a = row["arm"]
        lines.append(
            f"| {phone} | {a['status']} | {a['notes']} | {_pct(a['contract_coverage'])} | "
            f"{_pct(a['evidence_rate'])} | {a['confidence_mean'] if a['confidence_mean'] is not None else '—'} | "
            f"{a['level'] or '—'} | "
            f"{'—' if a['sections'] is None else str(a['sections']) + '/9'} | "
            f"{a['usage']['llm_calls']} |"
        )

    if report.get("has_control"):
        lines += [
            "",
            "## 与对照组的差（正=本臂更多）",
            "",
            "| 号码 | Notes差 | 覆盖差 | 证据差 | 板块差 | 等级同 | 仅本臂字段 | 仅对照字段 | LLM差 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for phone, row in report["rows"].items():
            if "control" not in row:
                # 该号码在没有对照的批次里出现过（例如补跑遗留）：不做差，留一行占位。
                lines.append(f"| {phone} | — | — | — | — | 无对照 | — | — | — |")
                continue
            evidence = row["evidence_delta"]
            evidence_text = "—" if evidence is None else f"{evidence:+.3f}"
            arm_only = ",".join(row["arm_only_keys"]) or "—"
            control_only = ",".join(row["control_only_keys"]) or "—"
            level_same = "是" if row["level_same"] else "否"
            lines.append(
                f"| {phone} | {row['notes_delta']:+d} | {row['coverage_delta']:+.3f} | "
                f"{evidence_text} | "
                f"{'—' if row['sections_delta'] is None else f'{row[chr(115)+chr(101)+chr(99)+chr(116)+chr(105)+chr(111)+chr(110)+chr(115)+chr(95)+chr(100)+chr(101)+chr(108)+chr(116)+chr(97)]:+d}'} | "
                f"{level_same} | "
                f"{arm_only} | {control_only} | {row['llm_calls_delta']:+d} |"
            )

    lines += [
        "",
        "## 汇总",
        "",
        f"- 案例数：{report['totals']['cases']}",
        f"- 平均 Notes：{report['totals']['notes_mean']}",
        f"- 平均契约覆盖：{_pct(report['totals']['coverage_mean'])}",
        f"- 平均证据率：{_pct(report['totals']['evidence_mean'])}",
        f"- 等级分布：{report['totals']['levels']}",
        f"- LLM 调用合计：{report['totals']['llm_calls']}",
    ]
    if report.get("has_control"):
        t = report["totals"]
        lines += [
            f"- 与对照的 Notes 平均差：{t['notes_delta_mean']:+.2f}",
            f"- 与对照的覆盖平均差：{t['coverage_delta_mean']:+.3f}",
            f"- 等级与对照一致的案例：{t['level_same']}/{t['cases']}",
            f"- LLM 调用相对对照：{t['llm_calls_delta_sum']:+d}",
        ]
    return "\n".join(lines) + "\n"


def verify(arm_dir: Path, control_dir: Path | None, truth_dir: Path | None) -> dict[str, Any]:
    arm_cases = _case_dirs(arm_dir)
    control_cases = _case_dirs(control_dir) if control_dir else {}
    report: dict[str, Any] = {
        "arm_dir": str(arm_dir),
        "control_dir": str(control_dir) if control_dir else None,
        "truth_dir": str(truth_dir) if truth_dir else None,
        "rows": {},
        "has_control": bool(control_dir),
    }

    for phone, run_dir in arm_cases.items():
        arm = measure(run_dir)
        control = measure(control_cases[phone]) if phone in control_cases else None
        truth = None
        if truth_dir is not None:
            point = truth_dir / phone / "t2"
            if not point.is_dir():
                point = truth_dir / phone / "t1"
            if (point / "result1.json").is_file():
                payload = _load(point / "result1.json")
                truth = {
                    "notes": len(payload.get("analysis_result", {}).get("basic_notes_updates") or []),
                    "contract_coverage": round(
                        sum(
                            1
                            for key in CONTRACT_KEYS
                            if any(
                                str(n.get("attribute_key") or "") == key
                                for n in (payload.get("analysis_result", {}).get("basic_notes_updates") or [])
                            )
                        )
                        / len(CONTRACT_KEYS),
                        3,
                    ),
                }
        report["rows"][phone] = compare_case(arm, control, truth)

    rows = list(report["rows"].values())
    levels: dict[str, int] = {}
    for row in rows:
        levels[row["arm"]["level"] or "空"] = levels.get(row["arm"]["level"] or "空", 0) + 1
    totals: dict[str, Any] = {
        "cases": len(rows),
        "notes_mean": _mean([row["arm"]["notes"] for row in rows]),
        "coverage_mean": _mean([row["arm"]["contract_coverage"] for row in rows]),
        "evidence_mean": _mean([row["arm"]["evidence_rate"] for row in rows]),
        "levels": levels,
        "llm_calls": sum(row["arm"]["usage"]["llm_calls"] for row in rows),
    }
    if report["has_control"]:
        with_control = [row for row in rows if "control" in row]
        totals["notes_delta_mean"] = _mean([row["notes_delta"] for row in with_control])
        totals["coverage_delta_mean"] = _mean([row["coverage_delta"] for row in with_control])
        totals["level_same"] = sum(1 for row in with_control if row["level_same"])
        totals["llm_calls_delta_sum"] = sum(row["llm_calls_delta"] for row in with_control)
    report["totals"] = totals
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑批验证：数量与质量逐案对比")
    parser.add_argument("--arm", required=True, help="待验证批次目录（含 runs/）")
    parser.add_argument("--control", default=None, help="对照组批次目录（同夹具全量）")
    parser.add_argument("--truth", default=None, help="序列目录（取 t1/t2 的归档 result1 作参照）")
    parser.add_argument("--markdown", default=None, help="把报告写到这个路径")
    args = parser.parse_args(argv)

    arm_dir = Path(args.arm).resolve()
    if not arm_dir.is_dir():
        raise SystemExit(f"批次目录不存在：{arm_dir}")
    report = verify(
        arm_dir,
        Path(args.control).resolve() if args.control else None,
        Path(args.truth).resolve() if args.truth else None,
    )
    text = render(report)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
