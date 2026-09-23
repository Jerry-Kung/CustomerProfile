"""工作流定义集合。

每个模块定义一个真实工作流或验证用图，并把其中的确定性逻辑登记为普通 Python 函数。
``WORKFLOW_MODULES`` 是自动发现清单：漏登记一个模块就等于少一个工作流，因此由测试
断言「清单里的模块都能导入、且都产出一个 WorkflowDef」。
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..definitions import WorkflowDef

WORKFLOW_MODULES: tuple[str, ...] = (
    "customer_profile.workflows.mengshi_it_system_data",
    "customer_profile.workflows.human_corrected_info",
    "customer_profile.workflows.synthetic_fanout",
)


def load_all() -> dict[str, "WorkflowDef"]:
    """导入全部工作流模块，返回 ``{workflow_id: WorkflowDef}``。

    同时收集模块导出的 ``SUBWORKFLOWS``（子工作流）。子流程也必须是**完整定义**，
    不能被主流程引用却查不到——那会在运行期以「子工作流未注册」的形式失败，
    而在构建期发现它便宜得多。
    """
    registry: dict[str, WorkflowDef] = {}
    for module_name in WORKFLOW_MODULES:
        module = importlib.import_module(module_name)

        definitions: list[WorkflowDef] = []
        primary = getattr(module, "WORKFLOW", None)
        if primary is None:
            raise AttributeError(f"工作流模块 {module_name} 未导出 WORKFLOW 定义")
        definitions.append(primary)
        definitions.extend(getattr(module, "SUBWORKFLOWS", ()) or ())

        for definition in definitions:
            if definition.workflow_id in registry:
                raise ValueError(
                    f"工作流 ID 重复：{definition.workflow_id}（来自 {module_name}）"
                )
            registry[definition.workflow_id] = definition
    return registry


def load(workflow_id: str) -> "WorkflowDef":
    """按 ID 取一个工作流定义。"""
    try:
        return load_all()[workflow_id]
    except KeyError as exc:
        raise KeyError(
            f"工作流 {workflow_id!r} 未定义；已定义：{sorted(load_all())}"
        ) from exc
