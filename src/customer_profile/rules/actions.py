"""动作与统一执行器。

**规则函数只产出动作，不改状态**（设计 §4.1）。这是保证动作可排序、可留痕、可回放
的前提；迁移期最容易被破坏的也是这一条——往旧分支里顺手写一句
``facts.pop(...)``，规则就又变回了「散落在各处的 if」。

本模块不认识 ``Projection`` 这个类，只按属性名访问（duck-typed）：``facts`` /
``basis`` / ``to_confirm`` / ``rejected`` / ``downgraded``。这样规则包不必反向导入
``projection``（见 :mod:`customer_profile.rules.registry` 的依赖方向说明）。
SEVERITIES` / `ACTIONS` 的取值在 :mod:`customer_profile.rules.registry`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .registry import RuleRegistry, default as default_registry
from .trace import RuleTrace, RuleTraceCollector

BASIS_FACT = "fact"
BASIS_INFERRED_HIGH = "inferred_high"
BASIS_INFERRED_LOW = "inferred_low"
"""三分类置信档。字面量与 :mod:`customer_profile.projection` 的常量逐一对应，
此处重复声明是**有意的**：规则包不得导入投影层，而口头约定会在某次「顺手复用」时失效。
测试 ``tests/rules/test_actions.py`` 钉住三者同值。"""

TARGETS: tuple[str, ...] = ("field", "note", "evidence")
"""动作作用的**载体**。三种载体在管线里出现的位置不同，执行方式也不同：

- ``field``：改 ``Projection.facts`` / ``basis``（投影层）；
- ``note``：改 Notes 更新项（gate 层，条目整条删除或降级）；
- ``evidence``：改某条 Notes 的 ``reasoning_detail.evidence`` 里的一项
  （表达层写脏了证据位，只删那一项，不动条目）。

分开声明而不是让执行器去猜：同一个 ``reject`` 在三种载体上含义完全不同，靠
``payload`` 里有没有某个键来区分，是下一次误删的伏笔。
"""

DOWNGRADED_CONFIDENCE = 0.5
"""降级后写回 Notes 的置信度。

与 ``callback_build._DEFAULT_CONFIDENCE`` 同值（0.5）。理由：降级必须**可见**——
只改 ``basis`` 不改条目，销售点开看到的仍是原来的高置信，等于没降。
"""

KIND_PRIORITY: dict[str, int] = {"hard": 0, "consistency": 1, "spec": 2}
"""内容类优先级：**硬约束 > 一致性 > 合理性**。

