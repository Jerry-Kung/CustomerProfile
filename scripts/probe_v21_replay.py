"""v2.1 整改的**离线回放**验收探针（0 token，不出网，只读）。

与 ``probe_v21_audit.py`` 的分工：那个读**批次产物**（反映改动前的历史产物），
本探针把 L8 批次的 L1 断言账本**喂回当前代码**重算，因此能测出本次代码改动的效果。

    python -I scripts/probe_v21_replay.py
    python -I scripts/probe_v21_replay.py --batch experiments/abexperiments/batches/A-pipeline-L8-20261012

口径：只读批次里 ``pipeline_l1_localize.claims_json``（定位后的断言账本，带
``quote`` / ``basis`` / ``channel``），走一遍 ``localize_claims``（再次定坐标，
喂回时渠道已合法）→ ``project_claims`` → 统计可读性与覆盖读数。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from customer_profile.workflows import customer_profile_pipeline as pipe  # noqa: E402

DEFAULT = REPO / "experiments/abexperiments/batches/A-pipeline-L8-20261012/runs"

AUDIT_WORDS = (
    "归属过滤", "来源分级", "枚举归一", "单多值约束", "契约校验",
    "未知渠道", "社交自呈现", "一手交互", "第三方预测", "公开登记查询", "人工/系统登记",
    "按来源分级投影得到", "断言",
)


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
            index.setdefault(item.get("node_id") or item.get("id"), item.get("outputs") or {})
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v2.1 整改离线回放探针")
    parser.add_argument("--batch", default=str(DEFAULT))
    parser.add_argument("--tier", default="sufficient")
    args = parser.parse_args(argv)

    root = Path(args.batch)
    if not root.is_dir() and (root / "runs").is_dir():
        root = root / "runs"
    if not root.is_dir():
        print("[FAIL] 找不到批次：%s" % root)
        return 2

    sys.stdout.reconfigure(encoding="utf-8")

    hits: Counter = Counter()
    notes_total = evidence_total = unknown = with_quote = 0
    notes_per_case: dict[str, int] = {}
    empty_claims: Counter = Counter()
    basis_counter: Counter = Counter()

    for phone in sorted(p.name for p in root.iterdir() if p.is_dir()):
        index = nodes_index(root / phone / "nodes.json")
        # **必须读 L1 原始输出**（``pipeline_l1_llm``），不能读 ``pipeline_l1_localize``
        # 的产物——后者已经是「定位后」的账本，fact 早被当轮降级成 inferred_low，
        # 再喂回来只会测出「降级后的档位仍是降级后的档位」，永远得不到 fact/inferred_high。
        raw_node = index.get("pipeline_l1_llm") or {}
        claims_json = raw_node.get("parsed_json") or raw_node.get("result")
        if not claims_json:
            localize = index.get("pipeline_l1_localize") or {}
            claims_json = localize.get("claims_json")
        if not claims_json:
            continue
        claims = jload(claims_json) or {}
        claims = (claims.get("fields") if isinstance(claims, dict) else claims) or []

        # 喂回定位层（渠道已合法，等于把 quote 补齐）
        records = jload((index.get("pipeline_l0_records") or {}).get("records_json")) or []
        relocalized = pipe.localize_claims(
            json.dumps(records, ensure_ascii=False),
            json.dumps({"fields": claims}, ensure_ascii=False),
        )
        result = pipe.project_claims(relocalized["claims_json"], tier=args.tier)
        notes = jload(result["notes_updates_json"]) or []
        notes_per_case[phone] = len(notes)

        for name in jload(result.get("empty_claim_names") or "[]"):
            empty_claims[name] += 1
        for entry in notes:
            notes_total += 1
            detail = entry.get("reasoning_detail") or {}
            blob = json.dumps(
                {"summary": detail.get("summary"), "steps": detail.get("steps")},
                ensure_ascii=False,
            )
            for word in AUDIT_WORDS:
                if word in blob:
                    hits[word] += 1
            for snippet in detail.get("evidence") or []:
                evidence_total += 1
                text = str(snippet)
                if "未知渠道" in text:
                    unknown += 1
                if "：" in text:
                    with_quote += 1
        for basis in (jload(result["basis_json"]) or {}).values():
            basis_counter[basis] += 1

        # 空值占位率：用**代码侧的真实判据**（`_blank_value`），而不是本探针另立一套口径
        # ——两套口径各写各的，就会出现「探针说没有、代码说拒了」的假分歧。
        for raw in claims:
            if pipe._blank_value(raw.get("value")):
                empty_claims[str(raw.get("attribute") or "")] += 1

    print("=== v2.1 回放验收（L1 断言喂回当前代码） ===")
    print("逐案 Notes 条数:", notes_per_case)
    print("Notes 条数合计:", notes_total)
    print("审计词命中:", hits.most_common())
    print("证据串 %d：含「未知渠道」 %d，含「渠道：原文」 %d" % (evidence_total, unknown, with_quote))
    print("空值占位断言被拒:", empty_claims.most_common(12))
    print("字段级 basis 分布:", basis_counter.most_common())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
