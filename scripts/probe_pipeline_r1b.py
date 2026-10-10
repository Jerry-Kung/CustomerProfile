"""R1b 缺口归因：把复跑批次与**基线**在同案上逐字段对一遍，并定位每一层卡在哪。

用法::

    python scripts/probe_pipeline_r1b.py --batch-dir experiments/abexperiments/batches/A-pipeline-R1b-20261010

与 `probe_pipeline_r1.py` 的分工：那个查「探针期望」，这个查「链路规模与卡点」——
回答的是「字段少，少在 L1 没抽到、还是定位丢了、还是 L2 拒了」。只读，不出网，不写文件。
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

PROBES = "16761543238 13926073386 13530559591 15818891947 15308826588 13535737221 13702911703 13719210972 16541242243 18003012053 18675756738 18902281339 18906860015".split()
BASELINE = "experiments/batches/baseline-39-dsv41-noThink/runs"


def norm(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return value or []


def node_out(run_dir: Path):
    path = run_dir / "nodes.json"
    if not path.is_file():
        return None
    nodes = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(nodes, dict) and "nodes" in nodes:
        nodes = nodes["nodes"]
    idx: dict[str, list] = {}
    for item in (nodes if isinstance(nodes, list) else nodes.values()):
        idx.setdefault(item.get("node_id") or item.get("id"), []).append(item)

    def out(node_id, field):
        for item in idx.get(node_id, []):
            payload = item.get("outputs") or item.get("output") or {}
            if isinstance(payload, dict) and field in payload:
                return payload[field]
    return out


def notes_of(run_dir: Path) -> set[str] | None:
    path = run_dir / "result.json"
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    result1 = raw.get("result1")
    payload = json.loads(result1) if isinstance(result1, str) else result1
    entries = (((payload or {}).get("analysis_result") or {}).get("basic_notes_updates")) or []
    return {str(e.get("attribute_key")) for e in entries}


def main() -> int:
    parser = argparse.ArgumentParser(description="R1b 缺口归因（只读）")
    parser.add_argument("--batch-dir", required=True)
    parser.add_argument("--baseline-dir", default=BASELINE)
    args = parser.parse_args()

    runs = Path(args.batch_dir) / "runs"
    baseline = Path(args.baseline_dir)
    if not runs.is_dir():
        print(f"批次目录不存在：{runs}")
        return 2

    lost_all: Counter = Counter()
    print(f"{'案号':<13}{'基线':>5}{'本次':>5}{'L1原始':>7}{'保留':>5}{'短锚':>5}{'未找到':>6}{'L2拒':>5}")
    for phone in PROBES:
        run_dir = runs / phone
        out = node_out(run_dir)
        if out is None:
            print(f"{phone:<13}  未完成")
            continue
        raw = out("pipeline_l1_llm", "result")
        try:
            raw_claims = len((json.loads(raw) or {}).get("claims") or [])
        except Exception:
            raw_claims = 0
        kept = out("pipeline_l1_localize", "kept")
        dropped = norm(out("pipeline_l1_localize", "dropped_detail"))
        counts = Counter(x.get("reason") for x in dropped if isinstance(x, dict))
        rejected = norm(out("pipeline_l2_project", "rejected_json"))

        base_notes = notes_of(baseline / phone) or set()
        now_notes = notes_of(run_dir) or set()
        lost_all += Counter(base_notes - now_notes)
        print(
            f"{phone:<13}{len(base_notes):>5}{len(now_notes):>5}{raw_claims:>7}"
            f"{str(kept):>5}{counts.get('anchor_too_short', 0):>5}"
            f"{counts.get('anchor_not_found', 0):>6}{len(rejected):>5}"
        )

    print()
    print("=== 仍缺字段（基线有、本次无，跨案频次）===")
    for key, count in lost_all.most_common(30):
        print(f"  {key:<30}{count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
