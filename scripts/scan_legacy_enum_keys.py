#!/usr/bin/env python
"""废弃枚举键扫描：批次的回传体里还有没有旧版键值。

## 它回答什么

「回写出去的枚举取值，是不是全部落在现行契约的合法键里。」

与 :mod:`scripts.scan_english_residue` 分工：那个扫**正文**（``summary`` / ``steps`` /
``label``），这个扫 ``basic_notes_updates[].attribute_value``。两者互不重叠——
正文不该出现任何枚举键，取值位则必须**只有**合法键。

## 判据

对每个 ``attribute_value`` 逐项（多选按 ``||`` 切、结构化取 ``key``）判三件事：

1. **合法键** → 通过；
2. **旧版别名可救**（``unmarried`` → ``unmarried_no_child``）→ 记「可映射」，
   说明产出侧漏了归一，属**缺陷**（合规批次应为 0）；
3. **既非合法键也非别名**（``换购`` / ``本科及以上`` / ``微信``）→ 记「非法」。

``purchase_type=换购`` 一类**不再出现**才对：它应已在投影层被路由到
``purchase_nature``（编外字段，不回传）。

## 用法

    python scripts/scan_legacy_enum_keys.py --batch uat-18-legacykeys-20261027
    python scripts/scan_legacy_enum_keys.py --batch X --json

退出码 1 表示仍有「可映射」或「非法」的取值——可直接用于验收与 CI。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile import internal_fields as inf  # noqa: E402

BATCH_ROOT = REPO_ROOT / "experiments" / "batches"

LEGACY_KEYS: tuple[str, ...] = (
    "married",
    "unmarried",
    "commute",
    "transport",
    "replacement",
    "additional",
    "within_6_months",
    "easy-to-drive",
)
"""需求方点名的废弃键。用于报告里**独立统计**一行，不参与判据（判据就是合法键集合）。"""


def _payloads(run_dir: Path) -> Iterable[dict[str, Any]]:
    """该案所有发出去的 ``update-profile`` 请求体。"""
    path = run_dir / "attempts.json"
    if not path.is_file():
        return
    for attempt in json.loads(path.read_text(encoding="utf-8")):
        if "update-profile" not in (attempt.get("target") or ""):
            continue
        body = (attempt.get("request_json") or {}).get("request_body") or {}
        if isinstance(body, dict):
            yield body


def _items(attribute: str, value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_items(attribute, item))
        return out
    if isinstance(value, dict):
        key = value.get("key")
        return [str(key)] if key else []
    text = "" if value is None else str(value).strip()
    if not text:
        return []
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            return [text]
        if isinstance(parsed, dict) and parsed.get("key"):
            return [str(parsed["key"])]
        return [text]
    if inf.is_multi_value(attribute) and "||" in text:
        return [part.strip() for part in text.split("||") if part.strip()]
    return [text]


def scan_case(run_dir: Path) -> dict[str, list]:
    found: dict[str, list] = {"mappable": [], "illegal": [], "legacy_named": []}
    seen: set[tuple[str, str]] = set()
    for body in _payloads(run_dir):
        analysis = body.get("analysis_result") or {}
        for entry in analysis.get("basic_notes_updates") or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("attribute_key") or "")
            if not inf.is_enum(key):
                continue
            for item in _items(key, entry.get("attribute_value")):
                sig = (key, item)
                if sig in seen:
                    continue
                seen.add(sig)
                if item in inf.legal_keys(key):
                    continue
                alias = inf.legacy_alias(key, item)
                record = {"attribute": key, "value": item, "alias": alias}
                (found["mappable"] if alias else found["illegal"]).append(record)
                if item in LEGACY_KEYS:
                    found["legacy_named"].append(record)
    return found


def scan_batch(batch: str) -> dict[str, Any]:
    root = BATCH_ROOT / batch
    if not root.is_dir():
        raise SystemExit(f"批次目录不存在：{root}")
    runs = root / "runs"
    if not runs.is_dir():
        raise SystemExit(f"批次没有 runs 目录：{runs}")
    per_case: dict[str, dict[str, list]] = {}
    for run_dir in sorted(p for p in runs.iterdir() if p.is_dir()):
        result = scan_case(run_dir)
        if any(result.values()):
            per_case[run_dir.name] = result
    totals = defaultdict(int)
    for result in per_case.values():
        for kind, items in result.items():
            totals[kind] += len(items)
    return {
        "batch": batch,
        "cases": len(list(p for p in runs.iterdir() if p.is_dir())),
        "affected": len(per_case),
        "totals": dict(totals),
        "detail": per_case,
    }


def _print_human(report: dict[str, Any]) -> None:
    t = report["totals"]
    print(f"批次 {report['batch']}：{report['cases']} 案，{report['affected']} 案有问题")
    print(f"  点名废弃键（{', '.join(LEGACY_KEYS)}）：{t.get('legacy_named', 0)} 处")
    print(f"  可映射旧键（产出侧漏了归一）：{t.get('mappable', 0)} 处")
    print(f"  非法取值（中文当值等）：{t.get('illegal', 0)} 处")
    for phone, result in report["detail"].items():
        print(f"\n### {phone}")
        for kind, label in (
            ("legacy_named", "点名废弃键"),
            ("mappable", "可映射"),
            ("illegal", "非法"),
        ):
            for item in result[kind]:
                extra = f" → {item['alias']}" if item.get("alias") else ""
                print(f"  [{label}] {item['attribute']} = {item['value']!r}{extra}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = scan_batch(args.batch)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    totals = report["totals"]
    return 1 if totals.get("mappable") or totals.get("illegal") else 0


if __name__ == "__main__":
    raise SystemExit(main())
