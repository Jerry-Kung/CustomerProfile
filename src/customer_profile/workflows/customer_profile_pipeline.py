"""``业务测试臂`` —— 业务优化主方案的事实账本管线。

这是 :mod:`customer_profile.workflows.customer_profile_entry` 的**平行实现**，两臂共用
同一套取数子流程与同一个成文工作流，只在**中间的取证与投影**上换实现。AB 实验按
``scripts/run_baseline.py --workflow-id`` 切换两臂，产物形状与归档逻辑完全一致。

    基线臂（customer_profile_entry，2026-10-16 已从注册表摘除，仅作历史样本）:
      evidence_subagent（21 节点 / 14 路扇出，含 1 个 LLM 取证节点）
        → profile_features_analysis（人设特征判断）
          → customer_profile_pipeline → result1

    业务测试臂（本模块，阶段一/二）:
      取数层（14 路 tool，**原样复用**既有子流程）
        → L0 记录层（纯代码）：把清单与各渠道产物统一成 records[]
          → L4 分诊层（纯代码）：sufficient / thin / empty
            → 取证输入包（纯代码）：只喂「有内容」的渠道
              → 证据线索整理（复用既有提示词）
                → profile_features_analysis → 本模块 → result1

**阶段一/二的范围**：本模块此刻只落 L0 与 L4（``落地方案.md`` §2 阶段一），
以及把 L0 的渠道分级接进取证输入包。L1 取证层与 L2 投影层在阶段二接入，
届时替换「取证输入包 → 证据线索整理」这一段。

五条口径（``端到端方案.md`` §2 的设计约束，逐条对应）：

1. **渠道分级由代码按清单定**，不由模型猜。清单里没有该 ``channel`` 就是结构性空，
   记 ``status=exception``，不进取证输入包——这一条同时治愈「输入有录音、输出却说无录音」，
   因为「有没有」由记录层说了算，不由模型回忆。
2. **录音转写逐字留存**（``content_kind=transcript``），中间不插任何 LLM 摘要。
   取值自 ``audio_content_extract`` 的 ``auc_result``（ASR 六字段数组原文），
   **不是**它下游那份经 LLM 压缩的分析报告。
3. **``speaker`` 与逐句 ``gender`` 只作为原文属性附着**，不作归属依据——实测 39 案里
   没有一例「两个说话人、性别各自干净」，编号不可用来判断谁是客户。
4. **时间戳随记录携带**，供 L2 做时效判定（如「登记状态注销」的企业不得当作在职）。
5. **推理成本次之、准确性优先**：能写成代码的约束不交给提示词，因此 L0 与 L4 都是纯代码，
   **0 token**。

**与 ``evidence_subagent`` 的关系**：不修改它。它是基线臂的组成部分，改它会让同一条分支上
的两臂一起变，AB 立刻不成立。代价是 14 路 ``tool`` 声明在这里重复一份；``tests``
里有一条用例断言两份声明的渠道集合一致，防止日后单边漂移。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import internal_fields as inf
from .. import notes_contract as nc
from .. import projection as proj
from .. import readable as rdb
from .. import rules
from ..definitions import WorkflowDef, make_node
from ..rules.clusters import expression as expression_rules
from ..rules.clusters import hard as hard_rules
from ..rules import checker as rule_checker

WORKFLOW_ID = "customer_profile_pipeline"
"""**稳定标识，不得改动。** 模板资源名是 ``<slug>__<node_id>``，而 ``scripts/run_baseline.py
--workflow-id`` 与既有批次（``pipeline-v3.3-39``）的可比性判据都以它为准。"""

DISPLAY_NAME = "业务测试臂"
"""对外显示名。

该臂的实验已完成并通过核验，对外口径由「实验臂」改为「**业务测试臂**」（2026-10-15 拍板）。
**改名只限本层**：``WORKFLOW_ID`` 与 ``SLUG`` 一个字都不动，否则跑批入口与批次可比性判据
会一并断掉。"""

PROFILE_API_PREFIX = "/api/v1/remote/"
"""画像服务的 API 前缀。

原先与此常量同形的定义在生产臂 ``customer_profile_production`` 里，该臂已于
2026-10-27 整臂退役删除；现在这里是唯一一处定义。
"""

# ---------------------------------------------------------------- 节点 ID

START = "pipeline_start"
HISTORY_HTTP = "pipeline_history"
END = "pipeline_end"

# —— 取数层（14 路 tool，复用既有子流程）
MOMENTS = "pipeline_moments"
WECHAT_SEARCH = "pipeline_wechat_search"
WECHAT_HOME = "pipeline_wechat_home"
TEST_DRIVE = "pipeline_test_drive"
OUTBOUND_CALL = "pipeline_outbound_call"
ALIPAY = "pipeline_alipay"
XIAOHONGSHU = "pipeline_xiaohongshu"
DOUYIN = "pipeline_douyin"
LOCKED_NOTES = "pipeline_locked_notes"
JIGUANG = "pipeline_jiguang"
MENGSHI_IT = "pipeline_mengshi_it"
CHAT_HISTORY = "pipeline_chat_history"
FEEDBACK = "pipeline_feedback"
PHONE_COMPANY = "pipeline_phone_company"

# —— 录音原文（逐字转写来自这里，不是「录音信息提取」子流程的压缩报告）
TEST_DRIVE_FILES = "pipeline_test_drive_files"
TEST_DRIVE_ITER = "pipeline_test_drive_iter"
TEST_DRIVE_ITER_START = "pipeline_test_drive_iter_start"
TEST_DRIVE_EXTRACT = "pipeline_test_drive_extract"
OUTBOUND_FILES = "pipeline_outbound_files"
OUTBOUND_ITER = "pipeline_outbound_iter"
OUTBOUND_ITER_START = "pipeline_outbound_iter_start"
OUTBOUND_EXTRACT = "pipeline_outbound_extract"

# —— L0 记录层 / L4 分诊层
CHANNEL_BUNDLE = "pipeline_channel_bundle"
L0_RECORDS = "pipeline_l0_records"
L4_TRIAGE = "pipeline_l4_triage"

# —— L1 取证层（v3 三分层：事实 → 人设 → 分析投影）/ L2 投影层
L1_INPUT = "pipeline_l1_input"
L1_FACTS = "pipeline_l1_facts"
L1_PERSONA = "pipeline_l1_persona"
L1_FIELDS = "pipeline_l1_fields"
L1_MERGE = "pipeline_l1_merge"
"""L1 三层合并（纯代码）：把 1a 事实层与 1c 分析投影层按字段求并集。

**为什么必须有这一层**：三分层把「一次直出」拆成「1a 出硬事实 + 1c 补推断」，而 1c
提示词明说「事实清单已给的字段不用重复」——两层因此是**互补**而非重叠。若少了这一层，
1a 检出的事实（如 ``intended_brand=埃安``）根本不进账本，在 Notes 里直接变空值。
实测 8 案：1a=90 字段 / 1c=156 字段 / 两层重叠仅 6 / 仅 1a 有 84。"""
L1_LOCALIZE = "pipeline_l1_localize"
L1_INJECT = "pipeline_l1_inject"
"""确定性字段注入（纯代码）：``phone_number`` 直取入参、``driver_license_status`` 按
试驾/旧车规则定（v3.1 改造四）。

**为什么接在 ``L1_LOCALIZE`` 之后**：本节点的来源名（``客户留资手机号`` /
``试驾与置换记录``）在记录层没有对应记录，若接在定位层之前，:func:`localize_claims`
会按「给了渠道却找不到记录」把注入值降为 ``inferred_low`` 并清空渠道——正好废掉它
刻意标注的登记档。故链路为 ``L1_LOCALIZE → L1_INJECT → L2_PROJECT``。"""
L1_PERSONA_INPUT = "pipeline_l1_persona_input"
L1_FIELDS_INPUT = "pipeline_l1_fields_input"
L2_PROJECT = "pipeline_l2_project"
L2_FALLBACK = "pipeline_l2_fallback"
L2_APPLY = "pipeline_l2_apply"
L2_REVIEW = "pipeline_l2_review"
RULE_CHECK = "pipeline_rule_check"
RULE_APPLY = "pipeline_rule_apply"

# —— 商机等级定级（独立一次 LLM 调用，v3.6）
LEVEL_INPUT = "pipeline_level_input"
"""定级输入装配（纯代码）：判定业务阶段 + 把全部客户记录与已定稿事实拼成提示词输入。

**为什么单列一个阶段**：商机等级对销售的重要程度断档领先其它字段（2026-10-19 业务方
要求），必须独占一次 LLM 调用、拿到全量原始数据与足够的输出篇幅，不能像普通字段那样
由 L1 顺带一行产出。阶段判定用现成的 :func:`_has_test_drive`，口径与确定性字段注入一致。
"""
LEVEL_ASSESS = "pipeline_level_assess"
"""商机等级评定（LLM，1 次）：按业务专家《客户商机等级定级规范》输出 H/A/B/C/待识别。"""
LEVEL_APPLY = "pipeline_level_apply"
"""定级收口（纯代码）：枚举归一、阶段上限（R04）、证据门槛（R05）、原文定位校验。

**为什么规则必须在代码层再拦一次**：提示词已写明 R04/R05，但模型偶发越界；阶段上限与
「H/A 必须同时具备时间、需求、行动证据」都是可机器判定的硬规则，交给提示词等于把
业务红线押在模型自觉上。
"""

# —— L3 成文段
PRODUCTION = "pipeline_production"

# —— v2 回传（旁支，不改变主链与 END 的契约）
CALLBACK_BUILD = "pipeline_callback_build"
"""回传体构造（纯代码，0 token）。

**为什么挂在 PRODUCTION 之后而不是塞进 L3 子流程**：L3 是对外产物（``result1``）的产地，
它的装配形状被 ``tests/test_l3.py`` 逐条钉住。回传是新长出的一条**旁支**——END 仍挂
PRODUCTION，``result1`` 的来源与形状一个字不改。"""

CALLBACK_WRITE = "pipeline_callback_writeback"
"""回写节点。带 ``@writeback``，受既有的 ``WRITEBACK_ENABLED`` 闸门管辖（缺省不发送）。"""

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
SLUG = "customer_profile_pipeline"
SYSTEM_TEXT = "You are a helpful AI assistant."
HISTORY_PATH = "/api/v1/remote/data/history/"
LITERAL = "@literal:"

L3_WORKFLOW = "customer_profile_l3"
"""v2 的成文段：代码校验 + 1 次表达 + 代码装配（设计稿 §7）。

替代 production 的 12 个 LLM 节点：那张图里 9 个 section 与 Notes **各自生成**，
是本项目的主要成本项与「卡片与 Notes 不一致」的根源。"""
AUDIO_EXTRACT_WORKFLOW = "audio_content_extract_roled"
"""录音抽取子流程：**带角色标注**的变体。

**注意指向的不是 ``audio_content_extract_pure``。** 纯识别变体（零 LLM）是 2026-10 之前
的口径：只交逐字原文。实测发现原始 ``speaker`` 编号不可信（55 条录音中 41 条存在
「同一编号下性别稳定矛盾」），销售自述（「我们是4S店的」）一旦被错归到客户方，下游会把
客户职业整个判错。因此 v2 在纯识别之后多一次 LLM：按**语义**把每句判成
销售方 / 客户方 / 未知来源，勘误明细单独交出。子流程交出 ``transcript_bundle``
（纯正文 + 六字段原文 + 句级角色与字符区间 + 勘误明细）。

**为什么是另开一个 workflow_id 而不是在纯识别变体里加节点**：纯变体的语义是「零 LLM、
只交逐字原文」，``tests/test_pipeline_l0_pure.py`` 用节点数与零 LLM 钉住它；新开变体
让回退只需把本常量改回 ``audio_content_extract_pure``。
"""

MOMENTS_PURE = "moments_info_pure"
"""朋友圈截图子流程：同样指向纯识别变体（关掉「多图片内容总结」）。"""

TEST_DRIVE_PURE = "test_drive_audio_pure"
"""试驾录音链路：纯识别变体（关掉图内的「录音文件分析」与其内部的两个 LLM 节点）。"""

OUTBOUND_CALL_PURE = "outbound_call_audio_pure"
"""外呼录音链路：纯识别变体。"""


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


L1_FACTS_TEMPLATE = template_name(L1_FACTS)
"""L1-1a 事实提取层提示词：只聚焦「全 / 准 / 事实（高置信推断）」，输出 JSONL。"""

L1_PERSONA_TEMPLATE = template_name(L1_PERSONA)
"""L1-1b 人设画像层提示词：半结构化画像 + 一段整体描述。

**它不进证据位**（设计稿 §5.1）：只作后续推理的一致性参考，其结论不得写进任何
`reasoning_detail.evidence`。"""

L1_FIELDS_TEMPLATE = template_name(L1_FIELDS)
"""L1-1c 分析投影层提示词：基于事实 + 人设参考，补齐字段级结论。

证据只能顺到带坐标的事实，不得停在人设层（设计稿 §5.4）。"""

L2_FALLBACK_TEMPLATE = template_name(L2_FALLBACK)
"""L2 兜底提示词资源名（受约束的裁决者，见 `端到端方案设计.md` §6.3）。"""

L2_REVIEW_TEMPLATE = template_name(L2_REVIEW)
"""L2 审查核验层提示词资源名（v3 设计稿 §3、§7-5）。

审查层对 L1/L2 的产出做合规性复核：枚举合法、无效占位清除、Notes 与 facts 同源、
fact 必有锚点。**能代码判定的先由代码修**，只有代码判不了的语义问题才走这一次 LLM。"""

LEVEL_ASSESS_TEMPLATE = template_name(LEVEL_ASSESS)
"""商机等级定级提示词：规范全文要点 + 全量记录 + 已定稿事实，输出结构化定级结论。"""


# ====================================================================
# 渠道表：15 个取证渠道 → 清单渠道名 / 来源分级 / 内容形态
#
# ``manifest_channel`` 为 ``None`` 表示该渠道不来自
# ``GET /api/v1/remote/data/history/{phone}`` 清单（人工确认与反馈走 mhero），
# 其「有无数据」只能看子流程的产出，判据见 ``build_records``。
#
# 实测（39 案）：清单只出现 5 个 channel；其余 **9 个渠道 39/39 结构性为空**。
# ====================================================================

EXCEPTION = "exception"
PRIMARY_INTERACTION = "primary_interaction"
DECLARED = "declared"
WEAK = "weak"
PREDICTION = "prediction"

OK = "ok"

CHANNELS: tuple[dict[str, Any], ...] = (
    # (取证渠道名, 清单 channel, 来源分级, 内容形态, 产出它的 tool 节点)
    {
        "source_name": "朋友圈截图内容信息",
        "manifest_channel": "WeChatMomentsScreenshot",
        "source_class": DECLARED,
        "content_kind": "ocr",
        "node_id": MOMENTS,
    },
    {
        "source_name": "微信手机号搜索界面信息",
        "manifest_channel": "WeChatPhoneSearchScreenshot",
        "source_class": DECLARED,
        "content_kind": "ocr",
        "node_id": WECHAT_SEARCH,
    },
    {
        "source_name": "微信主页截图信息",
        "manifest_channel": "WeChatHomepageScreenshot",
        "source_class": DECLARED,
        "content_kind": "ocr",
        "node_id": WECHAT_HOME,
    },
    {
        "source_name": "试驾录音信息",
        "manifest_channel": "TestDriveRecordingFile",
        "source_class": PRIMARY_INTERACTION,
        "content_kind": "transcript",
        "node_id": TEST_DRIVE,
    },
    {
        "source_name": "外呼录音信息",
        "manifest_channel": "AIOutboundCallRecordingFile",
        "source_class": PRIMARY_INTERACTION,
        "content_kind": "transcript",
        "node_id": OUTBOUND_CALL,
    },
    {
        "source_name": "支付宝主页截图信息",
        "manifest_channel": "AlipayPersonalPageScreenshot",
        "source_class": DECLARED,
        "content_kind": "ocr",
        "node_id": ALIPAY,
    },
    {
        "source_name": "小红书主页截图信息",
        "manifest_channel": "XiaohongshuPersonalPageScreenshot",
        "source_class": DECLARED,
        "content_kind": "ocr",
        "node_id": XIAOHONGSHU,
    },
    {
        "source_name": "抖音主页截图信息",
        "manifest_channel": "DouyinPersonalPageScreenshot",
        "source_class": DECLARED,
        "content_kind": "ocr",
        "node_id": DOUYIN,
    },
    {
        "source_name": "手机号归属地信息",
        "manifest_channel": "RelatedEnterpriseInfo",
        "source_class": WEAK,
        "content_kind": "json",
        "node_id": PHONE_COMPANY,
    },
    {
        "source_name": "关联企业信息",
        "manifest_channel": "RelatedEnterpriseInfo",
        "source_class": WEAK,
        "content_kind": "json",
        "node_id": PHONE_COMPANY,
    },
    {
        "source_name": "第三方画像数据",
        "manifest_channel": "DatametInterestPoints",
        "source_class": PREDICTION,
        "content_kind": "json",
        "node_id": JIGUANG,
    },
    {
        "source_name": "销售人员人工确认的客户Notes信息",
        "manifest_channel": None,
        "source_class": DECLARED,
        "content_kind": "json",
        "node_id": LOCKED_NOTES,
    },
    {
        "source_name": "客户留存在我方销售数据系统中的信息记录",
        "manifest_channel": "MengShiITSystemData",
        "source_class": DECLARED,
        "content_kind": "json",
        "node_id": MENGSHI_IT,
    },
    {
        "source_name": "客户与我方销售人员的聊天记录信息",
        "manifest_channel": "MengShiCustomerChatInfo",
        "source_class": PRIMARY_INTERACTION,
        "content_kind": "json",
        "node_id": CHAT_HISTORY,
    },
    {
        "source_name": "销售人员对当前客户历史画像数据的反馈记录",
        "manifest_channel": None,
        "source_class": DECLARED,
        "content_kind": "json",
        "node_id": FEEDBACK,
    },
)
"""15 个取证渠道的元信息。

