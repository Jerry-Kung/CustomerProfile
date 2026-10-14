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
EXTENSION = REPO_ROOT / "src" / "customer_profile" / "notes_contract_extension.json"
"""**补充字段**：基线快照固化之后线上契约新增的字段。

基线批次冻结的是 42 字段那一版；其后线上 `note-attributes` 增了「竞品对比」一类
（`competing_brand` / `competing_model` / `test_driven_models` / `brand_concerns`），
基线产物里自然没有。整表改用线上 45 字段会**丢掉** 16 个现役字段
（`old_car_model` 被确定性注入依赖、`intended_brand` 被 18 处引用），因此走**增量补挂**：
以基线快照为底，只把补充文件里的字段追加进去。

补充文件的字段定义逐字取自线上契约（`GET /api/v1/remote/config/note-attributes`），
不含任何客户数据，可以入库。
"""

HEADER = '''"""Notes 字段契约快照（``GET /api/v1/remote/config/note-attributes``）。

**本文件由 :mod:`scripts.snapshot_note_contract` 自动生成，请勿手改。**
重新生成：``python scripts/snapshot_note_contract.py``。

契约实测跨 **39/39 案完全恒定**（payload 签名 ``{signature}``），共 **{count}** 个字段、
**{category_count}** 个分类（{category_summary}）。因此固化为离线快照，运行期不再取接口。
{extension_note}
两条使用口径：

- **枚举归一的唯一依据**。字段的合法键、是否多选、结构化枚举要填的 ``input_value``
  契约字段名，全部从这里读——不散落在规则表里手抄，避免与接口漂移。
- **单选/多选的分野**由 ``multiple_value`` 决定（实测 {single} 单选 / {multi} 多选），
  L2 的「单/多值约束」直接据此强制。
"""

from __future__ import annotations

from typing import Any

SOURCE = "GET /api/v1/remote/config/note-attributes"
SIGNATURE = "{signature}"
CAPTURED_FROM = "{batch_id}"

{category_constants}
CATEGORIES: tuple[str, ...] = (
{categories_tuple}
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


def load_extension(path: Path) -> list[dict]:
    """读补充字段文件（基线与线上契约的差集）。文件不存在时返回空表。"""
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("configs") or [])


def apply_extension(configs: list[dict], extra: list[dict]) -> list[dict]:
    """把补充字段追加到基线字段表**末尾**，同键以基线为准（不覆盖既有字段）。"""
    seen = {str(c.get("attribute_key")) for c in configs}
    out = list(configs)
    for entry in extra:
        key = str(entry.get("attribute_key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def _category_slug(name: str) -> str:
    """分类中文名 → 模块常量名（``基础身份`` → ``BASE_IDENTITY``）。"""
    table = {
        "基础身份": "BASE_IDENTITY",
        "购车需求": "PURCHASE_NEED",
        "预算与金融": "BUDGET_FINANCE",
        "决策与节奏": "DECISION_RHYTHM",
        "个人偏好与备注": "PREFERENCE_NOTE",
        "竞品对比": "COMPETITOR",
    }
    return table.get(name) or "CATEGORY_" + str(abs(hash(name)) % 10000)


def _ordered_categories(configs: list[dict]) -> list[str]:
    """按字段在契约里的出现顺序取分类（不硬编码分类名，新增分类自动跟上）。"""
    out: list[str] = []
    for entry in configs:
        name = str(entry.get("category") or "")
        if name and name not in out:
            out.append(name)
    return out


def _counts(configs: list[dict], pred) -> int:
    """按判定函数统计条数（判定函数接收**整条契约**，自己取需要的键）。"""
    return sum(1 for entry in configs if pred(entry))


def render(payload: dict, signature: str, batch_id: str, *, extended: bool = False) -> str:
    configs = payload["configs"]
    items = ",\n".join(
        "    " + pprint.pformat(entry, width=96, sort_dicts=False, compact=True)
        for entry in configs
    )
    import collections

    cats = _ordered_categories(configs)
    counts = collections.Counter(str(entry.get("category") or "") for entry in configs)
    summary = " / ".join(f"{name} {counts[name]}" for name in cats)
    constants = "\n".join(f'{_category_slug(name)} = "{name}"' for name in cats)
    categories_tuple = "\n".join(f"    {_category_slug(name)}," for name in cats)

    def _is_enum(entry: dict) -> bool:
        return bool(entry.get("enum_options"))

    def _is_multi(entry: dict) -> bool:
        return bool((entry.get("enum_options") or {}).get("multiple_value"))

    def _is_single(entry: dict) -> bool:
        options = entry.get("enum_options")
        return bool(options) and not options.get("multiple_value")

    extension_note = (
        "\n另含**补充字段**（基线固化后线上契约新增，见 "
        "``src/customer_profile/notes_contract_extension.json``）。\n"
        if extended
        else ""
    )
    return HEADER.format(
        signature=signature,
        count=len(configs),
        category_count=len(cats),
        category_summary=summary,
        single=_counts(configs, _is_single),
        multi=_counts(configs, _is_multi),
        category_constants=constants,
        categories_tuple=categories_tuple,
        extension_note=extension_note,
        batch_id=batch_id,
        configs=items,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 note-attributes 契约快照")
    parser.add_argument("--batch-dir", default=str(DEFAULT_BATCH))
    parser.add_argument("--extension", default=str(EXTENSION), help="补充字段文件（JSON）")
    parser.add_argument("--no-extension", action="store_true", help="只用基线快照，不补字段")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    batch_dir = Path(args.batch_dir)
    payload, signature = load_payload(batch_dir)
    extra = [] if args.no_extension else load_extension(Path(args.extension))
    if extra:
        payload = dict(payload)
        payload["configs"] = apply_extension(list(payload["configs"]), extra)
    text = render(payload, signature, batch_dir.name, extended=bool(extra))

    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current == text:
            print(
                f"[OK] 快照与批次一致（signature={signature}, {len(payload['configs'])} 字段"
                f"{'，含补充' if extra else ''}）"
            )
            return 0
        print("[FAIL] 快照与批次不一致，请运行 python scripts/snapshot_note_contract.py")
        return 1

    OUTPUT.write_text(text, encoding="utf-8", newline="\n")
    print(
        f"已写入 {OUTPUT}（signature={signature}, {len(payload['configs'])} 字段"
        f"{'，含补充 %d 个' % len(extra) if extra else ''}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
