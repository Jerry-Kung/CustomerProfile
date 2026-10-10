"""事实账本管线（L0 记录层 / L4 分诊层）的语义回归。

装置口径与 `tests/conftest.py` 一致：**不出网、不依赖批次产物、干净检出上全绿**。
批次产物的离线回归在 `scripts/probe_pipeline_l0.py`（那里读 `experiments/`，被 gitignore），
本文件用合成夹具断言同一套语义——两者互补，缺一不可。

覆盖的是**判定**而非**编排**：渠道该记 ok 还是 exception、录音原文有没有被压缩产物顶掉、
空壳与哨兵有没有被当内容。这些都是纯代码，出错不会报错、只会悄悄污染下游，
因此每条都要有用例钉住。
"""

from __future__ import annotations

import json

from customer_profile.workflows import customer_profile_pipeline as pipe
from customer_profile.workflows import evidence_subagent


# ---------------------------------------------------------------- 夹具

def manifest(*entries: dict) -> str:
    """构造入口清单响应体（`GET .../history/{phone}` 的 `$text`）。"""
    return json.dumps(list(entries), ensure_ascii=False)


def entry(channel: str, *, created_at: str = "2026-09-21T11:27:16", media=None, raw=None):
    return {
        "channel": channel,
        "created_at": created_at,
        "media_urls": media,
        "raw_payload": raw,
    }


def build(body: str, **channels) -> list[dict]:
    """驱动 L0，返回记录列表。"""
    bundle = pipe.bundle_channels(history_body=body, **channels)
    built = pipe.build_records(body, bundle["channels_json"], "13500000000")
    return json.loads(built["records_json"])


def record_for(records: list[dict], source_name: str) -> dict:
    return next(r for r in records if r["source_name"] == source_name)


# ---------------------------------------------------------------- 渠道表

def _acquisition_tools(workflow) -> dict:
    """某图里「取数工具」的声明表：``{节点标题: (子流程 ID, data_source)}``。

    排除迭代体内的 ``audio_content_extract`` 调用点——那是录音原文通道，不在
    ``evidence_subagent`` 的 14 路里，两边本就不同。
    """
    table: dict = {}
    for node in workflow.nodes:
        if node.node_type != "tool":
            continue
        child = node.config.get("@workflow")
        if child == pipe.AUDIO_EXTRACT_WORKFLOW:
            continue
        data_source = next(
            (
                value[len(pipe.LITERAL):]
                for value in (node.config.get("@inputs") or {}).values()
                if isinstance(value, str) and value.startswith(pipe.LITERAL)
            ),
            None,
        )
        table[node.title] = (child, data_source)
    return table


def test_acquisition_layer_matches_subagent_declarations():
    """实验臂的取数层与取数臂的 14 路声明必须逐条一致。

    实验臂的取数层是从 `evidence_subagent` 复刻来的（D2：不修改那个模块，代价就是重复
    一份声明）。重复的东西最容易单边漂移——谁日后只改了取数臂的某一路子流程 ID 或渠道名，
    这里先红，而不是等到跑批时才发现两臂的取数口径已经不同。

    比对的是「子流程 ID + data_source」这一对，而不是渠道名集合：`UserFeedbackInformation`
    等渠道走 mhero（`/data/profile/...`），不在入口清单里，因此不在记录层的
    `manifest_channel` 集合中——按渠道名集合比对会把这个**正确的**设计判成缺失。
    """
    pipeline_tools = _acquisition_tools(pipe.WORKFLOW)
    subagent_tools = _acquisition_tools(evidence_subagent.WORKFLOW)
    assert len(subagent_tools) == 14
    # 断言「取数臂声明的每一路，在实验臂里逐字一致」。不要求相等：实验臂还多出
    # 人设特征判断与画像成文两个 tool 调用，它们不是取数层，两边本就不同类。
    for title, declaration in subagent_tools.items():
        assert pipeline_tools.get(title) == declaration, (
            f"渠道 {title!r} 的取数声明在两臂之间不一致："
            f"取数臂={declaration}，实验臂={pipeline_tools.get(title)}"
        )


def test_channel_table_has_15_entries():
    """15 个取证渠道（`端到端方案.md` §2 的渠道清单）。"""
    assert len(pipe.CHANNELS) == 15
    assert len({c["source_name"] for c in pipe.CHANNELS}) == 15


