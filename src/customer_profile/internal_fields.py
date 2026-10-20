"""**编外字段**：只在内部流程中保留、调 API 回传前裁剪掉的字段。

v2 契约下线了 17 个字段。其中相当一批对销售策略有直接作用，因此不在本轮真的丢弃，
而是留在 ``facts`` 里继续取证——``facts`` 本就不受契约约束，内部保留几乎不需要改代码，
额外要做的只是让**回传体不再等于契约字段的遍历结果**。

三类处置（详见 ``docs/design/AI画像v2回传改造方案.md`` §4）：

1. **编外**（``INTERNAL_ONLY_KEYS``）：无回传位，内部照旧产出；
2. **迁移**（``MIGRATED_KEYS``）：值离开 Notes，落进画像卡的
   ``decision_psychology`` / ``deal_battle``；
3. **舍弃**（``DROPPED_KEYS``）：不再取证，仅在此登记以说明「为什么契约里没有了」。

**为什么要区分身份类编外字段**（``INTERNAL_IDENTITY_KEYS``）：身份字段在 L2 投影层
享有两道**额外**的保护——归属过滤（非客户本人的断言不得写入）与值形态门（取值指向
我方销售顾问的整条拒收）。这两道门原来按 ``notes_contract.IDENTITY_KEYS`` 判定，
字段一旦离开契约，保护会**静默消失**：``company`` 下线后，一条
``subject=unknown`` 的「我们单位也要买一个」就会被当作客户的企业写出去。
因此这里显式登记「虽然不在契约里、但仍属身份语义」的字段，由投影层并进判定集合。
"""

from __future__ import annotations

from typing import Any

from . import notes_contract as nc

INTERNAL_ONLY_KEYS: tuple[str, ...] = (
    # —— 换购推断的旁证链，且「客户在开什么车」是最基础的销售信息。
    #    新契约的 current_vehicle_info 能装现车，但承载不了它与购买类型的联动规则。
    "old_car_model",
    "owned_brand_model",
    "owned_car_age",
    # —— 关联企业是唯一能交叉验证职业与收入的渠道；「已注销企业不得当作在职」靠它。
    "company",
    "company_type",
    # —— 家庭用车决策的核心（谁开、坐几人），直接影响车型推荐。
    "seating_requirement",
    "primary_driver",
    # —— 意向品牌（区别于意向车型）、用车频次、驾驶感受、个性化要求，可读性高。
    "intended_brand",
    "usage_frequency",
    "driving_preference",
    "special_requirements",
    # —— 本轮**新增**的编外字段：承接被新 purchase_type 枚举（只剩个户/公户）
    #    挤掉的购车性质。首购/增购/换购是销售判断逼单策略的第一顺位信息。
    "purchase_nature",
)
"""有内部取证价值、但新契约无对应位的字段。

顺序即渲染顺序（L1 的《目标字段清单》按此列出），故保持稳定。
"""

INTERNAL_IDENTITY_KEYS: frozenset[str] = frozenset(
    {
        "company",
        "company_type",
        "old_car_model",
        "owned_brand_model",
        "owned_car_age",
    }
)
"""编外字段里**仍属身份语义**的那些。

它们原在契约的「基础身份」分类下，享有 L2 的两道额外保护（归属过滤 / 值形态门）。
离开契约后必须由本集合把保护补回来，否则是**静默的行为退化**：
不报错，只是某些本该被拦下的断言开始写进 facts（实测 ``company`` 已经这样）。
"""

MIGRATED_KEYS: tuple[str, ...] = (
    "emotion",
    "communication_rhythm",
    "personality_and_preferences",
    "resistances",
)
"""换位置而非下线的字段：Notes 里没有了，内容改走画像卡。

- ``emotion`` / ``communication_rhythm`` / ``personality_and_preferences`` →
  ``decision_psychology`` 的情绪状态 / 沟通偏好 / 决策风格；
- ``resistances`` → ``deal_battle.loss_conditions``。
"""

DROPPED_KEYS: tuple[str, ...] = ("douyin_id", "xiaohongshu_id")
"""明确舍弃的字段：账号标识类，分析价值低，平台偏好已由 ``frequent_social_media`` 覆盖。"""