顺序与 ``evidence_subagent`` 的「客户数据聚合」模板一致——取证输入包按同一套
中文键组织，``证据线索整理`` 提示词要求 ``evidence_source`` 的取值与输入 JSON 的 key
相同，两者必须对得上。
"""

# 需要逐字转写的两个录音渠道：它们的原话取自 ``audio_content_extract``，
# 而不是「录音信息提取」子流程的压缩报告。
TRANSCRIPT_SOURCES: tuple[str, ...] = ("试驾录音信息", "外呼录音信息")



# ====================================================================
# code 节点正文
# ====================================================================


def collect_audio_files(records, data_source) -> dict:
    """从清单里取出某录音渠道的 ``media_urls``，规范化成字符串列表。

    与 ``shared_code.parse_file_array`` 的分工：那个假定上游已给出一段 JSON 文本，
    空值时 ``json.loads("")`` 会抛。这里一并处理「渠道不存在」「载荷为 None」
    「已是列表」三种形态，供迭代节点直接消费。

    为什么自己写而不是复用 ``extract_media_urls`` + ``parse_file_array`` 两步：
    后者的空值路径会抛异常，而本管线刻意让「渠道不存在」不构成失败。
    """
    if records is None:
        return {"files": []}
    if isinstance(records, (list, tuple, dict)):
        data = records
    else:
        text = str(records).strip()
        if not text:
            return {"files": []}
        try:
            data = json.loads(text)
        except ValueError:
            return {"files": []}

    if not isinstance(data, list):
        data = [data]

    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("channel") != data_source:
            continue
        media = item.get("media_urls")
        if media is None:
            return {"files": []}
        if isinstance(media, str):
            media = [media]
        if isinstance(media, (list, tuple)):
            return {"files": [str(x) for x in media if x]}
        return {"files": []}
    return {"files": []}


def collect_test_drive_files(records) -> dict:
    """``code`` 节点入口：收集试驾录音的文件直链。

    为什么包一层而不是把渠道名做成入参：``code`` 节点的入参只能是 ``(节点, 字段)``
    引用（``make_node`` 会把它交给 :func:`parse_selector`），传不了字面量。因此渠道名
    写死在函数签名里，靠两个窄包装区分，而不是约定一个永远收不到值的入参。
    """
    return collect_audio_files(records, "TestDriveRecordingFile")


def collect_outbound_files(records) -> dict:
    """``code`` 节点入口：收集外呼录音的文件直链。见 ``collect_test_drive_files``。"""
    return collect_audio_files(records, "AIOutboundCallRecordingFile")


def bundle_channels(
    history_body=None,
    moments=None,
    wechat_search=None,
    wechat_home=None,
    alipay=None,
    xiaohongshu=None,
    douyin=None,
    locked_notes=None,
    mengshi_it=None,
    chat_history=None,
    feedback=None,
    phone_company=None,
    phone_location=None,
    jiguang=None,
    test_drive_transcript=None,
    outbound_transcript=None,
    test_drive_report=None,
    outbound_report=None,
) -> dict:
    """把 14 路子流程的产出与清单原文汇成一份「渠道 → 内容」表。

    录音渠道给两样东西：``*_transcript``（逐字原文，来自 ``audio_content_extract``）
    与 ``*_report``（压缩报告，来自「录音信息提取」子流程）。L0 取原文，
    报告只作兜底——**不**让压缩产物覆盖原文。

    **手机号归属地与企业信息分开接**（2026-10-19 可读性整改）：此前两条都指向
    ``company_analysis_result``，于是「手机号归属地信息」下面的正文其实是企业注册信息，
    L1 把「珠宝首饰零售」这一行业结论挂在归属地名下，信源与结论看起来毫不相干。
    归属地接 ``phonenumber_analysis_result``，企业接 ``company_analysis_result``。
    参数名 ``phone_company`` 保留原义（企业信息），接线处不再复用它。
    """
    channels = {
        "朋友圈截图内容信息": moments,
        "微信手机号搜索界面信息": wechat_search,
        "微信主页截图信息": wechat_home,
        "支付宝主页截图信息": alipay,
        "小红书主页截图信息": xiaohongshu,
        "抖音主页截图信息": douyin,
        "销售人员人工确认的客户Notes信息": locked_notes,
        "客户留存在我方销售数据系统中的信息记录": mengshi_it,
        "客户与我方销售人员的聊天记录信息": chat_history,
        "销售人员对当前客户历史画像数据的反馈记录": feedback,
        "手机号归属地信息": phone_location,
        "关联企业信息": phone_company,
        "第三方画像数据": jiguang,
    }
    return {
        "channels_json": json.dumps(
            {
                "history_body": history_body,
                "channels": channels,
                "transcripts": {
                    "试驾录音信息": test_drive_transcript,
                    "外呼录音信息": outbound_transcript,
                },
                "reports": {
                    "试驾录音信息": test_drive_report,
                    "外呼录音信息": outbound_report,
                },
            },
            ensure_ascii=False,
            default=str,
        )
    }


def _manifest_index(history_body: Any) -> dict[str, dict]:
    """把清单响应体解析成 ``{channel: 第一条记录}``。

    解析失败按「清单为空」处理：拿不到清单时**所有**渠道都记 ``exception``。
    这是刻意的保守取向——宁可如实说「不知道有哪些渠道」，也不要凭猜测给它内容。
    """
    if history_body is None:
        return {}
    if isinstance(history_body, (list, tuple)):
        data = list(history_body)
    else:
        text = str(history_body).strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            return {}
    if not isinstance(data, list):
        return {}

    index: dict[str, dict] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        channel = item.get("channel")
        if not channel:
            continue
        # 同一渠道出现多条时保留**最新**一条（清单自带 created_at）。
        current = index.get(channel)
        if current is None or str(item.get("created_at") or "") >= str(
            current.get("created_at") or ""
        ):
            index[channel] = item
    return index


def _is_empty_container(value: Any) -> bool:
    """判断一个结构化产出是不是「有形状但无事实」的空壳。

    实测量：``human_corrected_info`` 在 39/39 案返回 ``{"locked_notes": []}`` ——
    字符串非空，``bool()`` 为真，但它不承载任何客户事实。若按「非空即有内容」处理，
    这个渠道会以 ``status=ok`` 混进取证输入包，把一个「结构性缺失」伪装成一条证据。
    判定递归到底：字典的所有值都空、列表为空，即为空壳。
    """
    if isinstance(value, dict):
        return all(_is_empty_container(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return all(_is_empty_container(v) for v in value)
    if isinstance(value, str):
        return not value.strip()
    return value is None


def _transcript_parts(value: Any) -> tuple[str, str, list, list]:
    """把录音转录产出拆成（标注正文，六字段原文，句级角色，勘误明细）。

    两种形态都要认：

    - **新形态**：角色标注子流程交出的 ``transcript_bundle``——一段 JSON 对象，
      含 ``content``（纯正文）、``raw``（ASR 六字段原文）、``role_lines``（句级角色与
      字符区间）与 ``corrections``（勘误明细）；
    - **旧形态**：裸的 ``auc_result`` 数组文本。此时原文即正文，角色与勘误为空。

    迭代节点 ``flatten_output`` 后给的是**逐文件**的列表，逐项拆完再按换行拼接，
    因此多文件录音的原文与正文各自对齐、不会交叉。角色区间**按文件独立**，拼接后
    不跨文件复用——多文件时角色信息只保留各文件的区间偏移，由消费方按文件边界处理。
    """
    if value is None:
        return "", "", [], []
    if isinstance(value, (list, tuple)):
        parts = [_transcript_parts(v) for v in value]
        content_parts: list[str] = []
        raw_parts: list[str] = []
        role_lines: list = []
        corrections: list = []
        cursor = 0
        for content, raw, lines, items in parts:
            if content:
                # 多文件拼接时角色区间必须整体后移：各文件的区间是**文件内**偏移，
                # 直接拼接会让第二个文件起的所有区间落到错误的句子上。
                for line in lines:
                    if isinstance(line, dict):
                        shifted = dict(line)
                        shifted["start"] = int(shifted.get("start") or 0) + cursor
                        shifted["end"] = int(shifted.get("end") or 0) + cursor
                        role_lines.append(shifted)
                    else:
                        role_lines.append(line)
                content_parts.append(content)
                cursor += len(content) + 1
            if raw:
                raw_parts.append(raw)
            corrections.extend(items)
        return "\n".join(content_parts), "\n".join(raw_parts), role_lines, corrections
    if not isinstance(value, str):
        return "", "", [], []

    text = value.strip()
    if not text or _looks_like_sentinel(text):
        return "", "", [], []
    try:
        parsed = json.loads(text)
    except ValueError:
        return text, text, [], []

    if isinstance(parsed, dict) and "content" in parsed:
        content = str(parsed.get("content") or "").strip()
        raw = str(parsed.get("raw") or "").strip()
        role_lines = parsed.get("role_lines") or []
        corrections = parsed.get("corrections") or []
        if not raw:
            raw = content
        return (
            content,
            raw,
            list(role_lines) if isinstance(role_lines, list) else [],
            list(corrections) if isinstance(corrections, list) else [],
        )

    # ASR 六字段数组：解析得动就说明是转录本体，原样保留（逐字，不重排）。
    # 但空数组要归零：实测有 3 个录音文件识别结果是 ``"[]"``，它是「没识别出内容」，
    # 不是「一段叫 [] 的原文」。留成非空会把一个空录音记成 status=ok。
    if isinstance(parsed, list) and not parsed:
        return "", "", [], []
    return text, text, [], []


def _role_at(role_lines: Any, offset: int) -> str:
    """在句级角色表里按下标找角色：返回覆盖 ``offset`` 的那一句的角色。

    角色区间是正文的字符闭开区间，定位层切出的引文起点落在哪一句，就直接用那一句的
    角色——不跨句推断。找不到返回空串（非录音渠道、旧产物、标注失败都走这里）。
    """
    if not isinstance(role_lines, list):
        return ""
    for line in role_lines:
        if not isinstance(line, dict):
            continue
        try:
            start = int(line.get("start"))
            end = int(line.get("end"))
        except (TypeError, ValueError):
            continue
        if start <= offset < end or (start <= offset <= end and end == start):
            return str(line.get("role") or "")
    return ""


def _transcript_text(value: Any) -> str:
    """把录音转录产出归一成一段文本（标注正文侧）。保留给既有调用点。"""
    return _transcript_parts(value)[0]


def _role_segments(role_lines: Any) -> str:
    """把句级角色压成「句区间 → 角色」的短串，供分节头展示。

    形如 ``0-4 销售方；5 客户方；6-9 未知来源``。相邻同角色合并，避免几百句的录音
    把一行撑成几千字。``role_lines`` 为空（旧产物或标注失败）时返回空串。
    """
    if not isinstance(role_lines, list) or not role_lines:
        return ""
    merged: list[list[Any]] = []
    for item in role_lines:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        role = str(item.get("role") or "")
        if merged and merged[-1][2] == role and merged[-1][1] + 1 == index:
            merged[-1][1] = index
        else:
            merged.append([index, index, role])
    parts: list[str] = []
    for lo, hi, role in merged:
        span = f"{lo}" if lo == hi else f"{lo}-{hi}"
        parts.append(f"{span} {role}")
    return "；".join(parts)


def _correction_footnote(corrections: Any) -> str:
    """勘误明细压成一句脚注，供分节头展示。

    正文里用的是**勘误后的写法**，原识别文本只在 ``content_raw`` 里；把「哪句改过」
    在分节头点出来，L1 与定级都能知道哪些句子不是逐字原话。最多列 8 条，
    避免长录音的勘误把分节头撑大。
    """
    if not isinstance(corrections, list) or not corrections:
        return ""
    parts: list[str] = []
    for item in corrections[:8]:
        if not isinstance(item, dict):
            continue
        index = item.get("i")
        reason = str(item.get("reason") or "").strip()
        parts.append(f"第 {index} 句（{reason}）" if reason else f"第 {index} 句")
    more = len(corrections) - len(parts)
    return "、".join(parts) + (f" 等 {len(corrections)} 处" if more > 0 else "")


def _as_content(value: Any) -> str:
    """把任意渠道产出归一成文本。空值一律返回空串，不返回 ``"None"``。

    空壳（``{"locked_notes": []}``）与「无数据」提示语（``该用户不存在人工反馈数据``）
    一并归零：它们都是**没有事实**的产出，保留下来只会污染下游。
    """
    if value is None:
        return ""
    if _is_empty_container(value):
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text or _looks_like_sentinel(text):
            return ""
        # 字符串里可能是**序列化后的空壳**（``'{"locked_notes": []}'``）：字符串非空，
        # 但它承载的事实为零。实测人工确认 Notes 在 39/39 案就是这一形态，若按「非空即有
        # 内容」处理，九个结构性空渠道里会漏掉它，取证包多进一条空证据。
        try:
            parsed = json.loads(text)
        except ValueError:
            return text
        if _is_empty_container(parsed):
            return ""
        return text
    return json.dumps(value, ensure_ascii=False, default=str)


def build_records(history_body, channels_json, phone_number: str = "") -> dict:
    """L0 记录层：把清单与各渠道产物统一成 ``records[]``。

    记录形状（``端到端方案.md`` §3.2）::

        {record_id, channel, source_name, source_class,
         status, created_at, content, content_kind}

    四条判定口径：

    - **清单里没有该 channel ⇒ ``status=exception``**。清单是「有哪些渠道」的唯一事实来源，
      因此「输入里到底有没有录音」由这里说了算。
    - 不走清单的两个渠道（人工确认 Notes、历史画像反馈，走 mhero）按其产出是否为空判定。
    - 录音渠道的内容取**逐字原文**；原文缺失时回落压缩报告，并把 ``content_kind``
      标成 ``report``——明确区分「这是原话」与「这是二次加工产物」。
    - 每条记录都带 ``created_at``（清单自带；缺失时为空串，不伪造时间）。
    """
    payload: dict[str, Any] = {}
    if channels_json:
        try:
            payload = json.loads(channels_json) if isinstance(channels_json, str) else dict(channels_json)
        except (ValueError, TypeError):
            payload = {}

    manifest = _manifest_index(history_body)
    contents: dict[str, Any] = payload.get("channels") or {}
    transcripts: dict[str, Any] = payload.get("transcripts") or {}
    reports: dict[str, Any] = payload.get("reports") or {}

    records: list[dict[str, Any]] = []
    for spec in CHANNELS:
        source_name = spec["source_name"]
        manifest_channel = spec["manifest_channel"]
        item = manifest.get(manifest_channel) if manifest_channel else None

        if manifest_channel is not None:
            present = item is not None
        else:
            # 无清单背书的渠道：产出非空即视为有内容。
            present = bool(_as_content(contents.get(source_name)))

        content_kind = spec["content_kind"]
        content = ""
        content_raw = ""
        role_lines: list = []
        corrections: list = []
        if present:
            if source_name in TRANSCRIPT_SOURCES:
                content, content_raw, role_lines, corrections = _transcript_parts(
                    transcripts.get(source_name)
                )
                if content:
                    content_kind = "transcript"
                else:
                    # 原文缺失才回落压缩报告。报告经过一次 LLM 加工，且「无录音」时
                    # 本身就是一句提示语（会被 _as_content 归零），因此不该覆盖原文。
                    content = _as_content(contents.get(source_name)) or _as_content(
                        reports.get(source_name)
                    )
                    content_kind = "report"
            else:
                content = _as_content(contents.get(source_name))
                # 媒体渠道（截图/企业/第三方）的正文常常只在清单的 raw_payload 里
                # （视觉子流程未命中时返回一句「查询异常」的哨兵文本）。清单有实体载荷
                # 就以它为正文，避免把哨兵当成内容。
                if item is not None and item.get("raw_payload") is not None:
                    raw = _as_content(item.get("raw_payload"))
                    if raw and (not content or _looks_like_sentinel(content)):
                        content = raw

        status = OK if present and content else EXCEPTION
        records.append(
            {
                "record_id": f"rec_{manifest_channel or source_name}_{phone_number}",
                "channel": manifest_channel or source_name,
                "source_name": source_name,
                "source_class": spec["source_class"],
                "status": status,
                "created_at": str((item or {}).get("created_at") or ""),
                "content": content,
                "content_kind": content_kind,
                # 录音的**未标注**六字段原文。角色标注与勘误都可能出错，而正文一经改写
                # 就无法回查，因此原文单独留一份（非录音渠道为空串）。
                "content_raw": content_raw,
                # 句级角色（``{i, role, start, end}``）与勘误明细。角色供 L1 输入装配
                # 与投影层的证据标注使用；二者都只对录音渠道非空。
                "role_lines": role_lines,
                "corrections": corrections,
            }
        )

    return {
        "records_json": json.dumps(records, ensure_ascii=False),
        "records_count": len(records),
        "exception_count": sum(1 for r in records if r["status"] == EXCEPTION),
    }


SENTINEL_MARKERS: tuple[str, ...] = (
    "查询异常",
    "未查询到",
    "不存在",
    "无有效内容",
    "未获取到",
    "未查询到",
    "无数据",
    "为空",
)


def _looks_like_sentinel(text: str) -> bool:
    """判断一段产出是不是「无数据」的固定提示语。

    这些子流程在取不到数据时会返回一句自然语言提示（如「微信朋友圈数据查询异常」）。
    它与真实内容都是字符串，因此只能按措辞识别。识别不出来时**按内容处理**——
    宁可把一句提示当内容，也不要误删真实数据。
    """
    if not text:
        return False
    candidate = text.strip()
    if len(candidate) > 60:
        return False
    return any(marker in candidate for marker in SENTINEL_MARKERS)


def triage(records_json) -> dict:
    """L4 分诊层（纯代码）：判定本案素材是否足以走事实流程。

    三档（``端到端方案.md`` §3.6）：

    - ``sufficient``：有一手交互（录音/聊天）且有实质内容 → 走事实流程；
    - ``thin``：只有声明/社交/弱登记/预测 → 保守模板，保留登记档、第三方画像高置信
      与无渠道高置信字段（判据见 :func:`_thin_keeps`）；
    - ``empty``：几乎无数据 → 输出「待补充」骨架，不产生任何具体断言。

    **为什么是纯代码而不是一次小调用**：判据是「有哪些渠道、各有多长」，
    这是可从记录层直接读出的结构事实。交给模型既多一次成本，又引入不可复现的抖动；
    「能写成代码的约束不交给提示词」是本案的既定口径。
    """
    try:
        records = json.loads(records_json) if isinstance(records_json, str) else list(records_json or [])
    except (ValueError, TypeError):
        records = []

    usable = [r for r in records if r.get("status") == OK and (r.get("content") or "").strip()]
    primary = [r for r in usable if r.get("source_class") == PRIMARY_INTERACTION]

    if primary:
        tier = "sufficient"
        reason = f"含 {len(primary)} 个一手交互渠道（{', '.join(r['source_name'] for r in primary)}）"
    elif usable:
        tier = "thin"
        reason = f"仅 {len(usable)} 个非一手渠道有内容，无录音或聊天记录"
    else:
        tier = "empty"
        reason = "全部渠道无实质内容"

    return {
        "tier": tier,
        "tier_reason": reason,
        "usable_count": len(usable),
    }


def build_evidence_input(records_json, tier: str = "sufficient") -> dict:
    """把记录层里**有内容**的渠道拼成取证输入包。

    与既有的「客户数据聚合」模板的差别：那个模板把 15 个渠道**全部**渲染进去
    （空渠道渲染成一句提示语），于是 9 个结构性空渠道 × 39 案 = 351 条 E 级噪声进入
    提示词。这里只放 ``status=ok`` 的渠道，空渠道在记录层留痕、不进上下文。

    键名与既有模板一致（``证据线索整理`` 提示词要求 ``evidence_source`` 的取值与输入
    JSON 的 key 相同），因此提示词正文无需改动。

    ``thin`` / ``empty`` 档此刻仍走同一条取证路径：阶段一的收益是「不再被空渠道污染」，
    保守模板（只输出能坐实的字段）随 L1/L2 在阶段二接入。
    """
    try:
        records = json.loads(records_json) if isinstance(records_json, str) else list(records_json or [])
    except (ValueError, TypeError):
        records = []

    package: dict[str, str] = {}
    for spec in CHANNELS:
        source_name = spec["source_name"]
        for record in records:
            if record.get("source_name") != source_name or record.get("status") != OK:
                continue
            content = (record.get("content") or "").strip()
            if not content:
                continue
            # 同一渠道既可能有原文又可能有报告（录音）：原文优先。
            existing = package.get(source_name)
            if existing and record.get("content_kind") == "report":
                continue
            package[source_name] = content

    return {"output": json.dumps(package, ensure_ascii=False)}




def _records_of(records_json) -> list[dict]:
    """把 ``records_json`` 解析成记录列表；解析不动一律当空。"""
    if records_json is None:
        return []
    if isinstance(records_json, str):
        text = records_json.strip()
        if not text:
            return []
        try:
            records_json = json.loads(text)
        except ValueError:
            return []
    if isinstance(records_json, dict):
        records_json = records_json.get("records") or []
    if not isinstance(records_json, (list, tuple)):
        return []
    return [r for r in records_json if isinstance(r, dict)]


def _claims_payload(claims_json) -> tuple[list[dict], bool]:
    """解出 ``{"claims": [...]}``；返回 ``(断言列表, 是否成功解析)``。

    第二个返回值是必须的：L1 返回的不是 JSON 时，静默按「零断言」处理会让一次失败的
    抽取看起来像「本案确实没提取到东西」，与「模型什么都没找到」无从区分。
    """
    data = claims_json
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return [], True
        try:
            data = json.loads(text)
        except ValueError:
            return [], False
    if isinstance(data, dict):
        # v2 起 L1 输出键是 ``fields``（字段级结论表）；v1 与既有批次产物是 ``claims``
        # （断言账本）。两者条目形状同构，**都要认**——否则一切离线回放（R1b 的 13 案
        # 产物、全部既有用例）都会在改键当天失效。
        data = data.get("fields", data.get("claims"))
    if not isinstance(data, (list, tuple)):
        return [], False
    return [c for c in data if isinstance(c, dict)], True


_BASIS_RANK: dict[str, int] = {
    proj.FACT: 3,
    proj.INFERRED_HIGH: 2,
    proj.INFERRED_LOW: 1,
}


def merge_l1_layers(facts_json, fields_json) -> dict:
    """L1 合并层（纯代码，0 token）：把 1a 事实层与 1c 分析投影层按字段求并集。

    **为什么需要这一层**（v3 三分层的接线遗漏，见 ``L1_MERGE`` 常量说明）：
    三层把原先「一次直出全部字段」拆成「1a 出硬事实 + 1c 补推断」。1c 提示词明说
    「事实清单已经给了的字段，你不用重复」，两层因此是**互补**而非重叠——若不合并，
    1a 检出的事实（``intended_brand=埃安`` 这类带原文锚点的 ``fact``）根本不进账本，
    在最终 Notes 里直接变空值。实测 8 案：1a=90 / 1c=156 / 重叠仅 6 / 仅 1a 有 84。

    **同字段两边都有时的取舍**：按 ``basis`` 档位取更硬的一条（``fact`` >
    ``inferred_high`` > ``inferred_low``）；档位相同则**以 1a 为准**——1a 是「钉死的事实
    层」，其锚点已由 :func:`_locate` 校验，比 1c 的推断更可信。被覆盖的一条留痕在
    ``overrode`` 里，便于事后核对是 1a 抢了 1c、还是 1c 本就该赢。

    返回 ``claims_json``：形状与 1c 的 ``{"fields": [...]}`` 一致，直接喂
    :func:`localize_claims`，下游无需改口径。
    """
    facts, _ = _claims_payload(facts_json)
    fields, _ = _claims_payload(fields_json)

    merged: dict[str, dict] = {}
    order: list[str] = []

    def _put(entry: dict, *, prefer: bool) -> None:
        """写入一条结论；``prefer`` 为真时无条件覆盖已有。"""
        attribute = str(entry.get("attribute") or "").strip()
        if not attribute:
            return
        existing = merged.get(attribute)
        if existing is None:
            merged[attribute] = dict(entry)
            order.append(attribute)
            return
        if not prefer:
            return
        old_rank = _BASIS_RANK.get(str(existing.get("basis") or "").strip() or _basis_of(existing), 0)
        new_rank = _BASIS_RANK.get(str(entry.get("basis") or "").strip() or _basis_of(entry), 0)
        # 只有「新的一条更硬」或「同档且来自 1a」才覆盖。
        if new_rank >= old_rank:
            merged[attribute] = dict(entry)

    # 先放 1c，再放 1a（1a 同档优先，且更硬时覆盖）。
    for entry in fields:
        _put(entry, prefer=False)
    from_facts_only = 0
    overrode = 0
    for entry in facts:
        attribute = str(entry.get("attribute") or "").strip()
        before = merged.get(attribute)
        _put(entry, prefer=True)
        if before is None:
            from_facts_only += 1
        elif merged.get(attribute) is not before:
            overrode += 1

    payload = [merged[k] for k in order]

    # v3.1 改造二：``subject`` 以 1a 为准。
    # 1c 的输出示例默认给每条结论写 ``subject=customer``，而 1a 若已判定该字段属于
    # 销售顾问（``subject=salesperson``），那份判断更硬——1a 是「只看原文里有什么」的
    # 事实层，1c 是带着推断目标的分析层。故用 1a 的 ``subject`` 覆盖同字段 1c 的。
    #
    # 1a **没有**该字段时保留 1c 的判断，不置 ``unknown``：置空会让 1c 单独检出的身份
    # 字段被归属过滤整批拦下（实测 7 案 38 个字段，占 Notes 的 16.2%），把 v3 修好的
    # 覆盖又打回去。1c 误报「顾问信息属于客户」由投影层的值形态门兜底。
    subject_overrides = 0
    for entry in facts:
        attribute = str(entry.get("attribute") or "").strip()
        target = merged.get(attribute)
        if not attribute or target is None:
            continue
        declared = str(entry.get("subject") or "").strip()
        if declared and str(target.get("subject") or "").strip() != declared:
            target["subject"] = declared
            subject_overrides += 1

    return {
        "claims_json": json.dumps({"fields": payload}, ensure_ascii=False, default=str),
        "merged_count": len(payload),
        "facts_count": len(facts),
        "fields_count": len(fields),
        "from_facts_only": from_facts_only,
        "overrode": overrode,
        "subject_overrides": subject_overrides,
    }


MIN_SHORT_ANCHOR = 4
"""短锚可用性的**下限**：短于这个长度即使唯一命中也不作数。
本常量是**代码侧能接受的最短引文**——再短就没有证据价值了（「好」「幺三九」
这种切片指不回任何有内容的原话）。R1 实测的短锚绝大多数落在 4–7 字，都在此之上。
"""


GREETING_ANCHORS: frozenset[str] = frozenset(
    {"喂", "你好", "您好", "喂喂", "喂你好", "你好你好", "喂您好", "在吗", "喂你好啊"}
)
"""**寒暄锚点**：去掉标点后只剩这些词的锚点零信息量，不作证据（v3.1 改造三）。