# ---------------------------------------------------------------- 判据一：清单定「有没有」

def test_absent_channel_is_exception():
    """清单里没有该 channel ⇒ exception。这是「输入有没有录音」的唯一事实来源。"""
    records = build(manifest(entry("WeChatPhoneSearchScreenshot")))
    for source in ("朋友圈截图内容信息", "外呼录音信息", "试驾录音信息", "支付宝主页截图信息"):
        assert record_for(records, source)["status"] == pipe.EXCEPTION


def test_absent_channel_is_exception_even_when_producer_returns_text():
    """清单没有它，哪怕子流程回了文本，也记 exception。

    这正是「输入有 256 字符外呼转写、输出却说『无外呼录音』」的反面：
    有没有由清单说了算，不由模型回忆、也不由下游那句提示语说了算。
    """
    records = build(
        manifest(entry("WeChatPhoneSearchScreenshot")),
        outbound_report="**本次通话整体分析**\n\n- 无法分析。录音文件无有效内容。",
    )
    assert record_for(records, "外呼录音信息")["status"] == pipe.EXCEPTION


def test_manifest_present_marks_ok_and_inherits_created_at():
    records = build(
        manifest(entry("WeChatPhoneSearchScreenshot", created_at="2026-09-21T15:53:28")),
        wechat_search='{"nickname": "清欢"}',
    )
    record = record_for(records, "微信手机号搜索界面信息")
    assert record["status"] == pipe.OK
    assert record["created_at"] == "2026-09-21T15:53:28"
    assert record["channel"] == "WeChatPhoneSearchScreenshot"


def test_raw_payload_is_used_when_producer_text_is_a_sentinel():
    """媒体渠道正文常在清单 `raw_payload` 里，而子流程只回一句「查询异常」。

    此时若以哨兵文本为正文，一条真实记录会被记成「有内容但内容是提示语」。
    """
    records = build(
        manifest(entry("RelatedEnterpriseInfo", raw='{"company": "某某电力局"}')),
        phone_company="公司数据查询异常",
    )
    assert record_for(records, "关联企业信息")["status"] == pipe.OK
    assert "某某电力局" in record_for(records, "关联企业信息")["content"]


# ---------------------------------------------------------------- 判据二：原文逐字留存

def test_transcript_is_kept_verbatim_from_auc_result():
    transcript = json.dumps(
        [{"speaker": "1", "gender": "male", "text": "喂，你好。"}], ensure_ascii=False
    )
    records = build(
        manifest(entry("AIOutboundCallRecordingFile")),
        outbound_report="无法分析。录音文件无有效内容。",
        outbound_transcript=[transcript],
    )
    record = record_for(records, "外呼录音信息")
    assert record["status"] == pipe.OK
    assert record["content_kind"] == "transcript"
    assert "喂，你好。" in record["content"]


def test_compressed_report_never_overrides_transcript():
    """有原文时，压缩报告不得顶替原文。"""
    transcript = json.dumps([{"text": "原文原话"}], ensure_ascii=False)
    records = build(
        manifest(entry("AIOutboundCallRecordingFile")),
        outbound_report="这是经过 LLM 压缩的报告正文",
        outbound_transcript=[transcript],
    )
    record = record_for(records, "外呼录音信息")
    assert "原文原话" in record["content"]
    assert "压缩的报告" not in record["content"]


def test_transcript_falls_back_to_report_and_is_labelled():
    """原文缺失时才回落报告，且 `content_kind` 明确标成 report。"""
    records = build(
        manifest(entry("AIOutboundCallRecordingFile")),
        outbound_report="这是经过 LLM 压缩的报告正文",
        outbound_transcript=None,
    )
    record = record_for(records, "外呼录音信息")
    assert record["status"] == pipe.OK
    assert record["content_kind"] == "report"


def test_empty_audio_array_is_not_content():
    """`auc_result` 为 `"[]"` 表示没识别出内容，不是一段叫 `[]` 的原文。"""
    records = build(
        manifest(entry("AIOutboundCallRecordingFile")),
        outbound_transcript=["[]"],
    )
    assert record_for(records, "外呼录音信息")["status"] == pipe.EXCEPTION


