"""增量画像入口工作流（暂名）。

**为什么是新的入口工作流而不是改现成的 pipeline**（``01`` §3）：现行
``customer_profile_pipeline`` 一字不动，作为 T3 的子运行被调用。定义哈希与基线可比性
因此不受影响，回退只改入口常量。

P1 的形态（``03`` T1-4）只有两档：**T0 原样回传**与 **T3 全量重建**。

    start(phone_number, batch_id)
      ├─ history(GET /data/history/{phone})
      └─ dispatch(code)  读基线 → 差分 → 定档
           ├─ if-else tier == T0 ──► 副本换 batch_id ──► passthrough ──┐
           └─ else ──► 子运行 customer_profile_pipeline ──► passthrough ─┤
                                                                        ▼
                                                              aggregate → commit(写基线) → end

**T1 / T2 的判据本期一律落 T3**：调度层判定后如果档位不在放行集合里，直接改成 T3。
放开只改 ``dispatch`` 的放行集合，不改图。
"""

from __future__ import annotations

from typing import Any

from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "customer_profile_incremental"
DISPLAY_NAME = "客户画像增量更新"

START = "incremental_start"
HISTORY_HTTP = "incremental_history"
DISPATCH = "incremental_dispatch"
TIER_BRANCH = "incremental_tier_branch"
REUSE_COPY = "incremental_reuse_copy"
REUSE_WRITE = "incremental_reuse_writeback"
REUSE_PASS = "incremental_reuse_pass"
PROFILE_HTTP = "incremental_profile"
FULL_RUN = "incremental_full_run"
FULL_PASS = "incremental_full_pass"
MERGE = "incremental_callback_merge"
COMMIT = "incremental_baseline_commit"
END = "incremental_end"

PIPELINE_WORKFLOW = "customer_profile_pipeline"
"""T3 的全量管线。**按缺省别名引用**，不在本模块复制它的节点。"""

HISTORY_PATH = "/api/v1/remote/data/history/"
PROFILE_PATH = "/api/v1/remote/data/profile/"
MODULE = "customer_profile.incremental.nodes"