INTERNAL_ENUM_SPECS: dict[str, dict[str, Any]] = {
    # 取值逐字取自 v2 之前的契约快照。这些字段虽已离开契约，取值口径不变——
    # 它们要喂给 decision_psychology 与画像卡，仍需归一与合法性校验。
    "emotion": {
        "items": {
            "guard_against": "防备强",
            "need_guidance": "需要引导",
            "straightforward": "喜欢直接",
            "prone_to_anxiety": "容易焦虑",
            "hate_being_rushed": "讨厌被催",
            "requires_approval": "需要认可",
        },
        "multiple_value": True,
    },
    "communication_rhythm": {
        "items": {
            "prudent": "谨慎",
            "hesitate": "纠结",
            "procrastination": "拖延",
            "prompt_decisive": "爽快",
        },
        "multiple_value": False,
    },
    "personality_and_preferences": {
        "items": {
            "social": "社交型",
            "rational": "理性型",
            "practical": "实用型",
            "sensibility": "感性型",
        },
        "multiple_value": False,
    },
    # 旧契约里是结构化枚举（要随带填「乘坐人数」）。
    "seating_requirement": {
        "items": {
            "large_space": {"input_value": "乘坐人数", "display_name": "需大空间"},
            "normal_space": "常规空间",
        },
        "multiple_value": False,
    },
    # 旧契约里是多选枚举。漏登记它，中文展示名（「平顺」）就归不到键，归一静默失效。
    "driving_preference": {
        "items": {
            "smooth": "平顺",
            "strong": "强劲",
            "comfortable": "舒适",
            "easy_to_drive": "好开",
        },
        "multiple_value": True,
    },
    # 本轮**新增**的编外枚举：承接被新 purchase_type 枚举（只剩个户/公户）
    # 挤掉的购车性质。首购/增购/换购是销售判断逼单策略的第一顺位信息。
    "purchase_nature": {
        "items": {
            "first": "首购",
            "additional": "增购",
            "replacement": "换购",
        },
        "multiple_value": False,
    },
    # 画像卡字段：``ai_insight.purchase_intent_level``。取值按业务专家的
    # 《客户商机等级定级规范》：H/A/B/C/待识别。``D`` 仅留给历史批次兼容。
    # 不给枚举定义，L1 会输出「高意向」「B级客户」之类的自由文本，归一失效后整项消失。
    "deal_level": {
        "items": {
            "H": "H",
            "A": "A",
            "B": "B",
            "C": "C",
            "D": "D",
            "待识别": "待识别",
        },
        "multiple_value": False,
    },
}
"""编外字段里**仍是枚举**的那些。

不给它们留这份定义，L1 若照旧输出英文键或中文展示名，投影层会当作自由文本原样落库——
归一失效，且下游（``decision_psychology``、画像卡）拿到的是一个未归一的串。
"""

INTERNAL_FIELD_NAMES: dict[str, str] = {
    "old_car_model": "旧车车型",
    "owned_brand_model": "拥车车型（本品）",
    "owned_car_age": "拥车车龄",
    "company": "企业",
    "company_type": "企业属性",
    "seating_requirement": "乘坐需求",
    "primary_driver": "用车人",
    "intended_brand": "意向品牌",
    "usage_frequency": "用车频次",
    "driving_preference": "驾驶感受",
    "special_requirements": "特殊要求",
    "purchase_nature": "购买性质",
    "emotion": "沟通风格",
    "communication_rhythm": "沟通节奏",
    "personality_and_preferences": "性格偏好",
    "deal_level": "商机等级",
}
"""编外字段的中文名（L1《目标字段清单》渲染用；契约里已无 ``attribute_name`` 可读）。"""