def test_empty_flattened_item_is_dropped_but_other_files_kept():
    """一个录音文件无结果、另一个有：只有后者的原文进记录。"""
    good = json.dumps([{"text": "第二段录音的原话"}], ensure_ascii=False)
    records = build(
        manifest(entry("TestDriveRecordingFile", media=["a.mp3", "b.mp3"])),
        test_drive_transcript=["[]", good],
    )
    record = record_for(records, "试驾录音信息")
    assert record["status"] == pipe.OK
    assert "第二段录音的原话" in record["content"]
    assert record["content"].count("[]") == 0


# ---------------------------------------------------------------- 空壳与哨兵

def test_empty_locked_notes_is_exception():
    """`{"locked_notes": []}` 有形状无事实，不得记 ok 混进取证包。"""
    records = build(
        manifest(entry("WeChatPhoneSearchScreenshot")),
        locked_notes='{"locked_notes": []}',
    )
    assert record_for(records, "销售人员人工确认的客户Notes信息")["status"] == pipe.EXCEPTION


def test_non_empty_locked_notes_is_ok():
    records = build(
        manifest(entry("WeChatPhoneSearchScreenshot")),
        locked_notes='{"locked_notes": [{"note": "客户姓王"}]}',
    )
    assert record_for(records, "销售人员人工确认的客户Notes信息")["status"] == pipe.OK


def test_sentinel_feedback_is_exception():
    records = build(
        manifest(entry("WeChatPhoneSearchScreenshot")),
        feedback="该用户不存在人工反馈数据",
    )
    assert record_for(records, "销售人员对当前客户历史画像数据的反馈记录")["status"] == pipe.EXCEPTION


def test_long_real_text_that_mentions_nothing_is_not_mistaken_for_sentinel():
    """只有短提示语才按哨兵处理，长正文即使含「不存在」也照收。"""
    long_text = "客户表示目前不考虑换车，因为家里那台车还很新。" * 10
    records = build(
        manifest(entry("WeChatPhoneSearchScreenshot")),
        wechat_search=long_text,
    )
    assert record_for(records, "微信手机号搜索界面信息")["status"] == pipe.OK


# ---------------------------------------------------------------- 判据三：分诊

def test_triage_sufficient_when_primary_interaction_present():
    records = build(
        manifest(entry("AIOutboundCallRecordingFile")),
        outbound_transcript=[json.dumps([{"text": "喂"}], ensure_ascii=False)],
    )
    tier = pipe.triage(json.dumps(records, ensure_ascii=False))
    assert tier["tier"] == "sufficient"


def test_triage_thin_when_only_declared_channels():
    records = build(
        manifest(entry("WeChatPhoneSearchScreenshot")),
        wechat_search='{"nickname": "清欢"}',
    )
    tier = pipe.triage(json.dumps(records, ensure_ascii=False))
    assert tier["tier"] == "thin"


def test_triage_empty_when_nothing_usable():
    records = build(manifest(entry("WeChatPhoneSearchScreenshot")))
    tier = pipe.triage(json.dumps(records, ensure_ascii=False))
    assert tier["tier"] == "empty"


# ---------------------------------------------------------------- 取证输入包

def test_evidence_input_excludes_empty_channels():
    """取证包只放有内容的渠道；结构性空渠道在记录层留痕，不进上下文。"""
    records = build(
        manifest(entry("WeChatPhoneSearchScreenshot")),
        wechat_search='{"nickname": "清欢"}',
    )
    package = json.loads(
        pipe.build_evidence_input(json.dumps(records, ensure_ascii=False), "thin")["output"]
    )
    assert set(package) == {"微信手机号搜索界面信息"}
    assert "朋友圈截图内容信息" not in package


def test_evidence_input_prefers_transcript_over_report():
    records = build(
        manifest(entry("AIOutboundCallRecordingFile")),
        outbound_report="压缩报告",
        outbound_transcript=[json.dumps([{"text": "逐字原话"}], ensure_ascii=False)],
    )
    package = json.loads(
        pipe.build_evidence_input(json.dumps(records, ensure_ascii=False), "sufficient")["output"]
    )
    assert "逐字原话" in package["外呼录音信息"]


def test_records_json_is_pure_utf8_json():
    """记录层产出必须能被 `json.loads` 直接吃下（下游全线按 JSON 解析）。"""
    records = build(
        manifest(entry("WeChatPhoneSearchScreenshot")),
        wechat_search='{"nickname": "清欢🌲"}',
    )
    assert isinstance(json.loads(json.dumps(records, ensure_ascii=False)), list)
