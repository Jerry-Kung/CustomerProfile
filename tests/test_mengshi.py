"""猛士IT系统数据工作流的确定性逻辑测试。

固定输入下的确定性输出是**可以严格比较**的部分（`Dify迁移任务说明.md` §3.2），
因此这里用人工推算的期望值逐字断言，包括两处兜底文案。

期望值不是「跑一遍记下来」的——都是从 DSL 代码读出来的规则手算的。跑完再记录会
把错误固化成基线。
"""

from __future__ import annotations

import json

from customer_profile.workflows import mengshi_it_system_data as mengshi


# ---------------------------------------------------------------- 数据提取


def test_extract_returns_raw_payload_for_matching_channel():
    payload = json.dumps(
        [
            {"channel": "other", "raw_payload": {"a": 1}},
            {"channel": "MengShiITSystemData", "raw_payload": {"b": 2}},
        ],
        ensure_ascii=False,
    )
    result = mengshi.extract_channel_payload(payload, "MengShiITSystemData")
    assert json.loads(result["result"]) == {"b": 2}


def test_extract_returns_string_payload_as_is():
    payload = json.dumps([{"channel": "c", "raw_payload": "已经是字符串"}], ensure_ascii=False)
    assert mengshi.extract_channel_payload(payload, "c")["result"] == "已经是字符串"


def test_extract_uses_ensure_ascii_false():
    """中文不得被转成 ``\\uXXXX``——下游直接展示这段文本。"""
    payload = json.dumps([{"channel": "c", "raw_payload": {"名": "张三"}}], ensure_ascii=False)
    result = mengshi.extract_channel_payload(payload, "c")["result"]
    assert "张三" in result
    assert "\\u" not in result


def test_extract_returns_empty_on_none():
    assert mengshi.extract_channel_payload(None, "c")["result"] == ""


def test_extract_returns_empty_on_blank():
    assert mengshi.extract_channel_payload("   ", "c")["result"] == ""


def test_extract_returns_empty_on_invalid_json():
    assert mengshi.extract_channel_payload("{不是数组}", "c")["result"] == ""


def test_extract_returns_empty_when_toplevel_is_not_list():
    assert mengshi.extract_channel_payload('{"channel": "c"}', "c")["result"] == ""


def test_extract_returns_empty_when_channel_missing():
    payload = json.dumps([{"channel": "x", "raw_payload": {"a": 1}}], ensure_ascii=False)
    assert mengshi.extract_channel_payload(payload, "c")["result"] == ""


def test_extract_returns_empty_when_payload_is_null():
    payload = json.dumps([{"channel": "c", "raw_payload": None}], ensure_ascii=False)
    assert mengshi.extract_channel_payload(payload, "c")["result"] == ""


def test_extract_skips_non_dict_items():
    payload = json.dumps(["字符串", 42, {"channel": "c", "raw_payload": "值"}], ensure_ascii=False)
    assert mengshi.extract_channel_payload(payload, "c")["result"] == "值"


# ---------------------------------------------------------------- 数据整合


FULL_CUSTOMER = {
    "customer_name": "张三",
    "customer_nick": "小张",
    "customer_phone": "13800000001",
    "customer_remark": "",
    "customer_phone_brand": "华为",
    "customer_register_time": "2026-01-02 03:04:05",
    "sales_consultant_account": "sales01",
    "customer_phone_device_model": "Mate 60",
    "customer_address": "武汉市洪山区",
    "customer_channel": "1",
    "customer_wechat_id": "wx_zhangsan",
    "customer_stage": "2",
    "customer_leads_status": "100410",
    "customer_last_follow_content": "已到店",
    "customer_lead_rating": "A",
}


def test_integrate_builds_labelled_text_lines():
    result = mengshi.integrate_customer_data(json.dumps(FULL_CUSTOMER, ensure_ascii=False))
    info = result["system_customer_info"]

    assert info.startswith("客户留存在销售数据系统中的信息记录：\n")
    assert "客户姓名：张三" in info
    assert "客户手机号码：13800000001" in info
    # 枚举码值必须转换
    assert "客户线索渠道：app" in info
    assert "客户线索阶段（线索or商机）：商机" in info
    assert "客户线索状态：跟进中" in info
    # 空字段不产生数据行
    assert "customer_remark" not in info
    assert "我方销售人员手动添加的客户备注信息" not in info


