"""``客户初始画像（事实账本管线）`` —— 业务优化主方案的实验臂。

这是 :mod:`customer_profile.workflows.customer_profile_entry` 的**平行实现**，两臂共用
同一套取数子流程与同一个成文工作流，只在**中间的取证与投影**上换实现。AB 实验按
``scripts/run_baseline.py --workflow-id`` 切换两臂，产物形状与归档逻辑完全一致。

    基线臂（customer_profile_entry）:
      evidence_subagent（21 节点 / 14 路扇出，含 1 个 LLM 取证节点）
        → profile_features_analysis（人设特征判断）
          → customer_profile_production（41 节点 / 11 路 LLM 生成）

    实验臂（本模块，阶段一/二）:
      取数层（14 路 tool，**原样复用**既有子流程）
        → L0 记录层（纯代码）：把清单与各渠道产物统一成 records[]
          → L4 分诊层（纯代码）：sufficient / thin / empty
            → 取证输入包（纯代码）：只喂「有内容」的渠道
              → 证据线索整理（复用既有提示词）
                → profile_features_analysis → customer_profile_production → result1

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
from typing import Any

from .. import projection as proj
from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "customer_profile_pipeline"
DISPLAY_NAME = "客户初始画像（事实账本管线）"

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
EVIDENCE_INPUT = "pipeline_evidence_input"

# —— L1 取证层 / L2 投影层 / T5 兼容覆写
L1_INPUT = "pipeline_l1_input"
L1_LLM = "pipeline_l1_llm"
L1_LOCALIZE = "pipeline_l1_localize"
L2_PROJECT = "pipeline_l2_project"
NOTES_OVERWRITE = "pipeline_notes_overwrite"

# —— 取证与成文（复用既有工作流与提示词）
EVIDENCE_LLM = "pipeline_evidence_llm"
STANDARD_AGGREGATE = "pipeline_standard_aggregate"
PROFILE = "pipeline_profile"
PRODUCTION = "pipeline_production"

SHARED_CODE_MODULE = "customer_profile.workflows.shared_code"
EVIDENCE_MODULE = "customer_profile.workflows.evidence_subagent"
SLUG = "customer_profile_pipeline"
SYSTEM_TEXT = "You are a helpful AI assistant."
HISTORY_PATH = "/api/v1/remote/data/history/"
LITERAL = "@literal:"

# 复用既有工作流的提示词资源名（正文不改，只改喂进去的输入）。
EVIDENCE_PROMPT_TEMPLATE = "evidence_subagent__1777366329758"
# 复用既有子流程的 ID（取数层与成文层）
EVIDENCE_WORKFLOW = "evidence_subagent"
PROFILE_WORKFLOW = "profile_features_analysis"
PRODUCTION_WORKFLOW = "customer_profile_production"
AUDIO_EXTRACT_WORKFLOW = "audio_content_extract"


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


L1_PROMPT_TEMPLATE = template_name(L1_LLM)
"""L1 取证提示词资源名（口径 B · 锚点定位，2026-10-10 定稿）。"""


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
        "手机号归属地信息": phone_company,
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


def _transcript_text(value: Any) -> str:
    """把录音转录产出归一成一段文本。

    迭代节点 ``flatten_output`` 后给的是**逐文件**的 ``auc_result`` 列表，每项是一段
    ASR 六字段数组文本。空项（识别无结果时是 ``"[]"``）必须剔除——若直接 JSON 序列化，
    ``["[]", "<两千字原文>"]`` 这段噪声会原样进入上下文，且掩盖「这一段其实没识别出内容」。
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_transcript_text(v) for v in value]
        return "\n".join(p for p in parts if p)
    if isinstance(value, str):
        text = value.strip()
        if not text or _looks_like_sentinel(text):
            return ""
        try:
            parsed = json.loads(text)
        except ValueError:
            return text
        # ASR 六字段数组：解析得动就说明是转录本体，原样保留（逐字，不重排）。
        # 但空数组要归零：实测有 3 个录音文件识别结果是 ``"[]"``，它是「没识别出内容」，
        # 不是「一段叫 [] 的原文」。留成非空会把一个空录音记成 status=ok。
        if isinstance(parsed, list) and not parsed:
            return ""
        return text
    return ""


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
        if present:
            if source_name in TRANSCRIPT_SOURCES:
                content = _transcript_text(transcripts.get(source_name))
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
    - ``thin``：只有声明/社交/弱登记/预测 → 保守模板，只输出能坐实的字段；
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
        data = data.get("claims")
    if not isinstance(data, (list, tuple)):
        return [], False
    return [c for c in data if isinstance(c, dict)], True


