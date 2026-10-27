#!/usr/bin/env python
"""对客产出英文残留扫描：把一个批次的结果分成「真残留 / 白名单 / 待人工」三类。

## 它回答什么

「回传的画像结果里还有多少英文是**不该有**的。」

只看正文（``summary`` / ``steps`` / ``evidence`` / ``label`` / section 文本）。
``attribute_key`` / ``attribute_value`` / ``id`` / ``key`` 是契约字段，**不在扫描范围**
——枚举取值按契约必须填英文键，前端靠 ``display_name`` 映射（治理方案 §7）。

## 三分类口径

1. **契约键（不计）**：正文中出现的串若恰是某字段的合法枚举键（``power`` / ``male``），
   记作「可改」——这正是 ``EXPR-01`` 要处理的对象。改造后应为 0。
2. **白名单（不计）**：品牌、车型、技术缩写、等级代号（``M817`` / ``SUV`` / ``ADS`` /
   ``to B`` / ``A``～``H``）。判据见 ``ALLOWLIST`` 与「中文语境中的字母等级」两条规则。
3. **待人工（列出）**：既不是枚举键、也不在白名单里的英文串（客户原话片段、模型编的
   缩写等）。**不判违规**——客户原话（``Wi，0950刘总转介绍``）不得被改写。

## 用法

    python scripts/scan_english_residue.py --batch english-residue-8-20261026
    python scripts/scan_english_residue.py --batch uat-18-20261026 --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile import internal_fields as inf  # noqa: E402

BATCH_ROOT = REPO_ROOT / "experiments" / "batches"

TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

CONTRACT_LEAVES: frozenset[str] = frozenset(
    {"attribute_key", "attribute_value", "id", "key", "batch_id", "phone", "phone_number"}
)
"""这些路径末端的值是契约字段，不参与判定。"""

ALLOWLIST: frozenset[str] = frozenset(
    {
        # 品牌 / 车型
        "BMW", "AUDI", "BYD", "Tesla", "AION", "TESLA",
        "M817", "M8T7", "M82", "M6", "M7", "G919", "N90", "R7", "Z9", "Z9GT", "G2",
        # 版本与配置
        "Max", "Pro", "Plus", "SE", "max", "pro", "Air", "GT",
        # 技术缩写
        "SUV", "MPV", "OTA", "ADAS", "ADS", "VLA", "EV", "PHEV", "DM", "DMi",
        "APP", "Wi", "WiFi", "Wi-Fi", "iOS", "Android", "HarmonyOS", "NFC",
        # 行业通行说法
        "to", "B", "b", "IT", "Ultra", "ultra",
        # 手机与生态
        "vivo", "oppo", "OPPO", "iPhone",
    }
)
"""品牌、车型、技术缩写与行业通行说法。判据刻意宽松——**误报比漏报便宜**：
这张表的用处是让报告读起来聚焦，不是裁决；真正的拦截判据在 ``EXPR-01``（按枚举键）。"""

GRADE = re.compile(r"^[A-H]$")
"""单字母等级（``A``/``B``/``C``/``H``/``F``）。商机等级原样回传，不是残留。"""


def _walk(node: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value, path)
    elif isinstance(node, str):
        yield path, node


def _result_of(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "result.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    payload = (data.get("outputs") or {}).get("result1") or data.get("result1")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    return payload if isinstance(payload, dict) else None


def scan_result(payload: dict[str, Any], *, index: dict[str, str]) -> dict[str, list]:
    """对单案结果做三分类。返回 ``{"enum_keys": [...], "pending": [...]}``。"""
    analysis = payload.get("analysis_result") or payload
    found: dict[str, list] = {"enum_keys": [], "pending": []}
    seen: set[tuple[str, str]] = set()
    for path, text in _walk(analysis):
        leaf = path.rsplit("/", 1)[-1].split("[")[0]
        if leaf in CONTRACT_LEAVES:
            continue
        for token in TOKEN.findall(text):
            if token in ALLOWLIST or GRADE.match(token):
                continue
            key = (path, token)
            if key in seen:
                continue
            seen.add(key)
            if token in index:
                found["enum_keys"].append({"path": path, "token": token, "display": index[token]})
            else:
                found["pending"].append({"path": path, "token": token})
    return found


def scan_batch(batch: str) -> dict[str, Any]:
    root = BATCH_ROOT / batch
    if not root.is_dir():
        raise SystemExit(f"批次目录不存在：{root}")
    index = inf.enum_display_index()
    per_case: dict[str, dict[str, list]] = {}
    for run_dir in sorted((root / "runs").iterdir()):
        if not run_dir.is_dir():
            continue
        payload = _result_of(run_dir)
        if payload is None:
            continue
        result = scan_result(payload, index=index)
        if result["enum_keys"] or result["pending"]:
            per_case[run_dir.name] = result

    totals = {
        "cases": len(per_case),
        "enum_keys": sum(len(v["enum_keys"]) for v in per_case.values()),
        "pending": sum(len(v["pending"]) for v in per_case.values()),
    }
    return {"batch": batch, "totals": totals, "cases": per_case}


def _print_human(report: dict[str, Any]) -> None:
    totals = report["totals"]
    print(f"批次 {report['batch']}")
    print(f"  有英文的案数：{totals['cases']}")
    print(f"  枚举键（可改，EXPR-01 的作用对象）：{totals['enum_keys']}")
    print(f"  待人工确认（非键非白名单）：{totals['pending']}")
    for name, result in report["cases"].items():
        print(f"\n### {name}")
        for item in result["enum_keys"][:10]:
            print(f"  [可改] {item['path']}  {item['token']} → {item['display']}")
        for item in result["pending"][:10]:
            print(f"  [待人工] {item['path']}  {item['token']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True, help="experiments/batches 下的批次目录名")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args(argv)

    report = scan_batch(args.batch)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    # 退出码 1 表示仍有可改的枚举键——可直接用于验收与 CI。
    return 1 if report["totals"]["enum_keys"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
