"""M2M 本体渠道载荷的中文渲染。

2026-10-31 起 ``GET /api/v1/remote/data/history/{phone}`` 的渠道集合换成一套
"本体"命名（``OntologyLeadInfo`` / ``OntologyLeadStage`` / …），原有
``MengShiITSystemData`` 等渠道已由业务方确认移除。本模块只做一件事：把本体
``raw_payload`` 渲染成**中文标签的正文**，供 L0 记录层直接当渠道内容使用。

**为什么不新开一个子流程**：这些载荷的字段已经是扁平中文语义明确的取值，不需要
OCR、转写或 LLM 归纳。开子流程会多一层图结构、多一份模板，收益只是"看起来统一"。
记录层本来就把"清单里有、子流程没有"的渠道按 ``raw_payload`` 兜底，这里顺着同一条
路径做结构化渲染即可，且 ``code`` 通道天然 0 token。

**为什么字段标签要在这里维护而不是让模型猜**：L1 拿到的分节正文会被抄成证据锚点，
若正文里是 ``leads_status: 100414``，锚点里就带着字典码；译成「线索状态：首次跟进」
之后，锚点才是一句人话。字段标签以接口规范 §4 为准，缺标签的键**照原样输出**——
宁可露出一个英文键，也不要静默丢掉一个真实取值。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- 枚举
#
# 一律按 **(渠道, 字段)** 二维查表，不按字段名单独分发。同一个键名在不同渠道
# 含义完全不同：``record_type`` 在试驾排程是「录音方式」、在聊天记录是「来源平台」；
# ``status`` 在线索是「启用状态（0/1/2/-1）」、在试驾排程是「试驾记录状态」；
# ``order_status`` 在试驾排程是「试驾单状态」、在订单记录是「订单状态」。
# 按字段名分发会让这些码互相串味。
#
# 码值来源：``docs/reference/门店本体码值解码说明_20261102.md``（业务方 2026-11-02
# 提供）。该文档未覆盖的码**不在此处猜测**，由 :func:`_enum_label` 返回 ``None``，
# 渲染层原样输出——缺口见 ``docs/reference/门店本体码值缺口清单-20261102.md``。

STAGE_LABELS = {"1": "线索", "2": "商机"}
"""``OntologyLeadInfo.stage`` / ``OntologyLeadStage.stage``。接口规范 §4.2 明文给出。"""

LEADS_STATUS_LABELS = {
    "100401": "待处理",
    "100402": "有效",
    "100403": "无效",
    "100404": "待外呼清洗",
    "100405": "培育中",
    "100406": "待下发",
    "100407": "下发失败",
    "100408": "待分配",
    "100409": "待建档",
    "100410": "跟进中",
    "100411": "已锁单",
    "100412": "已暂败",
    "100413": "关闭",
    "100414": "首次跟进",
    "100415": "已预订",
}
"""``leads_status`` 字典码 → 中文。取值域与迁移前 ``mengshi_it_system_data`` 的同名表一致。"""

LEAD_ENABLED_LABELS = {
    "0": "未启用",
    "1": "启用",
    "2": "禁用",
    "-1": "删除",
}
"""线索启用状态（线索对象的 ``status``）。解码说明 §5.1，来源为平台属性描述。"""

FOLLOW_RESULT_LABELS = {
    "90451": "仍需后续跟进",
    "90452": "跟进目的达成",
    "90453": "暂败",
}
"""跟进结果（``prefix_id = 9045``）。解码说明 §3.1。"""

FOLLOW_PURPOSE_LABELS = {
    "90431": "邀请到店",
    "90432": "邀请试驾",
    "90433": "订车洽谈",
    "90434": "锁单跟进",
}
"""跟进目的（``prefix_id = 9043``）。解码说明 §3.2。"""

INTENDED_LEVEL_LABELS = {
    "904606": "F 级意向（低，待确认）",
    "90461": "H 级意向（最高）",
    "90462": "A 级意向（高）",
    "90463": "B 级意向（较高）",
    "90464": "C 级意向（中）",
    "90465": "O 级意向（较低）",
    "90466": "F 级意向（低）",
    "90467": "L 级意向（最低）",
}
"""意向级别（``prefix_id = 9046``）。解码说明 §3.3。