实测命中：``13530559591`` 的 ``title`` 证据为「喂，你好」。
"""

JSON_FRAGMENT_PATTERN = re.compile(r'"?[A-Za-z_][A-Za-z0-9_]{1,}"\s*:\s*"?')
"""**JSON 残片**：形如 ``gender": "男性`` / ``wechat_id": "13435844484`` 的 OCR 片段。

它们是结构化字段被截断后漏进原文的痕迹，本身不构成一句人话，也不该当证据
（实测命中：``13435844484`` 的 ``title`` / ``phone_number`` 证据）。
"""

PUNCTUATION = re.compile(r"[\s，。！？、,.!?~…：:；;\-—－_　]+")


def _is_noisy_anchor(quote: str) -> bool:
    """锚点切片是否**零信息量**（寒暄语 / JSON 残片）。

    v3 的 ``_locate`` 只判「能不能定位」，于是「喂，你好」这种到处都在的寒暄语被判为
    合格锚点。本函数补上「有没有信息」这一判——限定在**短锚**上判（长引文里夹一句
    寒暄是无害的，整条引文仍有信息）。
    """
    text = str(quote or "").strip()
    if not text:
        return True
    compact = PUNCTUATION.sub("", text)
    if compact in GREETING_ANCHORS:
        return True
    if len(text) <= 60 and JSON_FRAGMENT_PATTERN.search(text):
        return True
    return False


def _occurrences(content: str, anchor: str) -> int:
    """数 ``anchor`` 在 ``content`` 里出现几次（与 :func:`_locate` 同口径）。

    逐字匹配优先；一次都没命中时按**忽略空白**、再按**忽略 Markdown 标记**各数一次——
    与 ``_locate`` 的回落一致，否则会出现「``_locate`` 找得到、这里数不到」的错位，
    短锚会被白白丢掉。
    """
    if not content or not anchor:
        return 0
    count = content.count(anchor)
    if count:
        return count
    compact_content = "".join(c for c in content if not c.isspace())
    compact_anchor = "".join(c for c in anchor if not c.isspace())
    if not compact_anchor:
        return 0
    count = compact_content.count(compact_anchor)
    if count:
        return count
    return "".join(c for c, _ in _strip_markdown(content)).count(
        "".join(c for c, _ in _strip_markdown(anchor))
    )


MARKDOWN_MARKERS = frozenset("*_")
"""定位容错要忽略的 Markdown 标记。"""


def _strip_markdown(text: str) -> list[tuple[str, int]]:
    """去掉 Markdown 强调标记，返回 ``[(字符, 原串下标)]`` 以便映射回原文。"""
    pairs: list[tuple[str, int]] = []
    for index, char in enumerate(text):
        if char in MARKDOWN_MARKERS:
            continue
        pairs.append((char, index))
    return pairs


def _locate(content: str, anchor: str) -> tuple[int, int, str] | None:
    """在 ``content`` 里定位 ``anchor``，返回 ``(起, 止, 原文切片)``。

    先按逐字匹配；失败再按**忽略空白**匹配一次——ASR 转写里同一句话的断字与空格
    常与模型抄写有出入，只认逐字会误杀本来正确的断言。

    v3.4 增加第三档：**忽略 Markdown 标记**（``**国标行业**`` 与 ``国标行业``）。
    实测单这一档救回 7 条（「行业」「企业名称」等结构化登记字段的锚点都由渲染层
    加了加粗）。切片仍按原文下标还原，marker 一并留在引文里，销售可原样核对。

    **坐标一律由这里算出，模型不誊抄整段引文**——这正是「锚点可回溯率 100%」的含义：
    留下的引文全都是原文的切片，不可能出现模型自造的句子。
    """
    if not content or not anchor:
        return None
    index = content.find(anchor)
    if index >= 0:
        return index, index + len(anchor), anchor

    compact_flat = "".join(c for c in content if not c.isspace())
    compact_anchor = "".join(c for c in anchor if not c.isspace())
    if compact_anchor:
        found = compact_flat.find(compact_anchor)
        if found >= 0:
            positions = [i for i, c in enumerate(content) if not c.isspace()]
            start = positions[found]
            end = positions[found + len(compact_anchor) - 1] + 1
            return start, end, content[start:end]

    flat_chars: list[str] = []
    flat_index: list[int] = []
    for char, original in _strip_markdown(content):
        if char.isspace():
            continue
        flat_chars.append(char)
        flat_index.append(original)
    flat_anchor = "".join(c for c, _ in _strip_markdown(anchor) if not c.isspace())
    if not flat_anchor:
        return None
    found = "".join(flat_chars).find(flat_anchor)
    if found < 0:
        return None
    start = flat_index[found]
    end = flat_index[found + len(flat_anchor) - 1] + 1
    return start, end, content[start:end]


def enum_key_guide() -> str:
    """把契约里的**合法枚举键清单**渲染成给 L1 看的附录。

    **为什么要有这一段**：R0 实测（`docs/business_opt01/R0结果.md` §5.1）暴露，L1 原提示词
    要求「照实写原始表述、归一化交给下游」，但 L2 的 ``normalize_enum`` 只能**精确匹配**
    合法键或展示名，客户口语（「担心电池衰减」）一概归不了，断言整条被丢弃。基线能做对，
    是因为 production 有 ``GET /config/note-attributes`` 把这份清单渲染进了提示词输入——
    实验臂此前缺的正是这一段。

    **清单由契约快照生成，不手抄**：字段一改附录跟着改，不会与
    :mod:`customer_profile.notes_contract` 漂移。**编外字段一并渲染**——L1 照这份清单取证，
    只列契约字段等于把编外字段的取证任务删掉，``purchase_decisions`` 与 ``deal_battle``
    随即失去料；但编外字段在后面加一句「不回传」，免得日后有人以为它们该出现在 Notes 里。
    """
    lines = ["## 可选字段的合法取值（枚举字段必须用英文键）", ""]
    for key in inf.EXTRACTION_KEYS:
        options = inf.enum_options(key)
        if not options:
            continue
        plural = "多选，多个值用 || 连接" if options.get("multiple_value") else "单选"
        parts: list[str] = []
        for enum_key, value in (options.get("items") or {}).items():
            if isinstance(value, dict):
                display = value.get("display_name") or enum_key
                extra = value.get("input_value")
                parts.append(
                    f"{enum_key}={display}（**需随带填{extra}**）" if extra else f"{enum_key}={display}"
                )
            else:
                parts.append(f"{enum_key}={value}")
        suffix = (
            "　【内部字段，不回传】" if inf.is_internal_only(key)
            else "　【画像卡字段，不写入 Notes】" if inf.is_card_only(key)
            else ""
        )
        lines.append(f"- `{key}`（{plural}）：" + " / ".join(parts) + suffix)
    return "\n".join(lines)


def field_inference_guide() -> str:
    """渲染《目标字段清单》：全部字段的键、名称、类型与**契约原文口径**。

    **为什么要有这一段**：v2 的 L1 要求「默认覆盖全部字段」，但原先的输入里只有
    《合法取值清单》（枚举键），没有《字段清单》——模型不知道一共有哪些字段、每个字段
    要什么、口径是什么，只能靠提示词正文里的几个例子猜。把契约自身的 ``description``
    渲染进去，模型看到的口径与 :mod:`customer_profile.notes_contract` **逐字一致**，
    也不会有第二处手抄的说明日后漂移。

    与 :func:`enum_key_guide` 同法：**由契约快照生成，不手抄**。编外字段在此追加一节，
    口径同样逐字取自登记表（原先的契约原文），并在每行标出「不回传」。
    """
    # 取证方向的正文已随 SPEC 规则迁入 ``rules/texts/spec.txt``。这里按字段取，
    # 不再持有第二份：常量与正文各写一份，正是「改一处漏一处」的老问题。
    notes = rules.render.field_notes(stage="extract")
    lines = ["## 目标字段清单（默认全部覆盖，确实无关的可留空）", ""]
    current: str | None = None
    for entry in nc.RAW_CONFIGS:
        category = entry.get("category") or ""
        if category != current:
            current = category
            lines.append(f"### {category}")
        key = entry["attribute_key"]
        name = entry.get("attribute_name") or ""
        field_type = entry.get("field_type") or "text"
        description = (entry.get("description") or "").strip()
        lines.append(f"- `{key}`（{name}，{field_type}）：{description}")
        note = notes.get(key)
        if note:
            lines.append(f"  - 取证范围：{note}")

    # ---- 画像卡字段：不在 Notes 契约里，但产出回传体的分析区块
    lines.append("")
    lines.append("### 画像卡字段（照常取证，**不写入 Notes**，直接决定画像卡的意向等级）")
    for key in inf.CARD_ONLY_KEYS:
        name = inf.name_of(key)
        field_type = inf.field_type_of(key)
        description = inf.description_of(key)
        lines.append(f"- `{key}`（{name}，{field_type}）：{description}")
        note = notes.get(key)
        if note:
            lines.append(f"  - 取证范围：{note}")

    # ---- 编外字段：照样取证，但不回传（契约里已经没有它们，不列出来模型就不会去找）
    lines.append("")
    lines.append("### 内部字段（照常取证，但**不写入 Notes**，供销售策略与画像卡使用）")
    for key in inf.EXTRACTION_ONLY_KEYS:
        name = inf.name_of(key)
        field_type = inf.field_type_of(key)
        description = inf.description_of(key)
        lines.append(f"- `{key}`（{name}，{field_type}）：{description}")
        note = notes.get(key)
        if note:
            lines.append(f"  - 取证范围：{note}")
    return "\n".join(lines)


def l1_guide() -> str:
    """L1 输入的前置清单：先《字段清单》，再《合法取值清单》。"""
    return field_inference_guide() + "\n\n" + enum_key_guide()


def build_l1_input(records_json) -> dict:
    """L1 取证层的输入装配：把有内容的渠道排成分节文本。

    与 ``build_evidence_input`` 的分工：那个交给既有「证据线索整理」提示词（A-E 五级
    证据口径），本函数交给 L1 断言账本提示词，因此要多带两样东西——

    - **来源分级标注**：L1 要据此判 ``claim_type`` 与 ``confidence``，而分级由**代码按渠道
      定**，不由模型猜（``端到端方案.md`` §3.2 硬规则 1）；
    - **记录编号**：供代码回定位原文，也是断言与出处的可追溯凭据；
    - **《目标字段清单》**：契约全部字段的键、口径与取证方向，见 :func:`field_inference_guide`。

    排序按来源档**从高到低**（人工/系统登记 → 一手交互 → 公开登记查询 → 社交自呈现 →
    第三方预测）：长上下文里靠后的内容召回更低，把登记事实与录音原文前置。
    """
    records = _records_of(records_json)
    usable = [r for r in records if r.get("status") == OK and (r.get("content") or "").strip()]
    # 按**来源档的优先级序号**排序，不能按档名排序：「firsthand」按字母序排在
    # 「registry」之前，用档名当键会把一手交互顶到人工登记前面。
    usable.sort(
        key=lambda r: proj.SOURCE_TIERS.index(
            proj.CHANNEL_TIER.get(r.get("source_name") or "", proj.SOCIAL)
        )
    )

    sections: list[str] = []
    for record in usable:
        source_name = record.get("source_name") or record.get("channel") or "未知渠道"
        tier = proj.CHANNEL_TIER.get(source_name, proj.SOCIAL)
        header = [
            f"## {source_name}",
            f"来源分级：{proj.TIER_LABEL[tier]}",
            f"记录编号：{record.get('record_id') or ''}",
        ]
        created_at = str(record.get("created_at") or "").strip()
        if created_at:
            header.append(f"采集时间：{created_at}")
        header.append(f"内容形态：{record.get('content_kind') or 'text'}")
        # 录音的说话方分区：**放在分节头里、不插进正文**。插进正文会让 L1 抄锚点时
        # 连段头一起抄，而锚点必须能在正文里逐字定位。
        segments = _role_segments(record.get("role_lines") or [])
        if segments:
            header.append(f"说话方分区：{segments}")
        notes = _correction_footnote(record.get("corrections") or [])
        if notes:
            header.append(f"已勘误：{notes}")
        sections.append("\n".join(header) + "\n\n" + str(record.get("content")))

    body = "\n\n---\n\n".join(sections)
    # 枚举键清单置于**开头**：R1 实测把它放在末尾时，尾部这一串短 token（`Mr=先生 /
    # Ms=女士`）会诱导模型把 anchor 也写成同样短的裸串（R0 中位锚长 16 → R1 的 6），
    # 导致大量正确断言被长度门丢弃。改置开头，让**记录原文仍是最后读到的内容**。
    guide = l1_guide()
    output = f"{guide}\n\n---\n\n{body}" if body else guide
    return {
        "output": output,
        "section_count": len(sections),
        "chars": len(output),
        "guide_chars": len(guide),
    }


def localize_claims(records_json, claims_json, min_anchor: int = 8) -> dict:
    """代码定坐标：把 L1 的 ``anchor`` 回定位到原文。**定位不到只降级，不丢字段。**

    「引文可回溯」由这一层强制：模型只指认「原文里哪一段」，切出来的是原文本身；
    定位成功时切出的 ``quote`` 必然是原文的连续切片，模型不可能自造引文。

    **v2 的关键改动：定位失败的处置由「整条丢弃」改为「降级保留」。**
    v1 口径是召回的最大漏斗（R1 实测因长度门丢掉 226 条断言，其中 163 条在原文里
    **唯一可定位**）；v2 的口径是——事实指不出原文，不等于这个字段没有结论，只是它
    **不够硬**：

    - 渠道对不上记录层 → 保留，清空 ``record_channel``（来源档随之落到最弱一档），
      ``basis`` 降为 ``inferred_low``；
    - ``anchor`` 短于 ``min_anchor`` 且在原文里出现多次（定位有歧义）→ 降级；
    - ``anchor`` 在原文里找不到（抄错字）→ 降级；
    - ``basis`` 本就是 ``inferred_*`` 却不给 ``basis_of_inference`` → 降为 ``inferred_low``
      （推断没有任何依据的，只能算最低一档）。

    **降级到 ``inferred_low`` 而不是 ``inferred_high``，是刻意的保守取值。**
    「≥2 条独立来源同向」要看**全局**（同一字段有多条断言），单条断言在定坐标这一层
    看不到全局；升格交给 L2 的兜底裁决（弱证据同向汇聚）——那才是看得见全局的地方。

    降级与丢弃都留痕（``dropped_detail`` / ``downgraded_detail``）：静默处理会让人误以为
    模型没提取到，而实际是**提取到了但不够硬**，两者的处置完全不同。
    """
    records = _records_of(records_json)
    claims, parse_ok = _claims_payload(claims_json)

    by_name: dict[str, str] = {}
    by_id: dict[str, str] = {}
    roles_by_name: dict[str, list] = {}
    roles_by_id: dict[str, list] = {}
    for record in records:
        content = record.get("content") or ""
        if record.get("status") != OK or not content.strip():
            continue
        by_name.setdefault(str(record.get("source_name") or ""), content)
        by_id.setdefault(str(record.get("record_id") or ""), content)
        role_lines = record.get("role_lines") or []
        if isinstance(role_lines, list) and role_lines:
            roles_by_name.setdefault(str(record.get("source_name") or ""), role_lines)
            roles_by_id.setdefault(str(record.get("record_id") or ""), role_lines)

    kept: list[dict] = []
    dropped: list[dict] = []
    downgraded: list[dict] = []
    for raw in claims:
        entry = dict(raw)
        channel = str(raw.get("record_channel") or "").strip()
        content = by_name.get(channel) or by_id.get(channel)
        if content is None and channel:
            content = by_name.get(proj._match_channel(channel))
        basis_now = str(raw.get("basis") or "").strip() or _basis_of(raw)

        def _downgrade(reason: str, *, clear_channel: bool = False) -> None:
            """把 ``basis`` 降为 ``inferred_low`` 并留痕，**字段本身保留**。"""
            entry["basis"] = proj.INFERRED_LOW
            if clear_channel:
                entry["record_channel"] = ""
            downgraded.append(
                {
                    "claim_id": raw.get("claim_id"),
                    "attribute": raw.get("attribute"),
                    "reason": reason,
                    "anchor": str(raw.get("anchor") or "").strip(),
                }
            )

        if content is None:
            # 两种「取不到内容」要分开处置（v2.1 修复）：
            # - **给了渠道名却找不到记录** → 渠道对不上，降级并清空渠道（v2 行为保留）；
            # - **压根没给渠道** → 推断类本就允许留空（L1 模板明说），不降级、不动档位。
            #   v2 把后者也当成「渠道对不上」，把 inferred_high 一并压成 inferred_low
            #   （8 案实测 209 条，占全部降级的 92%），是三分类在字段级塌陷的根因。
            if channel or basis_now == proj.FACT:
                _downgrade("unknown_channel", clear_channel=True)
        else:
            anchor = str(raw.get("anchor") or "").strip()
            located = _locate(content, anchor)
            # 短锚不一律丢，改为**看唯一性**：长度从来只是「能不能唯一定位」的代理指标，
            # R1 实测 226 条短锚里 189 条逐字存在于原文、其中 163 条**只出现一次**——
            # 也就是说它们在原文里本来就唯一可定位，长度门杀的是正确断言。
            ambiguous = len(anchor) < min_anchor and (
                len(anchor) < MIN_SHORT_ANCHOR or _occurrences(content, anchor) != 1
            )
            if located is None:
                # v3.4：锚点抄不出连续原文时，用**字段取值**在**同一渠道**原文里回查。
                # 只查同渠道——跨渠道会把别的渠道里恰好同值的文字误当成证据。
                # 回查命中只补证据位，置信档仍按原规则降级，不因此升级。
                value_text = str(raw.get("value") or "").strip()
                signal_text = str(raw.get("value_signal") or "").strip()
                for probe in (signal_text, value_text):
                    if not probe:
                        continue
                    hit = _locate(content, probe)
                    if hit is not None:
                        entry["quote"] = hit[2]
                        entry["span"] = [hit[0], hit[1]]
                        break
                _downgrade("anchor_not_found")
            elif ambiguous:
                _downgrade("anchor_too_short")
            else:
                start, end, quote = located
                entry["quote"] = quote
                entry["span"] = [start, end]
                entry["record_channel"] = channel
                # 录音的角色标注：引文起点落在哪一句，就记那一句的说话方。
                # 投影层据此在证据位加「销售原话 / 客户原话 / 说话者身份存疑」前缀。
                role_lines = roles_by_name.get(channel) or roles_by_id.get(channel) or []
                role = _role_at(role_lines, start)
                if role:
                    entry["speaker_role"] = role
                # v3.1 改造三：**锚点相关性门**。能定位 ≠ 有信息。
                #   ① 噪音锚点：问候语（「喂，你好」）、OCR 的 JSON 残片（`gender": "男性`）
                #      —— 它们在原文里逐字存在且唯一，但零信息量；
                #   ② 取值信号缺失：L1 声明了 `value_signal` 却不在锚点里，说明锚点跟
                #      取值不相关。
                # 两种情形都**收回引文**（证据位留白胜过硬凑），并降为 inferred_low。
                signal = str(raw.get("value_signal") or "").strip()
                if signal:
                    entry["value_signal"] = signal
                noisy = _is_noisy_anchor(quote)
                mismatch = bool(signal) and signal not in quote
                if noisy or mismatch:
                    entry.pop("quote", None)
                    entry.pop("span", None)
                    entry["anchor_noisy"] = True
                    _downgrade("noisy_anchor" if noisy else "signal_mismatch")
                # v3 接口一：坐标形态。L1 若声明为当归型（ASR 纠错 / 归一），
                # 值不是原文给的——此时必须留住**改动前的原文**（``source_quote``），
                # 否则销售分不清「客户说的」与「模型改的」。
                kind = str(raw.get("anchor_kind") or "").strip()
                source_quote = str(raw.get("source_quote") or "").strip()
                if kind == proj.ANCHOR_NORMALIZED or source_quote:
                    entry["anchor_kind"] = proj.ANCHOR_NORMALIZED
                    entry["source_quote"] = source_quote or quote
                else:
                    entry["anchor_kind"] = proj.ANCHOR_VERBATIM

        # 推断类没有依据 → 只能算最低一档（「有据推断」的门槛在 L2，这里先如实降级）。
        basis = str(entry.get("basis") or "").strip() or _basis_of(raw)
        entry["basis"] = basis
        if basis in (proj.INFERRED_HIGH, proj.INFERRED_LOW):
            hints = raw.get("basis_of_inference") or ()
            if isinstance(hints, str):
                hints = [hints] if hints.strip() else []
            hints = [str(h) for h in hints if str(h).strip()]
            if not hints:
                _downgrade("inferred_without_basis")
                entry["basis_of_inference"] = []
            else:
                entry["basis_of_inference"] = hints
        kept.append(entry)

    return {
        "claims_json": json.dumps({"fields": kept}, ensure_ascii=False, default=str),
        "kept": len(kept),
        "dropped": len(dropped),
        "dropped_detail": json.dumps(dropped, ensure_ascii=False, default=str),
        "downgraded": len(downgraded),
        "downgraded_detail": json.dumps(downgraded, ensure_ascii=False, default=str),
        "parse_ok": parse_ok,
    }


INJECTED_PHONE_CHANNEL = proj.INJECTED_PHONE_CHANNEL
"""``phone_number`` 的注入来源名（登记档，与投影层共用同一常量）。"""

INJECTED_LICENSE_CHANNEL = proj.INJECTED_LICENSE_CHANNEL
"""``driver_license_status`` 的注入来源名（登记档）。"""

TEST_DRIVE_SOURCE_NAME = "试驾录音信息"
"""判定「有没有试驾」看的渠道：到店试驾本身即驾照的充分证据（没驾照不能试驾）。"""

OLD_CAR_ATTRIBUTES: frozenset[str] = frozenset(
    {"old_car_model", "owned_brand_model", "owned_car_age"}
)
"""「有旧车」的旁证字段：任一条有值即说明客户在开（过）自己的车。"""

TRADE_IN_MARKERS: tuple[str, ...] = ("置换", "有旧车")


def _has_test_drive(records: list[dict]) -> bool:
    """记录层里是否存在**有实质内容**的试驾录音。"""
    for record in records:
        if str(record.get("source_name") or "") != TEST_DRIVE_SOURCE_NAME:
            continue
        if record.get("status") != OK:
            continue
        if (record.get("content") or "").strip():
            return True
    return False


def _has_old_car(claims: list[dict]) -> bool:
    """断言里是否存在「有旧车 / 要置换」的旁证（与 :func:`_projection_context` 同口径）。"""
    for claim in claims:
        attribute = str(claim.get("attribute") or "")
        if attribute in OLD_CAR_ATTRIBUTES and not _blank_value(claim.get("value")):
            return True
        if attribute != "trade_in_requirement":
            continue
        text = json.dumps(claim.get("value"), ensure_ascii=False, default=str).lower()
        if "yes" in text or any(marker in text for marker in TRADE_IN_MARKERS):
            return True
    return False


def inject_deterministic(records_json, claims_json, phone_number: str = "") -> dict:
    """S1 · 确定性字段代码注入（v3.1 改造四，纯代码 0 token）。

    两个字段有**定义级**的确定取值，不该交给模型归纳：

    - ``phone_number``：真值就是本案入参手机号，无需从截图（``wechat_id``）反捞再降级；
    - ``driver_license_status``：**到店试驾**或**有旧车 / 要置换**本身即「有驾照」的
      充分证据（没驾照不能试驾、不能开自家车）。

    注入值一律 ``basis=fact`` + 登记档来源（见 :data:`INJECTED_PHONE_CHANNEL`），
    不受模型抖动影响。同字段若已有断言，按 ``basis`` 取更硬的一条，**同档以注入为准**
    （注入是定义级蕴含，不是猜测）。

    **为什么接在 ``localize`` 之后而不是之前**：本函数的来源名在记录层没有对应记录，
    若接在 :func:`localize_claims` 之前，定位层会按「给了渠道却找不到记录」把它降为
    ``inferred_low`` 并清空渠道——正好废掉这里刻意标注的登记档。故注入点是
    ``L1_LOCALIZE → L1_INJECT → L2_PROJECT``（见设计稿 §5 施工回填）。
    """
    records = _records_of(records_json)
    claims, parse_ok = _claims_payload(claims_json)

    injected: list[dict] = []
    phone = str(phone_number or "").strip()
    if phone:
        injected.append(
            {
                "attribute": "phone_number",
                "value": phone,
                "basis": proj.FACT,
                "subject": "customer",
                "record_channel": INJECTED_PHONE_CHANNEL,
                "confidence": 0.95,
            }
        )

    has_drive = _has_test_drive(records)
    has_old_car = _has_old_car(claims)
    if has_drive or has_old_car:
        injected.append(
            {
                "attribute": "driver_license_status",
                # 结构化枚举：契约要求 ``yes`` 随带「驾龄年限」。仅凭试驾/旧车推不出驾龄，
                # 故 ``input_value`` 如实留空（写死一个年限就是编造）。
                "value": {"key": "yes", "input_value": ""},
                "basis": proj.FACT,
                "subject": "customer",
                "record_channel": INJECTED_LICENSE_CHANNEL,
                "basis_of_inference": ["到店试驾记录" if has_drive else "有旧车可用于置换"],
                "confidence": 0.95,
            }
        )

    by_attr: dict[str, dict] = {}
    order: list[str] = []
    for entry in claims:
        attribute = str(entry.get("attribute") or "").strip()
        if not attribute:
            continue
        by_attr[attribute] = entry
        order.append(attribute)

    added = replaced = 0
    for entry in injected:
        attribute = entry["attribute"]
        existing = by_attr.get(attribute)
        if existing is None:
            by_attr[attribute] = entry
            order.append(attribute)
            added += 1
            continue
        old_rank = _BASIS_RANK.get(
            str(existing.get("basis") or "").strip() or _basis_of(existing), 0
        )
        if _BASIS_RANK.get(entry["basis"], 0) >= old_rank:
            by_attr[attribute] = entry
            replaced += 1

    payload = [by_attr[key] for key in order]
    return {
        "claims_json": json.dumps({"fields": payload}, ensure_ascii=False, default=str),
        "added": added,
        "replaced": replaced,
        "injected": len(injected),
        "license_injected": has_drive or has_old_car,
        "parse_ok": parse_ok,
    }


def _basis_of(raw: dict) -> str:
    """从 v1 断言档位推出 v2 的 ``basis``（离线回放既有批次产物时用）。

    ``fact`` → ``fact``（v1 的 fact 本来就要求锚点）；``observation`` → ``inferred_high``
    （「多条记录反复出现、可归纳」本身就是多条同向）；``prediction`` → ``inferred_high``
    （v3.2：第三方画像标签是采购来的商用画像数据，单独支撑某字段时按可信的高置信推断
    处理——此前记 ``inferred_low``，使这类字段在字段级塌陷、最终留白）。

    **来源档优先于自述档位**：渠道本身就在「第三方预测」档的，不看它自称 ``claim_type``
    是什么——来源档是代码按渠道定的，比模型自述更硬。第三方预测永不冒充 ``fact``，
    但也不再被压到最低档。
    """
    channel = str(raw.get("record_channel") or "").strip()
    if proj.CHANNEL_TIER.get(channel, proj.SOCIAL) == PREDICTION:
        return proj.INFERRED_HIGH
    claim_type = str(raw.get("claim_type") or "").strip()
    if claim_type == "prediction":
        return proj.INFERRED_HIGH
    if claim_type == "observation":
        return proj.INFERRED_HIGH
    return proj.FACT


def _projection_context(claims) -> dict:
    """从断言账本读投影所需的旁证（原文级事实，不从字段值反推）。"""
    from customer_profile.rules.clusters import consistency as consistency_rules

    deregistered = False
    old_car = False
    for claim in claims:
        text = json.dumps(claim.value, ensure_ascii=False, default=str)
        if claim.attribute in {"company", "company_type"} and any(
            marker in text for marker in ("注销", "吊销")
        ):
            deregistered = True
        if claim.attribute in {"old_car_model", "owned_brand_model", "owned_car_age"}:
            # **「无」不是旧车证据**：客户原话「目前无车」「没有旧车」时 L1 仍可能给出
            # ``old_car_model="无"``。只看「属性出现过」会把首购客户改写成换购，
            # 卡片上凭空多出一条「购车性质：换购」。占位取值一律不算凭据。
            #
            # **下单词也不是旧车证据**（CONSIST-04 同口径）：含小订/大定的拥车断言
            # 在 a2 段被整条拒收，但它若先在这里立了「有旧车」，CONSIST-01 仍会把
            # 购车性质改写成换购——旁证必须与断言过滤看同一件事。
            if _is_placeholder_value(claim.value):
                continue
            if any(phrase in text for phrase in consistency_rules.DEPOSIT_PHRASES):
                continue
            old_car = True
        if claim.attribute == "trade_in_requirement" and ("有旧车" in text or "置换" in text):
            old_car = True
    return {"company_deregistered": deregistered, "has_old_car_evidence": old_car}


def _thin_keeps(channels: list[str], basis: str) -> bool:
    """``thin`` 档保留判据（P2 口径）：按**来源档位 + 字段置信档**判，不读证据文本。

    保留三种字段：

    1. **有登记档渠道支撑**——不论置信档。登记档是销售本人确认或系统留痕，取值可回溯；
    2. **第三方画像数据高/事实档**——v3.2 起采购的商用画像在无一手记录时是唯一可靠
       信源（见 :attr:`proj.PREDICTION`），该档本就不该被 thin 排除；
    3. **无渠道但非低置信**——``record_channel`` 为空但 L1 自评 fact/inferred_high。

    其余（仅社交自呈现、仅公开登记查询、低置信推断）一律不留：thin 案没有一手记录
    可供核对，宁可少写。``inferred_low`` 是「已降级、待人工确认」的档，不满足上述任一
    条时不留。

    **旧实现的缺陷**（2026-10-28 修复）：原判据在 ``reasoning_detail.evidence`` 里找
    「人工/系统登记」标签，但证据位自 2026-10-19 可读性整改起只写「渠道：原文」，
    全量 87 案 1234 条 Notes 里命中数为 0——thin 档被整批清空，只剩单独追加的姓名。
    """
    if proj.REGISTRY in {proj.CHANNEL_TIER.get(ch) for ch in channels if ch}:
        return True
    if basis == proj.INFERRED_LOW:
        return False
    if not any(channels):
        return True
    return any(proj.CHANNEL_TIER.get(ch) == proj.PREDICTION for ch in channels if ch)


def _thin_keep_candidates(candidates: list[dict]) -> list[dict]:
    """``thin`` 档的候选集收窄：与 :func:`_thin_keeps` 同口径，按选项的来源档与置信档判。"""
    kept: list[dict] = []
    for entry in candidates:
        options = entry.get("options") or []
        if any(
            _thin_keeps(
                [str(option.get("channel") or "").strip()],
                str(option.get("basis") or proj.FACT),
            )
            for option in options
        ):
            kept.append(entry)
    return kept


def _thin_keep_updates(
    updates: list[dict], channels_by_attr: dict[str, list[str]], basis_by_attr: dict[str, str]
) -> list[dict]:
    """``thin`` 档的 Notes 收窄：只留 :func:`_thin_keeps` 判为可留的字段。"""
    kept: list[dict] = []
    for entry in updates:
        key = str(entry.get("attribute_key") or "")
        if _thin_keeps(
            channels_by_attr.get(key) or [],
            basis_by_attr.get(key) or proj.FACT,
        ):
            kept.append(entry)
    return kept


def _blank_value(value) -> bool:
    """判断一个取值是不是「空值占位」。

    L1 提示词明令「``value`` 不得为空，要么给取值、要么整条不输出」；本函数是代码侧的
    对账——把提示词的承诺变成**可数的 KPI**（v2.1 设计稿 §3 R2），而不是让一条
    ``value=""`` 的占位结论白占一行，还把「L1 漏了这个字段」伪装成「这个字段确实没内容」。
    """
    if value is None:
        return True
    if isinstance(value, str):
        # 「[]」「{}」「null」是模型把「空」写成字符串的常见形态；它们同样是占位，
        # 不是取值——按字面量一起认掉，否则它们会伪装成一条有效结论混进账本。
        return not value.strip() or value.strip() in ("[]", "{}", "null", "None")
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    return False


def _disabled_rule_ids() -> tuple[str, ...]:
    """``RULES_DISABLED`` 的运行时取值（单条规则灰度 / 回滚）。

    代码节点保持纯函数便于离线回放与单测，因此这里**惰性**读配置：测试与回放不构造
    ``Settings`` 也不受影响。读取失败按「没有关闭任何规则」处理——灰度开关写错不该
    让整条管线停摆。投影段与 gate 段共用这一处读取，两段才关得掉同一条规则。
    """
    try:
        from ..settings import get_settings

        return get_settings().disabled_rule_ids
    except Exception:
        return ()


def project_claims(claims_json, tier: str = "sufficient") -> dict:
    """L2 投影层（纯代码，0 token）：断言账本 → 规范事实 + Notes 更新项。

    ``tier`` 来自 L4 分诊：``sufficient`` 走事实流程；``thin`` 只输出登记档 / 第三方
    画像高置信 / 无渠道高置信三类字段（见 :func:`_thin_keeps`）；
    ``empty`` 不产出任何断言（保守模板，``端到端方案.md`` §3.6）。
    """
    claims, _ = _claims_payload(claims_json)

    def _num(value) -> float | None:
        """L1 自报置信度：解析成 0–1 的浮点；缺省或非法返回 ``None``（由投影层给默认值）。"""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, number))

    def _hints(raw: dict) -> tuple[str, ...]:
        value = raw.get("basis_of_inference") or ()
        if isinstance(value, str):
            return (value,) if value.strip() else ()
        return tuple(str(item) for item in value if str(item).strip())

    def _steps(raw: dict) -> tuple[str, ...]:
        """v3.1 改造一：L1 显式给出的**推导链**（`reasoning_chain`），一路带到 Notes 出口。"""
        value = raw.get("reasoning_chain") or ()
        if isinstance(value, str):
            return (value,) if value.strip() else ()
        return tuple(str(item) for item in value if str(item).strip())

    ledger: list[proj.Claim] = []
    empty_claim_names: list[str] = []
    for raw in claims:
        attribute = str(raw.get("attribute") or "").strip()
        if not attribute:
            continue
        if _blank_value(raw.get("value")):
            # 只丢「空值」这一条，**不**丢字段：字段名进计数，交给 R2 的覆盖 KPI 追。
            empty_claim_names.append(attribute)
            continue
        ledger.append(
            proj.Claim(
                attribute=attribute,
                value=raw.get("value"),
                channel=str(raw.get("record_channel") or "").strip(),
                # 主体为空**不**回落成 customer：规则要求「主体说不清」的断言不得支撑身份字段，
                # 默认成客户本人恰好会把该拦下的放行。
                subject=str(raw.get("subject") or "").strip(),
                claim_type=str(raw.get("claim_type") or "").strip() or "fact",
                claim_id=str(raw.get("claim_id") or ""),
                # v2 的 ``basis`` 优先；缺失时从 v1 的 ``claim_type`` 推（离线回放既有批次产物）。
                basis=str(raw.get("basis") or "").strip() or _basis_of(raw),
                basis_of_inference=_hints(raw),
                # L1 定位层切出的原文切片一路带到 Notes 出口（v2.1 可读性，设计稿 §1）。
                quote=str(raw.get("quote") or "").strip(),
                # v3 接口一：坐标形态与当归型事实的原文（透传到 Notes 证据位）。
                anchor_kind=str(raw.get("anchor_kind") or "").strip(),
                source_quote=str(raw.get("source_quote") or "").strip(),
                # 录音证据的说话方（定位层按引文所在句反查得到）。出口证据位据此加
                # 「销售原话 / 客户原话 / 说话者身份存疑」前缀；非录音渠道为空。
                speaker_role=str(raw.get("speaker_role") or "").strip(),
                # v3.1 改造一：推导链透传到 Notes 的 ``steps``（与 ``evidence`` 分料）。
                reasoning_steps=_steps(raw),
                # v3.5：L1 自报的置信度透传到出口，作为字段级打分的基准
                # （旧实现按档位给固定值，模型 0.3–0.95 的真实打分被整条丢弃）。
                confidence=_num(raw.get("confidence")),
            )
        )
    result = proj.project(
        ledger,
        context=_projection_context(ledger),
        disabled_rules=_disabled_rule_ids(),
    )

    updates = result.notes_updates
    # 姓名条目在任何档位都保留：「未知」或白名单来源的真名都必须交代给销售，
    # thin/empty 档删掉它等于让下游分不清「没查」与「没提取」。
    name_updates = [u for u in updates if u.get("attribute_key") == "customer_name"]
    if tier == "empty":
        updates = name_updates
    elif tier != "sufficient":
        channels_by_attr: dict[str, list[str]] = {}
        for item in ledger:
            channels_by_attr.setdefault(item.attribute, []).append(item.channel)
        kept = _thin_keep_updates(updates, channels_by_attr, result.basis)
        updates = kept + [u for u in name_updates if u not in kept]

    # 候选集必须与 updates **同口径**：``thin`` 档刻意收窄到登记档 / 第三方画像高置信 /
    # 无渠道高置信三类，若兜底仍按全量候选裁决，它就能把该档排除掉的字段重新写回去——
    # 那等于让兜底绕过了分诊层的保守约束。因此候选集也跟着 tier 收窄。
    # ``covered`` 把「L1 已给结论的字段」排除在补全之外，其余契约字段成为缺字段候选（R2）。
    candidates = proj.build_l2_candidates(ledger, result, covered={c.attribute for c in ledger})
    if tier == "empty":
        candidates = []
    elif tier != "sufficient":
        candidates = _thin_keep_candidates(candidates)

    return {
        "facts_json": json.dumps(result.facts, ensure_ascii=False, default=str),
        # 喂 LLM 的事实表（L2 兜底）：**必须**是可读化中文，不得 dump 英文键与枚举键
        # （模型会照抄进理由，进而漏进对客产物）。代码消费仍用 ``facts_json``。
        "facts_readable": rdb.readable_facts(result.facts),
        "notes_updates_json": json.dumps(updates, ensure_ascii=False, default=str),
        "to_confirm_json": json.dumps(result.to_confirm, ensure_ascii=False, default=str),
        "rejected_json": json.dumps(result.rejected, ensure_ascii=False, default=str),
        # 兜底节点的输入：**只有规则判不了的字段**会出现在这里（设计稿 §6.1
        # 「凡是能写成规则的，不交给 LLM」）。
        "candidates_json": json.dumps(candidates, ensure_ascii=False, default=str),
        # 字段级置信档。L3 校验据此区分「写成确定值却没有依据」（要拦）与
        # 「已明确降级为 inferred_low 的软值」（要放行）——两者在 facts 里长得一样。
        "basis_json": json.dumps(result.basis, ensure_ascii=False, default=str),
        "notes_count": len(updates),
        # R2 覆盖 KPI：L1 发了却没给出取值的字段名。空值占位不再静默通过——
        # 计出来才知道「L1 漏了多少字段」，而不是把它伪装成「字段确实没内容」。
        "empty_claims": len(empty_claim_names),
        "empty_claim_names": json.dumps(sorted(set(empty_claim_names)), ensure_ascii=False),
        "tier": tier,
        # 规则留痕（追加输出，不改既有产物形状）。M2 起由投影层的 code 通道规则写入，
        # checker 通道自 M5 起追加。下游按 ``rule_id / attribute / action`` 统计
        # 命中与动作分布（设计 §8）。
        "rule_trace_json": json.dumps(
            result.rule_trace, ensure_ascii=False, default=str
        ),
    }


def merge_l2_verdict(candidates_json, verdict_json, notes_updates_json) -> dict:
    """把 LLM 兜底的终判并入 Notes 更新项（纯代码）。

    兜底只做三件事（设计稿 §6.3）：弱证据同向汇聚、语义冲突判断、给出终判与理由。
    本函数负责把**越界的终判挡在门外**（:func:`projection.apply_l2_verdict` 判），
    再把通过的那些并进 ``notes_updates``。

    并入时**保持产物形状不变**：`basic_notes_updates` 的三键结构是下游契约，
    兜底改的只是取值与置信度，不新增键。
    """
    candidates = _claims_payload(candidates_json)[0]
    verdict = proj.apply_l2_verdict(candidates, verdict_json)
    try:
        updates = json.loads(notes_updates_json) if isinstance(notes_updates_json, str) else list(
            notes_updates_json or []
        )
    except ValueError:
        updates = []
    if not isinstance(updates, list):
        updates = []

    by_key = {str(u.get("attribute_key")): u for u in updates if isinstance(u, dict)}
    applied = 0
    for item in verdict["accepted"]:
        key = item["attribute_key"]
        # 缺字段补全恒为软值（inferred_low），置信度必须低于「候选集内裁决」，
        # 否则销售分不清「三条来源同向汇出来的」与「表里什么都没有、模型补的空」。
        # v3.5：软补全从 0.4 降到 0.25——它基本等于纯猜（用户口径：20–30%）。
        soft = bool(item.get("missing"))
        confidence = 0.25 if soft else 0.8
        reason = item.get("reason") or ""
        # v3.3：移除 reason 中的工程术语措辞（问题2修复）。
        # L2 提示词已规范，但为防止模型偶发漏出，代码再清洗一遍。
        if soft:
            lead = f"根据已有信息推断：{reason}" if reason else "据已有事实归纳（低置信，供核对）"
        else:
            lead = reason if reason else "多条来源同向，取一致结论"
        # 证据位填**理由里的事实部分**（结论从句之前的那段），而不是留空让前端整块不渲染。
        # 兜底补的字段本就没有原文锚点，这是能给到销售的最接近「依据」的内容。
        factual = proj._strip_conclusion(reason) if reason else ""
        existing = by_key.get(key)
        if existing is not None:
            existing["attribute_value"] = item["attribute_value"]
            detail = existing.setdefault("reasoning_detail", {})
            detail["confidence"] = confidence
            steps = detail.setdefault("steps", [])
            steps.append(lead)
            if factual and not detail.get("evidence"):
                detail["evidence"] = [factual]
        else:
            updates.append(
                {
                    "attribute_key": key,
                    "attribute_value": item["attribute_value"],
                    "reasoning_detail": {
                        "summary": (
                            "由已有信息归纳补充（低置信，供核对）"
                            if soft
                            else "由多条来源同向归纳得到"
                        ),
                        "steps": [lead],
                        "evidence": [factual] if factual else [],
                        "confidence": confidence,
                    },
                }
            )
        applied += 1

    return {
        "notes_updates_json": json.dumps(updates, ensure_ascii=False, default=str),
        "notes_count": len(updates),
        "applied": applied,
        "out_of_bounds": verdict["out_of_bounds"],
        "verdict_json": json.dumps(verdict["accepted"], ensure_ascii=False, default=str),
    }


def review_notes(notes_updates_json, facts_json, basis_json="{}") -> dict:
    """L2 审查核验层（纯代码，0 token）：对 L1/L2 产出做合规性复核，并**执行**判决。

    原实现只记 ``issue``——判出违规照样进最终 Notes，蓝图 §2.2 称之为「能判但不拦」。
    M3 起四条判据由 :mod:`customer_profile.rules.clusters` 产出动作，本函数交给
    统一执行器落地：

    1. **无效占位清除**（HARD-01）：整条删除。占位串看着有值、点进去没内容，等于没填。
    2. **枚举合法性**（HARD-02）：非法且未降级的，把该条目的置信度降为 0.5。
       保留取值而不是删除——归不到键的往往就是客户原话（P3 规则 d 的口径）。
    3. **无锚点 fact**（HARD-03）：``fact`` 却指不出原文、也不是登记档的，同样降置信。
       这是「编造的确定性」的出口闸门。
    4. **证据位自指**（EXPR-02）：把「推断为…」这类结论从证据位里摘掉，条目本身保留。

    **判据与 ``validate_l3`` 共用一份实现**（``rules.clusters.hard.is_unbacked_fact``
    与同一张登记档渠道表）：此前两处各写一份，改一处漏一处。
    """
    try:
        updates = json.loads(notes_updates_json) if isinstance(notes_updates_json, str) else list(
            notes_updates_json or []
        )
    except ValueError:
        updates = []
    if not isinstance(updates, list):
        updates = []
    updates = [entry for entry in updates if isinstance(entry, dict)]

    facts = _loads_object(facts_json)
    basis = _loads_object(basis_json)
    table = rules.default()
    disabled = _disabled_rule_ids()
    if disabled:
        table = table.without(disabled)
    state = rules.NotesState(
        notes=[dict(entry) for entry in updates],
        facts=facts,
        basis=dict(basis),
    )

    gate_actions = hard_rules.check_notes(
        state.notes,
        basis=state.basis,
        is_conclusion=proj._is_conclusion,
        enum_known=inf.is_enum,
        legal_keys=inf.legal_keys,
        display_to_key=inf.display_to_key,
        is_enum_key=inf.looks_like_enum_key,
    ) + expression_rules.check_evidence(
        state.notes, is_conclusion=proj._is_conclusion
    ) + expression_rules.check_prose_keys(
        state.notes, is_placeholder=inf.is_english_placeholder
    ) + expression_rules.check_placeholders(
        state.notes, is_placeholder=inf.is_english_placeholder
    )
    gate_actions = [item for item in gate_actions if item.rule in table]
    applied = rules.apply(state, gate_actions, max_rounds=1, registry=table)

    # ---- 留痕：仍按旧口径出四条 issue 明细（跨批次对账要靠它），
    #      另把逐条动作写进 rule_trace（带规则编号与版本）。
    by_rule: dict[str, list[dict]] = {}
    for trace in applied.traces:
        if not trace.reason.startswith("[unstable]"):
            by_rule.setdefault(trace.rule_id, []).append(trace.asdict())

    def _detail(rule_id: str) -> list[dict]:
        return (by_rule.get(rule_id) or [])[:10]

    issues: list[dict] = []
    if by_rule.get("HARD-01"):
        issues.append(
            {
                "rule": "placeholder_removed",
                "count": len(by_rule["HARD-01"]),
                "detail": [item["attribute"] for item in _detail("HARD-01")],
            }
        )
    if by_rule.get("HARD-02"):
        issues.append(
            {
                "rule": "enum_illegal",
                "count": len(by_rule["HARD-02"]),
                "detail": _detail("HARD-02"),
            }
        )
    if by_rule.get("HARD-03"):
        issues.append(
            {
                "rule": "unbacked_fact",
                "count": len(by_rule["HARD-03"]),
                "detail": _detail("HARD-03"),
            }
        )
    if by_rule.get("ENUM-01"):
        issues.append(
            {
                "rule": "enum_illegal_after_downgrade",
                "count": len(by_rule["ENUM-01"]),
                "detail": [item["attribute"] for item in _detail("ENUM-01")],
            }
        )
    if by_rule.get("EXPR-02"):
        issues.append(
            {
                "rule": "evidence_self_reference",
                "count": len(by_rule["EXPR-02"]),
                "detail": _detail("EXPR-02"),
            }
        )
    # ---- 英文残留治理：旧口径出裸串（跨批次对账沿用），编号与新 trace 一一对应。
    if by_rule.get("EXPR-01"):
        issues.append(
            {
                "rule": "english_in_prose",
                "count": len(by_rule["EXPR-01"]),
                "detail": [item["attribute"] for item in _detail("EXPR-01")],
            }
        )
    if by_rule.get("EXPR-03"):
        issues.append(
            {
                "rule": "english_placeholder_removed",
                "count": len(by_rule["EXPR-03"]),
                "detail": [item["attribute"] for item in _detail("EXPR-03")],
            }
        )

    return {
        "notes_updates_json": json.dumps(state.notes, ensure_ascii=False, default=str),
        "notes_count": len(state.notes),
        "issue_json": json.dumps(issues, ensure_ascii=False, default=str),
        "issue_count": sum(int(item["count"]) for item in issues),
        "placeholder_removed": len(by_rule.get("HARD-01") or ()),
        "enum_illegal": len(by_rule.get("HARD-02") or ()),
        # 降级后仍非法、整条剔除的条数（ENUM-01）。与 ``enum_illegal`` 分开计数：
        # 前者是「拦下了、取值还在」，后者是「取值已删」，对账时含义不同。
        "enum_illegal_after_downgrade": len(by_rule.get("ENUM-01") or ()),
        "unbacked_fact": len(by_rule.get("HARD-03") or ()),
        # 英文残留治理的两条规则合并计数：对下游与报告而言它们是一件事
        # （「这一条 Notes 曾带英文形态、已处置」），分开数会让规则报表出现两条永远同涨的线。
        "english_rule_hits": len(by_rule.get("EXPR-01") or ())
        + len(by_rule.get("EXPR-03") or ()),
        # 降级后的字段置信档：L3 据此区分「已明确降级的软值」与「写成确定值却没依据」。
        # 不往外传的话，本层的降级对表达层不可见，等于只改了一半。
        "basis_json": json.dumps(state.basis, ensure_ascii=False, default=str),
        "rule_trace_json": json.dumps(
            [trace.asdict() for trace in applied.traces], ensure_ascii=False, default=str
        ),
    }


async def rule_check(
    facts_json="{}",
    notes_updates_json="[]",
    basis_json="{}",
    llm=None,
    templates=None,
) -> dict:
    """checker 通道的取数节点：把可读化事实与本簇规则说明交给模型问一次。

    **它本身不做判断**，判断在模型；这里的职责是「在正确的时候不发起调用」
    （开关关闭 / 没有可比对的字段 / 预算耗尽），以及把失败压成状态而不是异常。
    真正的落地在 :func:`rule_apply`——两段分开是为了让「问了什么」与「改了什么」
    在留痕里能分开查。

    ``llm`` 与 ``templates`` 由运行时注入（``rt.extra['code_functions']``）。不注入时
    按「未启用」处理：单测与离线回放可以直接调这个函数而不碰网络。
    """
    return await rule_checker.run_check(
        facts_json=facts_json,
        notes_updates_json=notes_updates_json,
        basis_json=basis_json,
        llm=llm,
        templates=templates,
        disabled=_disabled_rule_ids(),
    )


def rule_apply(verdict_json="{}", brief_json="[]", notes_updates_json="[]", basis_json="{}") -> dict:
    """checker 通道的落地节点：复核模型裁决，通过的那部分交给统一执行器。

    ``rule_check`` 与 ``rule_apply`` 之间**只传裁决与送检清单**，不传事实：复核需要
    的全部信息都在清单里（字段必须属于该规则作用域）。这样即使两步之间隔了一次
    重试，也不会出现「按旧清单复核新裁决」的错配。

    模型给的动作**只有降级与转确认**，复核见
    :func:`customer_profile.rules.checker.recheck`。丢弃的条目逐条留痕——被丢的原因
    本身是「模型这条规则理解得不对」的证据，统计时能看出该不该继续挂它。
    """
    briefs = rule_checker._payload(brief_json)
    if not isinstance(briefs, list):
        briefs = []
    gate_actions, dropped = rule_checker.recheck(verdict_json, briefs, stage="project")

    table = rules.default()
    disabled = _disabled_rule_ids()
    if disabled:
        table = table.without(disabled)
    gate_actions = [item for item in gate_actions if item.rule in table]

    state = rules.NotesState(
        notes=_loads_list(notes_updates_json),
        basis=_loads_object(basis_json),
    )
    applied = rules.apply(state, gate_actions, max_rounds=1, registry=table)
    return {
        "notes_updates_json": json.dumps(state.notes, ensure_ascii=False, default=str),
        "notes_count": len(state.notes),
        # 降级会改置信档，必须往外传：下游读的是这一份 basis，不是审查层那份。
        "basis_json": json.dumps(state.basis, ensure_ascii=False, default=str),
        "checker_applied": len(applied.traces),
        "checker_dropped_json": json.dumps(dropped, ensure_ascii=False, default=str),
        "rule_trace_json": json.dumps(
            [trace.asdict() for trace in applied.traces], ensure_ascii=False, default=str
        ),
    }


def _loads_list(value) -> list:
    """把 JSON 串/序列归一成 list；解析不动一律空列表。"""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
    return []


QUOTE_MARKERS: tuple[str, ...] = hard_rules.QUOTE_MARKERS
"""证据串里「这是原文」的标记。**正身在** :mod:`customer_profile.rules.clusters.hard`
（HARD-03 的数据表），此处保留别名供既有调用点沿用。"""


def _has_quote_marker(text: str) -> bool:
    """实现见 :func:`customer_profile.rules.clusters.hard.has_quote_marker`。"""
    return hard_rules.has_quote_marker(text)


PLACEHOLDER_VALUES: frozenset[str] = hard_rules.PLACEHOLDER_VALUES
"""**占位串**：看着像有值、实际什么都没说的 ``attribute_value``。

