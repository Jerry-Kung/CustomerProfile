"""回传体构造：把**已定稿的 facts** 投影成 v2 契约的 ``analysis_result`` 六键。

**为什么这层是纯代码、0 token**（L-002 的定论，见 ``docs/business_opt01_v3/AI画像v2回传改造核查清单.md`` §6.1）：

1. **唯一 id 必须由代码生成。** 契约要求 ``(phone, item_id)`` 与
   ``(phone, psychology_key)`` 全局唯一，且回写是**先删后建**。id 撞号会让整次回传 500——
   把「回传能否成功」交给模型抖动是不可接受的。
2. **归类可判定，不该交给模型。** 六个分类与 facts 有明确对应（到店动因 ~
   ``purchase_purpose``；对比车型 ~ ``competing_*``；决策阻力 ~ ``brand_concerns`` 与
   抗性），按项目既有口径「能写成代码的约束不交给模型」。
3. **与 Notes 同源。** 本模块的每个条目都从 ``facts`` 取值、并**复用 Notes 那一条**
   的 ``reasoning_detail`` 措辞——同一个事实在两处长得一样，是「卡片与 Notes 不打架」
   这条设计口径的落地，而不是靠事后核对。

表达层（L3）只负责写它那 5 个 section；四个区块由本模块产出，表达层无权决定结构。

## 两个必须守住的契约语义

- **缺失即保留原值，显式空值才清空**（接口文档 §1 兼容策略）。因此某分类没有条目时，
  本模块**整个分类不出现**，而不是给一个 ``items: []``——后者会把客户已有的该分类数据
  清空。例外是「换车动因」与「对比车型」：这两类缺料时给一条「暂无…」填充项
  （见 :func:`build_purchase_decisions`），避免卡片出现空白行。同理 ``deal_battle`` 的
  两个数组缺料时不给出，``decision_psychology`` 无结论的 key 直接不返回（契约本就是
  「每客户 0~4 条」）。
- **``reasoning_detail`` 恒为四键**（``summary`` / ``steps`` / ``evidence`` / ``confidence``）。
  ``evidence`` 可以为空列表，由前端决定不渲染该区块。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from . import internal_fields as inf
from . import notes_contract as nc

# ====================================================================
# 契约常量（逐字取自 docs/business_opt01_v3/AI画像远程回传接口数据结构说明.md §2.4 / §2.5 / §2.6）
# ====================================================================

CATEGORY_VISIT = "到店动因"
CATEGORY_VEHICLE = "当前用车"
CATEGORY_CHANGE = "换车动因"
CATEGORY_COMPARE = "对比车型"
CATEGORY_FOCUS = "核心关注"
CATEGORY_BARRIER = "决策阻力"

PURCHASE_CATEGORIES: tuple[str, ...] = (
    CATEGORY_VISIT,
    CATEGORY_VEHICLE,
    CATEGORY_CHANGE,
    CATEGORY_COMPARE,
    CATEGORY_FOCUS,
    CATEGORY_BARRIER,
)
"""``purchase_decisions`` 的六个固定分类，顺序即落库顺序（``rank_order`` 缺省按数组顺序）。

**顺序不是随手排的**：它是卡片上的展示顺序，销售从上往下读的路径是「他为什么来 →
他现在开什么 → 为什么要换 → 他在比什么 → 他最在意什么 → 他还在顾虑什么」。
"""

PSYCH_DECISION_STYLE = "decision_style"
PSYCH_CURRENT_ATTITUDE = "current_attitude"
PSYCH_COMMUNICATION_PREFERENCE = "communication_preference"
PSYCH_EMOTION_STATE = "emotion_state"

PSYCHOLOGY_KEYS: tuple[tuple[str, str], ...] = (
    (PSYCH_DECISION_STYLE, "决策风格"),
    (PSYCH_CURRENT_ATTITUDE, "当前态度"),
    (PSYCH_COMMUNICATION_PREFERENCE, "沟通偏好"),
    (PSYCH_EMOTION_STATE, "情绪状态"),
)
"""``decision_psychology`` 的四个固定 ``key`` 与中文维度名（接口文档 §2.6 逐字）。

**``key`` 不另生成 id**：契约的唯一约束就是 ``(phone, psychology_key)``，
键名本身即标识。
"""

# 知识了解程度的档位序，用于「≥ basic」这类门槛判断。
KNOWLEDGE_RANK: Mapping[str, int] = {"none": 0, "basic": 1, "deep": 2}
"""``knowledge_of_*`` 的档位。``unknown`` 不在表内——它是「未知」，不参与门槛比较。"""

INITIAL_PURCHASE = "first"
ADDITIONAL_PURCHASE = "additional"
REPLACEMENT_PURCHASE = "replacement"


# ====================================================================
# 取值工具
# ====================================================================


def _payload(value: Any) -> Any:
    """兼容 String / Object 两种入参（上游时而是 JSON 串、时而是对象）。"""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            return {}
    return {}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "、".join(part for part in (_as_text(v) for v in value) if part)
    return json.dumps(value, ensure_ascii=False, default=str)


def _as_list(value: Any) -> list[Any]:
    """把取值摊成列表：``a||b`` / 列表 / 单值都认。空值给空列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[Any] = []
        for item in value:
            out.extend(_as_list(item))
        return out
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if "||" in text:
            return [part.strip() for part in text.split("||") if part.strip()]
        return [text]
    if isinstance(value, dict):
        return [value]
    return [value]