设计 §7.2 写的是「硬约束 > 一致性 > 合理性 > 表达」。表达类（不写内部字段名、证据须
可回指）在登记表里记 ``content_kind=hard``，因为它们是**形态**约束；把它们排在最后
意味着前面的规则已经拿脏值判过一遍。硬类先执行、把形态清干净，后面的判断才建立在
可信的取值上。这条顺序差异在此显式记录，不留给读者猜。
"""


@dataclass(frozen=True, slots=True)
class Action:
    """一条待执行动作。

    ``payload`` 用于**迁移期保形**：旧内联分支写进 ``rejected`` / ``downgraded`` /
    ``to_confirm`` 的条目各有自定义键（``candidates`` / ``action`` 文案 / 多值候选），
    执行器的统一形状覆盖不了。给了 ``payload`` 就以它为条目本体，执行器只补字段名。
    """

    rule: str
    attribute: str
    action: str
    severity: str
    reason: str
    customer_reason: str = ""
    value: Any = None
    stage: str = "project"
    target: str = "field"
    """作用载体，见 :data:`TARGETS`。缺省 ``field``（投影层的 facts）。"""

    payload: dict[str, Any] | None = None
    record: bool = True
    """``False`` 表示改状态但**不写留痕条目**（如等级唯一化时静默清掉冗余键）。"""

    affects_basis: bool = True
    """``rewrite`` 是否改置信档。确定性规则（旧车⇒换购）不动档位，因此置 ``False``。"""

    remove: bool = True
    """``reject`` 是否真的把字段从 ``facts`` 移除。

    ``False`` 用于「只记一次冲突，值已经落定」的情形：等级唯一化先把高等级写进
    ``deal_level``，再把两处不一致的事实记进 ``rejected`` 供核读——那不是撤回该字段。
    """

    def key(self) -> tuple[str, str, str, str]:
        return (self.rule, self.attribute, self.action, repr(self.value))


@dataclass(slots=True)
class NotesState:
    """``target=note`` / ``target=evidence`` 的动作载体：一份 Notes 更新项。

    与 :class:`customer_profile.projection.Projection` 同形但那是有主之物（投影层
    的产物），gate 阶段只借它的字段。单独声明而不是复用，是为了让规则包与投影层
    保持单向依赖：规则包不认识 ``Projection``，只认识「有这几个属性的东西」。
    """

    notes: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    basis: dict[str, str] = field(default_factory=dict)
    to_confirm: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    downgraded: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ApplyResult:
    """一次执行的结果。"""

    traces: tuple[RuleTrace, ...] = ()
    rounds: int = 0
    unstable: bool = False
    """达到轮次上限仍未收敛。**不做无限收敛**（设计 §7.3）：不收敛通常意味着两条
    规则在互相打架，那是人的问题，不该由运行时硬转。"""

    noop: int = 0
    """字段不存在的动作数。留痕里能查到，计数在这里。"""


def _sort_key(action: Action, registry: RuleRegistry) -> tuple[int, int, str, str, str]:
    rule = registry.get(action.rule)
    kind = KIND_PRIORITY.get(rule.content_kind, 1) if rule else 1
    if rule is None:
        scope_rank = 1
    elif rule.scope == ("*",):
        # 全局规则排在最后。判据必须是「scope 就是通配」，不能问
        # :meth:`Rule.matches_scope`——后者把 ``*`` 也算作命中，于是全局规则会与
        # 字段级规则并列，再按编号排序时排在前面，把它本该最后执行的位置抢走。
        scope_rank = 2
    elif rule.matches_scope(action.attribute):
        scope_rank = 0
    else:
        # 扇出到作用域外的字段：规则没声明这个字段，但动作已经产生了（可能是簇级规则
        # 的派生动作）。排在字段级之后、全局之前，不丢弃。
        scope_rank = 1
    return (kind, scope_rank, action.rule, action.attribute, action.action)


def order(actions: Iterable[Action], registry: RuleRegistry | None = None) -> list[Action]:
    """按设计 §7.2 排序：内容类 → 作用域 → 编号稳定序。"""
    table = registry if registry is not None else default_registry()
    unique: dict[tuple[str, str, str, str], Action] = {}
    for action in actions:
        unique.setdefault(action.key(), action)
    return sorted(unique.values(), key=lambda item: _sort_key(item, table))


def apply(
    state: Any,
    actions: Sequence[Action],
    *,
    check: Callable[[Any], Iterable[Action]] | None = None,
    max_rounds: int = 3,
    registry: RuleRegistry | None = None,
    source: str = "code",
) -> ApplyResult:
    """把动作应用到 ``state``，可选地收敛。

    ``check`` 是「复查器」：每轮应用后重新跑一遍本阶段全部规则，返回新产生的动作。
    设计 §7.3 要求这样做——A 规则降级后 B 规则的输入条件可能变了。最多 ``max_rounds``
    轮；``check`` 为空时只应用一轮（调用方自行保证顺序，如 gate 阶段的一次性收口）。
    """
    table = registry if registry is not None else default_registry()
    collector = RuleTraceCollector()
    seen: set[tuple[str, str, str, str]] = set()
    noop = 0
    rounds = 0
    pending = list(actions)

    for index in range(max_rounds):
        batch = [item for item in order(pending, table) if item.key() not in seen]
        if not batch:
            break
        rounds = index + 1
        for action in batch:
            seen.add(action.key())
            changed, trace = _apply_one(state, action, table, source)
            if trace is not None:
                collector.record(trace)
            if not changed:
                noop += 1
        if check is None:
            break
        pending = list(check(state))

    # 收敛判据是「还有没执行过的动作」，不是「跑满了轮数」——正好在最后一轮收敛
    # 是正常结束，不该记账成 unstable。
    leftover = [item for item in order(pending, table) if item.key() not in seen] if check else []
    unstable = bool(leftover)
    # 逐条记未执行的动作用**它自己**的规则编号。此前这段读的是上一轮循环残留的
    # ``action``，记下的是一条已执行动作的编号——排查时追到的规则根本不是它。
    for item in leftover:
        collector.record(
            RuleTrace(
                rule_id=item.rule,
                stage=item.stage,
                attribute=item.attribute,
                action=item.action,
                severity=item.severity,
                reason=f"[unstable] 规则应用 {max_rounds} 轮仍未收敛，未执行：{item.reason}",
                source=source,
                before=getattr(state, "facts", {}).get(item.attribute),
            )
        )
    return ApplyResult(
        traces=tuple(collector.entries),
        rounds=rounds,
        unstable=unstable,
        noop=noop,
    )


def _apply_one(
    state: Any, action: Action, registry: RuleRegistry, source: str
) -> tuple[bool, RuleTrace | None]:
    """执行一条动作，返回「是否真的改了状态」与 trace。"""
    if action.target == "note":
        changed, before, after = _apply_note(state, action)
    elif action.target == "evidence":
        changed, before, after = _apply_evidence(state, action)
    else:
        changed, before, after = _apply_field(state, action)

    rule = registry.get(action.rule)
    trace = RuleTrace(
        rule_id=action.rule,
        stage=action.stage,
        attribute=action.attribute,
        action=action.action,
        severity=action.severity,
        reason=action.reason,
        source=source,
        before=before,
        after=after,
        version=rule.version if rule else 0,
    )
    return changed, trace


def _entry(action: Action, default: dict[str, Any]) -> dict[str, Any]:
    """条目本体：有 ``payload`` 用它，否则用执行器的统一形状。"""
    return dict(action.payload) if action.payload is not None else default


def _apply_field(
    state: Any, action: Action
) -> tuple[bool, Any, Any]:
    """``target=field``：改 ``Projection.facts`` / ``basis``。"""
    facts: dict[str, Any] = state.facts
    basis: dict[str, str] = state.basis
    before = facts.get(action.attribute)
    after = before
    changed = False

    if action.action == "reject":
        if action.attribute in facts and action.remove:
            before = facts.pop(action.attribute)
            after = None
            basis.pop(action.attribute, None)
            changed = True
        # 断言级拒收（payload 自带条目）与「只记冲突」都不改 facts。
        if changed or action.payload is not None:
            if action.record:
                state.rejected.append(
                    _entry(
                        action,
                        {
                            "attribute": action.attribute,
                            "value": before,
                            "rule": action.rule,
                            "reason": action.reason,
                        },
                    )
                )
    elif action.action == "downgrade":
        present = action.attribute in facts
        # 阶段 b 的降级发生在字段写入 facts **之前**（来源分级先于取值落定），
        # 因此降级不能以「字段已在 facts 里」为前提——只要有条目要记，就落档。
        if present or action.payload is not None:
            if action.record:
                state.downgraded.append(
                    _entry(
                        action,
                        {
                            "attribute": action.attribute,
                            "value": before,
                            "rule": action.rule,
                            "reason": action.reason,
                            "action": "降为 inferred_low（保留取值）",
                        },
                    )
                )
            basis[action.attribute] = BASIS_INFERRED_LOW
            changed = True
    elif action.action == "rewrite":
        if action.value is not None:
            facts[action.attribute] = action.value
            after = action.value
            if (
                action.affects_basis
                and basis.get(action.attribute, BASIS_FACT) == BASIS_FACT
            ):
                # 改值必须有确定性依据，且依据不是「客户原话」——否则就是编造确定事实。
                basis[action.attribute] = BASIS_INFERRED_HIGH
            changed = before != after
    elif action.action == "require_confirm":
        _confirm(state, action)
        changed = True
    elif action.action == "annotate":
        # 只加说明，不动 facts。落点由调用方在 ``notes_updates`` 里读 trace 处理。
        changed = False
    return changed, before, after


def _apply_note(state: Any, action: Action) -> tuple[bool, Any, Any]:
    """``target=note``：改 Notes 更新项（gate 层）。

    「拦」在这里的语义是**改产物**，不是记 issue：占位串整条删掉、非法取值降置信。
    只记 issue 的话，违规条目照样进回传体——那正是蓝图 §2.2 说的「能判但不拦」。
    """
    notes: list[dict[str, Any]] = state.notes
    before = [
        entry.get("attribute_value")
        for entry in notes
        if entry.get("attribute_key") == action.attribute
    ]
    after = before
    changed = False

    if action.action == "reject":
        remaining = [
            entry
            for entry in notes
            if entry.get("attribute_key") != action.attribute
        ]
        if len(remaining) != len(notes):
            notes[:] = remaining
            changed = True
        if action.record and (changed or action.payload is not None):
            state.rejected.append(
                _entry(
                    action,
                    {
                        "attribute": action.attribute,
                        "value": before,
                        "rule": action.rule,
                        "reason": action.reason,
                    },
                )
            )
    elif action.action == "downgrade":
        for entry in notes:
            if entry.get("attribute_key") != action.attribute:
                continue
            # 降级必须**可见**：只改 basis 不改条目，销售点开看到的仍是原来的高置信。
            detail = entry.get("reasoning_detail")
            if not isinstance(detail, dict):
                detail = {}
            detail["confidence"] = DOWNGRADED_CONFIDENCE
            entry["reasoning_detail"] = detail
            changed = True
        if changed:
            state.basis[action.attribute] = BASIS_INFERRED_LOW
            if action.record:
                state.downgraded.append(
                    _entry(
                        action,
                        {
                            "attribute": action.attribute,
                            "value": before,
                            "rule": action.rule,
                            "reason": action.reason,
                            "action": "降为低置信（保留取值）",
                        },
                    )
                )
    elif action.action == "rewrite":
        # 改写 Notes 的**说明文字**（``reasoning_detail``），不动 ``attribute_value``：
        # 取值是契约枚举键（前端按 ``display_name`` 映射），正文才是给销售看的文案。
        # ``payload["reasoning_detail"]`` 由规则簇给出替换后的完整四键，执行器只落地。
        replacement = (action.payload or {}).get("reasoning_detail")
        if isinstance(replacement, Mapping):
            for entry in notes:
                if entry.get("attribute_key") != action.attribute:
                    continue
                entry["reasoning_detail"] = dict(replacement)
                changed = True
    elif action.action == "require_confirm":
        _confirm(state, action)
        changed = True
    return changed, before, after


def _apply_evidence(state: Any, action: Action) -> tuple[bool, Any, Any]:
    """``target=evidence``：删掉某条 Notes 证据位里的一个问题串。

    只删那一项，不动条目本身——证据位写脏了（把结论当依据），值可能仍然成立。
    """
    snippet = (action.payload or {}).get("snippet")
    before = [
        str(item)
        for entry in state.notes
        if entry.get("attribute_key") == action.attribute
        for item in ((entry.get("reasoning_detail") or {}).get("evidence") or [])
    ]
    changed = False
    for entry in state.notes:
        if entry.get("attribute_key") != action.attribute:
            continue
        detail = entry.get("reasoning_detail")
        if not isinstance(detail, dict):
            continue
        evidence = [str(item) for item in (detail.get("evidence") or [])]
        kept = [item for item in evidence if snippet is None or snippet not in item]
        if len(kept) != len(evidence):
            detail["evidence"] = kept
            changed = True
    if action.record and changed:
        state.rejected.append(
            _entry(
                action,
                {
                    "attribute": action.attribute,
                    "value": snippet,
                    "rule": action.rule,
                    "reason": action.reason,
                },
            )
        )
    return changed, before, [snippet]


def _confirm(state: Any, action: Action) -> None:
    """要求人工确认。**条目里不得出现规则编号**——``to_confirm`` 会随回传体下发。"""
    entry = _entry(
        action,
        {
            "attribute_key": action.attribute,
            "reason": action.customer_reason or action.reason,
            "suggested_action": action.customer_reason or "请销售与客户确认",
        },
    )
    if action.record and not any(
        item.get("attribute_key") == action.attribute for item in state.to_confirm
    ):
        state.to_confirm.append(entry)
