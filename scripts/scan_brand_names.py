#!/usr/bin/env python
"""品牌名残留守卫的命令行入口（T3-1）。

## 它回答什么

「提示词与代码里还有没有具体品牌/车型名，出现在不该出现的位置。」

判据与豁免机制见 :mod:`customer_profile.knowledge.brand_guard`。这里只是薄壳：
解析参数、打印、给退出码。

## 为什么用退出码而不是只打印

接进 CI 或提交前检查时，需要一条「红/绿」信号。退出码 1 表示有未豁免的命中**或**
有已失效的豁免条目——后者同样要拦：过期豁免会让守卫悄悄变松。

## 用法

    python scripts/scan_brand_names.py            # 人读报告
    python scripts/scan_brand_names.py --json     # 机器读
    python scripts/scan_brand_names.py --root src/customer_profile/templates
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile.knowledge import brand_guard as bg  # noqa: E402


def _print_human(report: dict, root: Path) -> None:
    hits = report["hits"]
    stale = report["stale_exemptions"]
    print(f"扫描范围：{root}")
    print(f"品牌词表：{report['brand_count']} 个（本方称谓 + 外部品牌）")
    if hits:
        print(f"\n未豁免的品牌名 {len(hits)} 处：")
        for item in hits:
            print(f"  {item['path']}:{item['line']}  [{item['brand']}]  {item['text']}")
    if stale:
        print(f"\n已失效的豁免 {len(stale)} 条（该处品牌名已不存在，条目应删除）：")
        for item in stale:
            print(f"  {item['path']}  [{item['brand']}]  {item['reason']}")
    if not hits and not stale:
        print("\n[OK] 无未豁免命中，无失效豁免")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--root",
        default="",
        help="扫描根目录（缺省 src/customer_profile）",
    )
    parser.add_argument(
        "--no-stale-check",
        action="store_true",
        help="不检查失效豁免（只在临时目录上跑时使用）",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else bg.DEFAULT_SCAN_ROOT
    hits = bg.scan(root)
    stale = [] if args.no_stale_check else bg.uncovered_exemptions(root)
    report = bg.report(hits, stale)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report, root)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
