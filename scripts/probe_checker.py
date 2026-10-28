"""checker 通道的离线回放：对基线批次逐案组装送检清单，不发起任何模型调用。

用途是 M5 的验收证据与「该挂几条规则」的盘算：

- 哪些案子**会**被送检（规则涉及的两个字段都有确定取值）；
- 哪些**不会**（字段缺失 / 占位串）——送检次数应当与这些成反比，否则是组装粒度出错；
- 提示词规模（字符数）。它决定每次调用的成本，也是「该拆簇」的信号。

**缺省不调用模型**：花的是真金白银，且基线不是受控基线，多跑一次不会让结论更硬。
要实测判决质量，用 ``--live``（需自行确认配额）。

用法：

    python scripts/probe_checker.py
    python scripts/probe_checker.py --case 19972120792
    python scripts/probe_checker.py --live --limit 2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile import readable as rdb  # noqa: E402
from customer_profile.rules import checker  # noqa: E402

DEFAULT_BATCH = REPO_ROOT / "experiments/uat_data/uat-18-20261026"
TEMPLATE = "customer_profile_pipeline__pipeline_rule_check"


def _outputs(case: Path) -> dict[str, Any]:
    """取该案 ``pipeline_l2_project`` 的输出；节点缺失或失败返回空 dict。"""
    path = case / "nodes.json"
    if not path.is_file():
        return {}
    nodes = json.loads(path.read_text(encoding="utf-8"))
    for entry in nodes:
        if entry.get("node_id") == "pipeline_l2_project" and entry.get("status") == "succeeded":
            return entry.get("outputs") or {}
    return {}


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value if value is not None else {}


def _prompt_chars(brief_list, facts) -> int:
    from customer_profile.templates import TemplateRepository

    return len(
        TemplateRepository().render(
            TEMPLATE,
            {
                "arg1": rdb.readable_facts(dict(facts)),
                "arg2": json.dumps(brief_list, ensure_ascii=False, indent=2),
            },
        )
    )


async def _live(facts, brief_list) -> str:
    """真调一次模型，返回一句话结论。只给人工核读用，不进任何判定。"""
    from customer_profile.runner import build_service

    service = await build_service(enable_store=False)
    try:
        out = await checker.run_check(
            facts_json=json.dumps(facts, ensure_ascii=False),
            llm=service.runtime.llm,
            enabled=True,
            registry=None,
        )
    finally:
        await service.aclose()
    return out["checker_status"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", default=str(DEFAULT_BATCH))
    parser.add_argument("--case", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--live", action="store_true", help="真调模型（消耗配额）")
    args = parser.parse_args()

    batch = Path(args.batch)
    cases = sorted(p for p in (batch / "runs").iterdir() if p.is_dir())
    if args.case:
        cases = [p for p in cases if p.name == args.case]
    if args.limit:
        cases = cases[: args.limit]

    sent = 0
    for case in cases:
        outputs = _outputs(case)
        facts = _json(outputs.get("facts_json"))
        if not isinstance(facts, dict) or not facts:
            print(f"{case.name}  无 l2_project 产物，跳过")
            continue
        brief_list = checker.briefs(facts)
        if not brief_list:
            print(f"{case.name}  不送检（规则涉及字段有缺失或占位）")
            continue
        sent += 1
        detail = ", ".join(
            f"{item['rule']}→" + "／".join(f"{f['name']}={f['value']}" for f in item["fields"])
            for item in brief_list
        )
        chars = _prompt_chars(brief_list, facts)
        line = f"{case.name}  送检 {len(brief_list)} 条 | 提示词 {chars} 字符 | {detail}"
        if args.live:
            line += f" | live={_live(facts, brief_list)}"
        print(line)

    print(f"\n{len(cases)} 案中 {sent} 案送检；每案建议调用 ≤ {checker.CHECKER_BUDGET} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
