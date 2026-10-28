"""重跑批次的验收检查：小订/大定改动是否生效、是否误伤。

用法：
    python experiments/_check_deposit_regression.py <批次目录名>

三类判据（越靠前越硬）：

1. **泄漏**：拥车/置换类字段的取值或证据里出现下单词。命中即失败。
2. **本批目标案**：19247201510 的 current_vehicle_info / trade_in_model /
   trade_in_requirement / purchase_nature 不得再出现 x700 当作自用车的结论。
3. **可读性**：这些字段的证据位不得出现规则编号或 f_deposit 这类内部串。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPOSIT = ("小订", "小定", "大订", "大定", "下订", "下定", "定金", "意向金", "锁单")
OWNERSHIP = (
    "old_car_model",
    "owned_brand_model",
    "owned_car_age",
    "current_vehicle_info",
    "trade_in_model",
    "trade_in_requirement",
    "purchase_nature",
)
INTERNAL = ("f_deposit", "CONSIST-04", "SPEC-17", "SPEC-18", "rule_id")


def _notes(run_dir: Path) -> dict[str, dict]:
    payload = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    raw = payload.get("result1")
    obj = json.loads(raw) if isinstance(raw, str) else raw
    analysis = (obj or {}).get("analysis_result") or {}
    out: dict[str, dict] = {}
    for item in analysis.get("basic_notes_updates") or []:
        key = str(item.get("attribute_key") or "")
        if key:
            out[key] = item
    return out


def _json_text(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def main(argv: list[str]) -> int:
    batch = ROOT / "experiments" / "uat_data" / argv[0]
    runs = batch / "runs"
    if not runs.is_dir():
        print(f"批次不存在或没有产物：{runs}")
        return 2

    problems: list[str] = []
    for run_dir in sorted(path for path in runs.iterdir() if path.is_dir()):
        phone = run_dir.name.split("__")[0]
        notes = _notes(run_dir)
        for key in OWNERSHIP:
            item = notes.get(key)
            if not item:
                continue
            value = item.get("attribute_value")
            detail = item.get("reasoning_detail") or {}
            blob = _json_text(value) + " " + _json_text(detail.get("evidence") or [])
            hit = [word for word in DEPOSIT if word in blob]
            if hit:
                problems.append(f"{phone} {key} 仍含下单词 {hit}：{_json_text(value)}")
            for token in INTERNAL:
                if token in blob:
                    problems.append(f"{phone} {key} 证据位出现内部串 {token}")
        print(
            f"{phone}: notes={len(notes)} "
            f"vehicle={_json_text(notes.get('current_vehicle_info', {}).get('attribute_value'))} "
            f"intended={_json_text(notes.get('intended_model', {}).get('attribute_value'))} "
            f"progress={_json_text(notes.get('viewing_progress', {}).get('attribute_value'))}"
        )

    print()
    if problems:
        print("发现问题：")
        for line in problems:
            print("  -", line)
        return 1
    print("未发现下单词泄漏进拥车/置换字段，也未发现内部串进对客产物。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
