#!/usr/bin/env python
"""增量画像的三项对比指标：稳定性、等价性、成本。

口径定义见 ``docs/feature-incremental-update/04-测试与验证方案.md`` §4。三个指标全部
从这里出，每期验收引用同一份定义，避免「这一期换了个算法所以数字变好看了」。

    python scripts/compare_incremental.py --a <批次目录A> --b <批次目录B> --markdown out.md
    python scripts/compare_incremental.py --a ... --b ... --sequence experiments/incremental/sequences

- ``--a`` / ``--b``：两个批次目录（``experiments/uat_data/<id>``，含 ``runs/<phone>/``）。
  比较按号码配对；同一号码多份运行时取第一份（与 ``compare_batches`` 同口径）。
- ``--sequence``：可选。给了就按序列目录里的 ``sequence.json`` 把每对号码的差异
  归到「新增记录触及的渠道」上，从而区分「受影响字段」与「未受影响字段」。
  这是稳定性指标的核心：**未受新数据影响的字段变化数**才是要治的漂移。
- ``--label-a`` / ``--label-b``：报告里的两臂名字，缺省取目录名。

指标：

| 指标 | 本脚本怎么算 |
|---|---|
| 稳定性 | 两批同号码的 Notes 取值差异字段数；有序列时再拆成「受影响 / 未受影响」 |
| 等价性 | 同号码 Notes 字段一致率 = 取值相同字段数 / 并集字段数；等级是否相同单列 |
| 成本 | LLM 调用次数、prompt/completion token、根运行耗时 |
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

BATCH_ROOT = REPO_ROOT / "experiments" / "uat_data"

CHANNEL_TO_FIELDS: dict[str, tuple[str, ...]] = {
    # 渠道 → **可能**受它影响的 Notes 字段。只用于「哪些字段的变化不需要解释」，
    # 因此宁可宽一点：漏判会把正常变化算成漂移，误判只会让指标偏乐观一点点。
    "OntologyOrder": (
        "intended_model",
        "viewing_progress",
        "payment_method",
        "budget_range",
        "planned_purchase_time",
        "optional_config",
        "trade_in_requirement",
        "trade_in_model",
    ),
    "OntologyOrderFinance": ("payment_method", "budget_range", "trade_in_requirement"),
    "OntologyLeadStage": ("viewing_progress", "planned_purchase_time"),
    "OntologyLeadFollowRecord": (
        "viewing_progress",
        "budget_range",
        "planned_purchase_time",
        "intended_model",
        "emotion",
        "core_concerns",
        "competing_brand",
        "comparison_models",
        "decision_maker",
        "pain_points",
        "follow_up_frequency",
    ),
    "OntologyManualOutboundCall": (
        "emotion",
        "core_concerns",
        "budget_range",
        "intended_model",
        "pain_points",
        "brand_concerns",
        "competing_brand",
        "comparison_models",
    ),
    "OntologyManualOutboundCallRecordingFile": (
        "emotion",
        "core_concerns",
        "budget_range",
        "intended_model",
        "pain_points",
        "brand_concerns",
        "competing_brand",
        "comparison_models",
        "decision_maker",
    ),
    "TestDriveRecordingFile": (
        "intended_model",
        "viewing_progress",
        "budget_range",
        "planned_purchase_time",
        "core_concerns",
        "emotion",
        "optional_config",
        "pain_points",
        "comparison_models",
        "competing_brand",
        "trade_in_requirement",
        "trade_in_model",
        "payment_method",
    ),
    "OntologyDefeatRecord": ("viewing_progress", "emotion", "competing_brand", "pain_points"),
    "OntologyTestDriveSchedule": ("viewing_progress", "planned_purchase_time", "intended_model"),
    "OntologyCustomerChatInfo": (
        "emotion",
        "core_concerns",
        "intended_model",
        "budget_range",
        "competing_brand",
    ),
    "OntologyLeadInfo": (),
    "OntologyAppRegistration": ("customer_name", "gender", "age", "phone_number"),
}


# ---------------------------------------------------------------- 读取

def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _notes(run_dir: Path) -> dict[str, str]:
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
    analysis = (raw or {}).get("analysis_result") or {}
    out: dict[str, str] = {}
    for item in analysis.get("basic_notes_updates") or []:
        key = str(item.get("attribute_key") or "")
        value = item.get("attribute_value")
        text = "" if value is None else str(value).strip()
        if key and text:
            out[key] = text
    return out


def _level(run_dir: Path) -> str:
    path = run_dir / "result.json"
    if not path.is_file():
        return ""
    payload = _load(path)
    raw = payload.get("result1")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return ""
    analysis = (raw or {}).get("analysis_result") or {}
    return str(((analysis.get("ai_insight") or {}).get("purchase_intent_level") or {}).get("value") or "")


def _usage(run_dir: Path) -> dict[str, int]:
    path = run_dir / "attempts.json"
    if not path.is_file():
        return {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    calls = 0
    prompt = 0
    completion = 0
    for item in _load(path):
        if item.get("kind") != "llm":
            continue
        calls += 1
        usage = item.get("usage_json") or {}
        prompt += int(usage.get("prompt_tokens") or 0)
        completion += int(usage.get("completion_tokens") or 0)
    return {"llm_calls": calls, "prompt_tokens": prompt, "completion_tokens": completion}


def _duration(run_dir: Path) -> int:
    path = run_dir / "result.json"
    if not path.is_file():
        return 0
    return int(_load(path).get("duration_ms") or 0)


def case_dirs(batch: Path) -> dict[str, Path]:
    picked: dict[str, Path] = {}
    root = batch / "runs"
    for path in sorted(root.iterdir() if root.is_dir() else []):
        if not path.is_dir():
            continue
        phone = path.name.split("__")[0]
        picked.setdefault(phone, path)
    return picked


def load_sequences(root: Path) -> dict[str, dict]:
    """``{phone: sequence.json}``，号码取目录名（与批次 runs 目录同形）。"""
    out: dict[str, dict] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*/sequence.json")):
        payload = _load(path)
        phone = str(payload.get("phone") or path.parent.name)
        out[phone] = payload
    return out


def affected_fields(step: dict, side: str) -> set[str]:
    """某个差分步骤可能影响的字段集合。

    ``side="new"``：新增/改版记录触及的渠道；``side="prev"``：上一版的同一批渠道
    （字段映射与档位无关，两者相同，保留参数是为了以后能按「渠道 + 记录键」细化）。
    """
    channels = list((step.get("touched_channels") or {}).keys())
    fields: set[str] = set()
    for channel in channels:
        fields.update(CHANNEL_TO_FIELDS.get(channel, ()))
    if step.get("removed"):
        # 移除的记录不知道属于哪个渠道，一并不做稳定断言。
        fields = set(CHANNEL_TO_FIELDS.get("OntologyLeadFollowRecord", ())) | fields
    return fields


# ---------------------------------------------------------------- 对比

def compare(
    a_dir: Path,
    b_dir: Path,
    sequences: dict[str, dict],
    label_a: str,
    label_b: str,
) -> dict[str, Any]:
    a_cases = case_dirs(a_dir)
    b_cases = case_dirs(b_dir)
    phones = sorted(set(a_cases) & set(b_cases))
    rows: list[dict[str, Any]] = []
    for phone in phones:
        a_notes = _notes(a_cases[phone])
        b_notes = _notes(b_cases[phone])
        keys = set(a_notes) | set(b_notes)
        same = [key for key in keys if a_notes.get(key) == b_notes.get(key)]
        changed = sorted(
            key for key in keys if a_notes.get(key) != b_notes.get(key)
        )
        a_level, b_level = _level(a_cases[phone]), _level(b_cases[phone])
        a_use, b_use = _usage(a_cases[phone]), _usage(b_cases[phone])

        row: dict[str, Any] = {
            "phone": phone,
            "notes_a": len(a_notes),
            "notes_b": len(b_notes),
            "same": len(same),
            "changed": changed,
            "union": len(keys),
            "agreement": (len(same) / len(keys)) if keys else None,
            "level_a": a_level,
            "level_b": b_level,
            "level_same": a_level == b_level,
            "usage_a": a_use,
            "usage_b": b_use,
            "duration_a": _duration(a_cases[phone]),
            "duration_b": _duration(b_cases[phone]),
        }

        sequence = sequences.get(phone)
        if sequence:
            # 用最后一步的差分估计「本轮哪些字段本来就该动」。T0 的纯重复序列没有
            # 步骤（steps 为空），此时没有任何字段应受影响。
            step = (sequence.get("steps") or [{}])[-1]
            expected = affected_fields(step, "new")
            row["expected_tier"] = step.get("expected_tier", "")
            row["affected"] = sorted(set(changed) & expected)
            row["unaffected"] = sorted(set(changed) - expected)
        rows.append(row)

    totals = {
        "cases": len(rows),
        "changed_total": sum(len(row["changed"]) for row in rows),
        "unaffected_total": sum(len(row.get("unaffected") or []) for row in rows),
        "level_changed": sum(1 for row in rows if not row["level_same"]),
        "llm_calls_a": sum(row["usage_a"]["llm_calls"] for row in rows),
        "llm_calls_b": sum(row["usage_b"]["llm_calls"] for row in rows),
        "prompt_tokens_a": sum(row["usage_a"]["prompt_tokens"] for row in rows),
        "prompt_tokens_b": sum(row["usage_b"]["prompt_tokens"] for row in rows),
        "completion_tokens_a": sum(row["usage_a"]["completion_tokens"] for row in rows),
        "completion_tokens_b": sum(row["usage_b"]["completion_tokens"] for row in rows),
        "duration_a": sum(row["duration_a"] for row in rows),
        "duration_b": sum(row["duration_b"] for row in rows),
    }
    return {
        "label_a": label_a,
        "label_b": label_b,
        "a_dir": str(a_dir),
        "b_dir": str(b_dir),
        "rows": rows,
        "totals": totals,
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def render(report: dict[str, Any]) -> str:
    rows = report["rows"]
    totals = report["totals"]
    lines: list[str] = []
    lines.append(f"# 增量画像对比：{report['label_a']} → {report['label_b']}")
    lines.append("")
    lines.append(f"- 配对数：{len(rows)}")
    if not rows:
        lines.append("")
        lines.append("没有可配对的号码。")
        return "\n".join(lines)
    agreements = [row["agreement"] for row in rows if row["agreement"] is not None]
    lines.append(f"- 平均字段一致率（等价性）：{_pct(_mean(agreements))}")
    lines.append(f"- 等级变化：{totals['level_changed']} / {len(rows)}")
    lines.append("")

    lines.append("## 1. 稳定性")
    lines.append("")
    if any("unaffected" in row for row in rows):
        lines.append("| 号码 | 预期档位 | 变化字段 | 受影响 | **未受影响** |")
        lines.append("|---|---|---|---|---|")
        for row in rows:
            if "unaffected" not in row:
                continue
            lines.append(
                f"| {row['phone']} | {row.get('expected_tier', '')} | {len(row['changed'])} | "
                f"{len(row['affected'])} | **{len(row['unaffected'])}** |"
            )
        lines.append("")
        lines.append(f"未受影响字段变化合计：**{totals['unaffected_total']}**（目标为 0）")
    else:
        lines.append("未提供序列目录，只报告总变化字段数：")
        lines.append("")
        lines.append("| 号码 | 变化字段 |")
        lines.append("|---|---|")
        for row in rows:
            lines.append(f"| {row['phone']} | {len(row['changed'])} |")
    lines.append(f"变化字段合计：{totals['changed_total']}")
    lines.append("")

    lines.append("## 2. 等价性")
    lines.append("")
    lines.append("| 号码 | Notes A | Notes B | 相同 | 并集 | 一致率 | 等级 A → B |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in rows:
        lines.append(
            f"| {row['phone']} | {row['notes_a']} | {row['notes_b']} | {row['same']} | "
            f"{row['union']} | {_pct(row['agreement'])} | "
            f"{row['level_a'] or '—'} → {row['level_b'] or '—'} |"
        )
    lines.append("")

    lines.append("## 3. 成本")
    lines.append("")
    lines.append("| 项 | A | B | 变化 |")
    lines.append("|---|---|---|---|")
    for key, label in (
        ("llm_calls", "LLM 调用次数"),
        ("prompt_tokens", "prompt token"),
        ("completion_tokens", "completion token"),
    ):
        a_value = totals[f"{key}_a"]
        b_value = totals[f"{key}_b"]
        if a_value:
            delta = f"{(b_value - a_value) / a_value * 100:+.1f}%"
        else:
            delta = "—"
        lines.append(f"| {label} | {a_value} | {b_value} | {delta} |")
    lines.append(f"| 根运行耗时 | {totals['duration_a']} ms | {totals['duration_b']} ms | — |")
    lines.append("")

    detail = [row for row in rows if row["changed"]]
    if detail:
        lines.append("## 4. 变化字段明细")
        lines.append("")
        for row in detail:
            lines.append(f"- **{row['phone']}**：{', '.join(row['changed'])}")
        lines.append("")
    return "\n".join(lines)


def _resolve(value: str) -> Path:
    path = Path(value)
    if path.is_dir():
        return path.resolve()
    return (BATCH_ROOT / value).resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="增量画像稳定性 / 等价性 / 成本对比")
    parser.add_argument("--a", required=True, help="批次 A（目录或 experiments/uat_data 下的名字）")
    parser.add_argument("--b", required=True, help="批次 B")
    parser.add_argument("--label-a", default="", help="A 臂名称，缺省取目录名")
    parser.add_argument("--label-b", default="", help="B 臂名称，缺省取目录名")
    parser.add_argument("--sequence", default="", help="序列目录（含 <phone>/sequence.json）")
    parser.add_argument("--markdown", default="", help="报告写到这个路径")
    args = parser.parse_args(argv)

    a_dir = _resolve(args.a)
    b_dir = _resolve(args.b)
    for path in (a_dir, b_dir):
        if not path.is_dir():
            raise SystemExit(f"批次目录不存在：{path}")

    sequences = load_sequences(Path(args.sequence).resolve()) if args.sequence else {}
    report = compare(
        a_dir,
        b_dir,
        sequences,
        args.label_a or a_dir.name,
        args.label_b or b_dir.name,
    )
    text = render(report)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
