"""规则迁移的离线对账：对基线批次的断言账本重跑投影，逐字段比对差异。

用途是 M2 的「行为不变」证据。基线来自 UAT 快照，**代码版本未知**（见批次 README），
所以这里比对的是「今天的新投影」与「当初归档的投影」两边的输出，差异既可能来自规则
迁移，也可能来自两次运行之间别的代码改动——因此脚本只**列差异**，不判定对错，判定由
人做。

用法：

    python scripts/probe_rules_diff.py                     # 全部基线案
    python scripts/probe_rules_diff.py --phone 19972120792
    python scripts/probe_rules_diff.py --limit 2 --verbose

差异分四类，按「值得看的程度」排序：

- ``facts``：字段取值变了（最要紧）；
- ``basis``：置信档变了；
- ``rejected`` / ``to_confirm``：留痕集合变了（规则迁移最容易动到这里）；
- ``notes``：Notes 条数变了。

退出码恒为 0：这是诊断工具，不是门禁。门禁是测试。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile import projection as proj  # noqa: E402
from customer_profile.workflows.customer_profile_pipeline import (  # noqa: E402
    _blank_value,
)

DEFAULT_BATCH = REPO_ROOT / "experiments/batches/rules-opt01-baseline-8-20261026"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _node_outputs(nodes: list[dict], node_id: str) -> dict | None:
    for entry in nodes:
        if entry.get("node_id") == node_id and entry.get("status") == "succeeded":
            return entry.get("outputs") or {}
    return None


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _rules_of(entries: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries or []:
        # trace 用 ``rule_id``，rejected / downgraded 用 ``rule``（迁移期保留旧裸串）。
        key = str(entry.get("rule_id") or entry.get("rule") or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def replay_case(run_dir: Path) -> dict[str, Any]:
    """重跑一个基线案，返回差异摘要。"""
    nodes = _load(run_dir / "nodes.json")
    inject = _node_outputs(nodes, "pipeline_l1_inject")
    archived = _node_outputs(nodes, "pipeline_l2_project")
    if inject is None or archived is None:
        return {"run": run_dir.name, "skipped": "缺少 l1_inject 或 l2_project 节点输出"}

    payload = _json(inject.get("claims_json")) or {}
    # 节点输出是 ``{"fields": [...]}``：``claims_json`` 存的是投影层要的断言账本，
    # 外面还包一层便于同节点挂其它字段。旧产物偶尔是裸列表，两种都接。
    claims = payload.get("fields") if isinstance(payload, dict) else payload
    ledger = [
        proj.Claim(
            attribute=str(raw.get("attribute") or ""),
            value=raw.get("value"),
            channel=str(raw.get("record_channel") or ""),
            subject=str(raw.get("subject") or ""),
            claim_type=str(raw.get("claim_type") or "") or "fact",
            basis=str(raw.get("basis") or ""),
            basis_of_inference=tuple(raw.get("basis_of_inference") or ()),
            quote=str(raw.get("quote") or ""),
            anchor_kind=str(raw.get("anchor_kind") or ""),
            source_quote=str(raw.get("source_quote") or ""),
            reasoning_steps=tuple(raw.get("reasoning_chain") or ()),
        )
        for raw in (claims or [])
        # 与 ``project_claims`` 同口径：空值断言在**进账本之前**就被剔除并计入
        # ``empty_claims``。不照做的话，账本里会多出几条 value 为空的断言，
        # 投影后表现为 ``None → ''`` 的假差异。
        if str(raw.get("attribute") or "").strip()
        and not _blank_value(raw.get("value"))
    ]
    fresh = proj.project(ledger, context=_context(ledger))
    old_facts = _json(archived.get("facts_json")) or {}
    old_basis = _json(archived.get("basis_json")) or {}
    old_rejected = _json(archived.get("rejected_json")) or []
    old_confirm = _json(archived.get("to_confirm_json")) or []
    old_notes = int(archived.get("notes_count") or 0)

    facts_changed = {
        key: {"old": old_facts.get(key), "new": fresh.facts.get(key)}
        for key in set(old_facts) | set(fresh.facts)
        if old_facts.get(key) != fresh.facts.get(key)
    }
    basis_changed = {
        key: {"old": old_basis.get(key), "new": fresh.basis.get(key)}
        for key in set(old_basis) | set(fresh.basis)
        if old_basis.get(key) != fresh.basis.get(key)
    }
    return {
        "run": run_dir.name,
        "facts_changed": facts_changed,
        "basis_changed": basis_changed,
        "rejected_old": _rules_of(old_rejected),
        "rejected_new": _rules_of(fresh.rejected),
        "confirm_old": len(old_confirm),
        "confirm_new": len(fresh.to_confirm),
        "notes_old": old_notes,
        "notes_new": len(fresh.notes_updates),
        "rule_trace": _rules_of(fresh.rule_trace),
    }


def _context(ledger) -> dict[str, Any]:
    """与 ``pipeline._projection_context`` 同口径的旁证（直接复用那一份）。"""
    from customer_profile.workflows.customer_profile_pipeline import (  # noqa: PLC0415
        _projection_context,
    )

    return _projection_context(ledger)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="规则迁移离线对账")
    parser.add_argument("--batch", default=str(DEFAULT_BATCH), help="基线批次目录")
    parser.add_argument("--phone", help="只跑这一案")
    parser.add_argument("--limit", type=int, help="最多跑几案")
    parser.add_argument("--verbose", action="store_true", help="打印全部差异细节")
    args = parser.parse_args(argv)

    root = Path(args.batch)
    runs = sorted(p for p in (root / "runs").iterdir() if p.is_dir())
    if args.phone:
        runs = [p for p in runs if p.name == args.phone]
    if args.limit:
        runs = runs[: args.limit]
    if not runs:
        print(f"没有找到批次运行：{root / 'runs'}")
        return 2

    for run_dir in runs:
        try:
            summary = replay_case(run_dir)
        except Exception as exc:  # 对账是诊断，单案失败不该拖垮整轮
            print(f"[{run_dir.name}] 失败：{type(exc).__name__}: {exc}")
            continue
        if "skipped" in summary:
            print(f"[{run_dir.name}] 跳过：{summary['skipped']}")
            continue

        facts_n = len(summary["facts_changed"])
        basis_n = len(summary["basis_changed"])
        notes_delta = summary["notes_new"] - summary["notes_old"]
        flagged = facts_n or basis_n or notes_delta
        mark = "差异" if flagged else "一致"
        print(
            f"[{run_dir.name}] {mark}：facts 变 {facts_n}，basis 变 {basis_n}，"
            f"Notes {summary['notes_old']}→{summary['notes_new']}，"
            f"确认 {summary['confirm_old']}→{summary['confirm_new']}"
        )
        if args.verbose or flagged:
            for key, change in summary["facts_changed"].items():
                print(f"    facts.{key}: {change['old']!r} → {change['new']!r}")
            for key, change in summary["basis_changed"].items():
                print(f"    basis.{key}: {change['old']!r} → {change['new']!r}")
            print(f"    rejected 旧 {summary['rejected_old']}")
            print(f"    rejected 新 {summary['rejected_new']}")
            print(f"    rule_trace {summary['rule_trace']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