DISPATCH_KEYS: tuple[str, ...] = (
    "tier",
    "tier_label",
    "tier_reason",
    "baseline_found",
    "compat_ok",
    "added_count",
    "removed_count",
    "unchanged_count",
    "touched_channels",
    "stage",
    "previous_level",
    "previous_level_detail",
    "callback_json",
    "incremental_ctx",
    "base_stats_json",
    "compat_key",
    "adapt_error",
    "history_records",
)
"""``dispatch`` 节点的全部出参。整份落进运行树，就是「这次为什么走这一档」的审计留痕
（``01`` §4「调度判定留痕」）。"""


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=DISPLAY_NAME,
    entries=(START,),
    exits=(END,),
    outputs={"result1": "result1"},
    nodes=(
        make_node(
            START,
            "用户输入",
            "start",
            variables=("phone_number", "batch_id"),
            required_phone_number=True,
            required_batch_id=False,
        ),
        make_node(
            HISTORY_HTTP,
            "获取全量用户数据",
            "http-request",
            after=(START,),
            inputs={"@path": (START, "phone_number")},
            outputs=("body", "status"),
            **{
                "@service": "profile",
                "@method": "get",
                "@path": HISTORY_PATH,
                "@auth": True,
                "@outputs": {"body": "$text", "status": "$status"},
            },
        ),
        make_node(
            PROFILE_HTTP,
            "获取当前画像",
            "http-request",
            after=(START,),
            inputs={"@path": (START, "phone_number")},
            outputs=("body", "status"),
            **{
                "@service": "profile",
                "@method": "get",
                "@path": PROFILE_PATH,
                "@auth": True,
                "@outputs": {"body": "$text", "status": "$status"},
            },
        ),
        make_node(
            DISPATCH,
            "增量调度判定",
            "code",
            after=(HISTORY_HTTP, PROFILE_HTTP, START),
            inputs={
                "history_body": (HISTORY_HTTP, "body"),
                "profile_body": (PROFILE_HTTP, "body"),
                "phone_number": (START, "phone_number"),
                "batch_id": (START, "batch_id"),
            },
            outputs=DISPATCH_KEYS,
            function=f"{MODULE}:incremental_dispatch",
        ),
        make_node(
            TIER_BRANCH,
            "档位分流",
            "if-else",
            after=(DISPATCH,),
            inputs={"tier": (DISPATCH, "tier")},
            outputs=("tier",),
            branches=(
                {
                    "id": "reuse",
                    "condition": {"field": "tier", "operator": "eq", "value": "T0"},
                    "output": "tier",
                },
            ),
            else_id="full",
        ),
        # ---------------------------------------------------------- T0 分支
        make_node(
            REUSE_COPY,
            "回传体复制",
            "code",
            after=(TIER_BRANCH, DISPATCH),
            on_branch=(TIER_BRANCH, "reuse"),
            inputs={
                "value": (DISPATCH, "callback_json"),
                "batch_id": (START, "batch_id"),
            },
            outputs=("callback_json",),
            function=f"{MODULE}:reuse_callback",
        ),
        make_node(
            REUSE_WRITE,
            "画像回写（原样）",
            "http-request",
            after=(REUSE_COPY,),
            on_branch=(TIER_BRANCH, "reuse"),
            inputs={"@body_ref": (REUSE_COPY, "callback_json")},
            outputs=("body", "status"),
            **{
                "@body_ref": True,
                "@service": "profile",
                "@method": "post",
                "@path": "/api/v1/remote/callback/update-profile",
                "@auth": True,
                # 与现行管线同一个闸门：WRITEBACK_ENABLED=false 时只产出「已跳过」。
                "@writeback": True,
                "@outputs": {"body": "$text", "status": "$status"},
            },
        ),
        make_node(
            REUSE_PASS,
            "T0 原样回传",
            "code",
            after=(REUSE_WRITE,),
            on_branch=(TIER_BRANCH, "reuse"),
            inputs={"value": (REUSE_COPY, "callback_json")},
            outputs=("callback_json", "tier_used"),
            function=f"{MODULE}:passthrough_t0",
        ),
        # ---------------------------------------------------------- T3 分支
        make_node(
            FULL_RUN,
            "全量重建（现行管线）",
            "tool",
            after=(TIER_BRANCH, DISPATCH),
            on_branch=(TIER_BRANCH, "full"),
            inputs={
                "phone_number": (START, "phone_number"),
                "batch_id": (START, "batch_id"),
                # 增量上下文：T1/T2 由 dispatch 组装，T3 为空串（等于现行全量）。
                "incremental_ctx": (DISPATCH, "incremental_ctx"),
            },
            outputs=("result1",),
            **{
                "@workflow": PIPELINE_WORKFLOW,
                "@inputs": {
                    "phone_number": "phone_number",
                    "batch_id": "batch_id",
                    "incremental_ctx": "incremental_ctx",
                },
            },
        ),
        make_node(
            FULL_PASS,
            "T3 全量回传",
            "code",
            after=(FULL_RUN,),
            on_branch=(TIER_BRANCH, "full"),
            inputs={"value": (FULL_RUN, "result1")},
            outputs=("callback_json", "tier_used"),
            function=f"{MODULE}:passthrough_t3",
        ),
        # ---------------------------------------------------------- 汇合
        make_node(
            MERGE,
            "回传体汇总",
            "variable-aggregator",
            after=(REUSE_PASS, FULL_PASS),
            variables=((REUSE_PASS, "callback_json"), (FULL_PASS, "callback_json")),
            outputs=("output",),
        ),
        make_node(
            COMMIT,
            "基线写入",
            "code",
            after=(MERGE, DISPATCH, HISTORY_HTTP),
            inputs={
                "phone_number": (START, "phone_number"),
                "batch_id": (START, "batch_id"),
                "callback_json": (MERGE, "output"),
                "history_body": (HISTORY_HTTP, "body"),
                "tier": (DISPATCH, "tier"),
            },
            outputs=("baseline_saved", "baseline_error", "baseline_keys", "baseline_tier"),
            function=f"{MODULE}:baseline_commit",
        ),
        make_node(
            END,
            "输出",
            "end",
            after=(COMMIT, MERGE),
            inputs={"result1": (MERGE, "output")},
            outputs=("result1",),
        ),
    ),
)


def default_inputs(phone_number: str, batch_id: str = "") -> dict[str, Any]:
    """与主入口同一契约：``phone_number`` 必填、``batch_id`` 可选。"""
    return {"phone_number": phone_number, "batch_id": batch_id}


def history_url(phone_number: str) -> str:
    """该工作流实际请求的全量历史数据 URL。用于构造固定响应 fixture。"""
    return f"{HISTORY_PATH}{phone_number}"
