"""Notes 字段契约快照（``GET /api/v1/remote/config/note-attributes``）。

**本文件由 :mod:`scripts.snapshot_note_contract` 自动生成，请勿手改。**
重新生成：``python scripts/snapshot_note_contract.py``。

**快照取自线上响应的手工取回件** ``Notes_new.txt``（payload 签名 ``b36e78edf9a2``，按 ``configs`` 的规范化 JSON 计算）。

共 **45** 个字段、**7** 个分类（基础身份 15 / 购车需求 6 / 预算与金融 4 / 决策与节奏 5 / 竞品对比 4 / 个人偏好与备注 9 / 地域与生活圈 2）。因此固化为
离线快照，运行期不再取接口。

两条使用口径：

- **枚举归一的唯一依据**。字段的合法键、是否多选、结构化枚举要填的 ``input_value``
  契约字段名，全部从这里读——不散落在规则表里手抄，避免与接口漂移。
- **单选/多选的分野**由 ``multiple_value`` 决定（实测 17 单选 / 7 多选），
  L2 的「单/多值约束」直接据此强制。
"""

from __future__ import annotations

from typing import Any

SOURCE = "GET /api/v1/remote/config/note-attributes"
SIGNATURE = "b36e78edf9a2"
CAPTURED_FROM = "Notes_new.txt"