def _label(attribute: str, raw: Any) -> str:
    """把一个枚举取值渲染成**中文展示名**；非枚举原样返回。

    为什么不直接写英文键：四个区块是**给销售看的**卡片，写 ``fuel_efficiency``
    等于把内部字段键端到用户面前（L3 表达层的硬边界第 5 条同口径）。
    """
    text = _as_text(raw)
    if not text:
        return ""
    options = inf.enum_options(attribute)
    if options:
        display = inf.display_map(attribute).get(text)
        if display:
            return str(display)
        # 结构化枚举（如 trade_in_requirement）取值是 dict，取其 key 的展示名。
        for item in _as_list(raw):
            if isinstance(item, dict):
                key = _as_text(item.get("key"))
                display = inf.display_map(attribute).get(key)
                if display:
                    extra = _as_text(item.get("input_value"))
                    return f"{display}（{extra}）" if extra else str(display)
    return text


def _digest(*parts: str) -> str:
    """条目 id 的稳定后缀。

    **必须用内容摘要而不是 ``hash()``**：``hash()`` 对 str 是按进程随机加盐的，
    同一份输入在两次运行里会给出不同的 id。回写是「先删后建」，id 每次变会让
    服务端侧的引用（如人工锁定、前端深链）全部失配。这里取内容摘要，同输入恒同 id。
    """
    canonical = "\x1f".join(parts)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:10]


# ====================================================================
# reasoning_detail：优先复用 Notes 那一条的措辞
# ====================================================================


class _FactView:
    """一次回传里 facts 与 notes 的只读视图。

    ``reasoning_detail`` 的取法是本类的全部意义：**同一个字段，Notes 里已经有了一份
    经过校验、面向销售的措辞**（``projection._to_notes_updates`` 产出）。四个区块若各自
    另写一套，同一事实在卡片与 Notes 上就会有两种说法——那正是 production 图
    「卡片说 A、Notes 说 B」的老毛病换了个地方出现。
    """

    def __init__(self, facts: Mapping[str, Any], notes: Sequence[Any]) -> None:
        self.facts: Mapping[str, Any] = facts
        self._notes: dict[str, dict[str, Any]] = {}
        for entry in notes:
            if not isinstance(entry, dict):
                continue
            key = _as_text(entry.get("attribute_key"))
            if key:
                self._notes[key] = entry

    def has(self, attribute: str) -> bool:
        value = self.facts.get(attribute)
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple)):
            return bool(value)
        return True

    def raw(self, attribute: str) -> Any:
        return self.facts.get(attribute)

    def text(self, attribute: str) -> str:
        """取值的可读文本（枚举字段取中文展示名）。"""
        return _label(attribute, self.facts.get(attribute))

    def items(self, attribute: str) -> list[str]:
        """多值字段摊成可读文本列表。"""
        out: list[str] = []
        for raw in _as_list(self.facts.get(attribute)):
            text = _label(attribute, raw)
            if text and text not in out:
                out.append(text)
        return out

    def detail(self, *attributes: str, summary: str = "") -> dict[str, Any]:
        """组装 ``reasoning_detail`` 四键，措辞**优先取自 Notes 里那一条**。

        Notes 里没有该字段的条目（编外字段如 ``purchase_nature`` 不回传、因此没有条目；
        或取值来自表达层的 section）时，退回一段**由取值本身构成**的说明——不得编造
        推导链（``steps`` 宁可只说「据已定稿事实得出」）。
        """
        for attribute in attributes:
            entry = self._notes.get(attribute)
            if not entry:
                continue
            detail = entry.get("reasoning_detail")
            if isinstance(detail, dict):
                return _normalize_detail(detail)

        parts = [self.text(a) for a in attributes if self.has(a)]
        parts = [p for p in parts if p]
        if not parts:
            return _empty_detail()
        return {
            "summary": summary or ("已定稿事实：" + "、".join(parts)),
            "steps": ["由上游已定稿的事实直接给出，未作二次推断"],
            "evidence": [],
            "confidence": _DEFAULT_CONFIDENCE,
        }


_DEFAULT_CONFIDENCE = 0.7
"""无 Notes 措辞可复用时的置信度。

**不写 1.0**：这些条目是「代码从事实投影」的结论，不是客户原话。给满分会让销售
把一条规则拼出来的卡片当成客户已经表态（L3 校验层对同类问题给 0.5~0.6 是同口径）。
"""


def _normalize_detail(detail: Mapping[str, Any]) -> dict[str, Any]:
    """把任意 ``reasoning_detail`` 补齐成契约要求的四键。"""
    steps = [str(s) for s in (detail.get("steps") or []) if str(s).strip()]
    evidence = [str(s) for s in (detail.get("evidence") or []) if str(s).strip()]
    confidence = detail.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = _DEFAULT_CONFIDENCE
    return {
        "summary": str(detail.get("summary") or "").strip(),
        "steps": steps,
        "evidence": evidence,
        "confidence": max(0.0, min(1.0, float(confidence))),
    }