正身已随 HARD-01 迁到 :mod:`customer_profile.rules.clusters.hard`；此处保留别名，
因为 ``_projection_context`` 判「旧车『无』不是证据」时也用它。
"""


def _is_placeholder_value(value) -> bool:
    """实现见 :func:`customer_profile.rules.clusters.hard.is_placeholder`。"""
    return hard_rules.is_placeholder(value)


def _enum_values(value) -> list[str]:
    """实现见 :func:`customer_profile.rules.clusters.hard.enum_values`。"""
    return hard_rules.enum_values(value)


def _loads_object(value) -> dict:
    """把 JSON 串/对象归一成 dict；解析不动一律空 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# ====================================================================
# 商机等级定级（独立 LLM 环节，业务专家《客户商机等级定级规范》）
# ====================================================================

# rule: LEVEL-04 信息不足是「待识别」，不是 C
LEVEL_UNKNOWN = "待识别"
"""规范里的「信息不足」结论。**不是 C 级**（R08）——不能因为缺证据就认定客户不想买。"""

LEVEL_STAGE_LEAD = "lead"
LEVEL_STAGE_OPPORTUNITY = "opportunity"
LEVEL_STAGE_LABEL: dict[str, str] = {
    LEVEL_STAGE_LEAD: "线索邀约阶段",
    LEVEL_STAGE_OPPORTUNITY: "转商机阶段",
}


