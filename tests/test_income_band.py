"""第三方收入档位换算的回归用例（纯夹具，不出网、不依赖批次产物）。

换算发生在取数节点 ``shared_code.extract_jiguang_tags``：第三方标签一取到就把
``N档收入`` 改写成金额区间，下游各层只见到区间。这一层「错了不报错、只悄悄写错
金额」，所以三条边界各钉一组用例：

- 只改「预测收入水平」，「预测消费水平」的 ``N档消费`` 形态相同但不能碰；
- 非档位取值（``10-20万``）是客户自述或推断金额，原样透传；
- 同名字段重复两次时两条都要改。
"""

from __future__ import annotations

import json

import pytest

from customer_profile import income_band as ib
from customer_profile.workflows import shared_code

CHANNEL = "DatametInterestPoints"

# 实测形态：两条同名的「预测收入水平」（档位一致），一条消费档，一条已是区间。
REAL_TAGS = [
    {
        "name": "预测收入水平",
        "values": [{"score": 0.999999, "value": "3档收入"}],
    },
    {
        "name": "预测收入水平",
        "values": [{"score": 0.970001, "value": "3档收入"}],
    },
    {
        "name": "预测消费水平",
        "values": [{"score": 0.999999, "value": "3档消费"}],
    },
    {
        "name": "预测收入水平",
        "values": [{"score": 0.800000, "value": "10-20万"}],
    },
]


def records_with(tags, channel=CHANNEL):
    """构造 ``extract_jiguang_tags`` 的入参：JSON 数组字符串。"""
    return json.dumps(
        [{"channel": channel, "raw_payload": {"tags": tags}}], ensure_ascii=False
    )


def income_values(tags):
    """取出所有收入标签的取值，按出现顺序返回。"""
    return [
        item["value"]
        for tag in tags
        if tag.get("name") == ib.INCOME_TAG_NAME
        for item in tag.get("values") or []
    ]


# ------------------------------------------------------------ 取数节点：真实形态

def test_extract_converts_income_bands_on_real_shape():
    """真实形态的 tags：档位变区间、消费档不动、非档位值原样保留。"""
    result = shared_code.extract_jiguang_tags(records_with(REAL_TAGS), CHANNEL)

    tags = json.loads(result["result"])
    assert [t["name"] for t in tags] == [t["name"] for t in REAL_TAGS]

    # 两条重复的收入标签都改了，非档位那条不动
    assert income_values(tags) == ["20-30万", "20-30万", "10-20万"]

    # 分数等其它字段不受影响
    assert tags[0]["values"][0]["score"] == 0.999999
    assert tags[1]["values"][0]["score"] == 0.970001

    # 消费档形态相同但不得被换算
    assert tags[2]["values"][0]["value"] == "3档消费"


def test_extract_keeps_consumption_band_even_as_income_sibling():
    """同一个 values 里只有收入标签被改写，消费标签名字里不含「收入」。"""
    tags = [
        {"name": "预测消费水平", "values": [{"score": 0.9, "value": "3档消费"}]},
        {"name": "预测收入水平", "values": [{"score": 0.9, "value": "3档收入"}]},
    ]
    result = shared_code.extract_jiguang_tags(records_with(tags), CHANNEL)

    parsed = json.loads(result["result"])
    assert parsed[0]["values"][0]["value"] == "3档消费"
    assert parsed[1]["values"][0]["value"] == "20-30万"


# ------------------------------------------------------ 取数节点：user_tags 类型

def test_extract_handles_json_array_string():
    """user_tags 本身是合法 JSON 数组字符串时，同样完成换算。"""
    result = shared_code.extract_jiguang_tags(
        records_with(json.dumps(REAL_TAGS, ensure_ascii=False)), CHANNEL
    )

    parsed = json.loads(result["result"])
    assert income_values(parsed) == ["20-30万", "20-30万", "10-20万"]
    assert parsed[2]["values"][0]["value"] == "3档消费"


