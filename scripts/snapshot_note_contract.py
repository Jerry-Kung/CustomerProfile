"""把 ``note-attributes`` 契约固化成可入库的 Python 快照模块。

**两个数据源，用途不同**：

1. ``--from-response <file>`` —— **线上响应的手工取回件**。文件是 HTTP 信封
   （``{"body": "<契约 JSON 字符串>", "headers": ..., "status_code": 200}``），
   契约正文在 ``body`` 里、且是**字符串**而非对象，本脚本负责剥壳。
   **这是契约变更后的唯一正确用法**：取回的响应就是现行契约本身，直接固化即可。
2. ``--batch-dir <dir>``（缺省）—— 基线批次产物里该接口的响应。批次冻结的是
   取回当时的契约版本，**它不会随线上演进**，因此只在「复现历史快照」与
   「两版对照」时使用。

为什么输出成 ``.py`` 而不是 ``.json``：``src/data/`` 命中 ``.gitignore:232`` 的 ``data/``
规则被忽略，放那儿的快照进不了库；而写成模块可直接 import，没有路径解析、没有读盘，
也就没有「快照文件找不到」这类运行期失败模式。中文在源码里是字面量，编码由文件本身保证。

用法::

    python scripts/snapshot_note_contract.py --from-response docs/business_opt01_v3/Notes_new.txt
    python scripts/snapshot_note_contract.py --from-response docs/business_opt01_v3/Notes_new.txt --check
    python scripts/snapshot_note_contract.py --batch-dir <批次目录>    # 复现历史快照

**注意**：走 ``--from-response`` 时**不做增量合并**。线上 payload 已是完整现行契约，
再叠加任何旧快照的补充字段都会产出新老混杂的定义——比直接失败更糟。
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

这是 ``--batch-dir`` 模式的补丁机制：基线批次冻结的那一版契约缺若干字段，而
迁移期不允许整表换新，于是走**增量补挂**（以基线为底，只追加补充文件里基线没有的键）。

**2026-10-15 起该文件已清空**：v2 契约改走 ``--from-response``，线上 payload 本身
就是完整现行契约，不再需要补挂。文件保留是为了让 ``--batch-dir`` 模式仍可复现历史快照。
"""