def level_stage(records_json) -> dict:
    """按规范 §2 判定业务阶段（纯代码，确定性）。

    判定口径与 :func:`inject_deterministic` 的驾照规则同源：**有实质内容的试驾录音**
    即视为已进入转商机阶段。不要求试驾数据与试驾录音同时存在。客户口头说「准备试驾」
    不改变阶段（规范 §2.2 明文）。
    """
    records = _records_of(records_json)
    has_drive = _has_test_drive(records)
    stage = LEVEL_STAGE_OPPORTUNITY if has_drive else LEVEL_STAGE_LEAD
    return {
        "stage": stage,
        "stage_label": LEVEL_STAGE_LABEL[stage],
        "has_test_drive": has_drive,
    }


def _level_record_sections(records: list[dict]) -> list[str]:
    """把有内容的记录渲染成分节文本。与 L1 输入同形（来源名 + 记录编号 + 原文）。"""
    usable = [r for r in records if r.get("status") == OK and (r.get("content") or "").strip()]
    sections: list[str] = []
    for record in usable:
        source_name = record.get("source_name") or record.get("channel") or "未知渠道"
        header = [
            f"## {source_name}",
            f"记录编号：{record.get('record_id') or ''}",
        ]
        created_at = str(record.get("created_at") or "").strip()
        if created_at:
            header.append(f"采集时间：{created_at}")
        header.append(f"内容形态：{record.get('content_kind') or 'text'}")
        sections.append("\n".join(header) + "\n\n" + str(record.get("content")))
    return sections


