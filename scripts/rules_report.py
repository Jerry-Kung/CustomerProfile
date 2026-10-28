"""规则命中统计与批次对账。

回答一个问题：**本轮哪条规则真的生效了。**

口径（原 `history/rules-opt01/README.md` §3，规则层已收口）：

- 按规则 × 命中次数 × 动作分布统计，逐案也能看；
- 标出**连续 N 批命中为 0 的 active 规则**——登记了却从不触发，要么规则写错，
  要么该退役。这是规则层最容易积累的腐烂形态，必须可见；
- 基线案逐案对照：Notes 条数、商机等级、可读性。

## 数据来源与两种形态

- **新批次**（规则层上线后）：节点产物里的 ``rule_trace_json``，带新编号、阶段、通道；
- **旧批次**（规则层上线前）：只有 ``rejected_json`` / ``issue_json`` 里的旧裸串
  （``placeholder_removed`` 这类）。这里经登记表 ``legacy_codes`` 归一到新编号，
  归一不了的单独列出，不并进统计——它们是「还没迁进登记表的东西」，混进去会把
  命中数算高。

## 同源批次会被合并

``uat-18-20261026`` 与 ``uat-all-tasks-20261026-20261027`` 有号码重合，但不是同一次
运行的复制——按导出窗口认亲，同源只取一份（取案件多的那份）。

用法：

    python scripts/rules_report.py                       # 全部批次
    python scripts/rules_report.py --batch uat-18-20261026
    python scripts/rules_report.py --json                # 机器可读
    python scripts/rules_report.py --zero-batches 3      # 零命中告警的批次数
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile.rules import default as default_registry  # noqa: E402

BATCH_ROOT = REPO_ROOT / "experiments/uat_data"
SKIP_DIRS = {"__pycache__"}

TRACE_NODES = (
    "pipeline_l1_localize",
    "pipeline_l2_project",
    "pipeline_l2_enum_map",
    "pipeline_l2_review",
    "pipeline_level_apply",
    "pipeline_rule_apply",
)
"""可能带规则留痕的节点。只读这几个，避免把整棵树扫一遍。"""

LEGACY_KEYS = ("rejected_json", "issue_json")
"""旧批次的规则留痕所在字段。"""


# ====================================================================
# 批次与案件
# ====================================================================


@dataclass
class Batch:
    name: str
    created_at: str = ""
    source_key: str = ""
    cases: int = 0
    trace_hits: Counter = field(default_factory=Counter)
    """``{(rule_id, action): 次数}``，来自新编号留痕。"""

    legacy_hits: Counter = field(default_factory=Counter)
    """``{code: 次数}``，旧批次裸串。"""

    unmapped: Counter = field(default_factory=Counter)
    """归一不到新编号的旧裸串。"""

    per_case: dict[str, Counter] = field(default_factory=dict)
    checker_status: Counter = field(default_factory=Counter)
    rule_layer: bool = False
    """是否有**规则层产物**（任一节点输出 ``rule_trace_json`` / ``checker_status``）。

    早于规则层的批次不可能出现新编号——把它们算进「连续零命中」是把「这批没这个功能」
    误报成「这条规则没生效」，报告一旦常驻误报就没人看了。
    """

    @property
    def total_hits(self) -> int:
        return sum(self.trace_hits.values())


def _manifest(batch_dir: Path) -> dict:
    path = batch_dir / "manifest.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _source_key(manifest: dict, name: str) -> str:
    """同源批次认亲键。

    优先 ``source_snapshot_sha256``（同一份导出必然同 sha），其次 ``source_batch``，
    都没有就用目录名——不同批次的裸快照本来就该各自独立。
    """
    for key in ("source_snapshot_sha256", "source_snapshot"):
        value = manifest.get(key)
        if value:
            return f"snapshot:{value}"
    if manifest.get("source_batch"):
        return f"batch:{manifest['source_batch']}"
    return f"dir:{name}"


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def collect(batch_dir: Path, registry) -> Batch:
    manifest = _manifest(batch_dir)
    batch = Batch(
        name=batch_dir.name,
        created_at=str(manifest.get("created_at") or ""),
        source_key=_source_key(manifest, batch_dir.name),
    )
    runs = batch_dir / "runs"
    if not runs.is_dir():
        return batch

    for case in sorted(p for p in runs.iterdir() if p.is_dir() and p.name not in SKIP_DIRS):
        nodes_path = case / "nodes.json"
        if not nodes_path.is_file():
            continue
        try:
            entries = json.loads(nodes_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        batch.cases += 1
        case_hits: Counter = Counter()
        for entry in entries:
            node_id = entry.get("node_id") or ""
            if node_id not in TRACE_NODES:
                continue
            outputs = entry.get("outputs") or {}
            if entry.get("status") != "succeeded":
                continue

            if "rule_trace_json" in outputs or "checker_status" in outputs:
                batch.rule_layer = True

            # 新编号留痕
            for trace in _as_list(outputs.get("rule_trace_json")):
                if not isinstance(trace, dict):
                    continue
                rule_id = str(trace.get("rule_id") or "")
                if not rule_id:
                    continue
                action = str(trace.get("action") or "")
                batch.trace_hits[(rule_id, action)] += 1
                case_hits[(rule_id, action)] += 1

            # 旧裸串
            for key in LEGACY_KEYS:
                for item in _as_list(outputs.get(key)):
                    if not isinstance(item, dict):
                        continue
                    code = str(item.get("rule") or "")
                    if not code:
                        continue
                    batch.legacy_hits[code] += 1
                    if registry.resolve_legacy(code) is None:
                        batch.unmapped[code] += 1

            status = outputs.get("checker_status")
            if status:
                batch.checker_status[str(status)] += 1

        if case_hits:
            batch.per_case[case.name] = case_hits
    return batch


def load_batches(root: Path, only: Iterable[str] = (), registry=None) -> list[Batch]:
    table = registry if registry is not None else default_registry()
    names = {item for item in only if item}
    batches: list[Batch] = []
    for path in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in SKIP_DIRS):
        if names and path.name not in names:
            continue
        batches.append(collect(path, table))
    batches = [b for b in batches if b.cases]
    # 同源只留一份：取案件多的（视图更全），再按创建时间排序
    best: dict[str, Batch] = {}
    for batch in batches:
        kept = best.get(batch.source_key)
        if kept is None or batch.cases > kept.cases:
            best[batch.source_key] = batch
    ordered = sorted(best.values(), key=lambda b: (b.created_at or "", b.name))
    return ordered


# ====================================================================
# 统计
# ====================================================================


def classify(registry) -> dict[str, str]:
    """``{rule_id: 通道}``：``code`` / ``prompt`` / ``llm_checker``。

    一个规则挂多个通道时显示全部，用 ``+`` 连接——统计里「prompt 通道的命中 0 次」
    是正常的（它没有判据，只有正文），不标通道会误导成「这条规则没生效」。
    """
    return {rule.id: "+".join(rule.channels) for rule in registry.active()}


def rule_rows(batches: list[Batch], registry) -> list[dict[str, Any]]:
    """每个 active 规则一行：总命中、各动作次数、逐批命中、通道。"""
    channels = classify(registry)
    eligible = [batch.name for batch in batches if batch.rule_layer]
    rows: list[dict[str, Any]] = []
    for rule in registry.active():
        actions: Counter = Counter()
        per_batch: dict[str, int] = {}
        for batch in batches:
            hits = 0
            for (rule_id, action), count in batch.trace_hits.items():
                if rule_id != rule.id:
                    continue
                actions[action] += count
                hits += count
            per_batch[batch.name] = hits
        rows.append(
            {
                "rule": rule.id,
                "cluster": rule.cluster,
                "channels": channels.get(rule.id, ""),
                "status": rule.status,
                "actions": dict(sorted(actions.items())),
                "total": sum(actions.values()),
                "per_batch": per_batch,
                "zero_streak": _zero_streak(per_batch, eligible),
            }
        )
    rows.sort(key=lambda item: (-item["total"], item["rule"]))
    return rows


def _zero_streak(per_batch: dict[str, int], eligible: Iterable[str]) -> int:
    """尾部连续零命中的批次数。

    只数**含规则层产物**的批次（``eligible``）：规则层之前装好的归档批次里根本不会
    出现新编号，算进去就是误报。批次已按时间排序，这里从末尾往前数。
    """
    allowed = set(eligible)
    streak = 0
    for name, count in reversed(list(per_batch.items())):
        if name not in allowed:
            continue
        if count:
            break
        streak += 1
    return streak


def legacy_rows(batches: list[Batch], registry) -> list[dict[str, Any]]:
    """旧裸串 → 新编号的归一结果，以及归一不了的。"""
    totals: Counter = Counter()
    for batch in batches:
        totals.update(batch.legacy_hits)
    rows: list[dict[str, Any]] = []
    for code, count in totals.most_common():
        rule = registry.resolve_legacy(code)
        rows.append(
            {
                "code": code,
                "hits": count,
                "rule": rule.id if rule is not None else "",
                "mapped": rule is not None,
            }
        )
    return rows


# ====================================================================
# 基线对照（README §3 的五条口径）
# ====================================================================


def _case_outputs(case: Path) -> dict[str, dict]:
    path = case / "nodes.json"
    if not path.is_file():
        return {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return {
        str(entry.get("node_id")): entry.get("outputs") or {}
        for entry in entries
        if entry.get("status") == "succeeded"
    }


def case_profile(case: Path) -> dict[str, Any]:
    """单案的质量指标：Notes 条数与非空数、商机等级、可读性问题数。"""
    out = _case_outputs(case)
    notes = _as_list((out.get("pipeline_rule_apply") or out.get("pipeline_l2_review") or {}).get("notes_updates_json"))
    facts = _as_dict((out.get("pipeline_level_apply") or {}).get("facts_json"))
    filled = [
        item for item in notes
        if str((item or {}).get("attribute_value") or "").strip() not in {"", "未知", "未提供", "无", "暂无"}
    ]
    review = out.get("pipeline_l2_review") or {}
    return {
        "phone": case.name,
        "notes": len(notes),
        "filled": len(filled),
        "level": str(facts.get("deal_level") or ""),
        "placeholder_removed": review.get("placeholder_removed") or 0,
        "enum_illegal": review.get("enum_illegal") or 0,
        "unbacked_fact": review.get("unbacked_fact") or 0,
        "checker_status": (out.get("pipeline_rule_apply") and "applied") or "not_run",
    }


def baseline_rows(batch: Batch, root: Path) -> list[dict[str, Any]]:
    runs = root / batch.name / "runs"
    if not runs.is_dir():
        return []
    return [case_profile(case) for case in sorted(p for p in runs.iterdir() if p.is_dir())]


# ====================================================================
# 输出
# ====================================================================


def render_text(report: dict[str, Any], *, zero_batches: int) -> str:
    lines: list[str] = []
    lines.append("批次（按创建时间）：")
    for batch in report["batches"]:
        lines.append(
            f"  {batch['created_at'][:10] or '????-??-??'}  {batch['name']}  "
            f"{batch['cases']} 案  新编号命中 {batch['trace_hits']}"
        )
    lines.append("")

    lines.append(f"{'规则':<11}{'簇':<13}{'通道':<12}{'命中':>5}   动作分布")
    for row in report["rules"]:
        actions = "，".join(f"{k} {v}" for k, v in row["actions"].items()) or "—"
        mark = "  ← 零命中" if row["total"] == 0 and "code" in row["channels"] else ""
        if row["total"] == 0 and "prompt" in row["channels"]:
            mark = "  （prompt 通道无判据，不算零命中）"
        lines.append(
            f"{row['rule']:<11}{row['cluster']:<13}{row['channels']:<12}{row['total']:>5}   {actions}{mark}"
        )
    lines.append("")

    zero = [
        row for row in report["rules"]
        if row["zero_streak"] >= zero_batches and "code" in row["channels"]
    ]
    if zero:
        lines.append(f"连续 {zero_batches} 批零命中的 code 规则（该查是不是写错了，或者该退役）：")
        for row in zero:
            lines.append(f"  - {row['rule']}（{row['cluster']}）{row['per_batch']}")
    else:
        lines.append(f"没有连续 {zero_batches} 批零命中的 code 规则。")
    lines.append("")

    if report["legacy"]:
        lines.append("旧裸串归一：")
        for row in report["legacy"]:
            target = row["rule"] if row["mapped"] else "**未归一**"
            lines.append(f"  {row['code']:<28}{row['hits']:>4} 次 → {target}")
        lines.append("")

    if report["checker"]:
        lines.append("checker 状态分布：" + "，".join(f"{k} {v}" for k, v in report["checker"].items()))
        lines.append("")

    if report["cases"]:
        lines.append("逐案：")
        lines.append(f"  {'号码':<13}{'Notes':>6}{'非空':>6}  等级  占位删/枚举/无锚")
        for case in report["cases"]:
            lines.append(
                f"  {case['phone']:<13}{case['notes']:>6}{case['filled']:>6}  "
                f"{case['level'] or '-':<5}"
                f"{case['placeholder_removed']}/{case['enum_illegal']}/{case['unbacked_fact']}"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", action="append", default=[])
    parser.add_argument("--root", default=str(BATCH_ROOT))
    parser.add_argument("--zero-batches", type=int, default=3)
    parser.add_argument("--cases-batch", default="uat-18-20261026")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    registry = default_registry()
    root = Path(args.root)
    batches = load_batches(root, args.batch, registry)

    checker: Counter = Counter()
    for batch in batches:
        checker.update(batch.checker_status)

    report = {
        "batches": [
            {
                "name": b.name,
                "created_at": b.created_at,
                "cases": b.cases,
                "trace_hits": b.total_hits,
            }
            for b in batches
        ],
        "rules": rule_rows(batches, registry),
        "legacy": legacy_rows(batches, registry),
        "checker": dict(checker),
        "cases": [],
    }

    cases_batch = next((b for b in batches if b.name == args.cases_batch), None)
    if cases_batch is not None:
        report["cases"] = baseline_rows(cases_batch, root)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(report, zero_batches=args.zero_batches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
