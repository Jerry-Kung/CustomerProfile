"""知识视图：把知识包渲染成给模型的文本（K3、K4）。

两个视图，用途完全不同：

- :func:`render_brand_brief`（K3）—— 给 L1 事实层。**只给称谓，不给参数**。
  L1 的任务是归类（意向 / 对比 / 自用 / 试驾），不是讲解；给它配置参数只会诱使它
  把参数写进结论，而参数不是客户说的。约 200–300 字。
- :func:`render_product_brief`（K4）—— 给 L3 表达层。按本案 facts 筛出候选车型，
  只给命中的条目 + 至多一款价位相邻的参照。约 300–1000 字。

两者的共同前提是「知识不是事实」：视图文本必须自述这一区别，否则模型会把价格当成
客户已表达的偏好写进事实位。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .pack import EMPTY_PACK, ModelEntry, Pack
from .resolve import resolve_model

_TEXT_KEYS = ("intended_model", "competing_model", "test_driven_models", "old_car_model",
              "owned_brand_model")
"""这些字段里出现的车型称谓用来筛 K4 候选。前三个来自客户表达与对比，后两个是自用车——
自用车也要给资料：销售需要知道客户现在开什么、要不要置换。"""

_BUDGET_KEYS = ("budget_range", "budget")
"""预算字段。筛 K4 的「参考车型」时用它的价格带。"""


# ====================================================================
# K3 · 取证侧视图
# ====================================================================


def render_brand_brief(pack: Pack | None = None) -> str:
    """《本方品牌与在售车型》——L1 的归属判断依据与意向车型闭集。

    空包时返回空串，调用点据此保持提示词原样（产物逐字节不变）。

    **为什么把闭集写在视图里而不是提示词正文**：正文只留「怎么判断」的方法论。
    换合作伙伴时只换知识包，提示词一字不动。
    """
    resolved = pack or EMPTY_PACK
    if resolved.is_empty:
        return ""

    brand = resolved.brand.our_brand
    lines: list[str] = [
        "## 本方品牌与在售车型（用于归属与取值判断，不是事实来源）",
    ]
    names = [brand.canonical] + [item for item in brand.aliases if item]
    lines.append(f"本方品牌：{'（别名：' + '、'.join(names[1:]) + '）' if len(names) > 1 else names[0]}")

    models: list[str] = []
    for entry in resolved.models:
        if entry.status == "discontinued":
            continue
        alias = [item for item in entry.aliases]
        if alias:
            models.append(f"{entry.canonical_name}（{' / '.join(alias)}）")
        else:
            models.append(entry.canonical_name)
    if models:
        lines.append("在售车型：" + "、".join(models))

    lines.extend(
        [
            "",
            "**怎么用这份清单：**",
            "- **意向车型只能从上列在售车型中选。** 客户提到的其它任何车型都不是意向车型。",
            "- 客户已拥有、正在开、或打算置换的车，写 `old_car_model`，不进意向车型。",
            "- 上列车型即使被客户拿来与别的车比较，也**不写** `competing_brand` / `competing_model`"
            "——同品牌车型不构成竞品。",
            "- 上列之外的车型，客户拿来比较的写 `competing_brand` / `competing_model`；"
            "客户自己在用的写 `old_car_model`。",
        ]
    )
    if any(entry.status == "coming" for entry in resolved.models):
        coming = "、".join(
            entry.canonical_name for entry in resolved.models if entry.status == "coming"
        )
        lines.append(f"- 以下车型尚未正式开售，仍属本方意向范围，但表述时不得说成「在售」：{coming}。")

    return "\n".join(lines)


# ====================================================================
# K4 · 表达侧视图
# ====================================================================


def _values_of(facts: Mapping[str, Any], keys: Iterable[str]) -> list[str]:
    """取出若干字段里的全部字符串取值（多值按列表展开）。"""
    out: list[str] = []
    for key in keys:
        value = facts.get(key)
        if value is None:
            continue
        items = value if isinstance(value, (list, tuple)) else [value]
        for item in items:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
    return out


def _budget_bounds(facts: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """从预算字段里刮出上下限（万元）。刮不出就返回 ``(None, None)``。

    刻意做得很笨：只认「30万」「30-50万」「30~50万」这类形态。认不出就不筛——
    猜错预算区间比不筛更糟，它会让代码替销售决定「该推哪台车」。
    """
    import re

    text = " ".join(_values_of(facts, _BUDGET_KEYS))
    if not text:
        return None, None
    numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", text)]
    if not numbers:
        return None, None
    if len(numbers) == 1:
        value = numbers[0]
        return value * 0.75, value * 1.25
    return min(numbers), max(numbers)


def _adjacent(entry: ModelEntry, bounds: tuple[float | None, float | None], pack: Pack) -> bool:
    low, high = bounds
    if low is None or high is None:
        return False
    return entry.price.min_wan <= high and entry.price.core_max >= low


def render_product_brief(
    facts: Mapping[str, Any] | None = None, pack: Pack | None = None
) -> str:
    """本案相关的在售车型简报。空包时返回空串。

    筛选口径：

    1. 客户提到的本方车型（意向 / 对比 / 试驾 / 自用）→ 给**完整条目**；
    2. 预算区间与车型价格带重叠的本方车型 → 给**一行速览**；
    3. 一个都没命中 → 全系速览。

    为什么按案筛：无差别注入时模型要在长文里自己找「该推哪台」。筛后「候选」由代码
    给定，模型只负责措辞——这是「能写成代码的约束不交给模型」的延伸。
    """
    resolved = pack or EMPTY_PACK
    if resolved.is_empty:
        return ""

    data = dict(facts or {})
    mentioned: list[ModelEntry] = []
    for text in _values_of(data, _TEXT_KEYS):
        model_id = resolve_model(text, resolved)
        if model_id is None:
            continue
        entry = resolved.model(model_id)
        if entry is not None and entry not in mentioned:
            mentioned.append(entry)

    bounds = _budget_bounds(data)
    referenced = [
        entry
        for entry in resolved.models
        if entry not in mentioned
        and entry.status != "discontinued"
        and _adjacent(entry, bounds, resolved)
    ]

    lines: list[str] = [
        "## 本案相关车型（产品资料，不是客户已表达的事实）",
        "",
        "以下车型资料用于**选对卖点、选对邀约方式**。价格与配置是产品知识，"
        "**不得**当作客户已表达的偏好或预算写入事实位——只有事实表里有的才是客户说的。",
        "",
    ]

    if not mentioned and not referenced:
        lines.append("### 全系速览")
        for entry in resolved.models:
            if entry.status == "discontinued":
                continue
            lines.append(f"- **{entry.canonical_name}**｜{entry.label}｜{entry.positioning}")
        return "\n".join(lines)

    for entry in mentioned:
        lines.extend(_full_entry(entry, resolved))

    if referenced:
        lines.append("")
        lines.append("### 可作参照（价位相邻，客户未提及）")
        for entry in referenced:
            lines.append(f"- **{entry.canonical_name}**｜{entry.label}｜{entry.positioning}")
        lines.append("")
        lines.append("参照车型只在客户主动问起或预算明显错位时提一句，不要主动推销。")

    return "\n".join(lines)


def _full_entry(entry: ModelEntry, pack: Pack) -> list[str]:
    tier = pack.tier_of(entry.model_id)
    lines: list[str] = ["", f"### {entry.canonical_name}"]
    lines.append(f"- 定位：{entry.positioning}")
    if tier is not None:
        lines.append(f"- 产品矩阵位置：{tier.label}")
    lines.append(f"- 主力价格带：{entry.price.min_wan:g}–{entry.price.core_max:g} 万元")
    if entry.price.max_wan > entry.price.core_max + 0.01:
        lines.append(
            f"- 全系含特别/定制版最高 {entry.price.max_wan:g} 万元"
            + (f"（{entry.price.note}）" if entry.price.note else "")
        )
    if entry.powertrain:
        labels = {"bev": "纯电", "phev": "插电混动", "erev": "增程", "ice": "燃油"}
        lines.append(f"- 动力形式：{' / '.join(labels.get(item, item) for item in entry.powertrain)}")
    if entry.body:
        lines.append(f"- 车身：{entry.body}")
    if entry.configs:
        lines.append("- 档位：" + "；".join(
            f"{item.name} {item.price_wan:g}万" for item in entry.configs
        ))
    if entry.audience:
        lines.append(f"- 适合人群：{'、'.join(entry.audience)}")
    if entry.scenarios:
        lines.append(f"- 适用场景：{'、'.join(entry.scenarios)}")
    if entry.highlights:
        lines.append("- 核心卖点：")
        for item in entry.highlights:
            text = f"——{item.text}" if item.text else ""
            lines.append(f"  - **{item.title}**{text}")
    if entry.speech:
        lines.append("")
        lines.append(f"**推介话术参考：**{entry.speech}")
    return lines


def mentioned_models(
    facts: Mapping[str, Any] | None = None, pack: Pack | None = None
) -> Sequence[str]:
    """本案命中的本方 ``model_id`` 列表（供统计与测试断言）。"""
    resolved = pack or EMPTY_PACK
    out: list[str] = []
    for text in _values_of(dict(facts or {}), _TEXT_KEYS):
        model_id = resolve_model(text, resolved)
        if model_id is not None and model_id not in out:
            out.append(model_id)
    return out
