"""M2M 本体渠道接入（2026-10-31 接口改版）的回归。

覆盖四件在真实接口上会**静默出错**的事：

- 本体渠道的 ``raw_payload`` 没有被渲染成正文（记录层拿到一段英文键名 JSON）；
- ``removed`` 记录被当成证据（记录已不存在，却以 ``status=ok`` 进入提示词）；
- 同一渠道的多条记录被压成一条（三次跟进只剩最后一次）；
- ``fetched_at`` 丢失（增量更新无法判断「这份资料是哪一轮拉的」）。

夹具用接口规范里的真实响应片段，不构造理想载荷——与 ``test_phone_company_analysis``
同一条理由：理想载荷验不出真实形状的坑。
"""

from __future__ import annotations

import json

import pytest

from customer_profile.workflows import customer_profile_pipeline as pipe
from customer_profile.workflows import ontology_payload


# ---------------------------------------------------------------- 夹具

def manifest(*entries: dict) -> str:
    return json.dumps(list(entries), ensure_ascii=False)


def entry(channel: str, raw=None, created_at: str = "2026-10-31T11:28:02", media=None) -> dict:
    return {
        "channel": channel,
        "created_at": created_at,
        "media_urls": media,
        "raw_payload": raw,
    }


def build(body: str, **channels) -> list[dict]:
    bundle = pipe.bundle_channels(history_body=body, **channels)
    built = pipe.build_records(body, bundle["channels_json"], "13720203537")
    return json.loads(built["records_json"])


def record_for(records: list[dict], source_name: str) -> dict:
    return next(r for r in records if r["source_name"] == source_name)


LEAD_INFO = {
    "records": [
        {
            "customer_phone": "13720203537",
            "source_id": "4335706",
            "nick": "老女士",
            "stage": 1,
            "leads_status": 100414,
            "lead_rating": "C",
            "vehicle_model_name": "Ultra",
            "vehicle_series_names": ["X700"],
        }
    ],
    "fetched_at": "2026-10-31T11:28:02.376050",
}

FOLLOW_ONE = {
    "customer_phone": "13720203537",
    "source_id": "f1",
    "source_kind": "follow",
    "source_version": 1,
    "followcontent": "说是来看车的，有点落眼子",
    "intendedlevel": 90464,
    "vehiclemodelname": "Ultra",
    "createtime": "2026-10-31 11:21:53",
}

FOLLOW_TWO = dict(FOLLOW_ONE, source_id="f2", source_version=2, followcontent="下周再约一次到店试驾")


# ---------------------------------------------------------------- 渲染

def test_base_channel_records_are_rendered_with_chinese_labels():
    """基础渠道的 ``records`` 要被展开渲染，不能被序列化成一坨英文 JSON。"""
    records = build(manifest(entry("OntologyLeadInfo", raw=LEAD_INFO)))
    record = record_for(records, "客户线索基础资料")

    assert record["status"] == pipe.OK
    assert "昵称：老女士" in record["content"]
    assert "线索状态：首次跟进" in record["content"]
    assert "阶段：线索" in record["content"]
    # 数组值要展开成可读列表，而不是 Python 表示法
    assert "意向车系列表：X700" in record["content"]
    assert "'X700'" not in record["content"]


def test_enum_codes_are_translated_not_left_raw():
    """字典码要译成中文——锚点里带 ``100414`` 模型会把它抄进 Notes。"""
    records = build(manifest(entry("OntologyLeadInfo", raw=LEAD_INFO)))
    content = record_for(records, "客户线索基础资料")["content"]
    assert "100414" not in content


def test_order_amounts_are_converted_from_fen():
    """金额字段接口单位是分；渲染时换成元并标单位，避免与「万元」话术混用。"""
    order = {
        "source_id": "100265131136131072",
        "total_price_fen": 374900,
        "actual_price_fen": 350104,
        "amount_unit": "fen",
        "source_kind": "order",
        "customer_phone": "13797092818",
    }
    records = build(manifest(entry("OntologyOrder", raw=order)))
    content = record_for(records, "订单记录")["content"]
    assert "订单总价：3,749.00 元" in content
    assert "374900" not in content