INTERNAL_FIELD_DESCRIPTIONS: dict[str, str] = {
    # 口径**逐字取自 v2 之前的契约快照**——模型看到的口径因此与它被下线前完全一致，
    # 不会因为「字段搬进了内部登记表」而悄悄换一套说法。
    "old_car_model": "识别目前正在驾驶或准备卖掉的车辆品牌及车型。",
    "owned_brand_model": "识别是否已拥有本品牌车辆具体车型。",
    "owned_car_age": "识别目前正在驾驶或准备卖掉的车辆车龄。",
    "company": "提取客户具体供职的公司名称。",
    "company_type": (
        "判定客户供职公司所属组织性质。选项：外企、国企/央企、民企、个体、政府机关、自由职业等"
    ),
    "seating_requirement": (
        "提取客户用车时常乘人数及空间要求。选项包含：___人 需大空间□ 常规空间□；"
    ),
    "primary_driver": "明确给谁买的车，谁开这辆车的时间最长。选项包含：（家人/自己）",
    "intended_brand": "提取客户购车正在考虑的品牌。如“xxxx”等。为什么找销售顾问？",
    "usage_frequency": (
        "提取每周/每天驾驶频率。“天天开，一天80公里”“周末才动动车”“一个月开不了几次”。"
        "输出为：高频/中频/低频，或具体表述。"
    ),
    "driving_preference": "提取车辆动力相关的驾驶风格偏好；选项包含：平顺□ 强劲□ 好开□ 舒适□",
    "special_requirements": (
        "提取非常规个性化需求。例如：“有老人，地台不能太高”“喜欢露营，后排放倒得是平的”"
        "“要能拖挂房车”“对宠物友好”。简要概括。"
    ),
    "purchase_nature": (
        "识别购车性质：首购 / 增购 / 换购。首购＝家里第一辆车；增购＝已有车再添一辆；"
        "换购＝旧车置换或卖掉现有车。"
    ),
    "emotion": (
        "判断客户沟通风格：包括：喜欢直接 □ 讨厌被催 □ 需要引导 □ 容易焦虑 □ 防备强 □ 需要认可，"
        "用于指导销售顾问判断逼单方式。"
    ),
    "communication_rhythm": (
        "判断客户购车节奏：包括：爽快 □ 谨慎 □ 纠结 □ 拖延，用于指导销售顾问判断跟进节奏。"
    ),
    "personality_and_preferences": (
        "判断用户购车性格：理性型关注参数、逻辑、数据；感性型关注颜色、氛围、直观感受；"
        "社交型关注排场、圈层；实用型关注性价比。"
    ),
    "deal_level": (
        "判定客户的商机等级（H / A / B / C / 待识别），衡量最终下单成交的概率等级，"
        "供销售按等级排序跟进。**以系统定级环节的结果为准**：本字段在 L1 只作兜底，"
        "无法判定时不输出。"
    ),
}

INTERNAL_FIELD_NOTE = "内部字段：不写入 Notes，供画像卡与销售策略使用。"
"""渲染在编外字段后面的标注。

**必须有**：L1 是照《目标字段清单》取证的，清单里每个字段它都会去原文里找。若不标出
「这个字段不回传」，日后排查「为什么 Notes 里看不到 old_car_model」会绕远路。
"""

EXTRACTION_ONLY_KEYS: tuple[str, ...] = INTERNAL_ONLY_KEYS + (
    "emotion",
    "communication_rhythm",
    "personality_and_preferences",
)
"""**契约里已经没有、但仍须照常取证**的字段。

``resistances`` **不在内**：它的内容来自画像卡的表达层，不由 L1 从原文提取，
因此不该出现在取证目标清单里（列进去只会诱导模型去原文里找一段本就没有的话）。
"""

CARD_ONLY_KEYS: tuple[str, ...] = ("deal_level",)
"""**画像卡字段**：不在 Notes 契约里，但**是正式回传字段的取值来源**。

``deal_level`` 是购车意向等级：契约把它放在 ``ai_insight.purchase_intent_level``
（后台再派生同步到 ``customer_profile_main.deal_level``），`Notes 的 45 字段里没有它。
它既不是「无回传位的编外字段」（``INTERNAL_ONLY_KEYS``），也不能从 Notes 取——它是
回传体另一块的**输入**。v3 从生产图迁到业务测试臂时，旧模板里的「客户质量等级 A/B/C」
没有跟着进入《目标字段清单》，导致 facts 恒无此键、``ai_insight`` 只剩
``customer_segment``，前端「购车意向等级」长期空白。

与 ``CALLBACK_KEYS`` 的分工同编外字段：**取证有它，Notes 白名单仍不含它**。
"""

EXTRACTION_KEYS: tuple[str, ...] = nc.ALL_KEYS + EXTRACTION_ONLY_KEYS + CARD_ONLY_KEYS
"""L1 的**取证目标**全集：契约字段 + 编外字段 + 画像卡字段。

与 ``CALLBACK_KEYS``（即 :data:`notes_contract.ALL_KEYS`）的分工是本模块存在的全部理由：
**取证照旧全量，回传只走契约**。L1 若只按契约取证，编外字段会立刻掉空，
``purchase_decisions`` / ``deal_battle`` / ``decision_psychology`` 也就失去料；
画像卡字段掉空则 ``ai_insight.purchase_intent_level`` 整个消失。
"""

