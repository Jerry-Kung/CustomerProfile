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
    "customer_profile.workflows.moments_info",
    "customer_profile.workflows.wechat_search_homepage",
    "customer_profile.workflows.wechat_homepage",
    "customer_profile.workflows.xiaohongshu_homepage",
    "customer_profile.workflows.douyin_homepage",
    "customer_profile.workflows.alipay_homepage",
    "customer_profile.workflows.test_drive_audio",
    "customer_profile.workflows.outbound_call_audio",
    "customer_profile.workflows.audio_content_extract",
    "customer_profile.workflows.audio_content_extract_roled",
    "customer_profile.workflows.jiguang_data",
    "customer_profile.workflows.chat_history_data",
    "customer_profile.workflows.user_feedback_data",
    "customer_profile.workflows.profile_features_analysis",
    "customer_profile.workflows.evidence_subagent",
    "customer_profile.workflows.phone_company_analysis",
    # ``customer_profile_production``（生产臂）于 2026-10-27 整臂退役并删除：
    # 它承载的是 v1 的 45 字段旧清单（``married`` / ``commute`` / ``transport`` 等已废弃
    # 枚举键仍在它的两份提示词里），入口早已改指 ``customer_profile_pipeline``，
    # 留着只会让旧键继续被引用。见 ``docs/history/旧版枚举键与production臂整改方案.md``。
    # 它复用的业务资产模板已改名为 ``customer_profile_shared__*``，由 L3 表达层继续引用
    # （2026-10-29 起邀约策略迁入 knowledge/common/，模板侧剩 5 份）。
    #
    # ``customer_profile_entry``（客户初始画像，基线臂）于 2026-10-16 从注册表摘除：
    # 该臂已无业务用途，入口改指 ``customer_profile_pipeline``（业务测试臂）。
    # 模块文件保留——`tests/test_contract.py` 等仍以它为「子流程编排」的样本，
    # 且它是 AB 基线可复位的前提。它引用的 ``evidence_subagent`` 与
    # ``profile_features_analysis`` 因此保留在册（摘除会让基线臂无法复位）。
    # 它的末端节点原指生产臂，生产臂退役后改指 ``customer_profile_pipeline``。
    "customer_profile.workflows.customer_profile_pipeline",
    "customer_profile.workflows.customer_profile_l3",
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
