# -*- coding: utf-8 -*-
"""抽取 v3.3 全量 39 案的两臂输出，产出对照页数据 ``data/payload.json``。

**只读**：本脚本不改动任何批次目录，只从两侧的 ``runs/*/result.json`` 与 ``trace.db``
读取。Windows 下务必 ``python -X utf8`` 运行（含中文字面量）。

两侧口径（2026-10-15）：

- 新版：``experiments/batches/pipeline-v3.3-39/``（实验臂 ``customer_profile_pipeline``）
- 基线：``experiments/batches/baseline-39-dsv41-noThink/``（基线臂 ``customer_profile_entry``）

两臂跑的是**同一批 39 个手机号**，但走的是**两条不同的工作流**——这正是对照的意义：
差异来自实现本身，而不是输入。

产出结构见 ``README.md`` 第 3 节。渲染侧只依赖本文件产出的键名，不直接读批次目录。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent

NEW_BATCH = REPO_ROOT / "experiments/batches/pipeline-v3.3-39"
BASE_BATCH = REPO_ROOT / "experiments/batches/baseline-39-dsv41-noThink"
OUT = HERE.parent / "data" / "payload.json"

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
    "basic_notes_updates",
)

SECTION_LABELS = {
    "profile_summary": "画像摘要",
    "inferred_tags": "推断标签",
    "usage_scenarios": "用车场景",
    "communication_strategies": "沟通策略",
    "customer_overview": "客户概览",
    "purchase_motivations": "购车动机",
    "product_preferences": "产品偏好",
    "resistances": "抗性/顾虑",
    "emotion_state": "情绪状态",
    "basic_notes_updates": "基础信息更新（Notes）",
}

TIER_LABEL = {"fact": "事实", "inferred_high": "强推断", "inferred_low": "弱推断"}


# ---------------------------------------------------------------- 结果文件

def load_results(batch: Path) -> dict[str, dict]:
    """读一批的 ``runs/*/result.json``，返回 ``phone -> parsed``。

    ``result.json`` 里 ``result1`` 是 **JSON 字符串**（不是对象），
    真实层级是 ``result1`` → ``analysis_result`` → 10 个段落。
    """
    found: dict[str, dict] = {}
    for path in sorted((batch / "runs").glob("*/result.json")):
        phone = path.parent.name
        raw = json.loads(path.read_text(encoding="utf-8"))
        r1 = raw.get("result1")
        analysis: dict = {}
        if isinstance(r1, str) and r1:
            try:
                analysis = (json.loads(r1) or {}).get("analysis_result") or {}
            except ValueError:
                analysis = {}
        elif isinstance(r1, dict):
            analysis = r1.get("analysis_result") or {}
        found[phone] = {
            "status": raw.get("status"),
            "error": raw.get("error"),
            "duration_ms": raw.get("duration_ms"),
            "workflow_id": raw.get("workflow_id"),
            "sections": {k: analysis.get(k) for k in SECTIONS},
        }
    return found


# ---------------------------------------------------------------- 追踪库读数

def _descendant_ids(conn: sqlite3.Connection, root: str) -> set[str]:
    """收集以 ``root`` 为根的整棵运行树的 run_id（子工作流可有多层）。"""
    seen = {root}
    frontier = {root}
    while frontier:
        rows = conn.execute(
            "SELECT run_id FROM workflow_runs WHERE parent_run_id IN (%s)"
            % ",".join("?" * len(frontier)),
            tuple(frontier),
        ).fetchall()
        nxt = {r[0] for r in rows} - seen
        if not nxt:
            break
        seen |= nxt
        frontier = nxt
    return seen