def test_meta_keys_are_not_rendered_as_facts():
    """``source_kind`` / ``amount_unit`` / ``fetched_at`` 是接口自述，不是客户事实。"""
    records = build(manifest(entry("OntologyOrder", raw={"source_kind": "order", "amount_unit": "fen"})))
    record = record_for(records, "订单记录")
    assert record["status"] == pipe.EXCEPTION
    assert record["content"] == ""


# ---------------------------------------------------------------- removed

def test_removed_record_is_not_treated_as_content():
    """``operation=removed`` 表示业务系统已删除该条，不得当证据。"""
    removed = {"operation": "removed", "reason": "upstream_removed_or_reassigned"}
    records = build(manifest(entry("OntologyDefeatRecord", raw=removed)))
    record = record_for(records, "战败记录")

    assert record["status"] == pipe.EXCEPTION
    assert record["content"] == ""
    assert "upstream_removed" not in record["content"]


def test_removed_record_does_not_displace_a_valid_one():
    """同一渠道里有一条被移除、一条有效时，有效的必须留下。"""
    removed = {"operation": "removed", "reason": "upstream_removed_or_reassigned"}
    records = build(
        manifest(
            entry("OntologyLeadFollowRecord", raw=removed, created_at="2026-10-31T10:00:00"),
            entry("OntologyLeadFollowRecord", raw=FOLLOW_ONE, created_at="2026-10-31T11:00:00"),
        )
    )
    follow = [r for r in records if r["source_name"] == "销售跟进记录"]
    assert len(follow) == 1
    assert follow[0]["status"] == pipe.OK
    assert "有点落眼子" in follow[0]["content"]


# ---------------------------------------------------------------- 多条记录

def test_process_channel_keeps_every_record():
    """过程资料每条都是独立事实：两次跟进要两条记录，不能只剩最新一条。"""
    records = build(
        manifest(
            entry("OntologyLeadFollowRecord", raw=FOLLOW_ONE, created_at="2026-10-30T10:00:00"),
            entry("OntologyLeadFollowRecord", raw=FOLLOW_TWO, created_at="2026-10-31T10:00:00"),
        )
    )
    follow = [r for r in records if r["source_name"] == "销售跟进记录"]

    assert len(follow) == 2
    assert len({r["record_id"] for r in follow}) == 2, "record_id 必须唯一，否则定位层会互相覆盖"
    joined = "\n".join(r["content"] for r in follow)
    assert "有点落眼子" in joined
    assert "下周再约一次到店试驾" in joined


def test_base_channel_keeps_only_latest_snapshot():
    """基础渠道是**全量快照**：多轮之间取最新一轮，不按批次重复计数。"""
    records = build(
        manifest(
            entry("OntologyLeadInfo", raw=LEAD_INFO, created_at="2026-10-30T10:00:00"),
            entry("OntologyLeadInfo", raw=LEAD_INFO, created_at="2026-10-31T10:00:00"),
        )
    )
    lead = [r for r in records if r["source_name"] == "客户线索基础资料"]
    assert len(lead) == 1
    assert lead[0]["created_at"] == "2026-10-31T10:00:00"


def test_absent_ontology_channel_is_exception():
    """清单里没有的本体渠道要留痕成 exception，而不是静默消失。"""
    records = build(manifest(entry("OntologyLeadInfo", raw=LEAD_INFO)))
    assert record_for(records, "订单记录")["status"] == pipe.EXCEPTION


# ---------------------------------------------------------------- fetched_at