**方向**：字典 ``sequence`` 越大意向越高，H→A→B→C→O→F→L 是降序；上表按热到冷排列。

``904606`` 是字典未登记码，实测 67 万条（占跟进记录 4.9%），数量与 ``90466`` 同量级，
业务方推测为 F 级的新编码格式。**此处按业务可用性优先暂时转写为 F，属已知的推断而非
确认**；业务方给出定义后应立即替换。其余六位码（``904602``/``904603``/``904604`` 与 ``0``，
合计不足百条）仍原样输出。
"""

COMMUNICATION_METHOD_LABELS = {
    "90441": "电话",
    "90442": "微信",
    "90443": "店内沟通",
    "90444": "上门",
}
"""沟通方式（``prefix_id = 9044``）。解码说明 §3.4。"""

TEST_DRIVE_TYPE_LABELS = {
    "0": "到店试驾",
    "1": "上门试驾",
}
"""试驾方式。解码说明 §4.1（本体内置枚举，与字典 ``prefix_id = 9304`` 一致）。"""

TEST_DRIVE_CLASS_LABELS = {
    "0": "预约试驾",
    "1": "邀约试驾",
}
"""试驾类别。解码说明 §4.2。

``2``（主理人）与 ``3`` 未收录：前者只见于本体描述、字典无此项，后者字典与描述均未
定义。两者按缺口处理，原样输出。
"""

TEST_DRIVE_ORDER_STATUS_LABELS = {
    "0": "待排程",
    "1": "待排程",
    "2": "待排程·已逾期",
    "4": "已试驾·未完成回访",
    "5": "已完成",
    "6": "已取消",
}
"""试驾单状态。解码说明 §4.3。

**口径**：只有 ``5`` 是闭环完成的试驾；``4`` 是试驾已发生但回访未闭环。归因统计若把
``>= 4`` 都算作完成，会把悬空单计入，试驾转化率虚高约 28 个百分点。

``3`` 与 ``1``/``2``/``4`` 的档位含义在解码说明中标注为「基于数据分布反推」，非字典原文。
"""

TEST_DRIVE_RECORD_TYPE_LABELS = {
    "1": "App 录音",
    "2": "工牌录音",
}
"""试驾录音方式。解码说明 §4.4。"""

LEAD_RATING_LABELS = {
    "H": "H 级（最高）",
    "A": "A 级（高）",
    "B": "B 级（较高）",
    "C": "C 级（中）",
    "O": "O 级（较低）",
    "F": "F 级（低）",
    "L": "L 级（最冷）",
    "无": "无（尚未评级）",
}
"""线索评级（``prefix_id = 1701``「线索级别」）。缺口补充说明 §0.1 / §1.2。

与 :data:`INTENDED_LEVEL_LABELS` 是同一套七档字母：``lead_rating`` 是线索主数据的
**当前评级**，``intendedlevel`` 是**单次跟进时的评级快照**，两者会因主数据更新而
历史记录未回溯产生系统性偏离。``O`` 是原生档位（介于 C 与 F 之间），不是「其他」。
"""

DATA_SOURCE_LABELS = {
    "1": "总部下发",
}
"""线索数据来源。缺口补充说明 §1.1。

字典无对应 ``prefix_id``。仅 ``1`` 可用：三门店 29197 条（96%）。``2``/``3``/``4``/``16``
合计 8 条，业务方判定样本不足，不猜测，原样输出。
"""

OPERATION_TYPE_LABELS = {
    "1": "下发",
    "2": "分配",
    "3": "转移",
    "4": "回收",
}
"""线索操作类型（下发与分配**共用一张表**）。缺口补充说明 §1.3 / §1.4。