def load_metrics(batch: Path) -> dict[str, dict]:
    """从 ``trace.db`` 取逐案的机器读数：LLM 次数、token、耗时、重试与异常。

    新版库多两列（``anomaly`` / ``finish_reason``），基线库没有——因此对老库不查这两列，
    不能把「列不存在」当成「无异常」。
    """
    db = batch / "trace.db"
    if not db.is_file():
        return {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(request_attempts)")}
        has_anomaly = "anomaly" in cols

        tops = conn.execute(
            "SELECT business_ref, run_id FROM workflow_runs "
            "WHERE parent_run_id IS NULL AND business_ref IS NOT NULL"
        ).fetchall()

        out: dict[str, dict] = {}
        for phone, root in tops:
            ids = sorted(_descendant_ids(conn, root))
            marks = ",".join("?" * len(ids))
            llm = conn.execute(
                f"SELECT COUNT(*) FROM request_attempts "
                f"WHERE kind='llm' AND run_id IN ({marks})",
                ids,
            ).fetchone()[0]
            tok = conn.execute(
                f"SELECT SUM(json_extract(usage_json,'$.total_tokens')) "
                f"FROM request_attempts WHERE run_id IN ({marks})",
                ids,
            ).fetchone()[0]
            max_attempt = conn.execute(
                f"SELECT MAX(attempt_no) FROM request_attempts "
                f"WHERE kind='llm' AND run_id IN ({marks})",
                ids,
            ).fetchone()[0]
            entry = {
                "llm": llm,
                "tok": tok or 0,
                "max_attempt": max_attempt or 1,
                "anomaly": None,
            }
            if has_anomaly:
                entry["anomaly"] = conn.execute(
                    f"SELECT COUNT(*) FROM request_attempts "
                    f"WHERE anomaly IS NOT NULL AND run_id IN ({marks})",
                    ids,
                ).fetchone()[0]
            out[phone] = entry
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------- 组装

def notes_of(side: dict) -> list:
    """Notes 段落即 ``basic_notes_updates``：一条一个 ``attribute_key``。"""
    value = (side.get("sections") or {}).get("basic_notes_updates")
    if not isinstance(value, list):
        return []
    return [n for n in value if isinstance(n, dict) and n.get("attribute_key")]


def section_counts(side: dict) -> dict[str, int]:
    """逐段条数：列表按条数，字典按非空字段数。"""
    out: dict[str, int] = {}
    for key in SECTIONS:
        value = (side.get("sections") or {}).get(key)
        if isinstance(value, list):
            out[key] = len(value)
        elif isinstance(value, dict):
            out[key] = sum(1 for v in value.values() if _nonempty(v))
        else:
            out[key] = 1 if _nonempty(value) else 0
    return out


def _nonempty(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return True


def main() -> int:
    new = load_results(NEW_BATCH)
    base = load_results(BASE_BATCH)
    new_m = load_metrics(NEW_BATCH)
    base_m = load_metrics(BASE_BATCH)

    phones = sorted(set(new) | set(base))
    only_new = sorted(set(new) - set(base))
    only_base = sorted(set(base) - set(new))

    cases = []
    for phone in phones:
        sides = {}
        for tag, res, met in (("base", base, base_m), ("new", new, new_m)):
            side = res.get(phone)
            if side is None:
                sides[tag] = None
                continue
            m = met.get(phone, {})
            sides[tag] = {
                "status": side["status"],
                "error": side["error"],
                "duration_ms": side["duration_ms"],
                "workflow_id": side["workflow_id"],
                "llm": m.get("llm", 0),
                "tok": m.get("tok", 0),
                "max_attempt": m.get("max_attempt", 1),
                "anomaly": m.get("anomaly"),
                "sections": side["sections"],
                "notes": notes_of(side),
                "sec_counts": section_counts(side),
            }
        cases.append({"phone": phone, "base": sides["base"], "new": sides["new"]})

    payload = {
        "version": "v3.3",
        "batch_new": NEW_BATCH.name,
        "batch_base": BASE_BATCH.name,
        "new_workflow": (new and next(iter(new.values()))["workflow_id"]) or "",
        "base_workflow": (base and next(iter(base.values()))["workflow_id"]) or "",
        "sections": list(SECTIONS),
        "section_labels": SECTION_LABELS,
        "tier_label": TIER_LABEL,
        "only_new": only_new,
        "only_base": only_base,
        "cases": cases,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    size = OUT.stat().st_size
    print("已写出 %s" % OUT)
    print("  大小 %s 字节 ≈ %.2f MB" % (format(size, ","), size / 1048576))
    print("  案数 %d（新版 %d / 基线 %d）" % (len(cases), len(new), len(base)))
    if only_new:
        print("  ! 仅新版有：%s" % "、".join(only_new))
    if only_base:
        print("  ! 仅基线有：%s" % "、".join(only_base))
    print("  新版 %s / 基线 %s" % (payload["new_workflow"], payload["base_workflow"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