def test_fetched_at_is_taken_from_raw_payload():
    """基础渠道的抓取时刻取自 ``raw_payload.fetched_at``，与落库时刻分开。"""
    records = build(manifest(entry("OntologyLeadInfo", raw=LEAD_INFO)))
    record = record_for(records, "客户线索基础资料")

    assert record["fetched_at"] == "2026-10-31T11:28:02.376050"
    assert record["created_at"] == "2026-10-31T11:28:02"


def test_fetched_at_falls_back_to_created_at():
    """过程资料没有 ``fetched_at``：回落 ``created_at``，字段恒有值。"""
    records = build(manifest(entry("OntologyLeadFollowRecord", raw=FOLLOW_ONE)))
    record = record_for(records, "销售跟进记录")
    assert record["fetched_at"] == "2026-10-31T11:28:02"


def test_fetched_at_reaches_the_l1_section_header():
    """L1 分节头用采集时间提示资料新旧；落库时刻不同时另起一行。"""
    records = build(manifest(entry("OntologyLeadInfo", raw=LEAD_INFO)))
    built = pipe.build_l1_input(json.dumps(records, ensure_ascii=False))
    output = built["output"]

    assert "采集时间：2026-10-31T11:28:02.376050" in output
    assert "记录时间：2026-10-31T11:28:02" in output


# ---------------------------------------------------------------- 旧结构兼容

def test_nested_legacy_payload_is_still_readable():
    """部署版本早于扁平化提交时，基础渠道可能仍是嵌套结构；不得直接崩。"""
    legacy = {
        "records": [
            {
                "customers": {"customer_phone": "13720203537", "customer_nick": "老女士"},
                "stores": {"store_code": "MSCHUB023"},
            }
        ]
    }
    rendered = ontology_payload.render_payload("OntologyLeadInfo", legacy)
    assert "customer_nick" in rendered or "老女士" in rendered


@pytest.mark.parametrize("payload", [None, {}, [], "", {"records": []}])
def test_empty_payloads_render_to_empty_string(payload):
    """空载荷一律渲染成空串，由记录层记 exception——不抛异常。"""
    assert ontology_payload.render_payload("OntologyLeadInfo", payload) == ""


def test_recording_url_fallback():
    """录音渠道只给 ``raw_payload.recording_url`` 时也能取到直链。"""
    body = manifest(
        entry(
            "OntologyManualOutboundCallRecordingFile",
            raw={"recording_id": "1", "recording_url": "https://example.com/call.mp3"},
        )
    )
    files = pipe.collect_outbound_files(body)
    assert files["files"] == ["https://example.com/call.mp3"]


# ---------------------------------------------------------------- 码值翻译（解码说明 2026-11-02）

def test_follow_record_codes_are_translated():
    """跟进四字段的字典码按解码说明 §3 译成中文；裸码进提示词等于该字段白传。"""
    raw = {
        "source_id": "f1",
        "source_kind": "follow",
        "followresult": 90452,
        "followpurpose": 90432,
        "intendedlevel": 90462,
        "communicationmethods": 90444,
    }
    content = record_for(build(manifest(entry("OntologyLeadFollowRecord", raw=raw))), "销售跟进记录")["content"]

    assert "跟进结果：跟进目的达成" in content
    assert "跟进目的：邀请试驾" in content
    assert "意向级别：A 级意向（高）" in content
    assert "沟通方式：上门" in content
    for code in ("90452", "90432", "90462", "90444"):
        assert code not in content


def test_test_drive_codes_are_translated():
    """试驾四字段按解码说明 §4 译成中文。"""
    raw = {
        "source_id": "t1",
        "source_kind": "test_drive",
        "test_drive_type": "1",
        "test_drive_class": "1",
        "order_status": "5",
        "record_type": "1",
    }
    content = record_for(build(manifest(entry("OntologyTestDriveSchedule", raw=raw))), "试驾排程记录")["content"]

    assert "试驾方式：上门试驾" in content
    assert "试驾类别：邀约试驾" in content
    assert "试驾单状态：已完成" in content
    assert "录音方式：App 录音" in content