def _locate(content: str, anchor: str) -> tuple[int, int, str] | None:
    """在 ``content`` 里定位 ``anchor``，返回 ``(起, 止, 原文切片)``。

    先按逐字匹配；失败再按**忽略空白**匹配一次——ASR 转写里同一句话的断字与空格
    常与模型抄写有出入，只认逐字会误杀本来正确的断言。

    **坐标一律由这里算出，模型不誊抄整段引文**——这正是「锚点可回溯率 100%」的含义：
    留下的引文全都是原文的切片，不可能出现模型自造的句子。
    """
    if not content or not anchor:
        return None
    index = content.find(anchor)
    if index >= 0:
        return index, index + len(anchor), anchor

    compact_chars: list[str] = []
    compact_index: list[int] = []
    for position, char in enumerate(content):
        if char.isspace():
            continue
        compact_chars.append(char)
        compact_index.append(position)
    compact_anchor = "".join(c for c in anchor if not c.isspace())
    if not compact_anchor:
        return None
    found = "".join(compact_chars).find(compact_anchor)
    if found < 0:
        return None
    start = compact_index[found]
    end = compact_index[found + len(compact_anchor) - 1] + 1
    return start, end, content[start:end]


def build_l1_input(records_json) -> dict:
    """L1 取证层的输入装配：把有内容的渠道排成分节文本。

    与 ``build_evidence_input`` 的分工：那个交给既有「证据线索整理」提示词（A-E 五级
    证据口径），本函数交给 L1 断言账本提示词，因此要多带两样东西——

    - **来源分级标注**：L1 要据此判 ``claim_type`` 与 ``confidence``，而分级由**代码按渠道
      定**，不由模型猜（``端到端方案.md`` §3.2 硬规则 1）；
    - **记录编号**：供代码回定位原文，也是断言与出处的可追溯凭据。

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
        sections.append("\n".join(header) + "\n\n" + str(record.get("content")))

    return {
        "output": "\n\n---\n\n".join(sections),
        "section_count": len(sections),
        "chars": sum(len(s) for s in sections),
    }


def localize_claims(records_json, claims_json, min_anchor: int = 8) -> dict:
    """代码定坐标：把 L1 的 ``anchor`` 回定位到原文，定位不到即**丢弃该断言**。

    「引文可回溯」由这一层强制：模型只指认「原文里哪一段」，切出来的是原文本身。

    丢弃口径：

    - 渠道对不上记录层 → 丢弃；
    - ``anchor`` 短于 ``min_anchor``（提示词契约要求 >=8 字）→ 丢弃。过短的锚点极易
      偶然命中，留下它换来的不是召回而是噪声；
    - 在原文里找不到 → 丢弃。

    被丢弃的断言收集在 ``dropped_detail`` 里，供 R0/R1 逐案核对——静默丢弃会让人误以为
    模型没提取到，而实际是**提取到了但定位失败**，两者的处置完全不同。
    """
    records = _records_of(records_json)
    claims, parse_ok = _claims_payload(claims_json)

    by_name: dict[str, str] = {}
    by_id: dict[str, str] = {}
    for record in records:
        content = record.get("content") or ""
        if record.get("status") != OK or not content.strip():
            continue
        by_name.setdefault(str(record.get("source_name") or ""), content)
        by_id.setdefault(str(record.get("record_id") or ""), content)

    kept: list[dict] = []
    dropped: list[dict] = []
    for raw in claims:
        channel = str(raw.get("record_channel") or "").strip()
        content = by_name.get(channel) or by_id.get(channel)
        if content is None and channel:
            content = by_name.get(proj._match_channel(channel))
        if content is None:
            dropped.append({"claim_id": raw.get("claim_id"), "reason": "unknown_channel", "channel": channel})
            continue

        anchor = str(raw.get("anchor") or "").strip()
        if len(anchor) < min_anchor:
            dropped.append({"claim_id": raw.get("claim_id"), "reason": "anchor_too_short", "anchor": anchor})
            continue
        located = _locate(content, anchor)
        if located is None:
            dropped.append({"claim_id": raw.get("claim_id"), "reason": "anchor_not_found", "anchor": anchor})
            continue

        start, end, quote = located
        entry = dict(raw)
        entry["record_channel"] = channel
        entry["quote"] = quote
        entry["span"] = [start, end]
        kept.append(entry)

    return {
        "claims_json": json.dumps({"claims": kept}, ensure_ascii=False, default=str),
        "kept": len(kept),
        "dropped": len(dropped),
        "dropped_detail": json.dumps(dropped, ensure_ascii=False, default=str),
        "parse_ok": parse_ok,
    }


def _projection_context(claims) -> dict:
    """从断言账本读投影所需的旁证（原文级事实，不从字段值反推）。"""
    deregistered = False
    old_car = False
    for claim in claims:
        text = json.dumps(claim.value, ensure_ascii=False, default=str)
        if claim.attribute in {"company", "company_type"} and any(
            marker in text for marker in ("注销", "吊销")
        ):
            deregistered = True
        if claim.attribute in {"old_car_model", "owned_brand_model", "owned_car_age"}:
            old_car = True
        if claim.attribute == "trade_in_requirement" and ("有旧车" in text or "置换" in text):
            old_car = True
    return {"company_deregistered": deregistered, "has_old_car_evidence": old_car}


def _registry_only(updates: list[dict]) -> list[dict]:
    """``thin`` 档的保守口径：只保留**登记档**坐实的字段。"""
    label = proj.TIER_LABEL[proj.REGISTRY]
    kept: list[dict] = []
    for entry in updates:
        evidence = (entry.get("reasoning_detail") or {}).get("evidence") or []
        if any(label in str(item) for item in evidence):
            kept.append(entry)
    return kept


def project_claims(claims_json, tier: str = "sufficient") -> dict:
    """L2 投影层（纯代码，0 token）：断言账本 → 规范事实 + Notes 更新项。

    ``tier`` 来自 L4 分诊：``sufficient`` 走事实流程；``thin`` 只输出登记档坐实的字段；
    ``empty`` 不产出任何断言（保守模板，``端到端方案.md`` §3.6）。
    """
    claims, _ = _claims_payload(claims_json)
    ledger = [
        proj.Claim(
            attribute=str(raw.get("attribute") or "").strip(),
            value=raw.get("value"),
            channel=str(raw.get("record_channel") or "").strip(),
            # 主体为空**不**回落成 customer：规则要求「主体说不清」的断言不得支撑身份字段，
            # 默认成客户本人恰好会把该拦下的放行。
            subject=str(raw.get("subject") or "").strip(),
            claim_type=str(raw.get("claim_type") or "").strip() or "fact",
            claim_id=str(raw.get("claim_id") or ""),
        )
        for raw in claims
        if str(raw.get("attribute") or "").strip()
    ]
    result = proj.project(ledger, context=_projection_context(ledger))

    updates = result.notes_updates
    if tier == "empty":
        updates = []
    elif tier != "sufficient":
        updates = _registry_only(updates)

    return {
        "facts_json": json.dumps(result.facts, ensure_ascii=False, default=str),
        "notes_updates_json": json.dumps(updates, ensure_ascii=False, default=str),
        "to_confirm_json": json.dumps(result.to_confirm, ensure_ascii=False, default=str),
        "rejected_json": json.dumps(result.rejected, ensure_ascii=False, default=str),
        "notes_count": len(updates),
        "tier": tier,
    }


def overwrite_notes_updates(result1, notes_updates_json) -> dict:
    """T5 兼容覆写（``落地计划书.md`` D4）：把 L2 的 Notes 写进 ``result1``。

    **阶段一/二不碰 production 内部**，但 L2 纠正过的 Notes 必须真的出现在产物里，
    否则阶段二无从度量。因此在图尾覆写 ``analysis_result.basic_notes_updates``——
    这是**临时手段**，阶段三（L3）落地后连同本节点一起删除。

    解不动 ``result1`` 时原样返回并置 ``overwrite_applied=false``，不抛异常：
    一次成功的运行里 notes 为空，与整案失败，是完全不同的两件事，前者要能被看见。
    """
    try:
        updates = json.loads(notes_updates_json) if isinstance(notes_updates_json, str) else notes_updates_json
    except ValueError:
        updates = []
    if not isinstance(updates, list):
        updates = []

    payload = result1
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {
                "result1": result1,
                "overwrite_applied": False,
                "notes_count": 0,
                "overwrite_reason": "result1 不是合法 JSON，保持原样",
            }
    if not isinstance(payload, dict):
        return {
            "result1": result1 if isinstance(result1, str) else json.dumps(payload, ensure_ascii=False, default=str),
            "overwrite_applied": False,
            "notes_count": 0,
            "overwrite_reason": "result1 不是对象，保持原样",
        }

    analysis = payload.get("analysis_result")
    if not isinstance(analysis, dict):
        return {
            "result1": json.dumps(payload, ensure_ascii=False, default=str),
            "overwrite_applied": False,
            "notes_count": 0,
            "overwrite_reason": "result1 缺少 analysis_result，保持原样",
        }

    analysis["basic_notes_updates"] = updates
    return {
        "result1": json.dumps(payload, ensure_ascii=False, default=str),
        "overwrite_applied": True,
        "notes_count": len(updates),
        "overwrite_reason": "",
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
            inputs={"@output": (extract_node, "auc_result")},
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
            outputs=("auc_result",),
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
        _tool(MOMENTS, "朋友圈信息提取", "moments_info", "WeChatMomentsScreenshot"),
        _tool(
            WECHAT_SEARCH,
            "微信手机号搜索截图信息提取",
            "wechat_search_homepage",
            "WeChatPhoneSearchScreenshot",
        ),
        _tool(WECHAT_HOME, "微信主页信息提取", "wechat_homepage", "WeChatHomepageScreenshot"),
        _tool(TEST_DRIVE, "试驾录音信息提取", "test_drive_audio", "TestDriveRecordingFile"),
        _tool(
            OUTBOUND_CALL,
            "外呼录音信息提取",
            "outbound_call_audio",
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
        make_node(
            EVIDENCE_INPUT,
            "取证输入包",
            "code",
            after=(L0_RECORDS, L4_TRIAGE),
            inputs={
                "records_json": (L0_RECORDS, "records_json"),
                "tier": (L4_TRIAGE, "tier"),
            },
            outputs=("output",),
            function=f"customer_profile.workflows.{SLUG}:build_evidence_input",
        ),
        # ---------------------------------------------------------- L1 取证层（1 次 LLM + 代码定坐标）
        make_node(
            L1_INPUT,
            "L1 取证输入装配",
            "code",
            after=(L0_RECORDS,),
            inputs={"records_json": (L0_RECORDS, "records_json")},
            outputs=("output", "section_count", "chars"),
            function=f"customer_profile.workflows.{SLUG}:build_l1_input",
        ),
        make_node(
            L1_LLM,
            "L1 取证层",
            "llm",
            after=(L1_INPUT,),
            inputs={"arg1": (L1_INPUT, "output")},
            outputs=("result",),
            template=L1_PROMPT_TEMPLATE,
            output="result",
            expect_json=True,
            system_text=SYSTEM_TEXT,
        ),
        make_node(
            L1_LOCALIZE,
            "L1 代码定坐标",
            "code",
            after=(L1_LLM,),
            inputs={
                "records_json": (L0_RECORDS, "records_json"),
                "claims_json": (L1_LLM, "result"),
            },
            outputs=("claims_json", "kept", "dropped", "dropped_detail", "parse_ok"),
            function=f"customer_profile.workflows.{SLUG}:localize_claims",
        ),
        # ---------------------------------------------------------- L2 投影层（纯代码，0 token）
        make_node(
            L2_PROJECT,
            "L2 投影层",
            "code",
            after=(L1_LOCALIZE, L4_TRIAGE),
            inputs={
                "claims_json": (L1_LOCALIZE, "claims_json"),
                "tier": (L4_TRIAGE, "tier"),
            },
            outputs=(
                "facts_json",
                "notes_updates_json",
                "to_confirm_json",
                "rejected_json",
                "notes_count",
                "tier",
            ),
            function=f"customer_profile.workflows.{SLUG}:project_claims",
        ),
        # ---------------------------------------------------------- 取证与成文（复用）
        make_node(
            EVIDENCE_LLM,
            "证据线索整理",
            "llm",
            after=(EVIDENCE_INPUT,),
            inputs={"arg1": (EVIDENCE_INPUT, "output")},
            outputs=("result",),
            template=EVIDENCE_PROMPT_TEMPLATE,
            output="result",
            system_text=SYSTEM_TEXT,
        ),
        make_node(
            STANDARD_AGGREGATE,
            "客户标准信息数据聚合",
            "code",
            after=(WECHAT_SEARCH, MENGSHI_IT),
            inputs={
                "json_string_1": (WECHAT_SEARCH, "result"),
                "json_string_2": (MENGSHI_IT, "IT_json_data"),
            },
            outputs=("merged_json_string",),
            function=f"{EVIDENCE_MODULE}:aggregate_customer_standard_info",
        ),
        make_node(
            PROFILE,
            "人设特征判断",
            "tool",
            after=(EVIDENCE_LLM,),
            inputs={
                "evidence_items": (EVIDENCE_LLM, "result"),
                "phone_number": (START, "phone_number"),
            },
            outputs=("profile_result", "hobby_result", "style_result"),
            **{
                "@workflow": PROFILE_WORKFLOW,
                "@inputs": {
                    "evidence_items": "evidence_items",
                    "phone_number": "phone_number",
                },
            },
        ),
        make_node(
            PRODUCTION,
            "画像内容生成与回写",
            "tool",
            after=(PROFILE, STANDARD_AGGREGATE),
            inputs={
                "phone_number": (START, "phone_number"),
                "batch_id": (START, "batch_id"),
                "evidence_items": (EVIDENCE_LLM, "result"),
                "original_cus_data": (STANDARD_AGGREGATE, "merged_json_string"),
                "profile_analysis": (PROFILE, "profile_result"),
                "hobby_analysis": (PROFILE, "hobby_result"),
                "consumption_style_analysis": (PROFILE, "style_result"),
            },
            outputs=("result1",),
            **{
                "@workflow": PRODUCTION_WORKFLOW,
                "@inputs": {
                    "phone_number": "phone_number",
                    "batch_id": "batch_id",
                    "evidence_items": "evidence_items",
                    "original_cus_data": "original_cus_data",
                    "profile_analysis": "profile_analysis",
                    "hobby_analysis": "hobby_analysis",
                    "consumption_style_analysis": "consumption_style_analysis",
                },
            },
        ),
        # ---------------------------------------------------------- T5 兼容覆写（阶段三删除）
        make_node(
            NOTES_OVERWRITE,
            "Notes 兼容覆写",
            "code",
            after=(PRODUCTION, L2_PROJECT),
            inputs={
                "result1": (PRODUCTION, "result1"),
                "notes_updates_json": (L2_PROJECT, "notes_updates_json"),
            },
            outputs=("result1", "overwrite_applied", "notes_count", "overwrite_reason"),
            function=f"customer_profile.workflows.{SLUG}:overwrite_notes_updates",
        ),
        make_node(
            END,
            "输出",
            "end",
            after=(NOTES_OVERWRITE,),
            inputs={"result1": (NOTES_OVERWRITE, "result1")},
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
    EVIDENCE_INPUT: {"main": "build_evidence_input", "renames": {}},
    L1_INPUT: {"main": "build_l1_input", "renames": {}},
    L1_LOCALIZE: {"main": "localize_claims", "renames": {}},
    L2_PROJECT: {"main": "project_claims", "renames": {}},
    NOTES_OVERWRITE: {"main": "overwrite_notes_updates", "renames": {}},
}