两张物理表同源（``t_ods_source_data_leads_operation_record``），仅靠 ``type`` 区分下发与
分配；本体拆成两个对象，但码表不分家。
"""

DEFEAT_TYPE_LABELS = {
    "90471": "无法联系",
    "90472": "已购买竞品",
    "90474": "无购车意向",
    "90475": "暂不考虑",
    "90476": "异地线索",
}
"""战败类型（``prefix_id = 9047``）。缺口补充说明 §2.5。

跟进记录的 ``defeattype`` 与 ``OntologyDefeatRecord`` 的 ``defeat_type`` 同字段同字典。
"""

TEST_DRIVE_SOURCE_LABELS = {
    "1": "app",
    "2": "小程序",
    "3": "官网",
    "4": "门店",
    "5": "乾崑 app",
    "6": "总部",
}
"""试驾来源（``prefix_id = 9302``）。缺口补充说明 §2.7。

**以字典为准，不是本体描述**：本体只写了前 4 项，实际字典 6 项，``5``/``6`` 共 100 条
若按本体描述映射会退化成裸码。
"""

ORDER_STATUS_LABELS = {
    "100": "预订待支付",
    "110": "订单支付超时",
    "200": "预订已支付",
    "300": "预订退款中",
    "400": "订单已退订",
    "500": "锁单待支付",
    "600": "锁单成功",
    "610": "配置已确认",
    "630": "订单已排产",
    "640": "订单已配车",
    "650": "车辆涂装",
    "660": "车辆焊装",
    "670": "车辆总装",
    "680": "车辆下线",
    "690": "订单已发运",
    "691": "车辆验收入库",
    "695": "入库 PDI 已完成",
    "697": "预约交付",
    "700": "尾款已支付",
    "710": "订单已开票",
    "730": "车辆销售出库",
    "800": "锁单退款中",
    "810": "订单已取消",
    "900": "订单已交付",
    "910": "已售卖",
}
"""订单状态（``prefix_id = 9318``，三位数）。缺口补充说明 §2.1。

与试驾排程的 ``order_status``（本体内置枚举 0–6）**不是同一字典**，不可共用。
"""

ORDER_TYPE_LABELS = {
    "10": "普通订单",
    "11": "大客户员工订单",
    "12": "企业购车",
    "13": "大客户批采订单",
    "14": "试驾车订单",
    "15": "特殊车订单",
    "16": "批售订单",
    "17": "出口车订单",
    "18": "展车",
    "19": "巡展预订订单",
    "20": "投资人购车订单",
    "21": "代采订单",
    "30": "精品",
    "31": "用电设备",
    "32": "车机",
    "33": "备件",
    "34": "服务",
    "40": "维修保养",
    "41": "备件销售",
    "42": "取送车",
    "43": "上门服务",
    "44": "长途托运",
    "50": "充电",
    "99": "保证金",
}
"""订单类型（``prefix_id = 9223``）。缺口补充说明 §2.2。

C 端画像需先按 ``order_type = 10``（普通订单）过滤：实测企业占比 71% 是被批售、出口车、
试驾车等非零售订单抬上去的。
"""

ORDER_DIRECTION_LABELS = {
    "1": "正向",
    "-1": "负向（销退冲销）",
}
"""订单方向（``OntologyOrder.order_count``）。缺口补充说明 §2.2。

