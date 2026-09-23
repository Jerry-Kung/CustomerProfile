"""人工构造的小型验证图：分叉 + 汇合 + 子流程调用。

它不是业务工作流，是 V0.2 的**验收装置**（规划 §3 V0.2 的第三项载体）。要证的工程
性质只有三条，都难以用两个直线型真实工作流证明：

1. 多前置汇合节点**只执行一次**，且要等全部前置完成；
2. 分支**不设全局屏障**——并行的两支各自结束，而不是逐层等待（用时间戳证明）；
3. 子流程调用产生**嵌套运行**，父节点等待期间不占用子流程所需的外部请求名额。

图形（节点 ID 沿用 Dify 风格的雪花 ID 形式，便于与真实定义区分——本图用可读 ID）：

    start
      ├─ timer_fast ──┐
      ├─ timer_slow ──┼─→ join ─→ child_call ─→ end
      └─ branch ──────┘

``branch`` 走 if-else 的两支之一后再汇合，用来验证 P4 的缺省口径
（互斥分支汇聚到同一节点时按「先到者提供值」）。本图刻意让两支产出**不同字段名**，
从而不依赖 aggregator 的取值顺序——那属 V0.3 的分组聚合器实现范围。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "synthetic_fanout"
DISPLAY_NAME = "最小验证图（分叉+汇合+子流程）"

START = "syn_start"
FAST = "syn_timer_fast"
SLOW = "syn_timer_slow"
GATE = "syn_gate"
GATE_FAST = "syn_gate_fast"
GATE_SLOW = "syn_gate_slow"
JOIN = "syn_join"
CHILD_CALL = "syn_child_call"
FINISH = "syn_finish"
END = "syn_end"

# 子工作流：一个最小的两节点图，用来验证嵌套运行
CHILD_WORKFLOW_ID = "synthetic_echo"
CHILD_START = "child_start"
CHILD_WORK = "child_work"
CHILD_END = "child_end"

# 两侧延时的秒数。取 0.6 与 0.05，差值足以区分「并行」与「逐层屏障」：
# 若调度层有全局屏障，总耗时至少是两段之和；无屏障时应接近较长的那一段。
FAST_DELAY_SECONDS = 0.05
SLOW_DELAY_SECONDS = 0.6

# 并发探针：记录每个节点的开始/结束时刻，供测试断言重叠。
_timeline: list[dict[str, Any]] = []


def timeline() -> list[dict[str, Any]]:
    """返回本进程内累积的时间线记录（测试读完可 :func:`reset_timeline`）。"""
    return list(_timeline)


def reset_timeline() -> None:
    _timeline.clear()


def _mark(node_id: str, phase: str) -> None:
    _timeline.append({"node_id": node_id, "phase": phase, "at": time.monotonic()})


async def run_start(inputs: dict[str, Any]) -> dict[str, Any]:
    """入口：把入参透出，并按输入决定 gate 走哪一支。"""
    _mark(START, "start")
    _mark(START, "end")
    return {"payload": inputs.get("payload", ""), "route": inputs.get("route", "fast")}


async def fast_timer(payload: str) -> dict[str, Any]:
    """一支快任务：延时后返回带自身标记的文本。"""
    _mark(FAST, "start")
    await asyncio.sleep(FAST_DELAY_SECONDS)
    _mark(FAST, "end")
    return {"text": f"fast:{payload}"}


async def slow_timer(payload: str) -> dict[str, Any]:
    """一支慢任务：延时更久，与快任务并行。"""
    _mark(SLOW, "start")
    await asyncio.sleep(SLOW_DELAY_SECONDS)
    _mark(SLOW, "end")
    return {"text": f"slow:{payload}"}


async def gate(route: str) -> dict[str, Any]:
    """把路由值透出给 if-else 的条件判断。"""
    _mark(GATE, "start")
    _mark(GATE, "end")
    return {"route": route}


async def gate_fast_arm(text: str) -> dict[str, Any]:
    """``route == fast`` 时走的支。"""
    _mark(GATE_FAST, "start")
    _mark(GATE_FAST, "end")
    return {"fast_arm": text}


async def gate_slow_arm(text: str) -> dict[str, Any]:
    """``route == slow`` 时走的支。"""
    _mark(GATE_SLOW, "start")
    _mark(GATE_SLOW, "end")
    return {"slow_arm": text}


async def join(
    fast_text: str,
    slow_text: str,
    gate_value: str,
    fast_arm: Any = None,
    slow_arm: Any = None,
) -> dict[str, Any]:
    """三路汇合点。执行次数由测试断言——这是「汇合只执行一次」的证据。"""
    _mark(JOIN, "start")
    _mark(JOIN, "end")
    chosen = fast_arm if fast_arm is not None else slow_arm
    return {
        "joined": f"{fast_text}|{slow_text}",
        "gate_value": gate_value,
        "chosen_arm": chosen,
    }


async def child_work(text: str) -> dict[str, Any]:
    """子工作流内部的唯一处理节点。"""
    _mark(CHILD_WORK, "start")
    await asyncio.sleep(0.05)
    _mark(CHILD_WORK, "end")
    return {"echo": f"echo:{text}", "note": "child-ok"}


async def finish(final_text: str, final_note: str) -> dict[str, Any]:
    """出口：把结果拼成对外字符串契约 ``result1``。"""
    _mark(END, "start")
    _mark(END, "end")
    return {"result1": f"{final_text}/{final_note}"}


CHILD_WORKFLOW = WorkflowDef(
    workflow_id=CHILD_WORKFLOW_ID,
    display_name="最小验证图（子流程）",
    source_dsl=None,
    entries=(CHILD_START,),
    exits=(CHILD_END,),
    outputs={"echo": "echo", "note": "note"},
    nodes=(
        make_node(
            CHILD_START,
            "子流程输入",
            "start",
            variables=("text",),
            required_text=True,
        ),
        make_node(
            CHILD_WORK,
            "子流程处理",
            "code",
            after=(CHILD_START,),
            inputs={"text": (CHILD_START, "text")},
            outputs=("echo", "note"),
            function="customer_profile.workflows.synthetic_fanout:child_work",
        ),
        make_node(
            CHILD_END,
            "子流程输出",
            "end",
            after=(CHILD_WORK,),
            inputs={"echo": (CHILD_WORK, "echo"), "note": (CHILD_WORK, "note")},
            outputs=("echo", "note"),
        ),
    ),
)


# 子工作流必须一并导出，否则主流程引用它时查不到（见 workflows/__init__.load_all）
SUBWORKFLOWS = (CHILD_WORKFLOW,)


WORKFLOW = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=None,
    entries=(START,),
    exits=(END,),
    outputs={"result1": "result1"},
    nodes=(
        make_node(
            START,
            "验证图输入",
            "start",
            variables=("payload", "route"),
            required_payload=True,
            required_route=False,
        ),
        make_node(
            FAST,
            "快分支",
            "code",
            after=(START,),
            inputs={"payload": (START, "payload")},
            outputs=("text",),
            function="customer_profile.workflows.synthetic_fanout:fast_timer",
        ),
        make_node(
            SLOW,
            "慢分支",
            "code",
            after=(START,),
            inputs={"payload": (START, "payload")},
            outputs=("text",),
            function="customer_profile.workflows.synthetic_fanout:slow_timer",
        ),
        make_node(
            GATE,
            "路由取值",
            "code",
            after=(START,),
            inputs={"route": (START, "route")},
            outputs=("route",),
            function="customer_profile.workflows.synthetic_fanout:gate",
        ),
        make_node(
            GATE_FAST,
            "快路由支",
            "code",
            # after 必须含 FAST：绑定表只能引用祖先节点（校验会拦下非祖先引用）
            after=(FAST, GATE),
            inputs={"text": (FAST, "text")},
            outputs=("fast_arm",),
            function="customer_profile.workflows.synthetic_fanout:gate_fast_arm",
        ),
        make_node(
            GATE_SLOW,
            "慢路由支",
            "code",
            after=(SLOW, GATE),
            inputs={"text": (SLOW, "text")},
            outputs=("slow_arm",),
            function="customer_profile.workflows.synthetic_fanout:gate_slow_arm",
        ),
        make_node(
            JOIN,
            "三路汇合",
            "code",
            after=(FAST, SLOW, GATE, GATE_FAST, GATE_SLOW),
            inputs={
                "fast_text": (FAST, "text"),
                "slow_text": (SLOW, "text"),
                "gate_value": (GATE, "route"),
                "fast_arm": (GATE_FAST, "fast_arm"),
                "slow_arm": (GATE_SLOW, "slow_arm"),
            },
            outputs=("joined", "gate_value", "chosen_arm"),
            function="customer_profile.workflows.synthetic_fanout:join",
        ),
        make_node(
            CHILD_CALL,
            "调用子流程",
            "tool",
            after=(JOIN,),
            # 只绑定子流程真正需要的入参；子流程自身的输出由 tool 执行器透出，
            # 不在这里绑定（绑定表只能引用本图的祖先节点）
            inputs={"text": (JOIN, "joined")},
            outputs=("echo", "note"),
            **{
                "@workflow": CHILD_WORKFLOW_ID,
                "@inputs": {"text": "text"},
            },
        ),
        make_node(
            FINISH,
            "结果拼接",
            "code",
            after=(CHILD_CALL,),
            inputs={
                "final_text": (CHILD_CALL, "echo"),
                "final_note": (CHILD_CALL, "note"),
            },
            outputs=("result1",),
            function="customer_profile.workflows.synthetic_fanout:finish",
        ),
        make_node(
            END,
            "验证图输出",
            "end",
            after=(FINISH,),
            # end 节点用内置执行器，只按绑定与 outputs 声明收集字段。result1 由上游
            # 拼接节点合成（end 不计算，只收集）。
            inputs={
                "final_text": (FINISH, "result1"),
                "final_note": (CHILD_CALL, "note"),
                "result1": (FINISH, "result1"),
            },
            outputs=("result1",),
        ),
    ),
)


def default_inputs(payload: str = "p", route: str = "fast") -> dict[str, Any]:
    return {"payload": payload, "route": route}
