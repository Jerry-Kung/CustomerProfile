"""把 ``note-attributes`` 契约固化成可入库的 Python 快照模块。

数据源是基线批次 ``experiments/batches/baseline-39-dsv41-noThink`` 里该接口的响应。
实测 39 案的 payload **完全一致**（sha256 前 12 位 ``0479e326bc88``），因此可以固化成
离线快照，运行期不必再取——这既是 `落地方案.md` 阶段二的既定做法，也让 L2 投影层
在无网络、无凭据的情况下可被确定性测试。

为什么输出成 ``.py`` 而不是 ``.json``：``src/data/`` 命中 ``.gitignore:232`` 的 ``data/``
规则被忽略，放那儿的快照进不了库；而写成模块可直接 import，没有路径解析、没有读盘，
也就没有「快照文件找不到」这类运行期失败模式。中文在源码里是字面量，编码由文件本身保证。

用法::

    python scripts/snapshot_note_contract.py            # 重新生成
    python scripts/snapshot_note_contract.py --check    # 只校验现有快照与批次一致
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pprint
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_BATCH = REPO_ROOT / "experiments" / "batches" / "baseline-39-dsv41-noThink"
OUTPUT = REPO_ROOT / "src" / "customer_profile" / "notes_contract.py"

HEADER = '''"""Notes 字段契约快照（``GET /api/v1/remote/config/note-attributes``）。

**本文件由 :mod:`scripts.snapshot_note_contract` 自动生成，请勿手改。**
重新生成：``python scripts/snapshot_note_contract.py``。

契约实测跨 **39/39 案完全恒定**（payload 签名 ``{signature}``），共 **{count}** 个字段、
**5** 个分类（{category_summary}）。因此固化为离线快照，运行期不再取接口。

两条使用口径：

- **枚举归一的唯一依据**。字段的合法键、是否多选、结构化枚举要填的 ``input_value``
  契约字段名，全部从这里读——不散落在规则表里手抄，避免与接口漂移。
- **单选/多选的分野**由 ``multiple_value`` 决定（实测 16 单选 / 5 多选），
  L2 的「单/多值约束」直接据此强制。
