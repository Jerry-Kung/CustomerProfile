"""增量上下文的形状与解析。

一次增量运行要告诉现行管线三件它本来不知道的事：

1. **哪些记录是本轮新来的**——L1 只看这些；
2. **上一版已定稿的事实**——只用于让模型标注冲突，不作为取证来源；
3. **上一版等级与板块文本**——定级带锚点、表达可跳过。

这三样打包成一个 JSON 串，经 ``start`` 的可选入参 ``incremental_ctx`` 进入管线与 L3。
**空串表示不是增量运行**，三处消费点都按缺省值处理，产出与接入前逐字节相同。

为什么用一个字符串而不是三个入参：``start`` 的每个变量都是一条独立的入参契约，
而这三样永远一起出现、一起为空。一个键让「是不是增量」只有一处判据。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

EMPTY: dict[str, Any] = {
    "v": 1,
    "new_keys": [],
    "new_logical": [],
    "base_fields": [],
    "base_facts": {},
    "base_sections": {},
    "previous_level": "",
    "previous_level_detail": "",
    "base_updated_at": "",
    "base_facts_sig": "",
    "new_channels": [],
    "alive_channels": [],
}
"""空上下文。**不是增量运行**时的一切缺省值。"""


def loads(value: Any) -> dict[str, Any]:
    """解析上下文；空串、非法 JSON、结构不对一律返回 :data:`EMPTY`。

    不抛异常是刻意的：增量上下文是**加速手段**，它坏了最多是这次跑得慢、写得全，
    不该让一条已经能跑的管线因此失败。
    """
    if not value:
        return dict(EMPTY)
    data = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return dict(EMPTY)
        try:
            data = json.loads(text)
        except ValueError:
            return dict(EMPTY)
    if not isinstance(data, Mapping) or not data.get("v"):
        return dict(EMPTY)
    out = dict(EMPTY)
    out.update({key: data.get(key, EMPTY[key]) for key in EMPTY})
    return out


def dumps(ctx: Mapping[str, Any]) -> str:
    return json.dumps(dict(ctx), ensure_ascii=False, default=str)


def is_active(ctx: Mapping[str, Any]) -> bool:
    """是否真的带着增量信息。空上下文返回假——三处消费点据此走原路。"""
    return bool(ctx.get("new_keys") or ctx.get("new_logical") or ctx.get("base_fields"))


def new_key_sets(ctx: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    return (
        {str(item) for item in ctx.get("new_keys") or []},
        {str(item) for item in ctx.get("new_logical") or []},
    )


def settled_facts(ctx: Mapping[str, Any]) -> dict[str, str]:
    """上一版事实表（字段 → 可读文本）。渲染进 L1 输入包时只留取值，不留证据。"""
    facts = ctx.get("base_facts")
    if not isinstance(facts, Mapping):
        return {}
    return {str(k): _text(v) for k, v in facts.items() if _text(v)}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "、".join(_text(item) for item in value if _text(item))
    return json.dumps(value, ensure_ascii=False, default=str)


def build(
    *,
    new_keys: Iterable[str],
    new_logical: Iterable[str],
    base_fields: Sequence[dict],
    base_facts: Mapping[str, Any],
    base_sections: Mapping[str, Any],
    previous_level: str = "",
    previous_level_detail: str = "",
    base_updated_at: str = "",
    facts_sig: str = "",
    new_channels: Iterable[str] = (),
    alive_channels: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "v": 1,
        "new_keys": sorted({str(k) for k in new_keys if k}),
        "new_logical": sorted({str(k) for k in new_logical if k}),
        "base_fields": list(base_fields or []),
        "base_facts": dict(base_facts or {}),
        "base_sections": dict(base_sections or {}),
        "previous_level": str(previous_level or ""),
        "previous_level_detail": str(previous_level_detail or ""),
        "base_updated_at": str(base_updated_at or ""),
        "base_facts_sig": str(facts_sig or ""),
        "new_channels": sorted({str(c) for c in new_channels if c}),
        "alive_channels": sorted({str(c) for c in alive_channels if c}),
    }