CALLBACK_KEYS: tuple[str, ...] = nc.ALL_KEYS
"""回传白名单。回传体只由它派生（见 ``projection._to_notes_updates``）。"""


# ====================================================================
# 枚举访问层：与 :mod:`notes_contract` 同形，**先契约、后编外**
# ====================================================================


def enum_options(attribute_key: str) -> dict[str, Any] | None:
    """枚举定义：先问契约，问不到再问编外表。

    投影层与提示词渲染都通过这里读枚举，因此「编外字段仍是枚举」这件事只需在此声明一次。
    """
    contract = nc.enum_options(attribute_key)
    if contract is not None:
        return contract
    return INTERNAL_ENUM_SPECS.get(attribute_key)


def is_enum(attribute_key: str) -> bool:
    return enum_options(attribute_key) is not None


def legal_keys(attribute_key: str) -> tuple[str, ...]:
    """该字段的合法枚举键。"""
    options = enum_options(attribute_key) or {}
    return tuple((options.get("items") or {}).keys())


def is_multi_value(attribute_key: str) -> bool:
    options = enum_options(attribute_key) or {}
    return bool(options.get("multiple_value"))


def is_single_value(attribute_key: str) -> bool:
    options = enum_options(attribute_key)
    return bool(options) and not options.get("multiple_value")


def display_map(attribute_key: str) -> dict[str, str | None]:
    """``enum key -> 展示名``。结构化项取其 ``display_name``。"""
    out: dict[str, str | None] = {}
    for key, value in ((enum_options(attribute_key) or {}).get("items") or {}).items():
        if isinstance(value, str):
            out[key] = value
        elif isinstance(value, dict):
            out[key] = value.get("display_name")
    return out


def display_to_key(attribute_key: str, display: str) -> str | None:
    """展示名反向查键（把「讨厌被催」这类中文值归一到 ``hate_being_rushed``）。"""
    target = (display or "").strip()
    if not target:
        return None
    for key, name in display_map(attribute_key).items():
        if name == target:
            return key
    return None


def structured_spec(attribute_key: str, enum_key: str) -> str | None:
    """结构化枚举项要求随带填写的 ``input_value`` 契约字段名；非结构化返回 ``None``。

    编外字段目前**没有**结构化枚举（``seating_requirement`` 已下线），因此这里只透传契约，
    但保留同形签名，免得调用方为「契约还是编外」写两套分支。
    """
    contract = nc.structured_spec(attribute_key, enum_key)
    if contract is not None:
        return contract
    item = ((INTERNAL_ENUM_SPECS.get(attribute_key) or {}).get("items") or {}).get(enum_key)
    if isinstance(item, dict):
        return item.get("input_value")
    return None


def is_structured(attribute_key: str, enum_key: str) -> bool:
    return structured_spec(attribute_key, enum_key) is not None


def name_of(attribute_key: str) -> str:
    """字段中文名：先读契约的 ``attribute_name``，再读编外登记表，都没有则回落到键名。"""
    entry = nc.by_key(attribute_key)
    if entry is not None:
        return str(entry.get("attribute_name") or attribute_key)
    return INTERNAL_FIELD_NAMES.get(attribute_key, attribute_key)


def description_of(attribute_key: str) -> str:
    """字段口径原文：先读契约的 ``description``，再读编外登记表，都没有则返回空串。

    L1《目标字段清单》靠它把口径渲染进提示词，因此**编外字段也必须给**——
    只列键名不给口径，模型不知道这个字段要什么。
    """
    entry = nc.by_key(attribute_key)
    if entry is not None:
        return str(entry.get("description") or "").strip()
    return INTERNAL_FIELD_DESCRIPTIONS.get(attribute_key, "").strip()


def field_type_of(attribute_key: str) -> str:
    """字段类型：契约字段读 ``field_type``，编外字段按有无枚举定义推断。"""
    entry = nc.by_key(attribute_key)
    if entry is not None:
        return str(entry.get("field_type") or "text")
    return "enum" if is_enum(attribute_key) else "text"


def is_internal_only(attribute_key: str) -> bool:
    """是否为本轮回传**不再包含**的编外字段（渲染时据此加标注）。"""
    return attribute_key in EXTRACTION_ONLY_KEYS


def is_card_only(attribute_key: str) -> bool:
    """是否为画像卡字段：不回传进 Notes，但产出回传体的 ``ai_insight``。"""
    return attribute_key in CARD_ONLY_KEYS