def build_level_input(records_json, facts_json="{}", claims_json="{}") -> dict:
    """定级输入装配：业务阶段 + 已定稿事实 + **全部**客户原始记录。

    **为什么带上全部记录而不是只带事实表**：规范要求「尽可能详细地分析客户所有数据」
    并给出可追溯的判断依据；事实表只有结论，没有客户原话与上下文（例如试驾反馈的
    语气、客户对竞品的犹豫、顾问提到的交付条件）。定级是销售最看重的一条结论，
    证据必须比字段级推断更厚。

    **为什么不渲染《字段清单》**：那是 L1 的取证任务书，对定级是纯噪声。
    """
    stage = level_stage(records_json)
    records = _records_of(records_json)
    facts = _loads_object(facts_json)
    sections = _level_record_sections(records)
    body = "\n\n---\n\n".join(sections)

    payload = [
        f"【业务阶段】{stage['stage_label']}",
        "",
        "【已定稿事实（中文名 → 取值）】",
        rdb.readable_facts(facts),
        "",
        "【客户原始信息记录（按来源分节）】",
        body or "（无可用记录）",
    ]
    output = "\n".join(payload)
    return {
        "output": output,
        "stage": stage["stage"],
        "stage_label": stage["stage_label"],
        "section_count": len(sections),
        "chars": len(output),
    }


