"""v2.1 诊断：审计词到底从哪条断言字段漏进对客产出（只读，0 token，不出网）。

用途：``probe_v21_replay.py`` 只报「审计词命中 40」这样的汇总数，回答不了
「它从哪个字段进来的」。本探针逐条断言、逐个候选输出字段地定位，输出形如
``record_channel  第三方预测  40  原始值片段``。

    python -I scripts/probe_v21_leak.py
    python -I scripts/probe_v21_leak.py --batch experiments/abexperiments/batches/A-pipeline-L8-20261012
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

DEFAULT = REPO / "experiments/abexperiments/batches/A-pipeline-L8-20261012/runs"

# 与 probe_v21_audit.py 的黑名单同源；这里额外用于定位来源。
WORDS = (
    "归属过滤", "来源分级", "枚举归一", "单多值约束", "契约校验",
    "未知渠道", "社交自呈现", "一手交互", "第三方预测", "公开登记查询", "人工/系统登记",
    "按来源分级投影得到", "断言",
)

# 断言账本（L1 产出）里可能承载审计词的字段。
CLAIM_FIELDS = ("record_channel", "basis", "claim_type", "subject", "value",
                "quote", "anchor", "attribute", "basis_of_inference", "claim_id")
# 对客产出（Notes 出口）里可能承载审计词的字段。
NOTE_FIELDS = ("summary", "steps", "evidence")


def jload(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def nodes_index(path: Path) -> dict:
    nodes = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(nodes, dict) and "nodes" in nodes:
        nodes = nodes["nodes"]
    elif isinstance(nodes, dict):
        nodes = list(nodes.values())
    index: dict[str, dict] = {}
    for item in nodes or []:
        if isinstance(item, dict):
            key = item.get("node_id") or item.get("id")
            index.setdefault(key, item.get("outputs") or {})
    return index


def scan_blob(label: str, blob, where: Counter, samples: dict) -> None:
    for text in blob if isinstance(blob, list) else [blob]:
        if not isinstance(text, str):
            continue
        for word in WORDS:
            if word in text:
                key = (label, word)
                where[key] += 1
                samples.setdefault(key, text[:160])
                break


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v2.1 审计词来源定位探针")
    parser.add_argument("--batch", default=str(DEFAULT))
    args = parser.parse_args(argv)

    root = Path(args.batch)
    if not root.is_dir() and (root / "runs").is_dir():
        root = root / "runs"
    if not root.is_dir():
        print("[FAIL] 找不到批次：%s" % root)
        return 2

    sys.stdout.reconfigure(encoding="utf-8")

    where: Counter = Counter()
    samples: dict = {}
    cases = 0

    for phone in sorted(p.name for p in root.iterdir() if p.is_dir()):
        index = nodes_index(root / phone / "nodes.json")
        if not index:
            continue
        cases += 1

        for node_id, outputs in index.items():
            if not isinstance(outputs, dict):
                continue
            # L1 断言账本
            claims = jload(outputs.get("claims_json"))
            if claims:
                fields = (claims.get("fields") if isinstance(claims, dict) else claims) or []
                for entry in fields:
                    if not isinstance(entry, dict):
                        continue
                    for field in CLAIM_FIELDS:
                        scan_blob("claims." + field, entry.get(field), where, samples)
            # 对客产出（Notes）
            notes = jload(outputs.get("notes_updates_json"))
            if isinstance(notes, list):
                for entry in notes:
                    if not isinstance(entry, dict):
                        continue
                    detail = entry.get("reasoning_detail") or {}
                    for field in NOTE_FIELDS:
                        scan_blob("notes." + field, detail.get(field), where, samples)
                    scan_blob("notes.attribute_value", entry.get("attribute_value"),
                              where, samples)

    print("=== v2.1 审计词来源定位（扫 %d 案） ===" % cases)
    if not where:
        print("（无命中）")
        return 0
    for (label, word), count in where.most_common(60):
        print("%-26s %-10s %4d  %s" % (label, word, count, samples[(label, word)]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