def test_integrate_renames_fields_with_itsystem_prefix():
    result = mengshi.integrate_customer_data(json.dumps(FULL_CUSTOMER, ensure_ascii=False))
    structured = json.loads(result["system_customer_json_data"])

    assert set(structured) == {f"ITsystem_{f}" for f in mengshi.ORIGINAL_FIELDS}
    assert structured["ITsystem_customer_name"] == "张三"
    assert structured["ITsystem_customer_lead_rating"] == "A"


def test_integrate_keeps_null_but_fills_missing_with_empty_string():
    """原字段存在且为 null 时保留 None；字段缺失才补空串。这是 DSL 注释明说的区别。"""
    data = {"customer_name": None, "customer_nick": "小张"}
    result = mengshi.integrate_customer_data(json.dumps(data, ensure_ascii=False))
    structured = json.loads(result["system_customer_json_data"])

    assert structured["ITsystem_customer_name"] is None
    assert structured["ITsystem_customer_nick"] == "小张"
    assert structured["ITsystem_customer_address"] == ""


def test_integrate_marks_unknown_enum_value():
    """未收录的枚举值保留原文并标注未知，不得静默丢弃。"""
    data = {"customer_channel": "999", "customer_leads_status": "999999"}
    result = mengshi.integrate_customer_data(json.dumps(data, ensure_ascii=False))
    info = result["system_customer_info"]

    assert "客户线索渠道：999（未知）" in info
    assert "客户线索状态：999999（未知）" in info


def test_integrate_zero_and_false_are_not_empty():
    """数字 0 与 False 不算空——DSL 里显式写了这条。"""
    data = {"customer_lead_rating": 0}
    result = mengshi.integrate_customer_data(json.dumps(data))
    # INFO_FIELD_LABELS 里该字段的标签是长句，按完整标签断言
    assert (
        "销售顾问对该客户的初始线索评级（ABC三档，A为优质线索，B为一般线索，C为劣质线索）：0"
        in result["system_customer_info"]
    )


def test_integrate_lists_are_serialised_as_json_not_repr():
    data = {"customer_remark": ["a", "b"]}
    result = mengshi.integrate_customer_data(json.dumps(data, ensure_ascii=False))
    assert '["a", "b"]' in result["system_customer_info"]
    assert "['a', 'b']" not in result["system_customer_info"]


def test_integrate_empty_input_uses_fallback_copy():
    for bad in (None, "", "   ", "{不是 JSON}", "[1, 2]"):
        result = mengshi.integrate_customer_data(bad)
        assert result["system_customer_info"] == "客户留存在销售数据系统中的信息记录为空。"
        structured = json.loads(result["system_customer_json_data"])
        assert all(value == "" for value in structured.values())


def test_integrate_accepts_fenced_json():
    fenced = "```json\n" + json.dumps(FULL_CUSTOMER, ensure_ascii=False) + "\n```"
    result = mengshi.integrate_customer_data(fenced)
    assert "客户姓名：张三" in result["system_customer_info"]


def test_integrate_accepts_dict_input():
    result = mengshi.integrate_customer_data(dict(FULL_CUSTOMER))
    assert "客户姓名：张三" in result["system_customer_info"]


def test_integrate_empty_dict_uses_fallback_copy():
    result = mengshi.integrate_customer_data({})
    assert result["system_customer_info"] == "客户留存在销售数据系统中的信息记录为空。"


def test_integrate_field_order_is_stable():
    """字段顺序按 ORIGINAL_FIELDS，保证同一输入每次输出一致。"""
    data = {field: field for field in reversed(mengshi.ORIGINAL_FIELDS)}
    structured = json.loads(
        mengshi.integrate_customer_data(json.dumps(data, ensure_ascii=False))[
            "system_customer_json_data"
        ]
    )
    assert list(structured) == [f"ITsystem_{f}" for f in mengshi.ORIGINAL_FIELDS]


def test_integrate_does_not_follow_json_key_order_for_text_lines():
    """文本行按 INFO_FIELD_LABELS 的顺序，与输入 JSON 的键序无关。"""
    shuffled = dict(reversed(list(FULL_CUSTOMER.items())))
    result = mengshi.integrate_customer_data(json.dumps(shuffled, ensure_ascii=False))
    lines = result["system_customer_info"].splitlines()[1:]
    labels = [line.split("：", 1)[0] for line in lines]
    # 期望值同 _build_system_customer_info 的规则：按 INFO_FIELD_LABELS 顺序，
    # 且仅**非空**字段产生数据行（FULL_CUSTOMER 中 customer_remark 为空串）
    expected = [
        label
        for field, label, _ in mengshi.INFO_FIELD_LABELS
        if not mengshi._is_empty(FULL_CUSTOMER.get(field, ""))
    ]
    assert labels == expected