LEVEL_STEP_LIMIT = 8
"""推理链上限（用户 2026-10-20 口径：5–8 条，只讲等级怎么定出来的）。

提示词已要求精简，这里再兜一道：模型偶尔仍然写长，超出即截断前 8 条——
保留模型自己的优先级顺序，不改写内容。
"""

LEVEL_EVIDENCE_MIN_CHARS = 12
"""证据最短长度。短于此的视为「半句对白」碎片剔除（证据已改为可读化陈述，见模板第五节）。"""


def _level_evidence_readable(evidence: str) -> bool:
    """证据是否像一条可独立阅读的陈述——只挡碎片，不再核对逐字原文。

    **口径变化（2026-10-20）**：此前要求证据逐字出现在原始记录里，后果是外呼录音
    等 ASR 渠道只剩「家用啊，没有，真够啊。」这类碎片，读者看不出它证明了什么。
    现按用户要求以可读性优先，证据改为「出处 + 信号 + 指向」的整合陈述，代价是放弃
    逐字定位。这里只做最轻的一道守卫：过短即碎片。
    """
    text = str(evidence or "").strip()
    return len(text) >= LEVEL_EVIDENCE_MIN_CHARS


def _as_list(value) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def apply_level(records_json, facts_json, assess_json, stage="") -> dict:
    """定级收口（纯代码，0 token）：把模型结论变成可回传的 facts。

    三道收口，每道都对应规范里一条可机器判定的硬规则：

    1. **枚举归一**：只认 ``H`` / ``A`` / ``B`` / ``C`` / 待识别；非法值回落 L1 原等级
       （L1 也没有就留空），不把自由文本写进 facts。
    2. **阶段上限（R04）**：阶段由代码判定，线索邀约阶段最高 ``A``，``H`` 强制降级。
    3. **证据门槛（R05）**：``H`` / ``A`` 必须同时具备需求、时间、行动三类证据，
       缺任一类降为 ``B``。

    证据只做「碎片剔除 + 去重」，**不再回原文逐字核对**（读性优先，见
    :func:`_level_evidence_readable`）。推理链截断到 :data:`LEVEL_STEP_LIMIT` 条，
    且只讲定级本身——关键障碍与下一步建议不再拼进推理链（它们是跟进事项，不是等级依据）。

    解析失败时**不覆盖原等级**：保留 L1 的兜底值并留痕（``parse_ok=False``），
    而不是把一次失败的调用伪装成「客户被评成了待识别」。
    """
    stage_code = str(stage or "").strip()
    if stage_code not in LEVEL_STAGE_LABEL:
        stage_code = level_stage(records_json)["stage"]

    facts = _loads_object(facts_json)
    assess = _loads_object(assess_json)
    if not assess:
        return {
            "facts_json": json.dumps(facts, ensure_ascii=False, default=str),
            "level": "",
            "level_detail_json": "{}",
            "stage": stage_code,
            "rule_clamped": "",
            "evidence_fragments_removed": 0,
            "parse_ok": False,
        }

    level = proj.normalize_level(assess.get("level"))
    clamped = ""
    if level is None:
        # 模型给了契约外的自由文本：留痕，并回落到 L1 的兜底等级。
        level = proj.normalize_level(facts.get("deal_level"))
        clamped = "invalid_level"
    if level is None:
        return {
            "facts_json": json.dumps(facts, ensure_ascii=False, default=str),
            "level": "",
            "level_detail_json": "{}",
            "stage": stage_code,
            "rule_clamped": "invalid_level",
            "evidence_fragments_removed": 0,
            "parse_ok": True,
        }

    # rule: LEVEL-02 线索邀约阶段最高 A
    # ---- 2 阶段上限
    if stage_code == LEVEL_STAGE_LEAD and level == "H":
        level = "A"
        clamped = "lead_stage_caps_at_A"

    # ---- 证据清洗：碎片剔除 + 去重（不回原文核对）
    removed = 0

    def _clean(items) -> list[str]:
        nonlocal removed
        out: list[str] = []
        for item in _as_list(items):
            text = str(item or "").strip()
            if not text:
                continue
            if not _level_evidence_readable(text):
                removed += 1
                continue
            if text not in out:
                out.append(text)
        return out

    dimensions = assess.get("dimensions") if isinstance(assess.get("dimensions"), dict) else {}
    dim_evidence: dict[str, list[str]] = {}
    for key in ("want_to_buy", "can_to_buy", "when_to_buy"):
        entry = dimensions.get(key) if isinstance(dimensions.get(key), dict) else {}
        dim_evidence[key] = _clean(entry.get("evidence"))

    homework = _clean(assess.get("homework_evidence"))
    steps = [str(s).strip() for s in _as_list(assess.get("steps")) if str(s).strip()]

    # rule: LEVEL-03 H/A 须同时具备需求、时间、行动三类证据
    # ---- 3 证据门槛（R05）：H/A 必须同时具备需求、时间、行动三类证据
    if level in ("H", "A"):
        has_demand = bool(dim_evidence.get("want_to_buy"))
        has_time = bool(dim_evidence.get("when_to_buy")) or bool(
            str(assess.get("time_window") or "").strip()
        )
        has_action = bool(homework)
        if not (has_demand and has_time and has_action):
            level = "B"
            clamped = clamped or "insufficient_multidimensional_evidence"

    confidence = assess.get("confidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = max(0.0, min(1.0, confidence))

    all_evidence: list[str] = []
    for key in ("want_to_buy", "can_to_buy", "when_to_buy"):
        for item in dim_evidence.get(key) or []:
            if item not in all_evidence:
                all_evidence.append(item)
    for item in homework:
        if item not in all_evidence:
            all_evidence.append(item)

    summary = str(assess.get("summary") or "").strip()
    if not summary:
        summary = f"综合客户数据，判定商机等级为{level}"

    detail = {
        "summary": summary,
        "steps": steps[:LEVEL_STEP_LIMIT],
        "evidence": all_evidence[:12],
        "confidence": round(confidence, 2),
    }

    facts["deal_level"] = level
    facts["deal_level_detail"] = detail
    facts["business_opp_stage"] = LEVEL_STAGE_LABEL[stage_code]

    return {
        "facts_json": json.dumps(facts, ensure_ascii=False, default=str),
        "level": level,
        "level_detail_json": json.dumps(detail, ensure_ascii=False, default=str),
        "stage": stage_code,
        "rule_clamped": clamped,
        "evidence_fragments_removed": removed,
        "parse_ok": True,
    }


def _tool(
    node_id: str,
    title: str,
    workflow: str,
    data_source: str | None,
    *,
    outputs: tuple[str, ...] = ("result",),
):
    """构造一个下发给子流程的 ``tool`` 节点（与 ``evidence_subagent`` 同形态）。"""
    mapping: dict[str, str] = {
        "customer_data": "customer_data",
        "phone_number": "phone_number",
    }
    if data_source is not None:
        # 渠道名是常量，不是来自父图的数据：只在 @inputs 里登记字面量，不建立绑定。
        mapping["data_source"] = f"{LITERAL}{data_source}"
    return make_node(
        node_id,
        title,
        "tool",
        after=(HISTORY_HTTP,),
        inputs={
            "customer_data": (HISTORY_HTTP, "body"),
            "phone_number": (START, "phone_number"),
        },
        outputs=outputs,
        **{"@workflow": workflow, "@inputs": mapping},
    )


def _audio_chain(
    *,
    files_node: str,
    iter_node: str,
    iter_start: str,
    extract_node: str,
    files_function: str,
    title: str,
) -> tuple[Any, ...]:
    """构造一条「取录音文件 → 迭代 → 纯识别抽取」的链路。

    录音的逐字原文只在 ``audio_content_extract`` 的 ``auc_result`` 里；「录音信息提取」
    子流程交出的是一份经 LLM 压缩过的分析报告（实测长转写保留率低至 8%），不能当作原文。
    因此这里直接调用纯识别子流程，逐文件取回原文。

    走迭代而不是取第一个文件：实测试驾录音有 19/39 案带 **2 个**录音文件。
    """
    return (
        make_node(
            files_node,
            f"解析{title}文件",
            "code",
            after=(HISTORY_HTTP,),
            inputs={"records": (HISTORY_HTTP, "body")},
            outputs=("files",),
            function=files_function,
        ),
        make_node(
            iter_node,
            f"{title}逐字转写",
            "iteration",
            after=(files_node,),
            outputs=("output", "item"),
            iterator=(files_node, "files"),
            inputs={"@output": (extract_node, "transcript_bundle")},
            body=(iter_start, extract_node),
            flatten_output=True,
            error_handle_mode="terminated",
        ),
        make_node(
            iter_start,
            "",
            "iteration-start",
            outputs=("item", "index"),
            iteration_id=iter_node,
        ),
        make_node(
            extract_node,
            "录音文件内容抽取（纯识别，无加工）",
            "tool",
            after=(iter_start,),
            inputs={"file_url": (iter_node, "item")},
            outputs=("transcript_bundle", "annotated", "role_summary"),
            **{
                "@workflow": AUDIO_EXTRACT_WORKFLOW,
                "@inputs": {"file_url": "file_url"},
            },
            iteration_id=iter_node,
        ),
    )


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=DISPLAY_NAME,
    entries=(START,),
    exits=(END,),
    outputs={"result1": "result1"},
    nodes=(
        make_node(
            START,
            "用户输入",
            "start",
            variables=("phone_number", "batch_id"),
            required_phone_number=True,
            required_batch_id=False,
        ),
        make_node(
            HISTORY_HTTP,
            "获取全量用户数据",
            "http-request",
            after=(START,),
            inputs={"@path": (START, "phone_number")},
            outputs=("body", "status"),
            **{
                "@service": "profile",
                "@method": "get",
                "@path": HISTORY_PATH,
                "@auth": True,
                "@outputs": {"body": "$text", "status": "$status"},
            },
        ),
        # ---------------------------------------------------------- 取数层（14 路）
        _tool(MOMENTS, "朋友圈信息提取", MOMENTS_PURE, "WeChatMomentsScreenshot"),
        _tool(
            WECHAT_SEARCH,
            "微信手机号搜索截图信息提取",
            "wechat_search_homepage",
            "WeChatPhoneSearchScreenshot",
        ),
        _tool(WECHAT_HOME, "微信主页信息提取", "wechat_homepage", "WeChatHomepageScreenshot"),
        _tool(TEST_DRIVE, "试驾录音信息提取", TEST_DRIVE_PURE, "TestDriveRecordingFile"),
        _tool(
            OUTBOUND_CALL,
            "外呼录音信息提取",
            OUTBOUND_CALL_PURE,
            "AIOutboundCallRecordingFile",
        ),
        _tool(ALIPAY, "支付宝个人页信息提取", "alipay_homepage", "AlipayPersonalPageScreenshot"),
        _tool(
            XIAOHONGSHU,
            "小红书个人页信息提取",
            "xiaohongshu_homepage",
            "XiaohongshuPersonalPageScreenshot",
        ),
        _tool(DOUYIN, "抖音个人页信息提取", "douyin_homepage", "DouyinPersonalPageScreenshot"),
        # 人工确认信息只需要手机号（走 mhero，与 history 无关）
        make_node(
            LOCKED_NOTES,
            "人工确认信息提取",
            "tool",
            after=(HISTORY_HTTP,),
            inputs={"phone_number": (START, "phone_number")},
            outputs=("result",),
            **{
                "@workflow": "human_corrected_info",
                "@inputs": {"phone_number": "phone_number"},
            },
        ),
        _tool(JIGUANG, "极光数据", "jiguang_data", "DatametInterestPoints"),
        _tool(
            MENGSHI_IT,
            "猛士IT系统数据信息",
            "mengshi_it_system_data",
            "MengShiITSystemData",
            outputs=("IT_customer_info", "IT_json_data"),
        ),
        _tool(
            CHAT_HISTORY,
            "聊天记录数据信息",
            "chat_history_data",
            "MengShiCustomerChatInfo",
        ),
        _tool(
            FEEDBACK,
            "用户人工反馈数据提取",
            "user_feedback_data",
            "UserFeedbackInformation",
        ),
        _tool(
            PHONE_COMPANY,
            "手机号&企业信息分析",
            "phone_company_analysis",
            "RelatedEnterpriseInfo",
            outputs=("phonenumber_analysis_result", "company_analysis_result"),
        ),
        # ---------------------------------------------------------- 录音逐字原文
        *_audio_chain(
            files_node=TEST_DRIVE_FILES,
            iter_node=TEST_DRIVE_ITER,
            iter_start=TEST_DRIVE_ITER_START,
            extract_node=TEST_DRIVE_EXTRACT,
            files_function=f"customer_profile.workflows.{SLUG}:collect_test_drive_files",
            title="试驾录音",
        ),
        *_audio_chain(
            files_node=OUTBOUND_FILES,
            iter_node=OUTBOUND_ITER,
            iter_start=OUTBOUND_ITER_START,
            extract_node=OUTBOUND_EXTRACT,
            files_function=f"customer_profile.workflows.{SLUG}:collect_outbound_files",
            title="外呼录音",
        ),
        # ---------------------------------------------------------- L0 记录层
        make_node(
            CHANNEL_BUNDLE,
            "渠道产出汇总",
            "code",
            after=(
                MOMENTS,
                WECHAT_SEARCH,
                WECHAT_HOME,
                TEST_DRIVE,
                OUTBOUND_CALL,
                ALIPAY,
                XIAOHONGSHU,
                DOUYIN,
                LOCKED_NOTES,
                JIGUANG,
                MENGSHI_IT,
                CHAT_HISTORY,
                FEEDBACK,
                PHONE_COMPANY,
                TEST_DRIVE_ITER,
                OUTBOUND_ITER,
            ),
            inputs={
                "history_body": (HISTORY_HTTP, "body"),
                "moments": (MOMENTS, "result"),
                "wechat_search": (WECHAT_SEARCH, "result"),
                "wechat_home": (WECHAT_HOME, "result"),
                "test_drive_report": (TEST_DRIVE, "result"),
                "outbound_report": (OUTBOUND_CALL, "result"),
                "alipay": (ALIPAY, "result"),
                "xiaohongshu": (XIAOHONGSHU, "result"),
                "douyin": (DOUYIN, "result"),
                "locked_notes": (LOCKED_NOTES, "result"),
                "jiguang": (JIGUANG, "result"),
                "mengshi_it": (MENGSHI_IT, "IT_customer_info"),
                "chat_history": (CHAT_HISTORY, "result"),
                "feedback": (FEEDBACK, "result"),
                "phone_company": (PHONE_COMPANY, "company_analysis_result"),
                "phone_location": (PHONE_COMPANY, "phonenumber_analysis_result"),
                "test_drive_transcript": (TEST_DRIVE_ITER, "output"),
                "outbound_transcript": (OUTBOUND_ITER, "output"),
            },
            outputs=("channels_json",),
            function=f"customer_profile.workflows.{SLUG}:bundle_channels",
        ),
        make_node(
            L0_RECORDS,
            "L0 记录层",
            "code",
            after=(CHANNEL_BUNDLE,),
            inputs={
                "history_body": (HISTORY_HTTP, "body"),
                "channels_json": (CHANNEL_BUNDLE, "channels_json"),
                "phone_number": (START, "phone_number"),
            },
            outputs=("records_json", "records_count", "exception_count"),
            function=f"customer_profile.workflows.{SLUG}:build_records",
        ),
        make_node(
            L4_TRIAGE,
            "L4 分诊层",
            "code",
            after=(L0_RECORDS,),
            inputs={"records_json": (L0_RECORDS, "records_json")},
            outputs=("tier", "tier_reason", "usable_count"),
            function=f"customer_profile.workflows.{SLUG}:triage",
        ),
        # ---------------------------------------------------------- L1 取证层（v3 三分层：3 次 LLM + 代码定坐标）
        # 1a 事实提取 → 1b 人设画像 → 1c 分析投影 → 代码定坐标（设计稿 §3）。
        # 三层直接用绑定组装输入（``{{ arg1 }} {{ arg2 }} {{ arg3 }}``），不再插装配代码节点：
        # LLM 执行器本就支持多入参模板渲染，多插一层只是多一处失败点。
        make_node(
            L1_INPUT,
            "L1 取证输入装配",
            "code",
            after=(L0_RECORDS,),
            inputs={"records_json": (L0_RECORDS, "records_json")},
            outputs=("output", "section_count", "chars", "guide_chars"),
            function=f"customer_profile.workflows.{SLUG}:build_l1_input",
        ),
        # ---- 1a 事实提取层：只聚焦「全 / 准 / 事实（高置信推断）」，输出 JSONL
        make_node(
            L1_FACTS,
            "L1-1a 事实提取层",
            "llm",
            after=(L1_INPUT,),
            inputs={"arg1": (L1_INPUT, "output")},
            outputs=("result", "records", "dropped_lines", "parsed_json"),
            template=L1_FACTS_TEMPLATE,
            output="result",
            output_format="jsonl",
            system_text=SYSTEM_TEXT,
        ),
        # ---- 1b 人设画像层：事实 + 原始数据 → 半结构化画像（**不进证据位**）
        make_node(
            L1_PERSONA,
            "L1-1b 人设画像层",
            "llm",
            after=(L1_FACTS,),
            inputs={
                "arg1": (L1_INPUT, "output"),
                "arg2": (L1_FACTS, "result"),
            },
            outputs=("result", "parsed_json"),
            template=L1_PERSONA_TEMPLATE,
            output="result",
            expect_json=True,
            system_text=SYSTEM_TEXT,
        ),
        # ---- 1c 分析投影层：事实 + 人设参考 + 原始数据 → 补齐字段级结论
        make_node(
            L1_FIELDS,
            "L1-1c 分析投影层",
            "llm",
            after=(L1_PERSONA,),
            inputs={
                "arg1": (L1_INPUT, "output"),
                "arg2": (L1_FACTS, "result"),
                "arg3": (L1_PERSONA, "result"),
            },
            outputs=("result", "parsed_json"),
            template=L1_FIELDS_TEMPLATE,
            output="result",
            expect_json=True,
            system_text=SYSTEM_TEXT,
        ),
        make_node(
            L1_MERGE,
            "L1 三层合并（事实 ∪ 分析）",
            "code",
            after=(L1_FACTS, L1_FIELDS),
            inputs={
                "facts_json": (L1_FACTS, "result"),
                "fields_json": (L1_FIELDS, "result"),
            },
            outputs=("claims_json", "merged_count", "facts_count", "fields_count",
                     "from_facts_only", "overrode", "subject_overrides"),
            function=f"customer_profile.workflows.{SLUG}:merge_l1_layers",
        ),
        make_node(
            L1_LOCALIZE,
            "L1 代码定坐标",
            "code",
            after=(L1_MERGE,),
            inputs={
                "records_json": (L0_RECORDS, "records_json"),
                "claims_json": (L1_MERGE, "claims_json"),
            },
            outputs=(
                "claims_json",
                "kept",
                "dropped",
                "dropped_detail",
                "downgraded",
                "downgraded_detail",
                "parse_ok",
            ),
            function=f"customer_profile.workflows.{SLUG}:localize_claims",
        ),
        # ---------------------------------------------------------- 确定性字段注入（纯代码，0 token）
        # v3.1 改造四：``phone_number`` 直取入参、``driver_license_status`` 按试驾/旧车规则定。
        # 接在定位层**之后**——注入来源名在记录层无对应记录，接在前面会被定位层
        # 按 unknown_channel 降级（见 L1_INJECT 常量说明）。
        make_node(
            L1_INJECT,
            "确定性字段注入",
            "code",
            after=(L1_LOCALIZE,),
            inputs={
                "records_json": (L0_RECORDS, "records_json"),
                "claims_json": (L1_LOCALIZE, "claims_json"),
                "phone_number": (START, "phone_number"),
            },
            outputs=(
                "claims_json",
                "added",
                "replaced",
                "injected",
                "license_injected",
                "parse_ok",
            ),
            function=f"customer_profile.workflows.{SLUG}:inject_deterministic",
        ),
        # ---------------------------------------------------------- L2 投影层（纯代码，0 token）
        make_node(
            L2_PROJECT,
            "L2 投影层",
            "code",
            after=(L1_INJECT, L4_TRIAGE),
            inputs={
                "claims_json": (L1_INJECT, "claims_json"),
                "tier": (L4_TRIAGE, "tier"),
            },
            outputs=(
                "facts_json",
                "facts_readable",
                "notes_updates_json",
                "to_confirm_json",
                "rejected_json",
                "candidates_json",
                "basis_json",
                "notes_count",
                "tier",
                "rule_trace_json",
            ),
            function=f"customer_profile.workflows.{SLUG}:project_claims",
        ),
        # ---------------------------------------------------------- L2 兜底（1 次受约束 LLM）
        # 「凡是能写成规则的，不交给 LLM」（设计稿 §6.1）：输入只有规则判不了的字段，
        # 输出受 schema 与代码双重校验（不得新增候选集外的值，§6.3）。
        make_node(
            L2_FALLBACK,
            "L2 兜底裁决",
            "llm",
            after=(L2_PROJECT,),
            inputs={
                "arg1": (L2_PROJECT, "candidates_json"),
                # 缺字段补全必须**看着事实表**做（v2.1 R2）：缺字段条目没有候选值，
                # 不给事实表就是让它凭空编——那正是「受约束的裁决者」要避免的。
                # 只读可读化事实：原样 ``facts_json`` 里的英文键（``annual_income``）
                # 与英文枚举键会被抄进 ``reason``，而 ``reason`` 是给销售看的。
                "arg2": (L2_PROJECT, "facts_readable"),
            },
            outputs=("result",),
            template=L2_FALLBACK_TEMPLATE,
            output="result",
            expect_json=True,
            system_text=SYSTEM_TEXT,
        ),
        make_node(
            L2_APPLY,
            "L2 终判并入",
            "code",
            after=(L2_FALLBACK, L2_PROJECT),
            inputs={
                "candidates_json": (L2_PROJECT, "candidates_json"),
                "verdict_json": (L2_FALLBACK, "result"),
                "notes_updates_json": (L2_PROJECT, "notes_updates_json"),
            },
            outputs=(
                "notes_updates_json",
                "notes_count",
                "applied",
                "out_of_bounds",
                "verdict_json",
            ),
            function=f"customer_profile.workflows.{SLUG}:merge_l2_verdict",
        ),
        # ---------------------------------------------------------- L2 审查核验层（纯代码审核，0 token）
        # 承接 v3 设计稿 §3：对 L1/L2 产出做合规性复核——枚举合法、无效占位清除、
        # Notes 与 facts 同源、fact 必有锚点、证据位不自指。**能代码判定的先由代码修**。
        make_node(
            L2_REVIEW,
            "L2 审查核验层",
            "code",
            after=(L2_APPLY,),
            inputs={
                "notes_updates_json": (L2_APPLY, "notes_updates_json"),
                "facts_json": (L2_PROJECT, "facts_json"),
                "basis_json": (L2_PROJECT, "basis_json"),
            },
            outputs=(
                "notes_updates_json",
                "notes_count",
                "issue_json",
                "issue_count",
                "placeholder_removed",
                "enum_illegal",
                "unbacked_fact",
                # 本层执行判决后可能改变字段置信档（HARD-02/03 降级），必须往外传：
                # 不传的话表达层看到的仍是降级前的档位，等于只改了一半。
                "basis_json",
                "rule_trace_json",
            ),
            function=f"customer_profile.workflows.{SLUG}:review_notes",
        ),
        # ---------------------------------------------------------- 商机等级定级（独立 1 次 LLM）
        # 业务专家的《客户商机等级定级规范》要求「尽可能详细分析全部客户数据」并给出可追溯
        # 的推理链。这条字段对销售的重要程度断档领先，因此**不随 L1 的字段清单一起产出**，
        # 而是单开一次调用、独占全量原始记录与足量输出篇幅，再由代码做规则收口。
        # ---------------------------------------------------------- checker 通道（1 次受约束 LLM）
        # 语义判断写成规则表太硬、放进提示词又没人执行，交模型做一次「有没有违反」的
        # 问答。**用 code 节点承载而不是 llm 节点**：调度器对失败节点会阻塞整棵下游子树，
        # 做成 llm 节点的话一次调用失败就会挡掉定级、表达与回传——规则层成了新单点。
        # 关闭开关（RULE_CHECKER_ENABLED，缺省 false）时在发起调用前即返回，零 token。
        make_node(
            RULE_CHECK,
            "规则复核（checker）",
            "code",
            after=(L2_REVIEW,),
            inputs={
                "facts_json": (L2_PROJECT, "facts_json"),
                "notes_updates_json": (L2_REVIEW, "notes_updates_json"),
                "basis_json": (L2_REVIEW, "basis_json"),
            },
            outputs=(
                "verdict_json",
                "brief_json",
                "checker_status",
                "checker_calls",
                "checker_error",
                "notes_updates_json",
            ),
            function=f"customer_profile.workflows.{SLUG}:rule_check",
        ),
        make_node(
            RULE_APPLY,
            "规则裁决并入",
            "code",
            after=(RULE_CHECK,),
            inputs={
                "verdict_json": (RULE_CHECK, "verdict_json"),
                "brief_json": (RULE_CHECK, "brief_json"),
                "notes_updates_json": (RULE_CHECK, "notes_updates_json"),
                "basis_json": (L2_REVIEW, "basis_json"),
            },
            outputs=(
                "notes_updates_json",
                "notes_count",
                "basis_json",
                "checker_applied",
                "checker_dropped_json",
                "rule_trace_json",
            ),
            function=f"customer_profile.workflows.{SLUG}:rule_apply",
        ),
        # ---------------------------------------------------------- 商机等级定级（独立 1 次 LLM）
        # 业务专家的《客户商机等级定级规范》要求「尽可能详细分析全部客户数据」并给出可追溯
        # 的推理链。这条字段对销售的重要程度断档领先，因此**不随 L1 的字段清单一起产出**，
        # 而是单开一次调用、独占全量原始记录与足量输出篇幅，再由代码做规则收口。
        make_node(
            LEVEL_INPUT,
            "商机定级输入装配",
            "code",
            after=(RULE_APPLY,),
            inputs={
                "records_json": (L0_RECORDS, "records_json"),
                "facts_json": (L2_PROJECT, "facts_json"),
            },
            outputs=("output", "stage", "stage_label", "section_count", "chars"),
            function=f"customer_profile.workflows.{SLUG}:build_level_input",
        ),
        make_node(
            LEVEL_ASSESS,
            "商机等级评定",
            "llm",
            after=(LEVEL_INPUT,),
            inputs={"arg1": (LEVEL_INPUT, "output")},
            outputs=("result",),
            template=LEVEL_ASSESS_TEMPLATE,
            output="result",
            expect_json=True,
            system_text=SYSTEM_TEXT,
        ),
        make_node(
            LEVEL_APPLY,
            "商机定级收口",
            "code",
            after=(LEVEL_ASSESS, LEVEL_INPUT),
            inputs={
                "records_json": (L0_RECORDS, "records_json"),
                "facts_json": (L2_PROJECT, "facts_json"),
                "assess_json": (LEVEL_ASSESS, "result"),
                "stage": (LEVEL_INPUT, "stage"),
            },
            outputs=(
                "facts_json",
                "level",
                "level_detail_json",
                "stage",
                "rule_clamped",
                "evidence_fragments_removed",
                "parse_ok",
            ),
            function=f"customer_profile.workflows.{SLUG}:apply_level",
        ),
        # ---------------------------------------------------------- 取证与成文（复用）
        # ---------------------------------------------------------- L3 校验与表达（1 次 LLM）
        # 替代 production：**同一份 facts** 既喂 Notes、又写 9 个 section（设计稿 §7.4）。
        # 输入只有已定稿的事实，没有证据原文——表达层因此**无从新增事实**。
        # facts 取自 **LEVEL_APPLY**（而非 L2_PROJECT）：商机等级是 L3 之后才定稿的，
        # 若仍喂 L2 的 facts，卡片上会出现「等级与它的推理链对不上」。
        make_node(
            PRODUCTION,
            "L3 校验与表达",
            "tool",
            after=(LEVEL_APPLY,),
            inputs={
                "phone": (START, "phone_number"),
                "batch_id": (START, "batch_id"),
                "facts_json": (LEVEL_APPLY, "facts_json"),
                # 取**审查层**的 basis 而不是投影层的：审查层会把非法枚举与无锚点
                # fact 降为 inferred_low，L3 校验据此区分「已降级的软值」与「写成确定值
                # 却没依据」——取投影层那份的话，审查层的降级对表达层不可见。
                "basis_json": (RULE_APPLY, "basis_json"),
                "notes_updates_json": (RULE_APPLY, "notes_updates_json"),
                "claims_json": (L1_INJECT, "claims_json"),
            },
            outputs=("result1", "section_count", "sections_missing",
                     "sections_hoisted", "new_fact_suspects", "suspects_json"),
            **{
                "@workflow": L3_WORKFLOW,
                "@inputs": {
                    "phone": "phone",
                    "batch_id": "batch_id",
                    "facts_json": "facts_json",
                    "basis_json": "basis_json",
                    "notes_updates_json": "notes_updates_json",
                    "claims_json": "claims_json",
                },
            },
        ),
        # ---------------------------------------------------------- v2 回传（旁支）
        make_node(
            CALLBACK_BUILD,
            "回传体构造",
            "code",
            after=(PRODUCTION,),
            inputs={
                "phone": (START, "phone_number"),
                "batch_id": (START, "batch_id"),
                "facts_json": (LEVEL_APPLY, "facts_json"),
                "notes_updates_json": (RULE_APPLY, "notes_updates_json"),
                # 表达层的 section 产物（resistances / emotion_state / overall_tag 从这里取）。
                # ``result1`` 是**字符串**：由 callback_build 内部二次解析。
                "express_json": (PRODUCTION, "result1"),
            },
            outputs=("callback_json",),
            # 指向 ``main`` 而不是 ``build_analysis_result``：后者返回的是 JSON **字符串**，
            # 而 ``execute_code`` 要求 code 函数返回字段字典（``executors_misc.py:141-144``
            # 对非 Mapping 直接抛「返回值应为字段字典」）。``main`` 把那个串包成
            # ``{"callback_json": ...}``，正是节点出参的形状。
            function="customer_profile.callback_build:main",
        ),
        make_node(
            CALLBACK_WRITE,
            "画像回写",
            "http-request",
            after=(CALLBACK_BUILD,),
            outputs=("body", "status"),
            **{
                # 请求体形状：顶层三键（phone / batch_id / analysis_result），
                # 其中 analysis_result 必须是**对象**。@body_template 恒把引用渲染成字符串，
                # 产不出对象，因此这里整段取自绑定值（@body_ref）。
                "@body_ref": True,
                "@service": "profile",
                "@method": "post",
                "@path": f"{PROFILE_API_PREFIX}callback/update-profile",
                "@auth": True,
                # 生产回写：WRITEBACK_ENABLED=false（缺省）时不发送，只产出「已跳过」的结果。
                "@writeback": True,
                "@outputs": {"body": "$text", "status": "$status"},
            },
            inputs={"@body_ref": (CALLBACK_BUILD, "callback_json")},
        ),
        make_node(
            END,
            "输出",
            "end",
            after=(CALLBACK_BUILD,),
            # ``result1`` 取 **v2 回传体**（``callback_json``）而不是 L3 的九板块旧结构：
            # 运行台「结果」页展示的是 ``result1``，旧结构会让业务方核对到一份与平台
            # 实际画像无关的数据。L3 的旧结构仍留在 ``PRODUCTION`` 节点出参里备查。
            inputs={"result1": (CALLBACK_BUILD, "callback_json")},
            outputs=("result1",),
        ),
    ),
)


