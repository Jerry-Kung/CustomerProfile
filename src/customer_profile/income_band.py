"""第三方「预测收入水平」档位 → 年收入区间。

第三方（极光）画像只给 ``1档收入`` … ``6档收入`` 这样的档位，销售要的是具体金额。
换算口径由业务专家确认（2026-10-20）：1 档最低、6 档最高，切点见
:data:`INCOME_BAND_RANGES`。

**换算发生在取数节点**（``shared_code.extract_jiguang_tags``）：第三方标签一取到就
把档位改写成金额区间，于是 L0 / L1 / L2 / L3 以及卡片、Notes 看到的 revenue 标签
本来就是一个区间，「档位」这个概念不进入下游任何一层。投影层的
:func:`customer_profile.projection.normalize_income` 保留为兜底，只处理离线回放的
旧产物与外部直接构造的档位断言。

只认档位原文，客户自述或模型推断的金额（``30-50万``）原样透传，不得被改写。
"""

from __future__ import annotations

import json
import re
from typing import Any

INCOME_BAND_RANGES: dict[str, str] = {
    "1": "8-15万",
    "2": "10-20万",
    "3": "20-30万",
    "4": "30-40万",
    "5": "40-60万",
    "6": "60万以上",
}
"""档位（1–6）→ 年收入区间（业务专家口径，2026-10-20）。"""

INCOME_TAG_NAME = "预测收入水平"
"""第三方标签名。只有该标签的取值做档位换算——``预测消费水平``（``N档消费``）
形态相同，但消费档是枚举字段，不能换成金额。"""

_INCOME_BAND_PATTERN = re.compile(r"(?:第\s*)?([1-6])\s*档")


def normalize_income(value: Any) -> str | None:
    """把第三方收入档位换成区间数值；不是档位形态的取值返回 ``None``。

    ``None`` 表示「这条取值本来就不是档位」，调用方应保留原值——推断出来的金额、
    客户自述的金额都不是档位，不能被这张表吞掉。
    """
    if isinstance(value, str):
        text = value.strip()
    elif value is None:
        return None
    else:
        text = str(value).strip()
    if not text:
        return None
    matched = _INCOME_BAND_PATTERN.search(text)
    if not matched:
        return None
    return INCOME_BAND_RANGES[matched.group(1)]


def normalize_income_tags(tags: Any) -> Any:
    """把第三方标签里的「预测收入水平」档位改写为金额区间（取数节点调用）。

    ``tags`` 的实测形态是 ``[{"name": "预测收入水平", "values": [{"score": 0.99,
    "value": "3档收入"}]}, …]``，也出现过**同名字段重复两次**的情况（两档取值一致）。
    JSON 字符串输入解析后返回新的 JSON 字符串；解析失败原样返回——取数层不能因为
    一个不认识的结构丢掉整批标签。非档位取值（``10-20万``）原样保留。
    """
    if tags is None:
        return tags

    if isinstance(tags, str):
        text = tags.strip()
        if not text:
            return tags
        try:
            parsed = json.loads(text)
        except ValueError:
            return tags
        converted = normalize_income_tags(parsed)
        return json.dumps(converted, ensure_ascii=False)

    if isinstance(tags, dict):
        converted_us = _convert_tag(tags)
        return {key: normalize_income_tags(val) for key, val in tags.items()} \
            if converted_us is None else converted_us

    if isinstance(tags, (list, tuple)):
        return [normalize_income_tags(item) for item in tags]

    return tags


def _convert_tag(tag: dict) -> dict | None:
    """转换单个收入标签；不是收入标签时返回 ``None``。"""
    name = tag.get("name")
    if not isinstance(name, str) or INCOME_TAG_NAME not in name:
        return None
    values = tag.get("values")
    if not isinstance(values, list):
        return None
    converted: list[Any] = []
    for item in values:
        if isinstance(item, dict) and "value" in item:
            mapped = normalize_income(item.get("value"))
            if mapped is not None:
                item = {**item, "value": mapped}
        converted.append(item)
    return {**tag, "values": converted}