HEADER = '''"""Notes 字段契约快照（``GET /api/v1/remote/config/note-attributes``）。

**本文件由 :mod:`scripts.snapshot_note_contract` 自动生成，请勿手改。**
重新生成：``python scripts/snapshot_note_contract.py``。

{source_note}

共 **{count}** 个字段、**{category_count}** 个分类（{category_summary}）。因此固化为
离线快照，运行期不再取接口。
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
CAPTURED_FROM = "{source_label}"

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


def _signature_of(configs: list[dict]) -> str:
    """契约内容的稳定签名。

    对 ``configs`` 的**规范化 JSON**取哈希，而不是对原始响应文本取——原始文本的
    空白与信封字段（``headers`` / ``date``）每次都不同，那样的签名只能证明「两次取的是
    同一份字节」，证明不了「契约没变」。这里要的正是后者。
    """
    canonical = json.dumps(configs, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def load_from_response(path: Path) -> tuple[dict, str]:
    """从**手工取回的线上响应**里取契约，并返回其签名。

    文件是 HTTP 信封：``{"body": "<契约 JSON 字符串>", "headers": {...}, "status_code": 200}``。
    契约正文在 ``body`` 里、且是**字符串**——直接 ``json.loads`` 外层只会得到一个
    ``body`` 是字符串的字典，``payload["configs"]`` 随即 KeyError。

    同时兼容裸契约（文件直接就是 ``{"configs": [...]}``）：手工整理过的文件不该被拒。
    """
    if not path.is_file():
        raise SystemExit(f"找不到响应文件：{path}")
    envelope = json.loads(path.read_text(encoding="utf-8"))

    if isinstance(envelope, dict) and "configs" in envelope:
        payload = envelope
    elif isinstance(envelope, dict) and "body" in envelope:
        body = envelope["body"]
        # 老接口把 body 包成 {"body": {"...": "..."}}，这里一并认下
        if isinstance(body, str):
            payload = json.loads(body)
        elif isinstance(body, dict):
            payload = body
        else:
            raise SystemExit(f"响应文件 {path} 的 body 既不是字符串也不是对象")
    else:
        raise SystemExit(
            f"响应文件 {path} 既不是契约（缺 configs），也不是信封（缺 body）"
        )

    configs = payload.get("configs")
    if not isinstance(configs, list) or not configs:
        raise SystemExit(f"响应文件 {path} 的 configs 不是非空数组")
    return payload, _signature_of(configs)


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
                payload = json.loads(text)
                return payload, _signature_of(payload["configs"])
    raise SystemExit("批次产物里没有 note-attributes 的响应")


def load_extension(path: Path) -> list[dict]:
    """读补充字段文件（基线与线上契约的差集）。文件不存在时返回空表。"""
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("configs") or [])


def apply_extension(configs: list[dict], extra: list[dict]) -> list[dict]:
    """把补充字段追加到基线字段表**末尾**，同键以基线为准（不覆盖既有字段）。

    **只适用于「基线 + 只增不改」的历史场景**（``--batch-dir``）。本轮契约有同名改定义
    与下线，走这条合并会静默留下旧定义、产出新老混杂的快照，因此 ``--from-response``
    一律不调用本函数。
    """
    seen = {str(c.get("attribute_key")) for c in configs}
    out = list(configs)
    for entry in extra:
        key = str(entry.get("attribute_key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


CATEGORY_SLUGS: dict[str, str] = {
    "基础身份": "BASE_IDENTITY",
    "购车需求": "PURCHASE_NEED",
    "预算与金融": "BUDGET_FINANCE",
    "决策与节奏": "DECISION_RHYTHM",
    "竞品对比": "COMPETITOR",
    "个人偏好与备注": "PREFERENCE_NOTE",
    "地域与生活圈": "REGION_LIFE",
}
"""分类中文名 → 模块常量名。

**新分类必须在此显式登记**。此前未登记的分类会落到
``"CATEGORY_" + str(abs(hash(name)) % 10000)``，而 Python 的字符串哈希**逐进程随机**：
常量名每次运行都不同，``--check`` 永远报不一致，排查方向还会被引向编码与换行符。
"""


def _category_slug(name: str) -> str:
    """分类中文名 → 模块常量名（``基础身份`` → ``BASE_IDENTITY``）。"""
    slug = CATEGORY_SLUGS.get(name)
    if slug is None:
        raise SystemExit(
            f"契约出现未登记的分类 {name!r}；请在 scripts/snapshot_note_contract.py 的 "
            f"CATEGORY_SLUGS 里补一条中文名 → 常量名的映射，"
            f"否则生成的常量名会随进程变化。已登记：{sorted(CATEGORY_SLUGS)}"
        )
    return slug


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


def render(
    payload: dict,
    signature: str,
    source_label: str,
    *,
    source_note: str,
    extended: bool = False,
) -> str:
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
        source_label=source_label,
        source_note=source_note,
        configs=items,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 note-attributes 契约快照")
    parser.add_argument(
        "--from-response",
        default=None,
        metavar="FILE",
        help="线上响应的手工取回件（HTTP 信封，契约正文在 body 里）。"
        "给出时以它为准，且不合并补充字段",
    )
    parser.add_argument("--batch-dir", default=str(DEFAULT_BATCH))
    parser.add_argument("--extension", default=str(EXTENSION), help="补充字段文件（JSON）")
    parser.add_argument("--no-extension", action="store_true", help="只用基线快照，不补字段")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    if args.from_response:
        response_path = Path(args.from_response)
        payload, signature = load_from_response(response_path)
        # 线上 payload 就是完整现行契约：不叠加任何旧快照，否则会产出新老混杂的定义。
        extra: list[dict] = []
        source_label = str(response_path.name)
        source_note = (
            f"**快照取自线上响应的手工取回件** ``{source_label}``"
            f"（payload 签名 ``{signature}``，按 ``configs`` 的规范化 JSON 计算）。"
        )
    else:
        batch_dir = Path(args.batch_dir)
        payload, signature = load_payload(batch_dir)
        extra = [] if args.no_extension else load_extension(Path(args.extension))
        if extra:
            payload = dict(payload)
            payload["configs"] = apply_extension(list(payload["configs"]), extra)
        source_label = batch_dir.name
        source_note = (
            f"**契约实测跨 39/39 案完全恒定**"
            f"（payload 签名 ``{signature}``，按 ``configs`` 的规范化 JSON 计算）。"
        )

    text = render(
        payload,
        signature,
        source_label,
        source_note=source_note,
        extended=bool(extra),
    )

    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current == text:
            print(
                f"[OK] 快照与来源一致（source={source_label}, signature={signature}, "
                f"{len(payload['configs'])} 字段"
                f"{'，含补充' if extra else ''}）"
            )
            return 0
        print(
            "[FAIL] 快照与来源不一致，请运行 "
            "python scripts/snapshot_note_contract.py --from-response <文件>"
        )
        return 1

    OUTPUT.write_text(text, encoding="utf-8", newline="\n")
    print(
        f"已写入 {OUTPUT}（source={source_label}, signature={signature}, "
        f"{len(payload['configs'])} 字段"
        f"{'，含补充 %d 个' % len(extra) if extra else ''}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
