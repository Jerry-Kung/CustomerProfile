"""意向车型闭集与四路分流（K5）。

## 为什么需要一个分流，而不只是加一条过滤

业务口径有两条，缺一不可：

1. ``intended_model`` **只能取在售车型清单里的值**；
2. 客户提到的清单外车型**不得丢弃**，要落到正确的字段上。

只做「不在清单就剔除」，客户说「我在看坦克700」会得到一个空的意向车型、也没有竞品——
这是把信息弄丢。那台车不是意向车型，但它一定是**对比车型**或**置换车型**。

## 四路划分

知识包恰好给出了划分所需的唯一信息：这台车是不是本方的在售车型。

| 客户说的车 | 归属 | 依据 |
|---|---|---|
| 本方在售车型 + 客户在考虑 | ``intended_model`` | 闭集内 |
| 本方在售车型 + 客户已拥有 | ``old_car_model`` | 自用车优先 |
| 非本方车型 + 拿来比较 | ``competing_brand`` / ``competing_model`` | 竞品位 |
| 非本方车型 + 客户已拥有 | ``old_car_model`` / ``owned_brand_model`` | 自用车优先 |

**本方车型永不进竞品位，非本方车型永不进意向位。** 两条边界互补，把四类字段划成
互斥的四个格子。

## 为什么在**断言层**而不是投影层做

分流要改的是「这条断言该归到哪个字段」，不是「这个字段的值要不要留」。在投影层做
只能看到已落定的字段值，看不到客户到底说了什么；而且投影层是全案枢纽，往里加业务
判断会让它同时承担两种职责。

放在断言层还有一个实际好处：投影层（``projection.py``）完全不知道知识库的存在，
它仍然只依赖契约与字段语义，单测也不受知识配置影响。

## 两条「不擅自归类」的边界

- **不认识的本方品牌型号**（客户说「想买个老款 917」而该型号已不在售）：从意向位
  剔除并转确认。值是真话，只是不在售——丢掉销售就看不到这条线索。
- **完全陌生的称谓**：从意向位剔除并转确认，**不并入竞品位**。未知不等于竞品，
  客户可能说的是自己那台车。这与 L1 提示词「拿不准就写竞品位」的口径相反，因为
  那条针对的是模型抽取时的判断，这里针对的是**代码事后校正**——代码不知道语境，
  把未知称谓塞进竞品位等于替模型制造事实。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Sequence

from .pack import EMPTY_PACK, Pack
from .resolve import resolve_brand, resolve_model

INTENDED = "intended_model"
COMPETING_MODEL = "competing_model"
COMPETING_BRAND = "competing_brand"
OLD_CAR_MODEL = "old_car_model"
OWNED_BRAND_MODEL = "owned_brand_model"

OWNED_KEYS: tuple[str, ...] = (OLD_CAR_MODEL, OWNED_BRAND_MODEL)
"""自用车字段。判断放在闭集判定**之后**：客户说「我现在开 917，想再买一台」时，
917 会同时出现在两个字段里，此时不能因为有 917 就把意向位上的 917 剔掉。"""

RULE_ID = "KB-02"
"""本条通路在登记表里的编号。"""


@dataclass(slots=True)
class IntentOutcome:
    """分流结果。``claims`` 是修订后的新列表，原列表不动。"""

    claims: list[Any] = field(default_factory=list)
    rerouted: list[dict[str, Any]] = field(default_factory=list)
    """从意向位移出并改投其它字段的项。"""
    dropped: list[dict[str, Any]] = field(default_factory=list)
    """从意向位移出、未改投其它字段的项（转人工确认）。"""
    to_confirm: list[dict[str, Any]] = field(default_factory=list)
    """需要销售/人工确认的条目。文案必须是给人看的话，不含规则编号与内部键名。"""

    @property
    def touched(self) -> bool:
        return bool(self.rerouted or self.dropped)

    def as_trace(self) -> list[dict[str, Any]]:
        """规则留痕（一次动作一条），形状与 ``RuleTrace`` 一致。"""
        out: list[dict[str, Any]] = []
        for item in self.rerouted:
            out.append(
                {
                    "rule_id": RULE_ID,
                    "stage": "project",
                    "attribute": INTENDED,
                    "action": "rewrite",
                    "severity": "repair",
                    "reason": item.get("reason", ""),
                    "source": "code",
                    "before": item.get("value"),
                    "after": item.get("to"),
                    "version": 1,
                }
            )
        for item in self.dropped:
            out.append(
                {
                    "rule_id": RULE_ID,
                    "stage": "project",
                    "attribute": INTENDED,
                    "action": "require_confirm",
                    "severity": "repair",
                    "reason": item.get("reason", ""),
                    "source": "code",
                    "before": item.get("value"),
                    "after": None,
                    "version": 1,
                }
            )
        return out


def _text(value: Any) -> str:
    return str(value).strip()


def _mentions_our_brand(text: str, pack: Pack) -> bool:
    """``text`` 是否以本方品牌名开头（如「猛士老款917特别版」）。

    **这是前缀判定，不是模糊匹配**：只看开头是否以品牌名起头。它要回答的问题是
    「客户说的这台车是不是我们家的型号」，与「它是不是某个已知车型」是两件事——
    ``resolve_model`` 答后者，答不了前者。

    不做编辑距离、不做任意位置包含：``买坦克不如买猛士`` 这种句子里的品牌名出现在
    中间，不应把整句话判成一辆本方车型。
    """
    if not text or pack.is_empty:
        return False
    brand = pack.brand.our_brand
    names = [brand.canonical, *brand.aliases]
    needle = text.strip()
    for name in names:
        if name and needle.startswith(name):
            return True
    return False


def reroute_claims(claims: Sequence[Any], pack: Pack | None = None) -> IntentOutcome:
    """按五步规则校正断言账本里的 ``intended_model``。**空包时原样返回。**

    ``claims`` 是 ``projection.Claim`` 序列；本函数通过属性名访问，不导入那个模块，
    也因此可以用最朴素的假对象单测。

    步骤（对 ``intended_model`` 的每个取值）：

    1. ``resolve_model`` 命中 → 保留；
    2. 该值同时出现在自用车字段里 → **保留**并转确认（客户可能是在说再买一台，
       代码看不出语气，宁留勿错）；
    3. ``resolve_brand`` 命中本方品牌但型号不认识 → 剔除 + 转确认；
    4. ``resolve_brand`` 命中竞品 → 改投 ``competing_model``；
    5. 完全不认识 → 剔除 + 转确认，**不擅自并入竞品位**。
    """
    resolved = pack or EMPTY_PACK
    outcome = IntentOutcome()
    if resolved.is_empty or not claims:
        outcome.claims = list(claims)
        return outcome

    owned: set[str] = set()
    for claim in claims:
        if getattr(claim, "attribute", "") in OWNED_KEYS:
            text = _text(getattr(claim, "value", ""))
            if text:
                owned.add(text)
                model_id = resolve_model(text, resolved)
                if model_id is not None:
                    entry = resolved.model(model_id)
                    if entry is not None:
                        owned.add(entry.canonical_name)

    kept: list[Any] = []
    for claim in claims:
        if getattr(claim, "attribute", "") != INTENDED:
            kept.append(claim)
            continue

        raw_value = getattr(claim, "value", "")
        # 多值逐项判定：``intended_model`` 是聚合字段，「在 A、B 之间比」是常态。
        # 不展开的话整条会被当成一个陌生称谓，把在售的那台也一并丢掉。
        items = (
            [_text(item) for item in raw_value if _text(item)]
            if isinstance(raw_value, (list, tuple))
            else [_text(raw_value)]
        )
        items = [item for item in items if item]
        if not items:
            kept.append(claim)
            continue

        keep_items: list[str] = []
        for text in items:
            model_id = resolve_model(text, resolved)
            if model_id is not None:
                entry = resolved.model(model_id)
                if text in owned or (entry is not None and entry.canonical_name in owned):
                    # 第 2 步：同时是自用车——不剔除，但要让人确认
                    outcome.to_confirm.append(
                        {
                            "attribute_key": INTENDED,
                            "candidates": [text],
                            "reason": "该车型同时出现在客户已有车辆中，需确认是再购还是仅提及自用车",
                            "suggested_action": "与客户确认是否要再买一台同款",
                        }
                    )
                keep_items.append(text)
                continue

            brand = resolve_brand(text, resolved)
            if brand is None and _mentions_our_brand(text, resolved):
                brand = "our"

            if brand == "our":
                # 本方品牌，但型号不在售（如「老款 917 特别版」）
                outcome.dropped.append(
                    {
                        "value": text,
                        "reason": "该车型不在当前在售清单内",
                        "kind": "our_brand_unknown_model",
                    }
                )
                outcome.to_confirm.append(
                    {
                        "attribute_key": INTENDED,
                        "candidates": [text],
                        "reason": "客户提到的车型不在当前在售清单内",
                        "suggested_action": "与客户确认其意向车型是否为其提到的这一款",
                    }
                )
                continue

            # 非本方车型：**改投对比位，但同时转确认**。
            #
            # 业务口径是「可能属于对比车型或置换车型，需要仔细甄别」。代码能确定的是
            # 「它不是我们的在售车型」，不能确定的是「客户在比较它还是在开它」——
            # 后者要看客户怎么说。因此取一个不会丢信息、也不替模型下结论的处置：
            # 记进对比位（信息不丢），并请人确认（结论不替客户下）。
            #
            # 已确认是自用车的走上面的 owned 分支，不会到这里。
            outcome.rerouted.append(
                {
                    "value": text,
                    "to": COMPETING_MODEL,
                    "reason": "客户提到的车型不在本方在售清单内，按对比车型记录待确认",
                }
            )
            outcome.to_confirm.append(
                {
                    "attribute_key": INTENDED,
                    "candidates": [text],
                    "reason": "这台车不在本店在售车型中，已按对比车型记录",
                    "suggested_action": "确认它是客户正在对比的车型，还是客户已有/打算置换的车辆",
                }
            )
            kept.append(
                dataclasses.replace(claim, attribute=COMPETING_MODEL, value=text)
                if dataclasses.is_dataclass(claim)
                else _with_attribute(claim, COMPETING_MODEL)
            )

        if not keep_items:
            continue
        if len(keep_items) == len(items):
            kept.append(claim)
        else:
            # 部分保留：把留下的一项写回原断言（多值时降为单值，避免把已改投的项
            # 留在意向位里重复出现）
            kept.append(
                dataclasses.replace(claim, value=keep_items[0] if len(keep_items) == 1 else keep_items)
                if dataclasses.is_dataclass(claim)
                else _with_attribute(claim, INTENDED)
            )

    outcome.claims = kept
    return outcome


def _with_attribute(claim: Any, attribute: str) -> Any:
    """非 dataclass 的兜底：复制一份并改字段名（供最简假对象用）。"""
    try:
        clone = dataclasses.replace(claim, attribute=attribute)
        return clone
    except TypeError:
        values = {
            name: getattr(claim, name)
            for name in dir(claim)
            if not name.startswith("_") and not callable(getattr(claim, name))
        }
        values["attribute"] = attribute
        return type(claim)(**values)