该字段**不是数量**，是方向标记：``1`` 正向参与统计、``-1`` 负向冲销不参与正向统计。
真正的数量统计用记录条数。
"""

PAYMENT_WAY_LABELS = {
    "0": "全款",
    "1": "分期付款",
}
"""付款方式。缺口补充说明 §2.3。``null`` 不等于全款。"""

PURCHASE_TYPE_LABELS = {
    "1": "个人",
    "2": "企业",
}
"""购买人类型。缺口补充说明 §2.3。含批售等非零售订单，勿直接当 C 端客户属性。"""

YES_NO_LABELS = {"1": "是", "0": "否"}

_ENUM_LABELS: dict[str, dict[str, dict[str, str]]] = {
    "OntologyLeadInfo": {
        "stage": STAGE_LABELS,
        "leads_status": LEADS_STATUS_LABELS,
        "status": LEAD_ENABLED_LABELS,
        "lead_rating": LEAD_RATING_LABELS,
        "data_source": DATA_SOURCE_LABELS,
    },
    "OntologyLeadStage": {
        "stage": STAGE_LABELS,
        "leads_status": LEADS_STATUS_LABELS,
        "status": LEAD_ENABLED_LABELS,
        "lead_rating": LEAD_RATING_LABELS,
        "is_delivery": YES_NO_LABELS,
    },
    "OntologyLeadDistribution": {
        "operation_type": OPERATION_TYPE_LABELS,
        "first_flag": YES_NO_LABELS,
    },
    "OntologyLeadAssignment": {
        "operation_type": OPERATION_TYPE_LABELS,
        "first_flag": YES_NO_LABELS,
    },
    "OntologyLeadFollowRecord": {
        "followresult": FOLLOW_RESULT_LABELS,
        "followpurpose": FOLLOW_PURPOSE_LABELS,
        "intendedlevel": INTENDED_LEVEL_LABELS,
        "communicationmethods": COMMUNICATION_METHOD_LABELS,
        "changebusinessopportunity": YES_NO_LABELS,
        "defeattype": DEFEAT_TYPE_LABELS,
    },
    "OntologyDefeatRecord": {
        "defeat_type": DEFEAT_TYPE_LABELS,
    },
    "OntologyTestDriveSchedule": {
        "test_drive_type": TEST_DRIVE_TYPE_LABELS,
        "test_drive_class": TEST_DRIVE_CLASS_LABELS,
        "order_status": TEST_DRIVE_ORDER_STATUS_LABELS,
        "record_type": TEST_DRIVE_RECORD_TYPE_LABELS,
        "test_drive_source": TEST_DRIVE_SOURCE_LABELS,
    },
    "OntologyOrder": {
        "order_status": ORDER_STATUS_LABELS,
        "order_type": ORDER_TYPE_LABELS,
        "payment_way": PAYMENT_WAY_LABELS,
        "purchase_type": PURCHASE_TYPE_LABELS,
        "order_count": ORDER_DIRECTION_LABELS,
    },
    "OntologyOrderFinance": {
        # 实测同一订单在两路渠道里的 ``order_status`` 同值（如 670），复用订单状态字典。
        "order_status": ORDER_STATUS_LABELS,
    },
}
"""``渠道 → 字段 → {码值: 中文}``。未登记的组合返回 ``None``，渲染层原样输出。"""

_CHANNEL_SKIP_KEYS: dict[str, frozenset[str]] = {
    "OntologyManualOutboundCall": frozenset({"call_status"}),
    "OntologyTestDriveSchedule": frozenset({"status"}),
}
"""按渠道丢弃的字段——**有值但无信息量，渲染出来只会污染证据锚点**。

