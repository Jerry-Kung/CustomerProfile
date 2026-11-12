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


def scope_predicate(scope: WorkflowScope, *, dialect: str = "sqlite") -> tuple[str, tuple[str, ...]]:
    """固定 SQL 谓词与绑定参数；供主任务领取、排队计数和恢复共同使用。

    **两种后端写法不同，语义必须相同。** ``GLOB`` 是 SQLite 独占；MySQL 侧用
    ``LEFT(...) = ?`` 而不是 ``LIKE ?``——``LIKE 'invitation_%'`` 里的 ``_`` 是单字符
    通配符，必须靠转义或 ``ESCAPE`` 才能表达字面下划线，而 ``LEFT()`` 没有这层歧义。
    """
    if scope == "all":
        return "1 = 1", ()
    if scope not in {"profile", "invitation"}:
        raise ValueError(f"未知的工作流范围：{scope}")
    is_invitation = scope == "invitation"
    if dialect == "sqlite":
        return (
            f"workflow_id {'GLOB' if is_invitation else 'NOT GLOB'} ?",
            (INVITATION_PREFIX + "*",),
        )
    if dialect == "mysql":
        operator = "=" if is_invitation else "<>"
        return (
            f"LEFT(workflow_id, ?) {operator} ?",
            (len(INVITATION_PREFIX), INVITATION_PREFIX),
        )
    raise ValueError(f"未知的 SQL 方言：{dialect}")
