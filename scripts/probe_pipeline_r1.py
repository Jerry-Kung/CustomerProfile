"""R1 探针核对：把实验臂批次产物按「已知正确答案」逐案对一遍（只读，不出网）。

用法::

    python scripts/probe_pipeline_r1.py --batch-dir experiments/abexperiments/batches/A-pipeline-R1-20261010

期望表来自 `docs/business_opt01/落地计划书.md` §5.4（13 案探针集）。核对项分两类：

- **可自动判定**：字段在不在、值等于什么、单选字段有没有被写成多值。
- **需人读**：如「不得引用中间画像自证」，脚本只把证据列出来供人看，不下结论。

**本脚本不写任何文件**，结论由人工整理进 `docs/business_opt01/R1结果.md`。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

# 探针集期望（`落地计划书.md` §5.4）
PROBES: dict[str, list[str]] = {
    "16761543238": ["family_structure 有值", "customer_name 不得再是「客户3238」形态"],
    "13926073386": ["「我们单位」不作客户职业", "企业已注销不得填 company/company_type"],
    "13530559591": ["有外呼转写 → 不得输出「无外呼录音」", "单选字段不得多值"],
    "15818891947": ["purchase_type=replacement", "old_car_model 落地"],
    "15308826588": ["有旧车证据 → purchase_type 应为 replacement"],
    "13535737221": ["微信未声明性别 → title 不得凭空断定"],
    "13702911703": ["等级两处一致"],
    "13719210972": ["customer_name 不得是「客户XXXX」占位"],
    "16541242243": ["family_structure 应为合法枚举键"],
    "18003012053": ["单选字段不得多值"],
    "18675756738": ["old_car_model 与 purchase_type 一致"],
    "18902281339": ["occupation 不得是长句自由表述（应归一到键或留白）"],
    "18906860015": ["无特定断言，作为对照组"],
}

MULTI_VALUE_KEYS = {"purchase_purpose", "core_concerns", "driving_preference", "emotion", "frequent_social_media"}


def load_notes(result_path: Path) -> dict[str, Any]:
    raw = json.loads(result_path.read_text(encoding="utf-8"))
    result1 = raw.get("result1")
    payload = json.loads(result1) if isinstance(result1, str) else result1
    entries = (((payload or {}).get("analysis_result") or {}).get("basic_notes_updates")) or []
    return {entry.get("attribute_key"): entry.get("attribute_value") for entry in entries}


def main() -> int:
    parser = argparse.ArgumentParser(description="R1 探针核对（只读）")
    parser.add_argument("--batch-dir", required=True)
    args = parser.parse_args()

    runs = Path(args.batch_dir) / "runs"
    if not runs.is_dir():
        print(f"批次目录不存在：{runs}")
        return 2

    for phone, expectations in PROBES.items():
        result_path = runs / phone / "result.json"
        print(f"\n=== {phone} ===")
        for item in expectations:
            print(f"  期望：{item}")
        if not result_path.is_file():
            print("  [!] 产物缺失")
            continue
        notes = load_notes(result_path)
        print(f"  Notes 字段数：{len(notes)}")
        for key in ("customer_name", "title", "occupation", "company", "company_type", "purchase_type", "old_car_model", "family_structure", "age"):
            if key in notes:
                print(f"    {key} = {json.dumps(notes[key], ensure_ascii=False)[:80]}")
        # 单选字段被写成多值 = 契约违约
        for key, value in notes.items():
            if key not in MULTI_VALUE_KEYS and isinstance(value, str) and "||" in value:
                print(f"    [!] 单选字段出现多值：{key} = {value[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