def test_extract_handles_json_object_string():
    """user_tags 是 JSON 对象字符串时正常解析并换算。"""
    payload = {"tags": [{"name": ib.INCOME_TAG_NAME, "values": [{"value": "4档收入"}]}]}
    result = shared_code.extract_jiguang_tags(
        records_with(json.dumps(payload, ensure_ascii=False)), CHANNEL
    )

    parsed = json.loads(result["result"])
    assert parsed["tags"][0]["values"][0]["value"] == "30-40万"


def test_extract_passes_through_unparseable_string():
    """解析不了的普通字符串原样返回，不能因为一个不认识的结构丢掉整批标签。"""
    raw = "3档收入,不是JSON"
    result = shared_code.extract_jiguang_tags(records_with(raw), CHANNEL)

    assert result == {"result": raw}


# ------------------------------------------------------ 取数节点：空输入与兜底

@pytest.mark.parametrize(
    "records",
    [
        "",
        "   ",
        None,
    ],
    ids=["empty", "blank", "none"],
)
def test_extract_returns_empty_on_blank_records(records):
    assert shared_code.extract_jiguang_tags(records, CHANNEL) == {"result": ""}


def test_extract_returns_empty_when_channel_not_found():
    records = records_with(REAL_TAGS, channel="OtherSource")
    assert shared_code.extract_jiguang_tags(records, CHANNEL) == {"result": ""}


def test_extract_returns_empty_when_tags_null():
    assert shared_code.extract_jiguang_tags(records_with(None), CHANNEL) == {
        "result": ""
    }


def test_extract_returns_empty_on_malformed_records():
    """records 不是 JSON 数组时保持返回空 result。"""
    assert shared_code.extract_jiguang_tags("{not json", CHANNEL) == {"result": ""}
    assert shared_code.extract_jiguang_tags('{"channel": "x"}', CHANNEL) == {
        "result": ""
    }


# ------------------------------------------------------------ 纯函数：标签换算

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3档收入", "20-30万"),
        ("1档收入", "8-15万"),
        ("6档收入", "60万以上"),
        ("2档收入", "10-20万"),
        ("4档收入", "30-40万"),
        ("5档收入", "40-60万"),
    ],
)
def test_normalize_income_tags_maps_bands(raw, expected):
    tags = [{"name": ib.INCOME_TAG_NAME, "values": [{"score": 0.9, "value": raw}]}]

    converted = ib.normalize_income_tags(tags)

    assert converted[0]["values"][0]["value"] == expected
    assert converted[0]["values"][0]["score"] == 0.9


def test_normalize_income_tags_leaves_non_band_values():
    """非档位取值与不含「预测收入水平」的标签都不改。"""
    tags = [
        {"name": ib.INCOME_TAG_NAME, "values": [{"value": "10-20万"}]},
        {"name": "预测消费水平", "values": [{"value": "3档消费"}]},
    ]

    converted = ib.normalize_income_tags(tags)

    assert converted[0]["values"][0]["value"] == "10-20万"
    assert converted[1]["values"][0]["value"] == "3档消费"


def test_normalize_income_pure_function():
    """单值换算：档位给区间，非档位给 None。"""
    assert ib.normalize_income("3档收入") == "20-30万"
    assert ib.normalize_income(" 6 档 ") == "60万以上"
    assert ib.normalize_income("10-20万") is None
    assert ib.normalize_income("3档消费") == "20-30万"  # 只认形态，标签名由调用方把关
    assert ib.normalize_income(None) is None
    assert ib.normalize_income("") is None


# ------------------------------------------------------------ 业务口径：映射表

def test_income_band_ranges_match_business_spec():
    """六档映射与业务专家口径（2026-10-20）逐项一致。"""
    assert ib.INCOME_BAND_RANGES == {
        "1": "8-15万",
        "2": "10-20万",
        "3": "20-30万",
        "4": "30-40万",
        "5": "40-60万",
        "6": "60万以上",
    }
    assert sorted(ib.INCOME_BAND_RANGES) == ["1", "2", "3", "4", "5", "6"]
