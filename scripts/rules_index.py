"""按编号或簇列出规则的全部落点。

「删一条规则」应当是机械动作（设计 §9.2）：按编号检索 → 得到落点清单 → 逐处删除。
手工 `grep` 会漏掉提示词里的成对标记与数据表里的并行登记键，这个脚本把它们一次列全。

用法：

    python scripts/rules_index.py                # 全部规则与落点
    python scripts/rules_index.py INCOME-01      # 单条规则
    python scripts/rules_index.py --cluster hard # 整簇
    python scripts/rules_index.py --problems     # 只列守卫发现的问题
    python scripts/rules_index.py --table        # 登记表全貌（含状态与旧裸串）

退出码：``--problems`` 模式下发现问题返回 1，便于挂进 CI。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile.rules import default as default_registry  # noqa: E402
from customer_profile.rules import guard as gd  # noqa: E402


def _print_rule(rule_id: str, marks, out=None) -> None:
    stream = out or sys.stdout
    entries = marks.get(rule_id, [])
    if not entries:
        print(f"{rule_id}: 无落点", file=stream)
        return
    print(f"{rule_id}: {len(entries)} 处落点", file=stream)
    for mark in entries:
        print(f"  {mark.path}:{mark.line} [{mark.kind}]", file=stream)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="规则落点索引")
    parser.add_argument("rule_id", nargs="?", help="只看这一条规则")
    parser.add_argument("--cluster", help="只看这一簇")
    parser.add_argument("--problems", action="store_true", help="只列守卫发现的问题")
    parser.add_argument("--table", action="store_true", help="打印登记表全貌")
    args = parser.parse_args(argv)

    table = default_registry()
    marks = gd.index_by_rule(gd.scan_marks())

    if args.table:
        header = f"{'id':<14}{'状态':<12}{'内容类':<14}{'通道':<24}{'作用域'}"
        print(header)
        print("-" * len(header))
        for rule in table.all():
            print(
                f"{rule.id:<14}{rule.status:<12}{rule.content_kind:<14}"
                f"{','.join(rule.channels):<24}{','.join(rule.scope)}"
            )
        return 0

    if args.problems:
        raw = gd.scan_marks()
        orphans = gd.orphan_marks(raw, table)
        unlanded = gd.unlanded_rules(raw, table)
        for mark in orphans:
            print(f"孤儿标记：{mark}")
        for rule_id in unlanded:
            print(f"未落地规则：{rule_id}")
        print(f"共 {len(orphans)} 处孤儿标记，{len(unlanded)} 条未落地规则")
        return 1 if (orphans or unlanded) else 0

    targets = []
    if args.rule_id:
        rule = table.get(args.rule_id)
        if rule is None:
            print(f"规则 {args.rule_id!r} 未登记")
            return 2
        targets = [rule]
    elif args.cluster:
        targets = [r for r in table.all() if r.cluster == args.cluster]
        if not targets:
            print(f"簇 {args.cluster!r} 下没有规则")
            return 2
    else:
        targets = list(table.all())

    for rule in targets:
        _print_rule(rule.id, marks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
