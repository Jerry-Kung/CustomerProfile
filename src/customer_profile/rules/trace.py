"""规则留痕：每次规则触发都记一条，可排序、可统计、可回放。

字段与设计 §8 的 ``rule_trace`` 对齐。三条口径：

1. **一条动作一条 trace**，动作没生效（字段不存在）也要留痕——「规则命中了但没改到
   东西」本身就是治理信号，静默跳过会让命中率统计偏乐观。
2. **trace 是追加，不是替换。** 既有的 ``rejected`` / ``downgraded`` / ``to_confirm``
   进回传体，形状属契约；trace 只进批次产物与报告。改前者是契约变更，改后者不是。
3. **对客产出不得出现 trace 内容**（``docs/README.md`` 硬约束）。``reason`` 是给
   工程与核读用的，可能带编号；对客文案走 ``Action.customer_reason``。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class RuleTrace:
    """一条规则动作的留痕。"""

    rule_id: str
    stage: str
    attribute: str
    action: str
    severity: str
    reason: str
    source: str = "code"
    """执行通道：``code`` / ``llm_checker`` / ``prompt``。统计时按它分开看。"""

    before: Any = None
    after: Any = None
    version: int = 0
    """规则版本号。回看「这条规则什么时候开始生效」靠它，不靠提交历史。"""

    def asdict(self) -> dict[str, Any]:
        return asdict(self)

    def as_json(self) -> str:
        return json.dumps(self.asdict(), ensure_ascii=False, default=str)


@dataclass(slots=True)
class RuleTraceCollector:
    """一次投影过程中的 trace 累积器。

    不依赖 ``Projection``——执行器与后续的 checker 节点都往这里写，谁先谁后不影响
    最终顺序（顺序即动作应用顺序，按设计 §7.2 的优先级排定）。
    """

    entries: list[RuleTrace] = field(default_factory=list)

    def record(self, trace: RuleTrace) -> None:
        self.entries.append(trace)

    def extend(self, traces: Iterable[RuleTrace]) -> None:
        self.entries.extend(traces)

    def __len__(self) -> int:
        return len(self.entries)

    def as_dicts(self) -> list[dict[str, Any]]:
        return [entry.asdict() for entry in self.entries]

    def as_json(self) -> str:
        return json.dumps(self.as_dicts(), ensure_ascii=False, default=str)

    def counts_by_rule(self) -> dict[str, int]:
        """按规则编号计数——核读清单与命中率统计的最小单位。"""
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.rule_id] = counts.get(entry.rule_id, 0) + 1
        return dict(sorted(counts.items()))

    def counts_by_action(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.action] = counts.get(entry.action, 0) + 1
        return dict(sorted(counts.items()))
