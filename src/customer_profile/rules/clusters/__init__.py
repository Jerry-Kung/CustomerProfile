"""规则簇装载。

一个簇 = 一组规则 + 一个代码守卫 + 可选一个 checker 提示词 + 一组测试（设计 §6）。
簇是**检查、渲染、统计、回归**的默认粒度，也是规则文件的分界：不这么做，
几百条规则会挤成第二个 ``pipeline.py``。

M0 阶段簇清单为空：基座先立住，规则从 M2 起逐条迁入。新增一个簇的步骤是
「建 ``clusters/<name>.py``，导出 ``RULES`` 与 ``CLUSTER``，在下方登记」。
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..registry import Rule

CLUSTER_MODULES: tuple[str, ...] = (
    "identity",
    "income",
    "consistency",
    "level",
    "hard",
    "expression",
    "spec",
    "knowledge",
)
"""已启用的簇模块名（相对本包）。顺序即同级动作的次级排序键，一经定下不轻易调换。"""

CLUSTER_TITLES: dict[str, str] = {
    "identity": "身份与归属",
    "income": "收入",
    "consistency": "跨字段一致性",
    "level": "商机等级",
    "hard": "形态与占位",
    "expression": "表达",
    "spec": "金标准与取证方向",
    "knowledge": "知识库",
}
"""簇的中文名。全部簇一次性登记，未启用的也在册——新增簇时只加模块，不改这张表。"""


def iter_rules() -> list["Rule"]:
    """按 :data:`CLUSTER_MODULES` 顺序收集全部规则。

    模块须导出 ``RULES: tuple[Rule, ...]``。簇模块在导入期不得有副作用——它们只
    声明常量，执行逻辑放在函数里由执行器调用。
    """
    from ..registry import Rule

    collected: list[Rule] = []
    for name in CLUSTER_MODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        rules = getattr(module, "RULES", None)
        if rules is None:
            raise ImportError(f"簇模块 {name} 未导出 RULES")
        collected.extend(rules)
    return collected


def cluster_of(rule_cluster: str) -> str:
    """簇名 → 中文名；未登记的原样返回（宁可露出英文，也不静默丢归属）。"""
    return CLUSTER_TITLES.get(rule_cluster, rule_cluster)
