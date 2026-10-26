"""规则层：一处登记、一处留痕、一处关闭。

公开面只有这些：

- :mod:`customer_profile.rules.registry` —— ``Rule`` / ``RuleRegistry`` / ``default()``
- :mod:`customer_profile.rules.actions` —— ``Action`` / ``apply()`` / ``order()``
- :mod:`customer_profile.rules.trace` —— ``RuleTrace`` / ``RuleTraceCollector``
- :mod:`customer_profile.rules.render` —— prompt 通道的正文切段与渲染
- :mod:`customer_profile.rules.guard` —— 落点检索与两道守卫

设计见 ``docs/history/rules-opt01/规则层设计.md``，施工见 ``docs/history/rules-opt01/开发实施计划.md``。
依赖方向：本包只依赖 ``notes_contract`` / ``internal_fields`` / ``text_utils`` /
``income_band``，**不得**导入 ``projection`` 或 ``workflows.*``。
"""

from __future__ import annotations

from . import render
from .actions import (
    Action,
    ApplyResult,
    NotesState,
    apply,
    order,
)
from .registry import (
    Rule,
    RuleRegistry,
    RuleTableError,
    default,
    load,
    reset_default,
)
from .trace import RuleTrace, RuleTraceCollector

__all__ = [
    "Action",
    "ApplyResult",
    "NotesState",
    "Rule",
    "RuleRegistry",
    "RuleTableError",
    "RuleTrace",
    "RuleTraceCollector",
    "apply",
    "default",
    "load",
    "order",
    "render",
    "reset_default",
]