def history_url(phone_number: str) -> str:
    """该工作流实际请求的全量历史数据 URL。用于构造固定响应 fixture。"""
    return f"{HISTORY_PATH}{phone_number}"


def default_inputs(phone_number: str, batch_id: str = "") -> dict[str, Any]:
    """该工作流的对外入参契约：与主入口一致（``phone_number`` 必填、``batch_id`` 可选）。"""
    return {"phone_number": phone_number, "batch_id": batch_id}


# ====================================================================
# code 节点 -> 函数名映射
# ====================================================================

CODE_SPECS: dict[str, dict] = {
    TEST_DRIVE_FILES: {"main": "collect_test_drive_files", "renames": {}},
    OUTBOUND_FILES: {"main": "collect_outbound_files", "renames": {}},
    CHANNEL_BUNDLE: {"main": "bundle_channels", "renames": {}},
    L0_RECORDS: {"main": "build_records", "renames": {}},
    L4_TRIAGE: {"main": "triage", "renames": {}},
    L1_INPUT: {"main": "build_l1_input", "renames": {}},
    L1_MERGE: {"main": "merge_l1_layers", "renames": {}},
    L1_LOCALIZE: {"main": "localize_claims", "renames": {}},
    L1_INJECT: {"main": "inject_deterministic", "renames": {}},
    L2_PROJECT: {"main": "project_claims", "renames": {}},
    L2_APPLY: {"main": "merge_l2_verdict", "renames": {}},
    L2_REVIEW: {"main": "review_notes", "renames": {}},
    RULE_CHECK: {"main": "rule_check", "renames": {}},
    RULE_APPLY: {"main": "rule_apply", "renames": {}},
    LEVEL_INPUT: {"main": "build_level_input", "renames": {}},
    LEVEL_APPLY: {"main": "apply_level", "renames": {}},
}