def test_order_status_4_is_not_labelled_completed():
    """状态 4 是「已试驾未闭环」，与 5「已完成」必须分开（解码说明 §4.3 的口径风险）。"""
    raw = {"source_id": "t2", "source_kind": "test_drive", "order_status": "4"}
    content = record_for(build(manifest(entry("OntologyTestDriveSchedule", raw=raw))), "试驾排程记录")["content"]
    assert "已完成" not in content
    assert "已试驾·未完成回访" in content


def test_same_key_does_not_share_a_table_across_channels():
    """``record_type`` / ``status`` 在不同渠道含义不同，不得串味。

    试驾排程的 ``record_type`` 是录音方式、``status`` 是试驾记录状态；聊天记录的
    ``record_type`` 是来源平台。按字段名分发的实现会在这里出错。
    """
    assert ontology_payload._enum_label("OntologyTestDriveSchedule", "record_type", "1") == "App 录音"
    assert ontology_payload._enum_label("OntologyCustomerChatInfo", "record_type", "1") is None
    assert ontology_payload._enum_label("OntologyTestDriveSchedule", "status", "1") is None
    assert ontology_payload._enum_label("OntologyLeadInfo", "status", "1") == "启用"


def test_undocumented_code_falls_back_to_raw_value():
    """解码说明未定义的码原样输出，不猜、不丢。"""
    assert ontology_payload._enum_label("OntologyTestDriveSchedule", "order_status", "3") is None
    assert ontology_payload._enum_label("OntologyTestDriveSchedule", "test_drive_class", "3") is None

    raw = {"source_id": "t3", "source_kind": "test_drive", "test_drive_class": "3"}
    content = record_for(build(manifest(entry("OntologyTestDriveSchedule", raw=raw))), "试驾排程记录")["content"]
    assert "试驾类别：3" in content


def test_lead_enabled_status_is_translated():
    """线索启用状态是平台属性（0/1/2/-1），不是线索阶段字典。"""
    payload = {"records": [dict(LEAD_INFO["records"][0], status=1)], "fetched_at": LEAD_INFO["fetched_at"]}
    content = record_for(build(manifest(entry("OntologyLeadInfo", raw=payload))), "客户线索基础资料")["content"]

    assert "线索启用状态：启用" in content
    # 同为 status=1，在线索是「启用」；在试驾排程是「试驾记录状态」，不得串用这张表
    assert "线索状态：首次跟进" in content


# ---------------------------------------------------------------- 补充说明 2026-11-02 的码表

def test_lead_rating_shares_the_seven_level_scale():
    """线索评级与意向级别是同一套七档字母；``O`` 是原生档位不是「其他」。"""
    for code, label in (("H", "H 级（最高）"), ("O", "O 级（较低）"), ("L", "L 级（最冷）")):
        assert ontology_payload._format_value("OntologyLeadInfo", "lead_rating", code) == label
    assert ontology_payload._format_value("OntologyLeadInfo", "lead_rating", "无") == "无（尚未评级）"


def test_lead_rating_and_intended_level_are_separate_fields():
    """两者语义不同：前者是主数据当前评级，后者是单次跟进快照，不得互为兜底。"""
    assert ontology_payload._format_value("OntologyLeadInfo", "lead_rating", "A") == "A 级（高）"
    assert ontology_payload._format_value(
        "OntologyLeadFollowRecord", "intendedlevel", 90462
    ) == "A 级意向（高）"


def test_operation_type_table_is_shared_between_distribution_and_assignment():
    """下发与分配是同一张物理表、同一套枚举，只是本体拆成了两个对象。"""
    for channel in ("OntologyLeadDistribution", "OntologyLeadAssignment"):
        assert ontology_payload._format_value(channel, "operation_type", "1") == "下发"
        assert ontology_payload._format_value(channel, "operation_type", "4") == "回收"