def _empty_detail() -> dict[str, Any]:
    return {"summary": "", "steps": [], "evidence": [], "confidence": _DEFAULT_CONFIDENCE}


# ====================================================================
# 条目与唯一性
# ====================================================================


def _item(
    prefix: str,
    attribute: str,
    label: str,
    detail: Mapping[str, Any],
    rank: int,
    *,
    extra_digest: str = "",
) -> dict[str, Any]:
    """造一个 ``items[]`` 元素。``id`` 前缀区分分类，后缀取来源事实的稳定摘要。"""
    item_id = f"{prefix}-{_digest(attribute, label, extra_digest)}"
    return {
        "id": item_id,
        "label": label,
        "reasoning_detail": dict(detail),
        "rank_order": rank,
    }


def _dedupe(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 ``id`` 去重并重排 ``rank_order``。

    **去重是必要的，静默才是危险的**：同一分类里两条来源字段指向同一个取值是常态
    （``core_concerns`` 既喂「到店动因」又喂「核心关注」，客户在多个字段里说同一件事）。
    这里按 id 合并——id 由内容摘要决定，同内容即同条目。合并之后仍重复，说明摘要
    碰撞或代码有误，由 :func:`_assert_unique_ids` 抛错，而不是静默少一项。
    """
    seen: dict[str, dict[str, Any]] = {}
    for item in items:
        seen.setdefault(item["id"], item)
    out = list(seen.values())
    for index, item in enumerate(out, start=1):
        item["rank_order"] = index
    return out


def _assert_unique_ids(ids: Sequence[str], where: str) -> None:
    """同客户全局唯一性断言：重复即抛错，不静默去重。

    **为什么必须抛错**：契约的唯一约束是 ``(customer_phone, item_id)``，而回写是
    先删后建——重复 id 会让整次回传递交失败（500）。更要紧的是**静默去重**这条路：
    它会把两条不同的结论合成一条，卡片上少一项而无人发现。宁可这一次回传失败并
    在日志里看见，也不要一份悄悄缺项的画像。
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for item_id in ids:
        if item_id in seen and item_id not in duplicates:
            duplicates.append(item_id)
        seen.add(item_id)
    if duplicates:
        raise ValueError(f"{where} 出现重复 id：{sorted(duplicates)}")


# ====================================================================
# 基础键：profile_summary / basic_notes_updates
# ====================================================================

GENDER_BY_TITLE: Mapping[str, str] = {"Mr": "男", "Ms": "女"}
"""``title``（契约的称呼字段）到 ``gender`` 的映射。

契约的 ``profile_summary.gender`` 要的是「男 / 女」这样的人类可读值，而 facts 里的
性别载体是 ``title`` 的 ``Mr`` / ``Ms``。不能照抄键名。
"""


def build_profile_summary(view: _FactView) -> dict[str, Any]:
    """``profile_summary``：新契约里**只剩 ``name`` 与 ``gender``**。

    旧结构里的 ``age`` / ``deal_level`` / ``overall_tag`` / ``personality_summary``
    一律不再从这里出去——意向等级改由 ``ai_insight.purchase_intent_level`` 承载，
    后台再派生同步回 ``customer_profile_main.deal_level``。
    """
    summary: dict[str, Any] = {}
    name = view.text("customer_name")
    if name:
        summary["name"] = name
    title = _as_text(view.raw("title"))
    gender = GENDER_BY_TITLE.get(title)
    if gender:
        summary["gender"] = gender
    return summary


def build_basic_notes_updates(notes: Sequence[Any]) -> list[dict[str, Any]]:
    """``basic_notes_updates``：**回传白名单投影**。

    这是「编外字段」设计的落点：``facts`` 里可能有一批契约已下线的字段
    （``old_car_model`` / ``company`` / ``purchase_nature`` …），它们对内部推理有用，
    但回传体里多一个契约没有的 ``attribute_key`` 反应不可预期。因此这里**遍历契约键**
    而不是遍历 ``facts``——白名单来自 :data:`internal_fields.CALLBACK_KEYS`。
    """
    allowed = set(inf.CALLBACK_KEYS)
    updates: list[dict[str, Any]] = []
    for entry in notes:
        if not isinstance(entry, dict):
            continue
        key = _as_text(entry.get("attribute_key"))
        if not key or key not in allowed:
            continue
        value = entry.get("attribute_value")
        if value is None:
            value = ""
        item: dict[str, Any] = {"attribute_key": key, "attribute_value": str(value)}
        detail = entry.get("reasoning_detail")
        if isinstance(detail, dict):
            item["reasoning_detail"] = _normalize_detail(detail)
        updates.append(item)
    return updates


# ====================================================================
# 区块一：ai_insight
# ====================================================================


def _level_detail(view: _FactView) -> dict[str, Any]:
    """商机等级的 ``reasoning_detail``：优先取定级环节的结构化结论。

    ``deal_level_detail`` 由 ``pipeline_level_apply`` 写入 facts，是 8–15 条推理链 +
    原文证据 + 关键障碍 + 下一步建议。它不在 Notes 契约里，因此 ``_FactView.detail``
    取不到它；没有它时退到 Notes 那条（若有），再退到通用说明。
    """
    raw = view.raw("deal_level_detail")
    if isinstance(raw, Mapping):
        detail = _normalize_detail(raw)
        if detail["summary"] or detail["steps"]:
            return detail
    return view.detail("deal_level", "business_opp_level")


def build_ai_insight(view: _FactView, express: Mapping[str, Any]) -> dict[str, Any]:
    """``ai_insight``：意向等级 + 客户群标签，两项都是 ``{value, reasoning_detail}``。

    - ``purchase_intent_level``：取 facts 的 ``deal_level`` 并归一，**按定级规范原样回传**
      ``H`` / ``A`` / ``B`` / ``C`` / 待识别（前端答复该字段是文本字段，非 ABCD 纯枚举，
      2026-10-20 撤掉过渡映射，见 L-008）；
    - ``customer_segment``：取表达层自由生成的 ``overall_tag``。本轮不约束取值口径
      （L-001，待业务方给人群标签字典），但**必须**带 ``reasoning_detail``——
      没有它的值在前端就是个不可点击的死值。
    """
    from . import projection as proj

    insight: dict[str, Any] = {}
    level = proj.normalize_level(view.raw("deal_level"))
    if level:
        insight["purchase_intent_level"] = {
            "value": level,
            "reasoning_detail": _level_detail(view),
        }

    segment = _as_text(_section_text(express, "overall_tag"))
    if segment:
        insight["customer_segment"] = {
            "value": segment,
            "reasoning_detail": _segment_detail(express),
        }
    return insight


def _section_text(express: Mapping[str, Any], key: str) -> Any:
    """从表达层产物里读一个取值。

    表达层的 ``profile_summary`` 是**对象**，``overall_tag`` 是它的子键；本函数兼容
    「顶层直接给」与「嵌在 profile_summary 里」两种形态（表达层偶发会把子键写平）。
    """
    top = express.get(key)
    if top not in (None, "", [], {}):
        return top
    summary = express.get("profile_summary")
    if isinstance(summary, dict):
        return summary.get(key)
    return None


def _segment_detail(express: Mapping[str, Any]) -> dict[str, Any]:
    """客户群标签的 ``reasoning_detail``。

    表达层把结论写在 ``profile_summary`` 里，措辞（``personality_summary`` /
    ``profile_summary`` 叙述）也在那一节——直接引用它，而不是在这里另写一段
    「综合判断得出」的空话。
    """
    summary = express.get("profile_summary")
    if isinstance(summary, dict):
        for key in ("personality_summary", "profile_summary"):
            text = _as_text(summary.get(key))
            if text:
                return {
                    "summary": text,
                    "steps": ["由表达层依同一份已定稿事实归纳"],
                    "evidence": [],
                    "confidence": _DEFAULT_CONFIDENCE,
                }
    return {
        "summary": "依已定稿事实归纳的客户群标签",
        "steps": ["由表达层依同一份已定稿事实归纳"],
        "evidence": [],
        "confidence": _DEFAULT_CONFIDENCE,
    }


# ====================================================================
# 区块二：purchase_decisions
# ====================================================================


def build_purchase_decisions(
    view: _FactView, express: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """``purchase_decisions``：六个固定分类，每类一个 ``items[]``。

    分类的顺序与取值来源见 :data:`PURCHASE_CATEGORIES` 与各 ``_category_*`` 函数。

    **换车动因 / 对比车型缺料时给一条「暂无…」填充项，而不是整个分类不出现。**
    这两类在销售卡片上是固定的两行，缺少整块会让前端显示空白、业务方以为数据丢了；
    填充项明确写出「暂无」，既保住了版式，也不伪造事实（置信度取低档）。
    其余四类缺料仍按「缺失即保留原值」的契约语义整个不出现。
    """
    blocks: list[dict[str, Any]] = []
    for category, items in (
        (CATEGORY_VISIT, _category_visit(view)),
        (CATEGORY_VEHICLE, _category_vehicle(view)),
        (CATEGORY_CHANGE, _category_change(view)),
        (CATEGORY_COMPARE, _category_compare(view)),
        (CATEGORY_FOCUS, _category_focus(view)),
        (CATEGORY_BARRIER, _category_barrier(view, express)),
    ):
        if not items:
            filler = _filler_item(category)
            if filler is None:
                continue
            items = [filler]
        blocks.append({"category": category, "items": _dedupe(items)})
    return blocks


FILLER_LABELS: Mapping[str, str] = {
    CATEGORY_CHANGE: "暂无换车动因",
    CATEGORY_COMPARE: "暂无对比车型",
}
"""缺料时填入的展示文案。

**不是空串、也不是单个「无」**：卡片是给销售读的，一句话说明「这一类暂时没有内容」
比一个「无」字更清楚；而 ``items`` 为空或整个分类缺席，前端就只能显示空白。
"""


def _filler_item(category: str) -> dict[str, Any] | None:
    """给指定分类造一条填充项；不在 :data:`FILLER_LABELS` 里的分类返回 ``None``。

    ``id`` 走同一套内容摘要，因此同分类每次回传的填充项 id 恒定——「先删后建」不会
    让前端深链每次失配。
    """
    label = FILLER_LABELS.get(category)
    if not label:
        return None
    prefix = "change" if category == CATEGORY_CHANGE else "compare"
    return _item(
        prefix,
        category,
        label,
        {
            "summary": f"已定稿事实中未见{category}相关信息",
            "steps": ["检视已定稿事实，未找到与该分类对应的事实"],
            "evidence": [],
            "confidence": 0.3,
        },
        1,
        extra_digest=label,
    )


def _category_visit(view: _FactView) -> list[dict[str, Any]]:
    """到店动因：``purchase_purpose``（买车用来干什么）+ ``core_concerns``（最在意什么）。

    这两项合起来回答「他为什么来店里」，因此同归一类而不是拆成两类；``core_concerns``
    同时进「核心关注」，重复是刻意的——分类边界重叠是已登记的风险（R4），
    宁可两处都出现，也不要销售在某一类里找不到这条最关键的信息。
    """
    items: list[dict[str, Any]] = []
    for purpose in view.items("purchase_purpose"):
        items.append(
            _item("visit", "purchase_purpose", purpose,
                  view.detail("purchase_purpose", summary=f"购车用途为{purpose}"), len(items) + 1,
                  extra_digest=purpose)
        )
    for concern in view.items("core_concerns"):
        items.append(
            _item("visit", "core_concerns", concern,
                  view.detail("core_concerns", summary=f"关注{concern}"), len(items) + 1,
                  extra_digest=concern)
        )
    return items


def _category_vehicle(view: _FactView) -> list[dict[str, Any]]:
    """当前用车：契约的 ``current_vehicle_info`` + 编外三个车型/车龄字段。

    **编外字段在这里第一次有了落点**：新契约用 ``current_vehicle_info`` 装现车，
    但它是一段自由文本，承载不了「旧车车型 / 品牌 / 车龄」这三项结构化信息，
    而「客户在开什么车」是最基础的销售信息（核查清单 §5）。
    """
    items: list[dict[str, Any]] = []
    current = view.text("current_vehicle_info")
    if current:
        items.append(
            _item("vehicle", "current_vehicle_info", current,
                  view.detail("current_vehicle_info"), len(items) + 1)
        )
    for attribute in ("old_car_model", "owned_brand_model", "owned_car_age"):
        text = view.text(attribute)
        if not text:
            continue
        # ---- 与 ``current_vehicle_info`` 重复的不再列一次。
        # 这两处装的常常是同一台车（L1 从同一句「我现在开的是一九年的君威」里同时抽出
        # 结构化车型与自由文本），照单全收会让销售在卡片上看到两条同内容的「当前用车」。
        # 判据用**包含**而非相等：``current_vehicle_info`` 常写成
        # 「有车（2019款别克君威，车龄6年）」，而 ``old_car_model`` 只是其中一个片段。
        if text in current:
            continue
        items.append(
            _item("vehicle", attribute, f"{inf.name_of(attribute)}：{text}",
                  view.detail(attribute), len(items) + 1, extra_digest=text)
        )
    return items


def _category_change(view: _FactView) -> list[dict[str, Any]]:
    """换车动因：编外的 ``purchase_nature``=换购 + ``pain_points``（对现有车的不满）。

    **只在 ``purchase_nature`` 确为换购时才成立**：首购客户没有「换车动因」，
    把他对别的事物的不满写进这一类会误导销售。``pain_points`` 是契约字段，
    但它既可能是「现有车的问题」也可能是「用车的痛点」，故以 ``purchase_nature``
    为闸门而不是无条件采信。
    """
    nature = _as_text(view.raw("purchase_nature"))
    items: list[dict[str, Any]] = []
    if nature != REPLACEMENT_PURCHASE:
        return items
    label = _label("purchase_nature", nature) or "换购"
    items.append(
        _item("change", "purchase_nature", f"购车性质：{label}",
              view.detail("purchase_nature", summary="由旧车证据判定为换购"),
              len(items) + 1, extra_digest=nature)
    )
    for pain in _as_list(view.raw("pain_points")):
        text = _as_text(pain)
        if not text:
            continue
        items.append(
            _item("change", "pain_points", text, view.detail("pain_points"),
                  len(items) + 1, extra_digest=text)
        )
    return items


def _category_compare(view: _FactView) -> list[dict[str, Any]]:
    """对比车型：``competing_brand`` / ``competing_model`` / ``test_driven_models``。

    **三者同列而不是只取一个**：品牌、车型、已试驾的车型是客户比价路径上的三个
    不同事实，销售需要看到全貌。品牌与车型分不清时两个字段可同时给值（契约口径），
    这里照单全收。
    """
    items: list[dict[str, Any]] = []
    for attribute in ("competing_model", "competing_brand", "test_driven_models"):
        prefix = "已试驾：" if attribute == "test_driven_models" else ""
        for text in view.items(attribute):
            items.append(
                _item("compare", attribute, f"{prefix}{text}",
                      view.detail(attribute), len(items) + 1, extra_digest=text)
            )
    return items


def _category_focus(view: _FactView) -> list[dict[str, Any]]:
    """核心关注：``core_concerns``（油耗 / 安全 / 空间…）。"""
    items: list[dict[str, Any]] = []
    for concern in view.items("core_concerns"):
        items.append(
            _item("focus", "core_concerns", concern,
                  view.detail("core_concerns", summary=f"关注{concern}"), len(items) + 1,
                  extra_digest=concern)
        )
    return items


def _category_barrier(
    view: _FactView, express: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """决策阻力：``brand_concerns`` + 表达层的 ``resistances``。

    **``resistances`` 由 Notes 迁到这里**（核查清单 §5）：它已离开 Notes 契约，
    内容改走本区块与 ``deal_battle.loss_conditions``。它由表达层产出（措辞是模型给的），
    因此本函数从 ``express`` 读，而不是从 facts 读。
    """
    items: list[dict[str, Any]] = []
    for concern in view.items("brand_concerns"):
        items.append(
            _item("barrier", "brand_concerns", concern,
                  view.detail("brand_concerns"), len(items) + 1, extra_digest=concern)
        )
    for entry in _resistances(express):
        name = _as_text(entry.get("resistance_name"))
        if not name:
            continue
        detail = entry.get("reasoning_detail")
        if not isinstance(detail, dict):
            desc = _as_text(entry.get("description"))
            detail = {
                "summary": desc or f"客户顾虑：{name}",
                "steps": ["由表达层依已定稿事实归纳"],
                "evidence": [],
                "confidence": _DEFAULT_CONFIDENCE,
            }
        items.append(
            _item("barrier", "resistances", name, _normalize_detail(detail),
                  len(items) + 1, extra_digest=name)
        )
    return items


def _resistances(express: Mapping[str, Any]) -> list[dict[str, Any]]:
    """读表达层的 ``resistances`` 数组（可能嵌在 ``customer_overview`` 里）。"""
    value = express.get("resistances")
    if not isinstance(value, list):
        overview = express.get("customer_overview")
        value = overview.get("resistances") if isinstance(overview, dict) else None
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


# ====================================================================
# 区块三：deal_battle
# ====================================================================


def build_deal_battle(
    view: _FactView, express: Mapping[str, Any]
) -> dict[str, Any]:
    """``deal_battle``：成交有利因素与战败风险因素，两个数组结构相同。

    **``win_conditions`` 与 ``loss_conditions`` 的 id 也需全局唯一**（契约明文），
    因此两者共用同一前缀空间（``win-`` / ``loss-``），并在最后统一断言。
    """
    battle: dict[str, Any] = {}
    win = _win_conditions(view)
    loss = _loss_conditions(view, express)
    if win:
        battle["win_conditions"] = _dedupe(win)
    if loss:
        battle["loss_conditions"] = _dedupe(loss)
    return battle


def _win_conditions(view: _FactView) -> list[dict[str, Any]]:
    """按「客户已表态的关注点 → 已建立的品牌认知 → 时间紧迫度 → 牌照条件」取料。

    这四项的共同点是**它们都指向「推进」**：在意某件事说明他有需求，了解品牌说明
    他被说服过，时间窗明确说明他准备好了，牌照条件明确说明流程上只差一步。
    """
    items: list[dict[str, Any]] = []

    for concern in view.items("core_concerns"):
        items.append(
            _item("win", "core_concerns", f"客户明确关注{concern}",
                  view.detail("core_concerns"), len(items) + 1, extra_digest=concern)
        )

    # —— 品牌认知门：只看 basic 与 deep。none 与 unknown 都不构成成交有利因素。
    for attribute in ("knowledge_of_mengshi", "knowledge_of_erev", "knowledge_of_adas"):
        raw = _as_text(view.raw(attribute))
        if KNOWLEDGE_RANK.get(raw, -1) < KNOWLEDGE_RANK["basic"]:
            continue
        display = _label(attribute, raw)
        items.append(
            _item("win", attribute, f"{_knowledge_name(attribute)}：{display}",
                  view.detail(attribute), len(items) + 1, extra_digest=raw)
        )

    # —— 时间窗：三个月内及更近的才构成紧迫度。
    planned = _as_text(view.raw("planned_purchase_time"))
    if planned in {"this_week", "this_month", "within_3_months"}:
        items.append(
            _item("win", "planned_purchase_time",
                  f"计划购车时间：{_label('planned_purchase_time', planned)}",
                  view.detail("planned_purchase_time"), len(items) + 1, extra_digest=planned)
        )

    plate = _as_text(view.raw("license_plate_status"))
    if plate:
        items.append(
            _item("win", "license_plate_status",
                  f"牌照条件：{_label('license_plate_status', plate)}",
                  view.detail("license_plate_status"), len(items) + 1, extra_digest=plate)
        )
    return items


def _knowledge_name(attribute: str) -> str:
    names = {
        "knowledge_of_mengshi": "对本方品牌的了解",
        "knowledge_of_erev": "对增程技术的了解",
        "knowledge_of_adas": "对智驾的了解",
    }
    return names.get(attribute, inf.name_of(attribute))


def _loss_conditions(
    view: _FactView, express: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """按「本方品牌的担忧 → 表达层的抗性 → 竞品竞争 → 征信风险」取料。

    **竞品进战败风险**：客户在比价本身就是流失风险，而不是有利因素——同一个事实
    在 ``purchase_decisions`` 里是中性信息，在这里是风险信号，两处都写是对的。
    """
    items: list[dict[str, Any]] = []

    for concern in view.items("brand_concerns"):
        items.append(
            _item("loss", "brand_concerns", f"对{concern}存在担忧",
                  view.detail("brand_concerns"), len(items) + 1, extra_digest=concern)
        )

    for entry in _resistances(express):
        name = _as_text(entry.get("resistance_name"))
        if not name:
            continue
        detail = entry.get("reasoning_detail")
        if not isinstance(detail, dict):
            detail = {
                "summary": _as_text(entry.get("description")) or f"客户顾虑：{name}",
                "steps": ["由表达层依已定稿事实归纳"],
                "evidence": [],
                "confidence": _DEFAULT_CONFIDENCE,
            }
        items.append(
            _item("loss", "resistances", name, _normalize_detail(detail),
                  len(items) + 1, extra_digest=name)
        )

    for attribute in ("competing_model", "competing_brand"):
        for text in view.items(attribute):
            items.append(
                _item("loss", attribute, f"存在竞品对比：{text}",
                      view.detail(attribute), len(items) + 1, extra_digest=text)
            )

    credit = view.text("credit_status")
    if credit:
        items.append(
            _item("loss", "credit_status", f"征信情况：{credit}",
                  view.detail("credit_status"), len(items) + 1, extra_digest=credit)
        )
    return items


# ====================================================================
# 区块四：decision_psychology
# ====================================================================


def build_decision_psychology(
    view: _FactView, express: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """``decision_psychology``：四个固定 ``key``，每客户 0~4 条。

    **三个编外字段在这里落位**（核查清单 §5 的「迁移」处置）：

    - ``personality_and_preferences`` → ``decision_style``
    - ``communication_rhythm`` + ``contact_preference`` → ``communication_preference``
    - ``emotion`` + 表达层 ``emotion_state`` → ``emotion_state``

    它们的取值离开 Notes 之后，唯一还能被业务方看到的地方就是这里——这正是
    「迁移」而非「舍弃」的含义。
    """
    entries: list[dict[str, Any]] = []
    for index, (key, label) in enumerate(PSYCHOLOGY_KEYS, start=1):
        value, detail = _psychology_value(key, view, express)
        if not value:
            continue
        entries.append(
            {
                "key": key,
                "label": label,
                "value": value,
                "reasoning_detail": detail,
                "rank_order": index,
            }
        )
    return entries


def _psychology_value(
    key: str, view: _FactView, express: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    """每个 key 的取值与依据。顺序即优先级：编外字段在前，表达层补充在后。"""
    if key == PSYCH_DECISION_STYLE:
        values = view.items("personality_and_preferences")
        if values:
            return "、".join(values), view.detail("personality_and_preferences")
        return "", _empty_detail()

    if key == PSYCH_CURRENT_ATTITUDE:
        progress = view.text("viewing_progress")
        knowledge = view.text("knowledge_of_mengshi")
        parts = [p for p in (progress, knowledge) if p]
        if not parts:
            return "", _empty_detail()
        return "；".join(parts), view.detail("viewing_progress", "knowledge_of_mengshi")

    if key == PSYCH_COMMUNICATION_PREFERENCE:
        contact = view.items("contact_preference")
        rhythm = view.items("communication_rhythm")
        parts = [p for p in contact + rhythm if p]
        if not parts:
            return "", _empty_detail()
        return "；".join(parts), view.detail("communication_rhythm", "contact_preference")

    # —— emotion_state：编外 emotion 与表达层结论都可能有，拼成一句。
    emotion = view.items("emotion")
    state = _express_emotion_state(express)
    parts = [p for p in emotion + ([state] if state else []) if p]
    if not parts:
        return "", _empty_detail()
    detail = view.detail("emotion")
    if not detail["summary"] and state:
        detail = {
            "summary": f"当前情绪状态：{state}",
            "steps": ["由表达层依已定稿事实归纳"],
            "evidence": [],
            "confidence": _DEFAULT_CONFIDENCE,
        }
    return "；".join(parts), detail


def _express_emotion_state(express: Mapping[str, Any]) -> str:
    """从表达层读情绪结论。

    表达层给的是 ``emotion_state.current_state.value``（对象套对象），且这一节它的
    形状本来就是对象——因此这里**只**认对象形态，不再去顶层找平铺的键：认平铺形态
    会把 ``emotion_state`` 这个 section 名误当成取值。
    """
    state = express.get("emotion_state")
    if not isinstance(state, dict):
        return ""
    current = state.get("current_state")
    if isinstance(current, dict):
        return _as_text(current.get("value"))
    return _as_text(current)


# ====================================================================
# 组装
# ====================================================================


def build_analysis_result(
    phone: Any = "",
    batch_id: Any = "",
    facts_json: Any = "{}",
    notes_updates_json: Any = "[]",
    express_json: Any = "{}",
) -> str:
    """把一份已定稿的事实装成回传请求体（顶层三键），返回 **JSON 字符串**。

    **为什么返回字符串而不是 dict**：这个函数是工作流 ``code`` 节点的实现，节点输出
    会被下游 ``@body_ref`` 直接当请求体用；在节点的出参上留一个 JSON 串是最稳的形态
    ——它不依赖执行器对复杂类型的序列化行为，也让产物在 ``result1`` 之外可单独落盘核对。

    输入：

    - ``facts_json``：L2 定稿的事实表（契约字段 + 编外字段）；
    - ``notes_updates_json``：L2 审查后的 Notes 更新项（``basic_notes_updates`` 的来源，
      同时是四个区块 ``reasoning_detail`` 的措辞来源）；
    - ``express_json``：L3 表达层的 section 产物（``resistances`` / ``emotion_state`` /
      ``overall_tag`` 从这里取）。

    **本函数不发任何请求**：它只造请求体。真实回传由带 ``@writeback`` 的节点负责，
    默认开关关闭（``WRITEBACK_ENABLED=false``）。
    """
    facts = _payload(facts_json)
    if not isinstance(facts, dict):
        facts = {}
    notes = _payload(notes_updates_json)
    if isinstance(notes, dict):
        notes = notes.get("basic_notes_updates", [])
    if not isinstance(notes, (list, tuple)):
        notes = []
    express = _payload(express_json)
    if isinstance(express, dict):
        express = express.get("analysis_result", express)
    if not isinstance(express, dict):
        express = {}

    view = _FactView(facts, notes)

    analysis_result: dict[str, Any] = {
        "profile_summary": build_profile_summary(view),
        "basic_notes_updates": build_basic_notes_updates(notes),
    }

    insight = build_ai_insight(view, express)
    if insight:
        analysis_result["ai_insight"] = insight

    decisions = build_purchase_decisions(view, express)
    if decisions:
        analysis_result["purchase_decisions"] = decisions

    battle = build_deal_battle(view, express)
    if battle:
        analysis_result["deal_battle"] = battle

    psychology = build_decision_psychology(view, express)
    if psychology:
        analysis_result["decision_psychology"] = psychology

    # ---- 全局唯一性：``(phone, item_id)`` 与 ``(phone, psychology_key)``。
    # id 重复会让整次回传递交失败，因此**在这里就拦下**，而不是等接口返回 500。
    item_ids: list[str] = []
    for block in decisions:
        item_ids.extend(item["id"] for item in block["items"])
    for key in ("win_conditions", "loss_conditions"):
        item_ids.extend(item["id"] for item in battle.get(key) or [])
    _assert_unique_ids(item_ids, "purchase_decisions / deal_battle 的 item_id")
    _assert_unique_ids(
        [entry["key"] for entry in psychology], "decision_psychology 的 psychology_key"
    )

    payload = {
        "phone": "" if phone is None else str(phone),
        "batch_id": "" if batch_id is None else str(batch_id),
        "analysis_result": analysis_result,
    }
    return json.dumps(payload, ensure_ascii=False)


def main(
    phone: Any = "",
    batch_id: Any = "",
    facts_json: Any = "{}",
    notes_updates_json: Any = "[]",
    express_json: Any = "{}",
) -> dict[str, Any]:
    """工作流 ``code`` 节点的入口（返回 ``{出参名: 值}``，由执行器逐字段取出）。

    **本模块不登记 ``CODE_SPECS``**：那张表服务的是「迁移节点 × 逐字符比对」，
    记录的是**本模块内**的函数名。本节点不是迁移节点，而节点定义用的是显式的
    ``customer_profile.callback_build:build_analysis_result`` 路径，本表填不填都不影响执行
    ——填一条 ``build_analysis_result`` 反而会让它看起来像是本模块内的函数。
    """
    return {
        "callback_json": build_analysis_result(
            phone, batch_id, facts_json, notes_updates_json, express_json
        )
    }


__all__ = [
    "PURCHASE_CATEGORIES",
    "PSYCHOLOGY_KEYS",
    "CATEGORY_VISIT",
    "CATEGORY_VEHICLE",
    "CATEGORY_CHANGE",
    "CATEGORY_COMPARE",
    "CATEGORY_FOCUS",
    "CATEGORY_BARRIER",
    "build_analysis_result",
    "build_ai_insight",
    "build_purchase_decisions",
    "build_deal_battle",
    "build_decision_psychology",
    "build_profile_summary",
    "build_basic_notes_updates",
    "main",
]

# 契约里的字段键集合在这里被引用一次，供静态检查与用例确认白名单来源。
_ = nc.ALL_KEYS