缺口补充说明 §1.8 / §0.2：``call_status`` 实测 130 万条全部为 ``1``（该表只记录成功外呼）；
试驾 ``status`` 是软删标志，实测 5324 条全部为 ``1``（``-1`` 的行根本不会被返回）。
取值恒定意味着模型从「呼叫状态：1」里得不到任何信息，却要多占一行锚点。
"""


# ---------------------------------------------------------------- 字段标签

_COMMON_TAIL = {
    "customer_phone": "客户手机号",
    "customer_name": "客户姓名",
}

FIELD_LABELS: dict[str, dict[str, str]] = {
    "OntologyAppRegistration": {
        "user_id": "用户 ID",
        "user_name": "用户名称",
        "phone_number": "手机号",
        "register_time": "注册时间",
        "phone_brand": "手机品牌",
        "phone_device_model": "手机型号",
    },
    "OntologyLeadInfo": {
        "source_id": "线索 ID",
        "leads_status": "线索状态",
        "stage": "阶段",
        "lead_rating": "线索评级",
        "lead_submit_time": "留资时间",
        "nick": "昵称",
        "remark": "备注",
        "data_source": "数据来源",
        "channel": "来源渠道码",
        "brand": "意向品牌",
        "province": "省",
        "city": "市",
        "address": "地址",
        "wechat_id": "微信 ID",
        "status": "线索启用状态",
        "vehicle_series_name": "意向车系",
        "vehicle_model_name": "意向车型",
        "source_names": "来源名称列表",
        "vehicle_series_names": "意向车系列表",
        "vehicle_model_names": "意向车型列表",
        **_COMMON_TAIL,
    },
    "OntologyLeadDistribution": {
        "source_id": "下发记录 ID",
        "operation_time": "下发操作时间",
        "operation_type": "操作类型",
        "first_flag": "是否首次下发",
        "update_time": "更新时间",
        "store_code": "下发门店编码",
        "store_name": "下发门店名称",
        **_COMMON_TAIL,
    },
    "OntologyLeadAssignment": {
        "source_id": "分配记录 ID",
        "operation_time": "分配操作时间",
        "operation_type": "操作类型",
        "first_flag": "是否首次分配",
        "update_time": "更新时间",
        "sales_consultant_account": "销售顾问账号",
        "sales_consultant_name": "销售顾问姓名",
        "store_code": "所属门店编码",
        **_COMMON_TAIL,
    },
    "OntologyLeadStage": {
        "source_id": "线索 ID",
        "leads_status": "线索状态",
        "stage": "线索阶段",
        "lead_rating": "线索评级",
        "last_follow_time": "最新跟进时间",
        "last_follow_content": "最新跟进内容",
        "next_follow_time": "下次跟进时间",
        "first_defeat_time": "首次战败时间",
        "order_no": "关联订单号",
        "lock_order_time": "锁单时间",
        "is_delivery": "是否交付",
        "delivery_time": "交付时间",
        "status": "线索启用状态",
        **_COMMON_TAIL,
    },
}

FIELD_LABELS["OntologyLeadFollowRecord"] = {
    "source_id": "跟进记录 ID",
    "followpurpose": "跟进目的",
    "communicationmethods": "沟通方式",
    "followresult": "跟进结果",
    "followcontent": "跟进内容",
    "intendedlevel": "意向级别",
    "vehiclemodelcode": "意向车型编码",
    "vehiclemodelname": "意向车型名称",
    "businessno": "业务单号",
    "nextfollowtime": "下次跟进时间",
    "arrivetime": "到店时间",
    "defeattype": "战败类型",
    "changebusinessopportunity": "是否转商机",
    "createtime": "跟进创建时间",
    "sales_consultant_account": "跟进顾问账号",
    "sales_consultant_name": "跟进顾问姓名",
    "store_code": "跟进门店编码",
    **_COMMON_TAIL,
}

FIELD_LABELS["OntologyDefeatRecord"] = {
    "source_id": "战败记录 ID",
    "defeat_time": "战败时间",
    "defeat_type": "战败类型",
    "defeat_reason": "战败原因",
    "defeat_content": "战败说明",
    "createtime": "创建时间",
    "update_time": "更新时间",
    "sales_consultant_account": "销售顾问账号",
    "sales_consultant_name": "销售顾问姓名",
    "store_code": "门店编码",
    **_COMMON_TAIL,
}

FIELD_LABELS["OntologyManualOutboundCall"] = {
    "source_id": "人工外呼 ID",
    "call_time": "呼叫时间",
    "call_status": "呼叫状态",
    "call_duration": "通话时长（秒）",
    **_COMMON_TAIL,
}

FIELD_LABELS["OntologyTestDriveSchedule"] = {
    "source_id": "试驾记录 ID",
    "schedule_time": "排程时间",
    "test_drive_date": "试驾日期",
    "test_drive_time_period_id": "试驾时间段 ID",
    "start_time": "开始时间",
    "end_time": "结束时间",
    "test_drive_type": "试驾方式",
    "door_time": "上门时间",
    "door_address": "上门地址",
    "order_status": "试驾单状态",
    "test_drive_class": "试驾类别",
    "test_drive_source": "试驾来源",
    "intended_car_series": "意向车系",
    "status": "试驾记录状态",
    "update_time": "更新时间",
    "record_type": "录音方式",
    "sales_consultant_account": "试驾顾问账号",
    "sales_consultant_name": "试驾顾问姓名",
    "store_code": "门店编码",
    **_COMMON_TAIL,
}

FIELD_LABELS["OntologyCustomerChatInfo"] = {
    "source_id": "聊天记录 ID",
    "record_type": "来源平台/记录类型",
    "dialoguecontent": "对话内容",
    "createtime": "创建时间",
    "status": "状态",
    "chat_deadline": "最晚一句话时间",
    "log_time": "记录时间",
    **_COMMON_TAIL,
}

FIELD_LABELS["OntologyOrder"] = {
    "source_id": "订单号",
    "lead_id": "关联线索 ID",
    "order_time": "下单时间",
    "deposit_pay_time": "锁单/大定时间",
    "config_lock_time": "锁定配置时间",
    "contract_time": "合同签署时间",
    "delivery_time": "交付时间",
    "cancel_time": "取消时间",
    "sales_return_time": "销退时间",
    "order_status": "订单状态",
    "order_type": "订单类型",
    "sales_order_type_code": "销售订单类型编码",
    "order_count": "订单方向",
    "total_price_fen": "订单总价",
    "actual_price_fen": "订单实际价格",
    "pay_amount_fen": "支付金额",
    "discount_amount_fen": "折扣金额",
    "coupon_amount_fen": "优惠券金额",
    "payment_way": "付款方式",
    "purchase_type": "购买人类型",
    "vin_code": "VIN 码",
    "license_number": "车牌号",
    "major_series_name": "大车系",
    "model_year": "年款",
    "exterior_color_code": "外饰色编码",
    "interior_color_code": "内饰色编码",
    "vehicle_version_code": "车型版本编码",
    "store_code": "销售门店编码",
    "store_name": "销售门店名称",
    "consultant_account": "销售顾问账号",
    "consultant_name": "销售顾问姓名",
    "update_time": "更新时间",
    **_COMMON_TAIL,
}

FIELD_LABELS["OntologyOrderFinance"] = {
    "source_id": "订单号",
    "lead_id": "关联线索 ID",
    "finance_plan_no": "金融方案编号",
    "finance_plan_name": "金融方案名称",
    "finance_plan_type": "金融方案类型",
    "finance_plan_type_code": "金融方案类型编码",
    "finance_plan_status": "金融方案状态",
    "financial_institution_code": "金融机构编码",
    "financial_institution_name": "金融机构名称",
    "loan_amount_fen": "贷款金额",
    "down_payment_amount_fen": "首付金额",
    "estimated_monthly_payment_fen": "预估月供",
    "loan_interest_fen": "贷款利息",
    "loan_disbursement_amount_fen": "放款金额",
    "down_payment_ratio": "首付比例",
    "balance_ratio": "尾款比例",
    "annual_rate": "年化费率",
    "loan_terms": "贷款期数",
    "loan_apply_time": "贷款申请时间",
    "loan_disbursement_time": "放款时间",
    "invoice_time": "开票时间",
    "sap_account_time": "SAP 记账时间",
    "order_status": "订单状态",
    "deposit_pay_time": "锁单时间",
    "store_code": "销售门店编码",
    "consultant_account": "销售顾问账号",
    "consultant_name": "销售顾问姓名",
    "update_time": "更新时间",
    **_COMMON_TAIL,
}

# 这些键是机读字段，渲染时不给客户看（``source_kind`` 等是接口自述，不是事实）。
_SKIP_KEYS = frozenset({"source_kind", "source_version", "fetched_at", "fetch_status", "amount_unit"})
"""不渲染的元字段：它们描述载荷本身，不是客户事实。