def test_boolean_flags_render_as_yes_no():
    """``first_flag`` / ``changebusinessopportunity`` 本体类型是 Boolean，不是多值枚举。"""
    assert ontology_payload._format_value("OntologyLeadDistribution", "first_flag", "1") == "是"
    assert ontology_payload._format_value("OntologyLeadAssignment", "first_flag", "1") == "是"
    assert ontology_payload._format_value("OntologyLeadFollowRecord", "changebusinessopportunity", 0) == "否"


def test_finance_channel_reuses_order_status_dictionary():
    """实测同一订单在两路渠道里的 ``order_status`` 同值，金融方案要复用订单状态字典。"""
    assert ontology_payload._format_value("OntologyOrderFinance", "order_status", "670") == "车辆总装"
    assert ontology_payload._format_value("OntologyOrder", "order_status", "670") == "车辆总装"
    # 但金融方案没有订单类型/付款方式这些键，不能跟着订单记录一起放进同一张表
    assert ontology_payload._enum_label("OntologyOrderFinance", "order_type", "10") is None


def test_order_status_does_not_collide_with_test_drive_order_status():
    """订单状态是三位数（9318），试驾单状态是 0–6 内置枚举——同名不同字典。"""
    assert ontology_payload._format_value("OntologyOrder", "order_status", "900") == "订单已交付"
    assert ontology_payload._format_value("OntologyTestDriveSchedule", "order_status", "900") == "900"
    assert ontology_payload._format_value("OntologyTestDriveSchedule", "order_status", "5") == "已完成"


def test_test_drive_source_uses_dictionary_not_the_four_item_description():
    """本体描述只写了 4 项，字典有 6 项；按描述映射会让 5/6 退化成裸码。"""
    assert ontology_payload._format_value("OntologyTestDriveSchedule", "test_drive_source", "5") == "乾崑 app"
    assert ontology_payload._format_value("OntologyTestDriveSchedule", "test_drive_source", "6") == "总部"


def test_new_intended_level_code_is_temporarily_mapped_to_f():
    """``904606`` 字典未登记，按业务可用性优先暂转写为 F，并标明待确认。"""
    label = ontology_payload._format_value("OntologyLeadFollowRecord", "intendedlevel", 904606)
    assert "F" in label
    assert "待确认" in label


def test_undocumented_intended_level_codes_still_fall_back():
    """其余未登记六位码不跟着 F 走，仍原样输出。"""
    for code in (904602, 904603, 904604, 0):
        assert ontology_payload._format_value("OntologyLeadFollowRecord", "intendedlevel", code) == str(code)


def test_constant_valued_fields_are_not_rendered():
    """取值恒定的字段渲染出来只会污染锚点：外呼 ``call_status``、试驾 ``status``。"""
    call = ontology_payload.render_record(
        "OntologyManualOutboundCall", {"source_id": "1", "call_status": "1", "call_duration": "22"}
    )
    assert "呼叫状态" not in call
    assert "通话时长（秒）：22" in call

    drive = ontology_payload.render_record(
        "OntologyTestDriveSchedule", {"source_id": "1", "status": "1", "order_status": "5"}
    )
    assert "试驾记录状态" not in drive
    assert "试驾单状态：已完成" in drive


def test_skip_keys_are_per_channel():
    """``status`` 只在该渠道跳过；线索的启用状态仍要渲染。"""
    lead = ontology_payload.render_record("OntologyLeadInfo", {"source_id": "1", "status": "1"})
    assert "线索启用状态：启用" in lead


def test_order_count_label_says_direction_not_quantity():
    """``order_count`` 是正向/负向标记，不是数量；标签与取值都不能写成数字。"""
    positive = ontology_payload.render_record("OntologyOrder", {"order_count": 1})
    assert "订单方向：正向" in positive
    assert "订单数量" not in positive

    negative = ontology_payload.render_record("OntologyOrder", {"order_count": -1})
    assert "订单方向：负向（销退冲销）" in negative
