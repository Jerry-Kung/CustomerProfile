"""主任务的部署范围；子工作流继承所属主任务，不单独竞争队列。"""

from typing import Literal

WorkflowScope = Literal["all", "profile", "invitation"]
INVITATION_PREFIX = "invitation_"


def accepts_workflow(scope: WorkflowScope, workflow_id: str) -> bool:
    if scope == "all":
        return True
    if scope not in {"profile", "invitation"}:
        raise ValueError(f"未知的工作流范围：{scope}")
    return workflow_id.startswith(INVITATION_PREFIX) == (scope == "invitation")


def scope_predicate(scope: WorkflowScope) -> tuple[str, tuple[str, ...]]:
    """固定 SQL 谓词与绑定参数；供主任务领取、排队计数和恢复共同使用。"""
    if scope == "all":
        return "1 = 1", ()
    if scope == "profile":
        return "workflow_id NOT GLOB ?", (INVITATION_PREFIX + "*",)
    if scope == "invitation":
        return "workflow_id GLOB ?", (INVITATION_PREFIX + "*",)
    raise ValueError(f"未知的工作流范围：{scope}")
