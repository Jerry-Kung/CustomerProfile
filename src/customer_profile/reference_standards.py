"""参考标准表：每个回传字段「该怎么填」的一处汇总。

定位见 ``docs/design/客户人设画像管线设计.md`` §3.8。它不是真源：字段名、系统键、业务类型、
定义描述、输出模式**全部从** :mod:`customer_profile.notes_contract` 与
:mod:`customer_profile.internal_fields` 现读，这里只持有契约里没有的那一列——**准入模式**
（以及日后补写的推理路径/禁止项/备注）。

为什么准入模式必须落在代码里：运行台页面是只读视图，读不到一份手写文档；而准入模式要能驱动
校验（``fact`` 字段只准出 ``fact``），就必须是运行期可读的常量。于是这一列在这里，页面从它
渲染，两边不会分叉。

三条口径：

1. **字段全集 = ``EXTRACTION_KEYS`` 去掉画像卡字段。** ``deal_level`` 的产出逻辑复杂且不在
   本表范围，按约定留待后续。
2. **编号一经发出不复用。** 与规则编号同一纪律：对话里用 ``F37`` 指代过某个字段，重排就会
   指错人。
3. **准入模式只有两类。** ``fact``（事实提取）与 ``controlled_inference``（受控推断）。
   「逻辑计算」「自由推理」尚未定义，先不引入空档枚举。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import internal_fields as inf
from . import notes_contract as nc

ADMISSION_FACT = "fact"
ADMISSION_INFERRED = "controlled_inference"
ADMISSION_MODES: tuple[str, ...] = (ADMISSION_FACT, ADMISSION_INFERRED)
"""准入模式。见模块 docstring 第 3 条。"""

ADMISSION_LABELS: dict[str, str] = {
    ADMISSION_FACT: "事实提取",
    ADMISSION_INFERRED: "受控推断",
}

ADMISSION_HINTS: dict[str, str] = {
    ADMISSION_FACT: "必须能在原文、截图或登记信息里逐字指回，不做自由推理。",
    ADMISSION_INFERRED: "须有可推理的证据，按标准路径归纳；无证据时留空，不猜。",
}


@dataclass(frozen=True, slots=True)
class FieldStandard:
    """一行参考标准。``code`` 是对外指代用的编号。"""

    code: str
    attribute_key: str
    admission: str
    reasoning_path: str = ""
    forbidden: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.admission not in ADMISSION_MODES:
            raise ValueError(
                f"{self.code} 的准入模式 {self.admission!r} 非法，可选：{', '.join(ADMISSION_MODES)}"
            )


_FACT_FIELDS: tuple[str, ...] = (
    "customer_name",
    "phone_number",
    "wechat_id",
    "education",
    "industry",
    "occupation",
    "consumption_level",
    "life_key_nodes",
    "driver_license_status",
    "current_vehicle_info",
    "trade_in_requirement",
    "trade_in_model",
    "intended_model",
    "competing_brand",
    "competing_model",
    "test_driven_models",
    "brand_concerns",
    "optional_config",
    "pain_points",
    "frequent_social_media",
    "electronic_ecosystem",
    "hobbies",
    "contact_preference",
    "residence_address",
    "license_plate_address",
    "old_car_model",
    "owned_brand_model",
    "owned_car_age",
    "company",
    "primary_driver",
    "intended_brand",
    "special_requirements",
)
"""事实提取字段（32 个）。顺序即编号顺序，改动需同步 ``FIELDS``。"""

_INFERRED_FIELDS: tuple[str, ...] = (
    "title",
    "age",
    "annual_income",
    "family_structure",
    "customer_type",
    "purchase_type",
    "purchase_purpose",
    "core_concerns",
    "budget_range",
    "payment_method",
    "credit_status",
    "license_plate_status",
    "planned_purchase_time",
    "viewing_progress",
    "decision_maker",
    "internet_preference",
    "follow_up_frequency",
    "knowledge_of_mengshi",
    "knowledge_of_erev",
    "knowledge_of_adas",
    "company_type",
    "seating_requirement",
    "usage_frequency",
    "driving_preference",
    "purchase_nature",
    "emotion",
    "communication_rhythm",
    "personality_and_preferences",
)
"""受控推断字段（28 个）。判据：值要经过一步归纳、换算或查表才能得到。"""


def _field_order() -> tuple[str, ...]:
    """表内字段顺序：先按契约顺序，再按编外注册顺序。

    按契约顺序而不是编号分组：读者手里多半是一份回传 payload，对照着往下扫才顺手。
    契约增删字段时这里随 ``RAW_CONFIGS`` 自动变化，不需要手改。
    """
    ordered: list[str] = [entry["attribute_key"] for entry in nc.RAW_CONFIGS]
    ordered.extend(key for key in inf.EXTRACTION_ONLY_KEYS if key not in ordered)
    return tuple(key for key in ordered if key not in inf.CARD_ONLY_KEYS)


FIELDS: tuple[FieldStandard, ...] = tuple(
    FieldStandard(
        code=f"F{number:02d}",
        attribute_key=key,
        admission=ADMISSION_FACT if key in _FACT_FIELDS else ADMISSION_INFERRED,
    )
    for number, key in enumerate(_field_order(), start=1)
)
"""60 行参考标准。编号按契约顺序生成，一经发出不复用。"""

_INDEX: dict[str, FieldStandard] = {item.attribute_key: item for item in FIELDS}


class StandardTableError(ValueError):
    """参考标准表自检未通过。"""


def verify() -> list[str]:
    """返回全部问题；空列表表示表可用。

    钉住四件事：覆盖全部应管字段、无孤儿键、准入模式不合法、编号不重复。
    """
    issues: list[str] = []
    expected = {key for key in inf.EXTRACTION_KEYS if key not in inf.CARD_ONLY_KEYS}
    actual = set(_INDEX)
    for key in sorted(expected - actual):
        issues.append(f"字段 {key!r} 没有准入模式登记")
    for key in sorted(actual - expected):
        issues.append(f"字段 {key!r} 已不是应管字段（编外或画像卡），表里仍有条目")
    codes = [item.code for item in FIELDS]
    if len(set(codes)) != len(codes):
        issues.append(f"编号重复：{codes}")
    for key in list(_FACT_FIELDS) + list(_INFERRED_FIELDS):
        if key not in _INDEX:
            issues.append(f"准入模式清单里出现了表外字段 {key!r}")
    return issues


def require_valid() -> None:
    issues = verify()
    if issues:
        raise StandardTableError("参考标准表自检未通过：\n" + "\n".join(f"  - {i}" for i in issues))


def get(attribute_key: str) -> FieldStandard | None:
    return _INDEX.get(attribute_key)


def admission_of(attribute_key: str) -> str | None:
    """字段的准入模式；表外字段返回 ``None``（不做约束，而不是默认按事实处理）。"""
    item = _INDEX.get(attribute_key)
    return item.admission if item is not None else None


# ---------------------------------------------------------------- 展示

_ADMISSION_ORDER = {ADMISSION_FACT: 0, ADMISSION_INFERRED: 1}


def _output_mode(attribute_key: str) -> str:
    """输出模式的一句话摘要：单选/多选 + 展示名，或「自由文本」。

    枚举清单本身在《合法取值清单》里，这里只给读者一个形状判断，不重复抄全部键。
    """
    options = nc.enum_options(attribute_key) or inf.enum_options(attribute_key)
    if options is None:
        return "自由文本"
    items = options.get("items") or {}
    names: list[str] = []
    structured = False
    for value in items.values():
        if isinstance(value, dict):
            structured = True
            names.append(str(value.get("display_name") or value.get("input_value") or ""))
        else:
            names.append(str(value))
    names = [name for name in names if name]
    prefix = "多选" if options.get("multiple_value") else "单选"
    suffix = "（部分取值需随带填参）" if structured else ""
    return f"{prefix}：{' / '.join(names)}{suffix}"


def _scope_label(attribute_key: str) -> str:
    if attribute_key in inf.CARD_ONLY_KEYS:
        return "画像卡"
    if inf.is_internal_only(attribute_key):
        return "编外（不回传）"
    return "回传 Notes"


def _rules_for(rule_scope: dict[str, list[dict[str, Any]]], attribute_key: str) -> list[dict[str, Any]]:
    """挂在该字段上的 active 规则。全局规则（``scope=('*',)``）不逐字段铺开。"""
    return rule_scope.get(attribute_key, [])


def build_rows() -> list[dict[str, Any]]:
    """渲染用的行数据。派生列全部现算，不在这里留第二份。"""
    from .rules import default as default_rules
    from .rules import render as rule_render

    registry = default_rules()
    blocks = rule_render.load_blocks()

    rule_scope: dict[str, list[dict[str, Any]]] = {}
    for rule in registry.active():
        block = blocks.get(rule.id)
        payload = {
            "id": rule.id,
            "title": rule.title,
            "channels": list(rule.channels),
            "default_action": rule.default_action,
            "text": block.text.strip() if block is not None else "",
        }
        for field in rule.scope:
            if field == "*":
                continue
            rule_scope.setdefault(field, []).append(payload)

    # 「标准推理路径」列**现读提示词正文**（2026-10-30）。
    # 此前这一列只有个空壳字段，页面显示「暂无附加口径」，而业务方以为写进去的口径
    # 会到模型那里——实际上它连渲染路径都没有。现在直接取 L1 真正读到的那一段：
    # 同一句话只有一个出处，改规则时不会出现「页面写了、提示词没变」。
    field_texts = rule_render.field_notes(stage="extract")

    rows: list[dict[str, Any]] = []
    for item in FIELDS:
        key = item.attribute_key
        rows.append(
            {
                "code": item.code,
                "attribute_key": key,
                "name": inf.name_of(key),
                "category": nc.category_of(key) or "编外字段",
                "field_type": inf.field_type_of(key),
                "scope_label": _scope_label(key),
                "description": inf.description_of(key),
                "admission": item.admission,
                "admission_label": ADMISSION_LABELS[item.admission],
                "admission_hint": ADMISSION_HINTS[item.admission],
                "output_mode": _output_mode(key),
                # 手写口径优先（业务方逐字段下达时写进 FieldStandard），
                # 否则现读该字段的提示词正文。
                "reasoning_path": item.reasoning_path or field_texts.get(key, ""),
                "forbidden": item.forbidden,
                "note": item.note,
                "rules": _rules_for(rule_scope, key),
            }
        )
    return rows


def build_payload() -> dict[str, Any]:
    """页面接口的返回体：行数据 + 规模统计 + 契约版本。"""
    from .rules import render as rule_render

    rows = build_rows()
    global_text = rule_render.global_notes(stage="extract")
    by_mode = {mode: 0 for mode in ADMISSION_MODES}
    for row in rows:
        by_mode[row["admission"]] += 1
    return {
        "contract_signature": nc.SIGNATURE,
        "total": len(rows),
        "by_admission": by_mode,
        "admission_labels": ADMISSION_LABELS,
        "rows": rows,
        # 全局规则（``scope=("*",)``）**单独成块**：它们不贴任何字段，塞进某一行就等于
        # 给它编造归属。这一块与 L1 提示词里那一段逐字同源。
        "global_rules": global_text,
    }


# ---------------------------------------------------------------- 规则归属

def is_field_bound(rule: Any) -> bool:
    """该规则是否已收进参考标准表、因此不再出现在「规则清单」页。

    判据：只挂单字段、且走提示词通道。多字段规则（``SPEC-17``）留在规则清单——
    一条口径同时管五个字段，塞进任何一行都只代表其中一个。
    """
    scope = tuple(field for field in rule.scope if field != "*")
    return len(scope) == 1 and rule.enables_channel("prompt")