``amount_unit`` 被跳过是因为金额字段已在渲染时按分换算，再留一个 ``fen`` 只会
让模型把两套单位混着用。
"""


def _label(channel: str, key: str) -> str:
    """取字段中文标签；没有登记就原样用键名（不静默丢字段）。"""
    return FIELD_LABELS.get(channel, {}).get(key, key)


def _enum_label(channel: str, key: str, value: Any) -> str | None:
    """按「渠道 + 字段」查枚举中文；未登记返回 ``None``（调用方原样输出）。"""
    table = _ENUM_LABELS.get(channel, {}).get(key)
    if not table:
        return None
    return table.get(str(value))


def _format_value(channel: str, key: str, value: Any) -> str:
    """把单个取值渲染成可读文本：枚举译中文、金额由分换算为元。"""
    if isinstance(value, bool):
        return "是" if value else "否"
    label = _enum_label(channel, key, value)
    if label is not None:
        return label
    if key.endswith("_fen") and isinstance(value, (int, float)) and not isinstance(value, bool):
        # 接口金额单位恒为分（``amount_unit=fen``）。这里直接换成元并标单位，
        # 免得下游把「374900」当成 37 万元的裸数字去和「万元」话术混用。
        return f"{value / 100:,.2f} 元"
    if isinstance(value, (list, tuple)):
        return "、".join(str(item) for item in value if item not in (None, ""))
    if isinstance(value, dict):
        # 嵌套只出现在旧版未扁平化的载荷里；平铺成 ``a=b; c=d`` 保留内容。
        return "; ".join(f"{k}={v}" for k, v in value.items() if v not in (None, ""))
    return str(value)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or value == [] or value == {}


def render_record(channel: str, record: dict[str, Any]) -> str:
    """渲染单条本体记录：一字段一行，``空值不写``。"""
    lines: list[str] = []
    skip = _CHANNEL_SKIP_KEYS.get(channel, frozenset())
    for key, value in record.items():
        if key in _SKIP_KEYS or key in skip or _is_blank(value):
            continue
        lines.append(f"{_label(channel, key)}：{_format_value(channel, key, value)}")
    return "\n".join(lines)


def render_channel(channel: str, records: list[Any]) -> str:
    """渲染一个渠道的多条记录。

    多条之间用空行分隔并加序号。序号是给模型区分「这是三条不同的跟进」用的，
    不是业务编号，因此不放进字段行里，避免被抄进证据锚点。
    """
    blocks: list[str] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        text = render_record(channel, record)
        if not text:
            continue
        blocks.append(text if len(records) == 1 else f"[第 {index} 条]\n{text}")
    return "\n\n".join(blocks)


def render_payload(channel: str, raw_payload: Any) -> str:
    """按渠道渲染一条清单记录的 ``raw_payload``。

    三类形态在这里统一：

    - **基础渠道**：``{"records": [...], "fetched_at": ...}`` —— 展开 ``records`` 逐条渲染；
    - **过程资料 / 录音**：``raw_payload`` 本身就是扁平对象 —— 直接渲染；
    - **外部补充**：字段各自定义 —— 直接渲染（未登记标签的键原样输出）。

    ``raw_payload`` 为 ``None``（截图类渠道常见）时返回空串，由记录层记 ``exception``。
    """
    if raw_payload is None:
        return ""
    if not isinstance(raw_payload, dict):
        return render_channel(channel, [raw_payload]) if isinstance(raw_payload, list) else ""

    records = raw_payload.get("records")
    if isinstance(records, list):
        return render_channel(channel, records)
    return render_channel(channel, [raw_payload])


def is_removed(raw_payload: Any) -> bool:
    """过程资料是否携带「上游已移除」信号。

    接口约定被移除的记录会返回 ``{"operation": "removed", "reason": ...}``。把它当正文
    渲染会让一条**不存在的记录**变成证据，因此必须先拦掉。
    """
    return isinstance(raw_payload, dict) and str(raw_payload.get("operation") or "") == "removed"
