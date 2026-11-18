"""画像详情接口读回体适配：把 ``GET /api/v1/remote/data/profile/{phone}`` 的响应
归一成一份「属性键 → 标准 Notes 项」的视图，供所有读取方共用。

**为什么集中在一处**（2026-11-17 接口改版）：改版前 Notes 容器是字典 ``basic_notes``，
四条读取路径各自 ``.get("basic_notes")`` 解析；改版后容器换成与回传体同形的列表
``basic_notes_updates``，四处一起静默读空——读不到不会报错，只会拿到空结论。以后再变，
只改这里。

**新旧两种形状**：

===========  ========================================  ==============================
版次          容器与单条形态                             本模块怎么读
===========  ========================================  ==============================
旧（≤11-02）  ``basic_notes``: ``{键: {"value", ...}}``  直接取字典项
新（11-17 起） ``basic_notes_updates``: ``[{"attribute_key", "attribute_value",
              "reasoning_detail", "is_locked", "ai_feedback"}]``  列表转字典
===========  ========================================  ==============================

**两种都认，新结构优先**：``experiments/uat_data`` 里 2026-11-02 及更早的归档批次保存
的是旧响应，回放这些批次时读到的仍是旧形状；两者同时出现（不该发生，真出现说明上游
拼了两份数据）时按新结构读。

**字段对照**（改版后仍在的被保留，消失的由本模块补成同一形态）：

- ``value`` ← 旧 ``value`` / 新 ``attribute_value``；
- ``reasoning_detail`` ← 旧 ``{"evidence": [reasoning_summary]}`` / 新原样；
- ``is_locked`` / ``ai_feedback`` 新旧都在**单条 Note 上**。2026-11-17 改版当天这两个键
  一度整体消失，业务方随后补回（线上实测 33/33 条都带，取值 ``0`` / ``null``）；
- ``locked_by`` 新版不再返回，本模块也不产出——项目内没有读取方。

标准 Notes 项的形状固定为 ``{"value", "reasoning_detail", "is_locked", "ai_feedback"}``，
读取方可以直接 ``.get(...)``，不必再关心来源是哪一种结构。
"""

from __future__ import annotations

import json
from typing import Any, Mapping

NOTE_LIST_KEY = "basic_notes_updates"
"""新结构：与回传体同形的 Notes 列表。"""

NOTE_DICT_KEY = "basic_notes"
"""旧结构：键为属性名的 Notes 字典。"""

_LOCKED_VALUES = (True, 1, "1")


def payload(value: Any) -> Mapping[str, Any]:
    """把响应体解析成对象。JSON 串、对象都接受；解析不出对象时给空字典。"""
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _detail_of_legacy(note: Mapping[str, Any]) -> dict[str, Any]:
    """旧结构没有 ``reasoning_detail``，只有一句 ``reasoning_summary``。

    这一句在原结构里是**推理说明**而不是证据，但两者在出口都落到「依据」一格，
    且旧的读取方（``invitation_context``）本来就是把 summary 当 evidence 用的。
    """
    text = note.get("reasoning_summary")
    text = str(text).strip() if text else ""
    return {"evidence": [text]} if text else {}


def _note(entry: Mapping[str, Any], *, legacy: bool) -> dict[str, Any]:
    detail = entry.get("reasoning_detail")
    if not isinstance(detail, Mapping):
        detail = _detail_of_legacy(entry) if legacy else {}
    return {
        "value": entry.get("value") if legacy else entry.get("attribute_value"),
        "reasoning_detail": dict(detail),
        "is_locked": entry.get("is_locked"),
        "ai_feedback": entry.get("ai_feedback"),
        # 旧结构的 ``reasoning_detail`` 是本模块从一句 ``reasoning_summary`` 造出来的，
        # 不是上游真给的（没有 steps / confidence）。读取方需要区分「A 自带完整推导」
        # 与「A 只有一句话」，因此把这个来源标出来。
        "legacy_detail": legacy,
    }


def notes(profile: Any) -> dict[str, dict[str, Any]]:
    """``{属性键: 标准 Notes 项}``。新结构列表优先，旧结构字典兜底。

    没有 ``attribute_key`` 的条目跳过——它落不到任何字段上，留着只会让下游拿着空键
    做匹配。同一属性重复出现时取**最后一条**（与「后者覆盖前者」的落库语义一致）。
    """
    body = payload(profile)

    updates = body.get(NOTE_LIST_KEY)
    if isinstance(updates, (list, tuple)):
        out: dict[str, dict[str, Any]] = {}
        for entry in updates:
            if not isinstance(entry, Mapping):
                continue
            attribute = str(entry.get("attribute_key") or "").strip()
            if not attribute:
                continue
            out[attribute] = _note(entry, legacy=False)
        return out

    legacy_notes = body.get(NOTE_DICT_KEY)
    if not isinstance(legacy_notes, Mapping):
        return {}
    return {
        str(key): _note(entry, legacy=True)
        for key, entry in legacy_notes.items()
        if isinstance(entry, Mapping)
    }


def is_locked(note: Any) -> bool:
    """``is_locked`` 兼容 ``1`` / ``'1'`` / ``True`` 三种写法。"""
    if not isinstance(note, Mapping):
        return False
    return note.get("is_locked") in _LOCKED_VALUES


def detail(note: Any) -> Mapping[str, Any]:
    if not isinstance(note, Mapping):
        return {}
    value = note.get("reasoning_detail")
    return value if isinstance(value, Mapping) else {}


def value_of(note: Any) -> Any:
    if not isinstance(note, Mapping):
        return None
    return note.get("value")
