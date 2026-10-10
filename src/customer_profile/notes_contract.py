"""Notes 字段契约快照（``GET /api/v1/remote/config/note-attributes``）。

**本文件由 :mod:`scripts.snapshot_note_contract` 自动生成，请勿手改。**
重新生成：``python scripts/snapshot_note_contract.py``。

契约实测跨 **39/39 案完全恒定**（payload 签名 ``0479e326bc88``），共 **42** 个字段、
**5** 个分类（基础身份 16 / 购车需求 10 / 预算与金融 4 / 决策与节奏 4 / 个人偏好与备注 8）。因此固化为离线快照，运行期不再取接口。

两条使用口径：

- **枚举归一的唯一依据**。字段的合法键、是否多选、结构化枚举要填的 ``input_value``
  契约字段名，全部从这里读——不散落在规则表里手抄，避免与接口漂移。
- **单选/多选的分野**由 ``multiple_value`` 决定（实测 16 单选 / 5 多选），
  L2 的「单/多值约束」直接据此强制。
"""

from __future__ import annotations

from typing import Any

SOURCE = "GET /api/v1/remote/config/note-attributes"
SIGNATURE = "0479e326bc88"
CAPTURED_FROM = "baseline-39-dsv41-noThink"

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
    {'attribute_key': 'customer_name',
 'attribute_name': '客户姓名',
 'category': '基础身份',
 'field_type': 'text',
 'description': '识别留资及对话中提及的客户真实姓名（如果没有完整姓名，则可先提取姓或昵称），区分“购车人”与“陪同人”。',
 'enum_options': None},
    {'attribute_key': 'title',
 'attribute_name': '称呼',
 'category': '基础身份',
 'field_type': 'enum',
 'description': '判定性别及社交礼貌称呼，选项包含：先生/女士',
 'enum_options': {'items': {'Mr': '先生', 'Ms': '女士'}, 'multiple_value': False}},
    {'attribute_key': 'phone_number',
 'attribute_name': '联系电话',
 'category': '基础身份',
 'field_type': 'text',
 'description': '客户留资手机号',
 'enum_options': None},
    {'attribute_key': 'douyin_id',
 'attribute_name': '抖音号',
 'category': '基础身份',
 'field_type': 'text',
 'description': '提取客户抖音号',
 'enum_options': None},
    {'attribute_key': 'xiaohongshu_id',
 'attribute_name': '小红书号',
 'category': '基础身份',
 'field_type': 'text',
 'description': '提取客户小红书ID',
 'enum_options': None},
    {'attribute_key': 'wechat_id',
 'attribute_name': '微信号',
 'category': '基础身份',
 'field_type': 'text',
 'description': '提取客户微信ID，如基于微信头像截图提取。（识别是否有“微信号同手机号”的明确表述。）',
 'enum_options': None},
    {'attribute_key': 'age',
 'attribute_name': '年龄',
 'category': '基础身份',
 'field_type': 'text',
 'description': '提取客户（留资购车人）具体年龄或根据工作年限、号段特征、生活方式推断阶段。优先精确值，否则区间推理。参考内容：___岁 ',
 'enum_options': None},
    {'attribute_key': 'occupation',
 'attribute_name': '职业',
 'category': '基础身份',
 'field_type': 'text',
 'description': '提取客户行业+职位信息。关注自述/聊天提及的“公司、加班、开会、出差”等语境。',
 'enum_options': None},
    {'attribute_key': 'company',
 'attribute_name': '企业',
 'category': '基础身份',
 'field_type': 'text',
 'description': '提取客户具体供职的公司名称。',
 'enum_options': None},
    {'attribute_key': 'company_type',
 'attribute_name': '企业属性',
 'category': '基础身份',
 'field_type': 'text',
 'description': '判定客户供职公司所属组织性质。选项：外企、国企/央企、民企、个体、政府机关、自由职业等',
 'enum_options': None},
    {'attribute_key': 'annual_income',
 'attribute_name': '年收入',
 'category': '基础身份',
 'field_type': 'text',
 'description': '根据职业、现有车型、生活消费描述综合推断客户个人或家庭年收入，输出人民币单位“万元”',
 'enum_options': None},
    {'attribute_key': 'family_structure',
 'attribute_name': '家庭结构',
 'category': '基础身份',
 'field_type': 'enum',
 'description': '提取客户婚姻及子女状况，如果识别有子女，则提取孩子个数。选项包含：已婚□ 未婚□ 有孩子□(__人)',
 'enum_options': {'items': {'married': '已婚',
                            'unmarried': '未婚',
                            'has_children': {'input_value': '孩子人数', 'display_name': '有孩子'}},
                  'multiple_value': False}},
    {'attribute_key': 'driver_license_status',
 'attribute_name': '驾照情况',
 'category': '基础身份',
 'field_type': 'enum',
 'description': '识别驾龄及驾驶能力描述。选项包含：有□(驾龄___年) 无□ 新手□',
 'enum_options': {'items': {'no': '无',
                            'yes': {'input_value': '驾龄年限', 'display_name': '有'},
                            'novice': '新手'},
                  'multiple_value': False}},
    {'attribute_key': 'owned_brand_model',
 'attribute_name': '拥车车型（本品）',
 'category': '基础身份',
 'field_type': 'text',
 'description': '识别是否已拥有本品牌车辆具体车型。',
 'enum_options': None},
    {'attribute_key': 'owned_car_age',
 'attribute_name': '拥车车龄',
 'category': '基础身份',
 'field_type': 'text',
 'description': '识别目前正在驾驶或准备卖掉的车辆车龄。',
 'enum_options': None},
    {'attribute_key': 'old_car_model',
 'attribute_name': '旧车车型',
 'category': '基础身份',
 'field_type': 'text',
 'description': '识别目前正在驾驶或准备卖掉的车辆品牌及车型',
 'enum_options': None},
    {'attribute_key': 'purchase_purpose',
 'attribute_name': '购车用途',
 'category': '购车需求',
 'field_type': 'enum',
 'description': '识别车辆的核心使用场景，支持多选。选项包含：家用□ 通勤□ 商务□ 越野□ 代步□',
 'enum_options': {'items': {'family': '家用',
                            'commute': '通勤',
                            'offroad': '越野',
                            'business': '商务',
                            'transport': '代步'},
                  'multiple_value': True}},
    {'attribute_key': 'purchase_type',
 'attribute_name': '购买类型',
 'category': '购车需求',
 'field_type': 'enum',
 'description': '识别购车类型。选项包含：首购/增购/换购/公户购车。“家里第一辆车”（首购），“想再添一辆”（增购），“旧车置换”（换购）“给公司买，抵税”（公户购车）。提取其中一个。',
 'enum_options': {'items': {'first': '首购',
                            'company': '公户购车',
                            'unknown': '未知',
                            'additional': '增购',
                            'replacement': '换购'},
                  'multiple_value': False}},
    {'attribute_key': 'seating_requirement',
 'attribute_name': '乘坐需求',
 'category': '购车需求',
 'field_type': 'enum',
 'description': '提取客户用车时常乘人数及空间要求。选项包含：___人 需大空间□ 常规空间□；',
 'enum_options': {'items': {'large_space': {'input_value': '乘坐人数', 'display_name': '需大空间'},
                            'normal_space': '常规空间'},
                  'multiple_value': False}},
    {'attribute_key': 'core_concerns',
 'attribute_name': '核心关注点',
 'category': '购车需求',
 'field_type': 'enum',
 'description': '提取客户最在意的TOP车辆特性，可多选，选项包含：动力□ 颜值□ 空间□ 油耗□ 安全□ 智能□',
 'enum_options': {'items': {'power': '动力',
                            'smart': '智能',
                            'space': '空间',
                            'safety': '安全',
                            'appearance': '颜值',
                            'fuel_efficiency': '油耗'},
                  'multiple_value': True}},
    {'attribute_key': 'primary_driver',
 'attribute_name': '用车人',
 'category': '购车需求',
 'field_type': 'enum',
 'description': '明确给谁买的车，谁开这辆车的时间最长。选项包含：（家人/自己）',
 'enum_options': {'items': {'self': '自己', 'family': '家人'}, 'multiple_value': False}},
    {'attribute_key': 'usage_frequency',
 'attribute_name': '用车频次',
 'category': '购车需求',
 'field_type': 'text',
 'description': '提取每周/每天驾驶频率。“天天开，一天80公里”“周末才动动车”“一个月开不了几次”。输出为：高频/中频/低频，或具体表述。',
 'enum_options': None},
    {'attribute_key': 'driving_preference',
 'attribute_name': '驾驶感受',
 'category': '购车需求',
 'field_type': 'enum',
 'description': '提取车辆动力相关的驾驶风格偏好；选项包含：平顺□ 强劲□ 好开□ 舒适□',
 'enum_options': {'items': {'smooth': '平顺',
                            'strong': '强劲',
                            'comfortable': '舒适',
                            'easy_to_drive': '好开'},
                  'multiple_value': True}},
    {'attribute_key': 'special_requirements',
 'attribute_name': '特殊要求',
 'category': '购车需求',
 'field_type': 'text',
 'description': '提取非常规个性化需求。例如：“有老人，地台不能太高”“喜欢露营，后排放倒得是平的”“要能拖挂房车”“对宠物友好”。简要概括。',
 'enum_options': None},
    {'attribute_key': 'intended_brand',
 'attribute_name': '意向品牌',
 'category': '购车需求',
 'field_type': 'text',
 'description': '提取客户购车正在考虑的品牌。如“xxxx”等。为什么找销售顾问？',
 'enum_options': None},
    {'attribute_key': 'intended_model',
 'attribute_name': '意向车型',
 'category': '购车需求',
 'field_type': 'text',
 'description': '提取客户购车正在考虑的车型。如“xx"，“xx"等。找销售顾问买什么车？',
 'enum_options': None},
    {'attribute_key': 'budget_range',
 'attribute_name': '预算区间',
 'category': '预算与金融',
 'field_type': 'text',
 'description': '提取客户购车预算上下限。单位：万（人民币）',
 'enum_options': None},
    {'attribute_key': 'payment_method',
 'attribute_name': '付款方式',
 'category': '预算与金融',
 'field_type': 'enum',
 'description': '提取客户购车支付方式与金融期望。选项包含：全款□ 贷款□ 首付___%',
 'enum_options': {'items': {'loan': '贷款', 'full_payment': '全款'}, 'multiple_value': False}},
    {'attribute_key': 'trade_in_requirement',
 'attribute_name': '置换需求',
 'category': '预算与金融',
 'field_type': 'enum',
 'description': '识别客户是否有旧车可用于置换，并提取旧车品牌。用于指导销售顾问压价或加价。',
 'enum_options': {'items': {'no': '无', 'yes': {'input_value': '旧车品牌', 'display_name': '有'}},
                  'multiple_value': False}},
    {'attribute_key': 'license_plate_status',
 'attribute_name': '牌照指标',
 'category': '预算与金融',
 'field_type': 'enum',
 'description': '提取客户汽车牌照/指标持有情况。选项包含：已有牌照□ 需办理□ 限牌城市□。用于判断客户购车紧迫性。',
 'enum_options': {'items': {'has_license': '已有牌照',
                            'needs_license': '需办理',
                            'restricted_city': '限牌城市'},
                  'multiple_value': False}},
    {'attribute_key': 'planned_purchase_time',
 'attribute_name': '计划时间',
 'category': '决策与节奏',
 'field_type': 'enum',
 'description': '提取购车明确时间窗。选项包含：本周□ 本月□ 3 个月内□ 6 个月内□。指导销售顾问决定什么时候逼单。',
 'enum_options': {'items': {'this_week': '本周',
                            'this_month': '本月',
                            'within_3_months': '3个月内',
                            'within_6_months': '6个月内'},
                  'multiple_value': False}},
    {'attribute_key': 'viewing_progress',
 'attribute_name': '看车进度',
 'category': '决策与节奏',
 'field_type': 'enum',
 'description': '判断客户购车目前所处的决策阶段。选项包含：首次看车□ 对比中□ 已看竞品□深度考虑□已经下定□',
 'enum_options': {'items': {'comparing': '对比中',
                            'deep_think': '深度考虑',
                            'first_time': '首次看车',
                            'already_decided': '已经下定',
                            'seen_competitors': '已看竞品'},
                  'multiple_value': False}},
    {'attribute_key': 'decision_maker',
 'attribute_name': '决策人',
 'category': '决策与节奏',
 'field_type': 'enum',
 'description': '明确谁最终决定购买。选项包含：本人□ 需家人同意□(关系)',
 'enum_options': {'items': {'self': '本人', 'needs_family_approval': '需家人同意'},
                  'multiple_value': False}},
    {'attribute_key': 'follow_up_frequency',
 'attribute_name': '跟进频率',
 'category': '决策与节奏',
 'field_type': 'text',
 'description': '提取客户偏好的联系节奏与时段。参考内容：每___天联系一次。',
 'enum_options': None},
    {'attribute_key': 'personality_and_preferences',
 'attribute_name': '性格偏好',
 'category': '个人偏好与备注',
 'field_type': 'enum',
 'description': '判断用户购车性格：可参考：理性型：关注参数、逻辑、数据；感性型：关注颜色、氛围、直观感受；社交型：关注排场、圈层；实用型：关注性价比。',
 'enum_options': {'items': {'social': '社交型',
                            'rational': '理性型',
                            'practical': '实用型',
                            'sensibility': '感性型'},
                  'multiple_value': False}},
    {'attribute_key': 'communication_rhythm',
 'attribute_name': '沟通节奏',
 'category': '个人偏好与备注',
 'field_type': 'enum',
 'description': '判断客户购车节奏：包括：爽快□ 谨慎□ 纠结□ 拖延□ ，用于指导销售顾问判断跟进节奏。',
 'enum_options': {'items': {'prudent': '谨慎',
                            'hesitate': '纠结',
                            'procrastination': '拖延',
                            'prompt_decisive': '爽快'},
                  'multiple_value': False}},
    {'attribute_key': 'emotion',
 'attribute_name': '沟通风格',
 'category': '个人偏好与备注',
 'field_type': 'enum',
 'description': '判断客户沟通风格：包括：□ 喜欢直接 □ 讨厌被催 □ 需要引导 □ 容易焦虑 □ 防备强 □ 需要认可，用于指导销售顾问判断逼单方式。',
 'enum_options': {'items': {'guard_against': '防备强',
                            'need_guidance': '需要引导',
                            'straightforward': '喜欢直接',
                            'prone_to_anxiety': '容易焦虑',
                            'hate_being_rushed': '讨厌被催',
                            'requires_approval': '需要认可'},
                  'multiple_value': True}},
    {'attribute_key': 'knowledge_of_mengshi',
 'attribute_name': '了解本品牌',
 'category': '个人偏好与备注',
 'field_type': 'enum',
 'description': '判断客户对本品牌(试驾/顾问推销的品牌)的认知深度。选项包含：不了解/初步了解/深度了解',
 'enum_options': {'items': {'deep': '深度了解', 'none': '不了解', 'basic': '初步了解', 'unknown': '未知'},
                  'multiple_value': False}},
    {'attribute_key': 'pain_points',
 'attribute_name': '用车痛点',
 'category': '个人偏好与备注',
 'field_type': 'text',
 'description': '客户现在或以往用车最不满意的地方。“现在车太小，孩子东西放不下”“油价太高，开不起”“旧车总坏”。用简短语句描述痛点。',
 'enum_options': None},
    {'attribute_key': 'frequent_social_media',
 'attribute_name': '常用社媒',
 'category': '个人偏好与备注',
 'field_type': 'enum',
 'description': '提取客户提及的社交媒体平台。“我刷抖音看见的”“小红书上有人发提车”。选项包含：（抖音/小红书/微博/头条），可多选。',
 'enum_options': {'items': {'weibo': '微博',
                            'douyin': '抖音',
                            'toutiao': '头条',
                            'xiaohongshu': '小红书'},
                  'multiple_value': True}},
    {'attribute_key': 'electronic_ecosystem',
 'attribute_name': '常用电子设备的生态',
 'category': '个人偏好与备注',
 'field_type': 'enum',
 'description': '客户使用的手机品牌。',
 'enum_options': {'items': {'oppo': 'oppo',
                            'vivo': 'vivo',
                            'apple': '苹果',
                            'honor': '荣耀',
                            'huawei': '华为',
                            'xiaomi': '小米'},
                  'multiple_value': False}},
    {'attribute_key': 'hobbies',
 'attribute_name': '兴趣备注',
 'category': '个人偏好与备注',
 'field_type': 'text',
 'description': '提取客户兴趣爱好或生活话题。“周末爱钓鱼”“喜欢自驾西藏”“搞收藏的”。找出可支撑销售顾问拉近关系的兴趣标签。',
 'enum_options': None}
)

_INDEX: dict[str, dict[str, Any]] = {c["attribute_key"]: c for c in RAW_CONFIGS}

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
    """取枚举定义（``{"items": {...}, "multiple_value": bool}``）；非枚举返回 ``None``。"""
    entry = _INDEX.get(attribute_key)
    return ((entry or {}).get("enum_options")) or None


def is_enum(attribute_key: str) -> bool:
    return enum_options(attribute_key) is not None


def legal_keys(attribute_key: str) -> tuple[str, ...]:
    """该字段的合法枚举键。"""
    options = enum_options(attribute_key) or {}
    return tuple((options.get("items") or {}).keys())


def is_multi_value(attribute_key: str) -> bool:
    """是否允许多值（``multiple_value=true``）。"""
    options = enum_options(attribute_key) or {}
    return bool(options.get("multiple_value"))


def is_single_value(attribute_key: str) -> bool:
    """是否单选（枚举字段且 ``multiple_value`` 非真）。"""
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
    item = ((enum_options(attribute_key) or {}).get("items") or {}).get(enum_key)
    if isinstance(item, dict):
        return item.get("input_value")
    return None


def is_structured(attribute_key: str, enum_key: str) -> bool:
    return structured_spec(attribute_key, enum_key) is not None