BASE_IDENTITY = "基础身份"
PURCHASE_NEED = "购车需求"
BUDGET_FINANCE = "预算与金融"
DECISION_RHYTHM = "决策与节奏"
COMPETITOR = "竞品对比"
PREFERENCE_NOTE = "个人偏好与备注"
REGION_LIFE = "地域与生活圈"
CATEGORIES: tuple[str, ...] = (
    BASE_IDENTITY,
    PURCHASE_NEED,
    BUDGET_FINANCE,
    DECISION_RHYTHM,
    COMPETITOR,
    PREFERENCE_NOTE,
    REGION_LIFE,
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
    {'attribute_key': 'education',
 'attribute_name': '学历',
 'category': '基础身份',
 'field_type': 'enum',
 'description': '高中及以下/大专/本科/硕士/博士及以上；',
 'enum_options': {'items': {'phd': '博士及以上',
                            'master': '硕士',
                            'college': '大专',
                            'bachelor': '本科',
                            'high_school_or_below': '高中及以下'},
                  'multiple_value': False}},
    {'attribute_key': 'industry',
 'attribute_name': '行业',
 'category': '基础身份',
 'field_type': 'text',
 'description': '提取客户的行业',
 'enum_options': None},
    {'attribute_key': 'occupation',
 'attribute_name': '职业',
 'category': '基础身份',
 'field_type': 'text',
 'description': '提取客户行业+职位信息。关注自述/聊天提及的“公司、加班、开会、出差”等语境。',
 'enum_options': None},
    {'attribute_key': 'annual_income',
 'attribute_name': '年收入',
 'category': '基础身份',
 'field_type': 'text',
 'description': '根据职业、现有车型、生活消费描述综合推断客户个人或家庭年收入，输出人民币单位“万元”',
 'enum_options': None},
    {'attribute_key': 'consumption_level',
 'attribute_name': '消费等级',
 'category': '基础身份',
 'field_type': 'enum',
 'description': '1-6档（阿里标签）',
 'enum_options': {'items': {'level_1': '1档',
                            'level_2': '2档',
                            'level_3': '3档',
                            'level_4': '4档',
                            'level_5': '5档',
                            'level_6': '6档'},
                  'multiple_value': False}},
    {'attribute_key': 'family_structure',
 'attribute_name': '家庭结构',
 'category': '基础身份',
 'field_type': 'enum',
 'description': '提取客户婚姻及子女状况，如果识别有子女，则提取孩子个数。选项包含：已婚□ 未婚□ 有孩子□(__人)',
 'enum_options': {'items': {'has_children': {'input_value': '孩子人数', 'display_name': '有孩子'},
                            'married_no_child': '已婚无孩',
                            'unmarried_no_child': '未婚无孩'},
                  'multiple_value': False}},
    {'attribute_key': 'life_key_nodes',
 'attribute_name': '人生关键节点',
 'category': '基础身份',
 'field_type': 'enum',
 'description': '【多值】买车中、备孕中、备考中、买房中、装修中、找工作中（阿里标签，也可二次整理）',
 'enum_options': {'items': {'buying_car': '买车中',
                            'renovating': '装修中',
                            'job_hunting': '找工作中',
                            'buying_house': '买房中',
                            'preparing_exam': '备考中',
                            'preparing_pregnancy': '备孕中'},
                  'multiple_value': True}},
    {'attribute_key': 'customer_type',
 'attribute_name': '客户类型',
 'category': '基础身份',
 'field_type': 'enum',
 'description': '价格价值型、科技尝鲜型、潮流跟随型、个性体验型。价格价值型：看重「值不值得买」；科技尝鲜型：看重「技术够不够先进」；潮流跟随型：看重「是不是热门、受认可」；个性体验型：看重「适不适合我、喜不喜欢」',
 'enum_options': {'items': {'price_value': '价格价值型',
                            'trend_follower': '潮流跟随型',
                            'tech_early_adopter': '科技尝鲜型',
                            'individual_experience': '个性体验型'},
                  'multiple_value': False}},
    {'attribute_key': 'driver_license_status',
 'attribute_name': '驾照情况',
 'category': '基础身份',
 'field_type': 'enum',
 'description': '识别驾龄及驾驶能力描述。选项包含：有□(驾龄___年) 无□ 新手□',
 'enum_options': {'items': {'no': '无', 'yes': {'input_value': '驾龄年限', 'display_name': '有'}},
                  'multiple_value': False}},
    {'attribute_key': 'current_vehicle_info',
 'attribute_name': '现车信息',
 'category': '基础身份',
 'field_type': 'text',
 'description': '无车，有车（XX车型，车龄；XX车型，车龄）；可能多款车',
 'enum_options': None},
    {'attribute_key': 'trade_in_requirement',
 'attribute_name': '置换需求',
 'category': '购车需求',
 'field_type': 'enum',
 'description': '识别客户是否有旧车可用于置换，并提取旧车品牌。用于指导销售顾问压价或加价。',
 'enum_options': {'items': {'no': '无', 'yes': {'input_value': '旧车品牌', 'display_name': '有'}},
                  'multiple_value': False}},
    {'attribute_key': 'trade_in_model',
 'attribute_name': '置换车型',
 'category': '购车需求',
 'field_type': 'text',
 'description': '客户准备置换掉的旧车车型',
 'enum_options': None},
    {'attribute_key': 'intended_model',
 'attribute_name': '意向车型',
 'category': '购车需求',
 'field_type': 'text',
 'description': '提取客户购车正在考虑的车型。如“xx"，“xx"等。找销售顾问买什么车？',
 'enum_options': None},
    {'attribute_key': 'purchase_type',
 'attribute_name': '购买类型',
 'category': '购车需求',
 'field_type': 'enum',
 'description': '识别购车类型。选项包含：个户/公户购车。“个人名义购买”（个户），“给公司买，抵税”（公户购车）。提取其中一个。',
 'enum_options': {'items': {'company': '公户购车', 'personal': '个户'}, 'multiple_value': False}},
    {'attribute_key': 'purchase_purpose',
 'attribute_name': '用车场景',
 'category': '购车需求',
 'field_type': 'enum',
 'description': '识别车辆的核心使用场景，支持多选。选项包含：家用□ 商务□ 越野□',
 'enum_options': {'items': {'family': '家用', 'offroad': '越野', 'business': '商务'},
                  'multiple_value': True}},
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
    {'attribute_key': 'credit_status',
 'attribute_name': '征信情况',
 'category': '预算与金融',
 'field_type': 'text',
 'description': '有无逾期，流水高低',
 'enum_options': None},
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
 'description': '提取购车明确时间窗。选项包含：本周□ 本月□ 3 个月内□ 3 个月以上□。指导销售顾问决定什么时候逼单。',
 'enum_options': {'items': {'this_week': '本周',
                            'this_month': '本月',
                            'over_3_months': '3个月以上',
                            'within_3_months': '3个月内'},
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
    {'attribute_key': 'internet_preference',
 'attribute_name': '上网偏好',
 'category': '决策与节奏',
 'field_type': 'enum',
 'description': '早高峰、晚高峰、午间、下午、睡前、其他作息（上网24小时分布）',
 'enum_options': {'items': {'noon': '午间',
                            'other': '其他作息',
                            'afternoon': '下午',
                            'before_sleep': '睡前',
                            'evening_peak': '晚高峰',
                            'morning_peak': '早高峰'},
                  'multiple_value': True}},
    {'attribute_key': 'follow_up_frequency',
 'attribute_name': '跟进时段',
 'category': '决策与节奏',
 'field_type': 'enum',
 'description': '提取客户偏好的联系节奏与时段。参考内容：每___天联系一次。',
 'enum_options': {'items': {'evening': '晚上',
                            'morning': '上午',
                            'weekday': '周中',
                            'weekend': '周末',
                            'afternoon': '下午'},
                  'multiple_value': True}},
    {'attribute_key': 'competing_brand',
 'attribute_name': '对比品牌',
 'category': '竞品对比',
 'field_type': 'text',
 'description': '客户购车正在对比的车企品牌。如“方程豹”“坦克”等',
 'enum_options': None},
    {'attribute_key': 'competing_model',
 'attribute_name': '对比车型',
 'category': '竞品对比',
 'field_type': 'text',
 'description': '客户购车正在对比的具体车型。如”豹8“，“坦克700”。',
 'enum_options': None},
    {'attribute_key': 'test_driven_models',
 'attribute_name': '试驾过的车型',
 'category': '竞品对比',
 'field_type': 'text',
 'description': '提取客户已试驾的车型清单。“上周开了xxx车型”“xx,xx都试了”。提取车型名，可多个。',
 'enum_options': None},
    {'attribute_key': 'brand_concerns',
 'attribute_name': '本品牌顾虑',
 'category': '竞品对比',
 'field_type': 'text',
 'description': '客户对购买本品（试驾/顾问推销的品牌）明确表达的担忧或犹豫点。如保值率、质量、售后等',
 'enum_options': None},
    {'attribute_key': 'optional_config',
 'attribute_name': '配置',
 'category': '个人偏好与备注',
 'field_type': 'text',
 'description': '提取客户购车偏好的车辆配置。如20寸轮毂等',
 'enum_options': None},
    {'attribute_key': 'knowledge_of_mengshi',
 'attribute_name': '了解猛士',
 'category': '个人偏好与备注',
 'field_type': 'enum',
 'description': '判断客户对本品牌(试驾/顾问推销的品牌)的认知深度。选项包含：不了解/初步了解/深度了解',
 'enum_options': {'items': {'deep': '深度了解', 'none': '不了解', 'basic': '初步了解', 'unknown': '未知'},
                  'multiple_value': False}},
    {'attribute_key': 'knowledge_of_erev',
 'attribute_name': '了解混动',
 'category': '个人偏好与备注',
 'field_type': 'enum',
 'description': '判断客户对增程技术的认知。选项包含：不了解/初步了解/深度了解',
 'enum_options': {'items': {'deep': '深度了解', 'none': '不了解', 'basic': '初步了解', 'unknown': '未知'},
                  'multiple_value': False}},
    {'attribute_key': 'knowledge_of_adas',
 'attribute_name': '了解智驾',
 'category': '个人偏好与备注',
 'field_type': 'enum',
 'description': '判断客户对智能驾驶技术的认知。选项包含：不了解/初步了解/深度了解',
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
 'description': '客户使用的手机品牌。选项包含：苹果/华为/小米/oppo/vivo/荣耀/其他/未知。',
 'enum_options': {'items': {'oppo': 'oppo',
                            'vivo': 'vivo',
                            'apple': '苹果',
                            'honor': '荣耀',
                            'other': '其他',
                            'huawei': '华为',
                            'xiaomi': '小米',
                            'unknown': '未知'},
                  'multiple_value': False}},
    {'attribute_key': 'hobbies',
 'attribute_name': '兴趣备注',
 'category': '个人偏好与备注',
 'field_type': 'text',
 'description': '提取客户兴趣爱好或生活话题。“周末爱钓鱼”“喜欢自驾西藏”“搞收藏的”。找出可支撑销售顾问拉近关系的兴趣标签。',
 'enum_options': None},
    {'attribute_key': 'contact_preference',
 'attribute_name': '沟通方式',
 'category': '个人偏好与备注',
 'field_type': 'enum',
 'description': '客户偏好沟通方式：电话/微信',
 'enum_options': {'items': {'phone': '电话', 'wechat': '微信'}, 'multiple_value': True}},
    {'attribute_key': 'residence_address',
 'attribute_name': '居住地址',
 'category': '地域与生活圈',
 'field_type': 'text',
 'description': '居住地址（省市）',
 'enum_options': None},
    {'attribute_key': 'license_plate_address',
 'attribute_name': '上牌地址',
 'category': '地域与生活圈',
 'field_type': 'text',
 'description': '上牌地址（省市）',
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
