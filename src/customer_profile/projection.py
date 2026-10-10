"""L2 核对与投影层（纯代码，0 token）。

全案的枢纽：唯一同时看得见**所有字段、所有断言、所有来源**的地方，因此「字段间矛盾」
在这一层被消解，而不是靠提示词祈祷模型不写矛盾（`端到端方案.md` §3.4）。

输入 ``claims[]``，输出 ``facts`` + ``notes_updates[]`` + ``to_confirm[]``：:

    claims[]   断言账本（L1 产出，或离线回放时由既有字段重建）
      ↓ a 归属过滤 → b 来源分级 → c 冲突消解 → d 枚举归一
      ↓ e 单/多值约束 → f 跨字段规则 → g 缺失即留白
    facts          规范事实：一个字段一个值（或一组多值），有据可查
    notes_updates[] 42 字段投影结果，形状与基线 `basic_notes_updates` 同形
    to_confirm[]   无法坐实的字段：**空着比填错强**，交销售补充

三条设计口径：

1. **宁空勿错**。冲突无法按来源排序消解时，字段留白并转 ``to_confirm``，
   绝不把两个值都写出去——那正是「Notes 里旧车车型有值、购买类型却仍是首购」的成因。
2. **来源分级是硬顺序**：人工/系统登记 > 一手交互 > 公开登记查询 > 社交自呈现 > 第三方预测。
   ``prediction`` **不得单独支撑** ``基础身份`` 类字段（16 个），只能进卡片软性叙述。
3. **枚举只认契约**。合法键、单/多选、结构化枚举的 ``input_value`` 全部读
   :mod:`customer_profile.notes_contract`，不在这里手抄一份。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import notes_contract as nc

# ====================================================================
# 来源分级
# ====================================================================

REGISTRY = "registry"
"""人工/系统登记：销售确认的 Notes、销售系统留痕、对历史画像的人工反馈。"""

FIRSTHAND = "firsthand"
"""一手交互：试驾录音、外呼录音、与销售的聊天记录。"""

PUBLIC_RECORD = "public_record"
"""公开登记查询：天眼查关联企业、手机号归属地。"""

SOCIAL = "social"
"""社交自呈现：微信/朋友圈/支付宝/小红书/抖音截图。"""

PREDICTION = "prediction"
"""第三方统计预测（极光打点）。"""

SOURCE_TIERS: tuple[str, ...] = (REGISTRY, FIRSTHAND, PUBLIC_RECORD, SOCIAL, PREDICTION)
_TIER_RANK: dict[str, int] = {tier: i for i, tier in enumerate(SOURCE_TIERS)}

TIER_LABEL: dict[str, str] = {
    REGISTRY: "人工/系统登记",
    FIRSTHAND: "一手交互",
    PUBLIC_RECORD: "公开登记查询",
    SOCIAL: "社交自呈现",
    PREDICTION: "第三方预测",
}

CHANNEL_TIER: dict[str, str] = {
    # —— 人工/系统登记
    "销售人员人工确认的客户Notes信息": REGISTRY,
    "客户留存在我方销售数据系统中的信息记录": REGISTRY,
    "销售人员对当前客户历史画像数据的反馈记录": REGISTRY,
    # —— 一手交互
    "试驾录音信息": FIRSTHAND,
    "外呼录音信息": FIRSTHAND,
    "客户与我方销售人员的聊天记录信息": FIRSTHAND,
    # —— 公开登记查询
    "关联企业信息": PUBLIC_RECORD,
    "手机号归属地信息": PUBLIC_RECORD,
    # —— 社交自呈现
    "朋友圈截图内容信息": SOCIAL,
    "微信手机号搜索界面信息": SOCIAL,
    "微信主页截图信息": SOCIAL,
    "支付宝主页截图信息": SOCIAL,
    "小红书主页截图信息": SOCIAL,
    "抖音主页截图信息": SOCIAL,
    # —— 第三方预测
    "第三方画像数据": PREDICTION,
}
"""渠道 → 来源分级。渠道名与 :mod:`customer_profile.workflows.customer_profile_pipeline`
的渠道表逐字一致（那里是取数口径，这里是消费口径，测试钉住两者同集合）。"""

IDENTITY_SUBJECT_EXEMPT: frozenset[str] = frozenset({"family_structure"})
"""身份字段里**不要求**主体为客户本人的例外。