"""

from __future__ import annotations

from typing import Any

SOURCE = "GET /api/v1/remote/config/note-attributes"
SIGNATURE = "{signature}"
CAPTURED_FROM = "{batch_id}"

BASE_IDENTITY = "基础身份"
PURCHASE_NEED = "购车需求"
BUDGET_FINANCE = "预算与金融"
DECISION_RHYTHM = "决策与节奏"
PREFERENCE_NOTE = "个人偏好与备注"

CATEGORIES: tuple[str, ...] = (
    BASE_IDENTITY,
    PURCHASE_NEED,
    BUDGET_FINANCE,
    DECISION_RHYTHM,
    PREFERENCE_NOTE,
)

RAW_CONFIGS: tuple[dict[str, Any], ...] = (
{configs}
)

_INDEX: dict[str, dict[str, Any]] = {{c["attribute_key"]: c for c in RAW_CONFIGS}}

ALL_KEYS: tuple[str, ...] = tuple(c["attribute_key"] for c in RAW_CONFIGS)

IDENTITY_KEYS: frozenset[str] = frozenset(
    c["attribute_key"] for c in RAW_CONFIGS if c["category"] == BASE_IDENTITY
)


def by_key(attribute_key: str) -> dict[str, Any] | None:
    """按字段键取契约条目。"""
    return _INDEX.get(attribute_key)


def category_of(attribute_key: str) -> str | None:
    entry = _INDEX.get(attribute_key)
    return entry["category"] if entry else None


def enum_options(attribute_key: str) -> dict[str, Any] | None:
    """取枚举定义（``{{"items": {{...}}, "multiple_value": bool}}``）；非枚举返回 ``None``。"""
    entry = _INDEX.get(attribute_key)
    return ((entry or {{}}).get("enum_options")) or None


def is_enum(attribute_key: str) -> bool:
    return enum_options(attribute_key) is not None


def legal_keys(attribute_key: str) -> tuple[str, ...]:
    """该字段的合法枚举键。"""
    options = enum_options(attribute_key) or {{}}
    return tuple((options.get("items") or {{}}).keys())


def is_multi_value(attribute_key: str) -> bool:
    """是否允许多值（``multiple_value=true``）。"""
    options = enum_options(attribute_key) or {{}}
    return bool(options.get("multiple_value"))


def is_single_value(attribute_key: str) -> bool:
    """是否单选（枚举字段且 ``multiple_value`` 非真）。"""
    options = enum_options(attribute_key)
    return bool(options) and not options.get("multiple_value")


def display_map(attribute_key: str) -> dict[str, str | None]:
    """``enum key -> 展示名``。结构化项取其 ``display_name``。"""
    out: dict[str, str | None] = {{}}
    for key, value in ((enum_options(attribute_key) or {{}}).get("items") or {{}}).items():
        if isinstance(value, str):
            out[key] = value
        elif isinstance(value, dict):
            out[key] = value.get("display_name")
    return out


def display_to_key(attribute_key: str, display: str) -> str | None:
    """展示名反向查键（用于把「油耗」这类中文值归一到 ``fuel_efficiency``）。"""
    target = (display or "").strip()
    if not target:
        return None
    for key, name in display_map(attribute_key).items():
        if name == target:
            return key
    return None


def structured_spec(attribute_key: str, enum_key: str) -> str | None:
    """结构化枚举项要求随带填写的 ``input_value`` **契约字段名**；非结构化返回 ``None``。

    例：``driver_license_status.yes`` 返回 ``"驾龄年限"``，
    ``family_structure.has_children`` 返回 ``"孩子人数"``。
    """
    item = ((enum_options(attribute_key) or {{}}).get("items") or {{}}).get(enum_key)
    if isinstance(item, dict):
        return item.get("input_value")
    return None


def is_structured(attribute_key: str, enum_key: str) -> bool:
    return structured_spec(attribute_key, enum_key) is not None
'''


def load_payload(batch_dir: Path) -> tuple[dict, str]:
    """从批次产物里取该接口的响应体，并返回其签名。"""
    runs = batch_dir / "runs"
    if not runs.is_dir():
        raise SystemExit(f"找不到批次产物目录：{runs}")
    for case in sorted(p for p in runs.iterdir() if p.is_dir()):
        attempts = case / "attempts.json"
        if not attempts.exists():
            continue
        for attempt in json.loads(attempts.read_text(encoding="utf-8")):
            target = str(attempt.get("target", ""))
            if attempt.get("kind") == "http" and "note-attributes" in target:
                text = attempt["response_text"]
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                return json.loads(text), digest
    raise SystemExit("批次产物里没有 note-attributes 的响应")


def render(payload: dict, signature: str, batch_id: str) -> str:
    configs = payload["configs"]
    items = ",\n".join(
        "    " + pprint.pformat(entry, width=96, sort_dicts=False, compact=True)
        for entry in configs
    )
    import collections

    counts = collections.Counter(entry["category"] for entry in configs)
    summary = " / ".join(f"{name} {counts[name]}" for name in (
        "基础身份", "购车需求", "预算与金融", "决策与节奏", "个人偏好与备注"
    ))
    return HEADER.format(
        signature=signature,
        count=len(configs),
        category_summary=summary,
        batch_id=batch_id,
        configs=items,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 note-attributes 契约快照")
    parser.add_argument("--batch-dir", default=str(DEFAULT_BATCH))
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    batch_dir = Path(args.batch_dir)
    payload, signature = load_payload(batch_dir)
    text = render(payload, signature, batch_dir.name)

    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current == text:
            print(f"[OK] 快照与批次一致（signature={signature}, {len(payload['configs'])} 字段）")
            return 0
        print("[FAIL] 快照与批次不一致，请运行 python scripts/snapshot_note_contract.py")
        return 1

    OUTPUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"已写入 {OUTPUT}（signature={signature}, {len(payload['configs'])} 字段）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
