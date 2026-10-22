"""取值归一的公共实现。

同一件事此前在多个模块各写了一份：``_payload`` 在 ``callback_build`` 与
``customer_profile_l3`` 逐字重复，``_as_text`` / ``_as_list`` 各有两三种写法。
函数名相同而语义不同，最容易在「改了一处以为三处都改」时出问题，因此这里按
**语义**给出具名函数，各调用方按名引用，不再各自实现。

三个语义档，不要混用：

- :func:`as_text`：原样序列化。字符串去空白，其余 ``json.dumps``。用于**取值本身**
  （写进 facts、参与比较、拼检索串）。
- :func:`as_display_text`：给**人读的展示**。布尔不渲染、数字给十进制、列表用顿号
  连接。用于回传体里给人看的字段。
- :func:`as_flat_list` / :func:`as_json_list`：把取值摊成列表的两种口径，
  分别对应「``a||b`` 多值串」与「JSON 数组串」。
"""

from __future__ import annotations

import json
import re
from typing import Any


def as_payload(value: Any) -> Any:
    """兼容 String / Object 两种入参（上游时而是 JSON 串、时而是对象）。

    解析失败、空串、其它类型一律给空字典——调用方按「没有载荷」处理。
    """
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            return {}
    return {}


def as_text(value: Any) -> str:
    """取值 → 文本：字符串去空白，其余按 JSON 序列化（布尔成 ``true`` / ``false``）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, default=str)


def as_display_text(value: Any) -> str:
    """取值 → 可读文本：布尔不渲染，数字给十进制，列表用顿号连接。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "、".join(part for part in (as_display_text(v) for v in value) if part)
    return json.dumps(value, ensure_ascii=False, default=str)


def as_flat_list(value: Any) -> list[Any]:
    """把取值摊成列表：``a||b`` / 列表 / 单值都认。空值给空列表。

    与 :func:`as_json_list` 的区别：这里认的是多值连接符 ``||``，不解析 JSON 串。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[Any] = []
        for item in value:
            out.extend(as_flat_list(item))
        return out
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if "||" in text:
            return [part.strip() for part in text.split("||") if part.strip()]
        return [text]
    return [value]


def as_json_list(value: Any) -> list[Any]:
    """把取值摊成列表：JSON 数组串解成数组，其它字符串原样成单元素。"""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except ValueError:
            return [value]
        return list(parsed) if isinstance(parsed, list) else [parsed]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def strip_code_fence(text: Any) -> Any:
    """去掉 LLM 产出的 ```json ... ``` 围栏（整段包裹的形态）。

    各工作流此前各写一份，差异只在三点：是否透传非字符串、语言标签是否大小写敏感、
    标签名是否只认 ``json``。这里取三者并集，对既有输入行为不变：

    - 非字符串原样返回（避免对 ``None`` / 字典调用 ``.strip()``）；
    - 只有以围栏开头才剥，正文中段出现的围栏不在这里处理
      （那是 :func:`customer_profile.workflows.evidence_subagent._extract_fenced_block`
      的职责）；
    - 语言标签认任意字母数字串（```python 之类不会被漏掉）。
    """
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()