婚否、有无孩子这类事实，其合理来源本就可能是配偶或家人（`端到端方案.md` §3.4 a：
「``subject != customer`` 的断言不得写入身份字段；**可写入家庭结构**、可进入卡片叙述」）。
因此归属过滤对这一项放行，其余 15 个身份字段一律要求主体是客户本人。"""

# 跨产物字段：同一事实在卡片与 Notes 各有一处，L2 在这里**唯一化**。
LEVEL_KEYS: tuple[str, ...] = ("deal_level", "business_opp_level")
"""等级在卡片里有两个载体（``profile_summary.deal_level`` 与
``customer_overview.business_opp_level.value``）。基线 39 案里 **4 案**两者不一致，
销售因此看到两个等级。L2 只保留一个。"""

LEVEL_ORDER: tuple[str, ...] = ("A", "B", "C", "D")
"""等级由高到低。冲突时取**证据更强者**，无法判定则取较高档并转 ``to_confirm``。"""

AGE_PATTERN = re.compile(r"(\d{1,3})\s*[~—\-－至]\s*(\d{1,3})")
AGE_OPEN_PATTERN = re.compile(r"(\d{1,3})\s*岁?\s*以上")

# ====================================================================
# 断言
# ====================================================================


@dataclass(frozen=True, slots=True)
class Claim:
    """一条断言账本条目。

    L1 产出它，离线回放时由既有字段重建。字段与 `端到端方案.md` §3.3 的 ``claims[]``
    对齐，去掉 ``span`` / ``quote``（那是「能否回溯到原文」的凭据，只影响 L1 的取舍，
    投影层按 ``record_id`` / ``channel`` 判来源即可）。
    """

    attribute: str
    """字段键（Notes 契约键，或 ``deal_level`` 这类跨产物字段）。"""

    value: Any
    channel: str = ""
    """来源渠道名（决定来源分级）。"""

    subject: str = "customer"
    """事实主体：``customer`` / ``spouse`` / ``family`` / ``colleague`` / ``salesperson`` /
    ``other`` / ``unknown``。"""

    claim_type: str = "fact"
    """``fact`` / ``observation`` / ``prediction``。"""

    created_at: str = ""
    claim_id: str = ""

    @property
    def tier(self) -> str:
        return CHANNEL_TIER.get(self.channel, SOCIAL)

    @property
    def rank(self) -> int:
        return _TIER_RANK.get(self.tier, len(SOURCE_TIERS))

    @property
    def is_prediction(self) -> bool:
        return self.claim_type == "prediction" or self.tier == PREDICTION


@dataclass(slots=True)
class Projection:
    """L2 的产物。"""

    facts: dict[str, Any] = field(default_factory=dict)
    notes_updates: list[dict[str, Any]] = field(default_factory=list)
    to_confirm: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return {
            "facts": self.facts,
            "notes_updates": self.notes_updates,
            "to_confirm": self.to_confirm,
            "rejected": self.rejected,
        }


# ====================================================================
# 取值归一
# ====================================================================


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, default=str)


def split_multi(value: Any) -> list[Any]:
    """把 ``"a||b"`` / ``["a","b"]`` 这类多值表示摊平成列表。

    ``||`` 是基线里模型自造的连接符（`核心关注点` 等 5 个多选字段大量出现），
    不是契约的一部分，因此只在**多选字段**上按它切分。
    """
    if isinstance(value, (list, tuple)):
        out: list[Any] = []
        for item in value:
            out.extend(split_multi(item))
        return out
    if isinstance(value, str) and "||" in value:
        return [part.strip() for part in value.split("||") if part.strip()]
    return [value]


def _parse_structured(value: Any) -> dict | None:
    """把 ``'{"key":"yes", "input_value":"驾龄年限"}'`` 这类字符串解成字典。"""
    if isinstance(value, Mapping):
        return dict(value)
    text = _as_text(value)
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def normalize_enum(attribute: str, value: Any) -> Any:
    """把一个取值归一到契约枚举键。

    接受四种形态，全部实测出现过：

    1. **已是合法键**（``"fuel_efficiency"``）→ 原样；
    2. **中文展示名**（``"油耗"``）→ 反查得键；
    3. **结构化对象**（``'{"key":"yes","input_value":"..."}'``）→ 解出并补 ``input_value``；
    4. **不合法** → ``None``（由调用方决定拒绝还是留白）。

    多选字段由调用方先 :func:`split_multi`，本函数只处理单个值。
    """
    if not nc.is_enum(attribute):
        return None
    legal = nc.legal_keys(attribute)

    structured = _parse_structured(value)
    if structured is not None:
        key = _as_text(structured.get("key"))
        if key not in legal:
            key = nc.display_to_key(attribute, key) or ""
        if not key:
            return None
        if nc.is_structured(attribute, key):
            raw_input = structured.get("input_value")
            return {"key": key, "input_value": _as_text(raw_input)}
        return key

    text = _as_text(value)
    if not text:
        return None
    if text in legal:
        return text
    by_display = nc.display_to_key(attribute, text)
    if by_display:
        if nc.is_structured(attribute, by_display):
            # 只给了展示名、没给 input_value：按契约补空串，由 L4 校验提示补充。
            return {"key": by_display, "input_value": ""}
        return by_display
    return None


def normalize_age(value: Any) -> str | None:
    """把年龄归一到统一格式。

    两套格式实测并存（卡片用 ``"40 ~ 49"``、Notes 用 ``"40-49岁"``，还有 ``"25~30岁"``、
    ``"50岁以上"``），同一客户的年龄因此在两处长得不一样。归一后统一为
    ``"40 ~ 49 岁"`` / ``"50 岁以上"``，两处取同一个值。
    """
    text = _as_text(value)
    if not text:
        return None
    matched = AGE_PATTERN.search(text)
    if matched:
        low, high = int(matched.group(1)), int(matched.group(2))
        if low > high:
            low, high = high, low
        return f"{low} ~ {high} 岁"
    opened = AGE_OPEN_PATTERN.search(text)
    if opened:
        return f"{int(opened.group(1))} 岁以上"
    return None


def normalize_level(value: Any) -> str | None:
    """把等级归一到 ``A`` / ``B`` / ``C`` / ``D``。"""
    text = _as_text(value).upper()
    for level in LEVEL_ORDER:
        if text == level or text.startswith(level):
            return level
    return None


# ====================================================================
# 冲突消解
# ====================================================================


def _pick_best(group: Sequence[Claim]) -> tuple[Claim, list[Claim]]:
    """按来源分级挑出最强的一条，返回 ``(最佳, 同档并列)``。"""
    best_rank = min(claim.rank for claim in group)
    same_tier = [claim for claim in group if claim.rank == best_rank]
    same_tier.sort(key=lambda c: (c.created_at or ""), reverse=True)
    return same_tier[0], same_tier


def _diverse(values: Iterable[Any]) -> list[Any]:
    """去重且保序（值可能是 dict，不能直接进 set）。"""
    out: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


# ====================================================================
# 主流程
# ====================================================================


def project(
    claims: Sequence[Claim],
    *,
    context: Mapping[str, Any] | None = None,
) -> Projection:
    """把断言账本投影成规范事实与 Notes 更新项。

    ``context`` 承载投影所需、但不在字段值里的旁证：

    - ``company_deregistered``：关联企业的登记状态是否为「注销」。
      已注销主体**不得**当作在职企业填入（基线 ``13926073386`` 即此缺陷）。
    - ``has_old_car_evidence``：原文里是否出现旧车证据（用于补 ``old_car_model``
      与纠正 ``purchase_type``）。
    """
    context = dict(context or {})
    result = Projection()

    # ---- a 归属过滤：非客户本人的断言不得写入身份字段
    kept: list[Claim] = []
    for claim in claims:
        if not claim.attribute:
            continue
        if (
            claim.attribute in nc.IDENTITY_KEYS
            and claim.attribute not in IDENTITY_SUBJECT_EXEMPT
            and claim.subject != "customer"
        ):
            result.rejected.append(
                {
                    "attribute": claim.attribute,
                    "value": claim.value,
                    "rule": "a_attribution",
                    "reason": f"事实主体是 {claim.subject}，不是客户本人",
                }
            )
            continue
        kept.append(claim)

    # ---- b 来源分级：预测不得**单独**支撑身份字段
    grouped: dict[str, list[Claim]] = {}
    for claim in kept:
        grouped.setdefault(claim.attribute, []).append(claim)

    surviving: dict[str, list[Claim]] = {}
    for attribute, group in grouped.items():
        if attribute in nc.IDENTITY_KEYS and all(c.is_prediction for c in group):
            for claim in group:
                result.rejected.append(
                    {
                        "attribute": attribute,
                        "value": claim.value,
                        "rule": "b_prediction_only",
                        "reason": "身份字段仅由第三方预测支撑",
                    }
                )
            result.to_confirm.append(
                {
                    "attribute_key": attribute,
                    "reason": "仅有第三方预测，不构成客户事实",
                    "suggested_action": "向客户或销售确认后补充",
                }
            )
            continue
        surviving[attribute] = group

    # ---- c/d/e 冲突消解 + 枚举归一 + 单多值约束
    for attribute, group in surviving.items():
        best, same_tier = _pick_best(group)
        values = _diverse(claim.value for claim in same_tier)

        if len(values) > 1 and not nc.is_multi_value(attribute):
            # 同来源档给出多个不同值 → 不可判，留白转确认，绝不两个都写。
            result.to_confirm.append(
                {
                    "attribute_key": attribute,
                    "candidates": values,
                    "reason": f"同来源档（{TIER_LABEL[best.tier]}）给出 {len(values)} 个不同值",
                    "suggested_action": "人工确认以哪一个为准",
                }
            )
            result.rejected.append(
                {
                    "attribute": attribute,
                    "value": values,
                    "rule": "c_conflict",
                    "reason": "同档多值冲突，未写入",
                }
            )
            continue

        if nc.is_enum(attribute):
            normalized: list[Any] = []
            pool = values if nc.is_multi_value(attribute) else [best.value]
            for raw in pool:
                for item in split_multi(raw):
                    key = normalize_enum(attribute, item)
                    if key is None:
                        result.rejected.append(
                            {
                                "attribute": attribute,
                                "value": item,
                                "rule": "d_enum",
                                "reason": "非法枚举值，无法归一",
                            }
                        )
                        continue
                    normalized.append(key)
            normalized = _diverse(normalized)
            if not normalized:
                result.to_confirm.append(
                    {
                        "attribute_key": attribute,
                        "reason": "取值无法归一到契约枚举",
                        "suggested_action": "人工确认后补填",
                    }
                )
                continue
            result.facts[attribute] = normalized[0] if nc.is_single_value(attribute) else normalized
        else:
            result.facts[attribute] = values[0] if len(values) == 1 else values

    # ---- f 跨字段规则
    _apply_cross_field_rules(result, context)

    # ---- 等级唯一化（跨产物字段）
    _unify_level(result)

    # ---- g 缺失即留白 + 产出 notes_updates
    result.notes_updates = _to_notes_updates(result, group_by_attribute(claims))
    return result


def group_by_attribute(claims: Sequence[Claim]) -> dict[str, list[Claim]]:
    grouped: dict[str, list[Claim]] = {}
    for claim in claims:
        grouped.setdefault(claim.attribute, []).append(claim)
    return grouped


def _apply_cross_field_rules(result: Projection, context: Mapping[str, Any]) -> None:
    """跨字段一致性：一处有证据，另一处必须跟上；一处被否，另一处不得留。"""
    facts = result.facts

    # —— 旧车 ⇒ 换购。基线多案正文写了旧车、``purchase_type`` 却仍填 ``first``。
    old_car = _as_text(facts.get("old_car_model"))
    has_old_car = bool(old_car) and old_car not in {"无", "没有", "无旧车"}
    has_old_car = has_old_car or bool(context.get("has_old_car_evidence"))
    trade_in = facts.get("trade_in_requirement")
    trade_in_yes = trade_in == "yes" or (isinstance(trade_in, dict) and trade_in.get("key") == "yes")

    # ``trade_in_requirement=yes`` **本身就是**「有旧车」的凭据，因此它与旧车证据同权：
    # 任一条成立即把购买类型定为换购。这里**不**清除 ``trade_in_requirement``——
    # 清除它会抹掉置换的唯一凭据，反而制造新的自相矛盾。
    if (has_old_car or trade_in_yes) and facts.get("purchase_type") not in {"replacement", "company"}:
        facts["purchase_type"] = "replacement"
    if not (has_old_car or trade_in_yes) and facts.get("purchase_type") == "replacement":
        # 声称换购却没有任何旧车凭据 → 撤销该断言，而不是留一个无证的「换购」。
        facts.pop("purchase_type", None)
        result.rejected.append(
            {
                "attribute": "purchase_type",
                "value": "replacement",
                "rule": "f_trade_in",
                "reason": "声称换购但无旧车证据支撑",
            }
        )
        result.to_confirm.append(
            {
                "attribute_key": "purchase_type",
                "reason": "换购与其证据不一致",
                "suggested_action": "确认客户是否有旧车置换",
            }
        )

    # —— 已注销企业 ⇒ 不填在职属性
    if context.get("company_deregistered"):
        for key in ("company", "company_type"):
            if key in facts:
                result.rejected.append(
                    {
                        "attribute": key,
                        "value": facts.pop(key),
                        "rule": "f_deregistered",
                        "reason": "关联企业登记状态为注销，不得当作在职企业",
                    }
                )

    # —— 年龄/姓名等跨产物字段在卡片侧的唯一化由 _to_notes_updates 与 L3 共用同一 facts。


def _unify_level(result: Projection) -> None:
    """把 ``deal_level`` 与 ``business_opp_level`` 收敛成一个 ``deal_level``。"""
    values: list[tuple[str, str]] = []
    for key in LEVEL_KEYS:
        if key in result.facts:
            level = normalize_level(result.facts[key])
            if level:
                values.append((key, level))
            if key != "deal_level":
                result.facts.pop(key, None)

    if not values:
        return
    distinct = {level for _, level in values}
    chosen = min(distinct, key=lambda lv: LEVEL_ORDER.index(lv))
    result.facts["deal_level"] = chosen
    if len(distinct) > 1:
        result.rejected.append(
            {
                "attribute": "deal_level",
                "value": dict(values),
                "rule": "f_level",
                "reason": "两处等级不一致，取较高档并转人工确认",
            }
        )
        result.to_confirm.append(
            {
                "attribute_key": "deal_level",
                "candidates": sorted(distinct, key=lambda lv: LEVEL_ORDER.index(lv)),
                "reason": "卡片与商机等级不一致",
                "suggested_action": "人工确认最终等级",
            }
        )


def _to_notes_updates(
    result: Projection, grouped: Mapping[str, list[Claim]]
) -> list[dict[str, Any]]:
    """把 ``facts`` 投影成 42 字段形态的 Notes 更新项。

    只输出**有事实支撑**的字段（缺失即留白，规则 g）。``reasoning_detail.evidence``
    写「来源档（渠道）」，L3 阶段会替换为代码展开的原文切片——此刻不编造引文。
    """
    updates: list[dict[str, Any]] = []
    for key in nc.ALL_KEYS:
        if key not in result.facts:
            continue
        value = result.facts[key]
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and not value:
            continue
        if key == "age":
            value = normalize_age(value) or value

        sources = grouped.get(key) or []
        evidence: list[str] = []
        for claim in sorted(sources, key=lambda c: c.rank):
            label = f"{TIER_LABEL[claim.tier]}（{claim.channel or '未知渠道'}）"
            if label not in evidence:
                evidence.append(label)
        updates.append(
            {
                "attribute_key": key,
                "attribute_value": value,
                "reasoning_detail": {
                    "summary": f"由 {len(sources)} 条断言按来源分级投影得到",
                    "steps": [
                        f"归属过滤：剔除主体非客户本人的断言（保留 {len(sources)} 条）",
                        f"来源分级：取最高档 {TIER_LABEL[sources[0].tier] if sources else '未知'}",
                        "枚举归一 / 单多值约束：按 note-attributes 契约校验",
                    ],
                    "evidence": evidence,
                    "confidence": 0.9 if sources and sources[0].tier == REGISTRY else 0.7,
                },
            }
        )
    return updates


# ====================================================================
# 回放适配：由既有 Notes 条目重建断言
# ====================================================================

EVIDENCE_PREFIX = re.compile(r"^(?P<channel>[^：:]{2,40})[：:]")


def claims_from_notes_entry(entry: Mapping[str, Any]) -> list[Claim]:
    """把一条既有 ``basic_notes_updates`` 重建为断言。

    ``reasoning_detail.evidence`` 的每一条形如「渠道名：原文片段」，渠道名可与
    :data:`CHANNEL_TIER` 对齐；据此判定来源档与是否「仅第三方预测撑腰」。

    识别不出渠道的条目按 ``social`` 处理（保守：不会把它当登记事实）。
    """
    attribute = str(entry.get("attribute_key") or "")
    if not attribute:
        return []
    detail = entry.get("reasoning_detail") or {}
    snippets = [str(x) for x in (detail.get("evidence") or [])]
    if not snippets:
        return [Claim(attribute=attribute, value=entry.get("attribute_value"))]

    claims: list[Claim] = []
    for snippet in snippets:
        matched = EVIDENCE_PREFIX.match(snippet.strip())
        channel = ""
        if matched:
            name = matched.group("channel").strip()
            channel = name if name in CHANNEL_TIER else _match_channel(name)
        tier = CHANNEL_TIER.get(channel, SOCIAL)
        claims.append(
            Claim(
                attribute=attribute,
                value=entry.get("attribute_value"),
                channel=channel,
                claim_type="prediction" if tier == PREDICTION else "fact",
                created_at=str(detail.get("summary") or ""),
            )
        )
    return claims


def _match_channel(name: str) -> str:
    """证据前缀与渠道名不完全一致时按包含关系就近匹配。"""
    for channel in CHANNEL_TIER:
        if channel in name or name in channel:
            return channel
    aliases = {
        "试驾录音": "试驾录音信息",
        "外呼录音信息": "外呼录音信息",
        "外呼录音": "外呼录音信息",
        "微信手机号搜索界面": "微信手机号搜索界面信息",
        "朋友圈": "朋友圈截图内容信息",
        "聊天记录": "客户与我方销售人员的聊天记录信息",
        "第三方画像数据": "第三方画像数据",
        "第三方画像": "第三方画像数据",
        "极光": "第三方画像数据",
        "关联企业信息": "关联企业信息",
        "手机号归属地": "手机号归属地信息",
    }
    for prefix, channel in aliases.items():
        if prefix in name:
            return channel
    return ""
