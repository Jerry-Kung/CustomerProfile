"""规则登记表：一条规则一个定义处。

定位见 ``docs/history/rules-opt01/规则层设计.md`` §3。本模块只回答「规则是什么、登记在哪」，
不执行任何规则——执行在 :mod:`customer_profile.rules.actions`，渲染在
:mod:`customer_profile.rules.render`。

三条硬约束：

1. **id 一经发出不复用。** 退役用 ``status=deprecated``，不删条目、不换编号。
2. **``active`` 必须有正反例测试引用。** 没有测试的规则不允许生效——这是启动期自检
   拦下的第一类错误，因为「规则写了但没人验证过」正是本层要消灭的形态。
3. **登记表在启动期自检。** :func:`default` 首次调用即装载并校验，失败直接抛，
   不带病开图。运行期不再二次校验（规则集是常量，不是用户输入）。

依赖方向：本包只依赖 :mod:`customer_profile.notes_contract` /
:mod:`customer_profile.internal_fields` / :mod:`customer_profile.text_utils` /
:mod:`customer_profile.income_band`。**不得**导入 ``projection`` 或 ``workflows.*``
——规则层反向依赖管线会立刻成环，也意味着「规则不再能被单独测试」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
"""规则编号形态：``<簇>-<序号>``，如 ``INCOME-01`` / ``CONSIST-03``。

短横线分隔而不是下划线：编号要能在一行代码注释里被正则稳定检索（见
:mod:`customer_profile.rules.guard`），短横线在中文文本里也不会被误当成词内符号。
"""

CONTENT_KINDS: tuple[str, ...] = ("hard", "spec", "consistency")
"""规则**说什么**（与执行通道是两维，见设计 §2）。

- ``hard``：输出形态、取值合法性、身份归属；
- ``spec``：某字段该怎么填、参考哪些源、证据怎么写；
- ``consistency``：字段间、事实与表达间的矛盾。
"""

CHANNELS: tuple[str, ...] = ("code", "prompt", "llm_checker")
"""规则**谁来执行**。一条规则可同时挂多个通道（同一份定义的两个投影）。"""

STAGES: tuple[str, ...] = ("extract", "project", "express", "gate")
"""生效阶段。与通道分开存，因为同一条规则可以在多个阶段各落一次。"""

SEVERITIES: tuple[str, ...] = ("block", "repair", "warn")
ACTIONS: tuple[str, ...] = ("reject", "downgrade", "rewrite", "require_confirm", "annotate")
"""动作语义见设计 §7.1。**默认动作是降级或转人工，不是删除。**"""

OWNERS: tuple[str, ...] = ("business", "engineering", "both")
STATUSES: tuple[str, ...] = ("proposed", "active", "deprecated")


class RuleTableError(ValueError):
    """登记表自检未通过。``issues`` 含全部问题，便于一次修完。"""

    def __init__(self, issues: Iterable[str]) -> None:
        self.issues = list(issues)
        super().__init__("规则登记表自检未通过：\n" + "\n".join(f"  - {i}" for i in self.issues))


def _require_choice(name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ValueError(f"规则字段 {name}={value!r} 非法，可选：{', '.join(allowed)}")


def _require_choices(name: str, values: tuple[str, ...], allowed: tuple[str, ...]) -> None:
    if not values:
        raise ValueError(f"规则字段 {name} 不得为空")
    for value in values:
        _require_choice(name, value, allowed)
    if len(set(values)) != len(values):
        raise ValueError(f"规则字段 {name} 有重复值：{values}")


@dataclass(frozen=True, slots=True)
class Rule:
    """一条规则的完整身份。

    与设计 §3 的概念字段逐项对应，另加两项工程字段：

    - ``tests``：正反例测试的引用（``"tests/rules/test_income.py::test_xxx"`` 这种
      人可读形态即可，不解析）。``active`` 要求至少两条——一条正例、一条反例。
    - ``legacy_codes``：迁移前散落在代码里的裸串（如 ``"f_trade_in"``）。保留它只为
      **跨批次对账**：旧批次产物里记的是裸串，新 trace 记的是编号，没有这张对照表
      就无法回答「这条规则上一批命中过几次」。规则退役时它随之失效，但条目保留。
    """

    id: str
    version: int
    title: str
    content_kind: str
    channels: tuple[str, ...]
    scope: tuple[str, ...]
    stages: tuple[str, ...]
    severity: str
    default_action: str
    message_internal: str
    message_customer: str
    owner: str
    status: str
    cluster: str = ""
    tests: tuple[str, ...] = ()
    legacy_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not ID_PATTERN.match(self.id):
            raise ValueError(f"规则编号 {self.id!r} 不合形态（应为 <簇>-<序号>，如 INCOME-01）")
        if not isinstance(self.version, int) or self.version < 1:
            raise ValueError(f"规则 {self.id} 的 version 必须是 >=1 的整数")
        if not self.title.strip():
            raise ValueError(f"规则 {self.id} 缺少 title")
        _require_choice("content_kind", self.content_kind, CONTENT_KINDS)
        _require_choices("channels", self.channels, CHANNELS)
        _require_choices("stages", self.stages, STAGES)
        _require_choice("severity", self.severity, SEVERITIES)
        _require_choice("default_action", self.default_action, ACTIONS)
        _require_choice("owner", self.owner, OWNERS)
        _require_choice("status", self.status, STATUSES)
        if not self.scope:
            raise ValueError(f"规则 {self.id} 的 scope 不得为空；全局规则写 ('*',)")
        if not self.message_internal.strip():
            raise ValueError(f"规则 {self.id} 缺少对内解释（message_internal）")
        # 对客解释只在**真的会写进对客产物**的动作上必填。当前执行器里只有
        # ``require_confirm`` 会把它写进 ``to_confirm``（那张表直接下发）。
        # ``reject`` / ``downgrade`` 只在回传体落一个置信档，``annotate`` 只留 trace
        # ——要求它们也写一句，只会逼出「本条已降级」这种把内部口径塞进对客位的话，
        # 正是可读性约束要防的。
        if self.default_action == "require_confirm" and not self.message_customer.strip():
            raise ValueError(
                f"规则 {self.id} 的默认动作是 require_confirm，会写进 to_confirm，"
                "因此必须给出对客解释（message_customer）"
            )

    def matches_scope(self, attribute: str) -> bool:
        """该规则是否作用于某字段。``("*",)`` 视为全部字段。"""
        return "*" in self.scope or attribute in self.scope

    def enables_channel(self, channel: str) -> bool:
        return channel in self.channels

    def enables_stage(self, stage: str) -> bool:
        return stage in self.stages


class RuleRegistry:
    """规则的唯一登记处。构造后**不可变**（``register`` 仅用于装载期）。"""

    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}
        self._legacy: dict[str, str] = {}

    # ---------------------------------------------------------------- 装载
    def register(self, rule: Rule) -> Rule:
        if rule.id in self._rules:
            raise RuleTableError([f"规则编号重复：{rule.id}"])
        for code in rule.legacy_codes:
            owner = self._legacy.get(code)
            if owner is not None:
                raise RuleTableError(
                    [f"旧裸串 {code!r} 同时登记给 {owner} 与 {rule.id}——对账会分不清归属"]
                )
            if code in self._rules:
                raise RuleTableError([f"旧裸串 {code!r} 与规则编号同名"])
            self._legacy[code] = rule.id
        self._rules[rule.id] = rule
        return rule

    def extend(self, rules: Iterable[Rule]) -> None:
        for rule in rules:
            self.register(rule)

    # ---------------------------------------------------------------- 查询
    def get(self, rule_id: str) -> Rule | None:
        return self._rules.get(rule_id)

    def require(self, rule_id: str) -> Rule:
        rule = self._rules.get(rule_id)
        if rule is None:
            raise KeyError(f"规则 {rule_id!r} 未登记。已登记：{sorted(self._rules)}")
        return rule

    def resolve_legacy(self, code: str) -> Rule | None:
        rule_id = self._legacy.get(code)
        return self._rules.get(rule_id) if rule_id else None

    def all(self) -> tuple[Rule, ...]:
        return tuple(self._rules[key] for key in sorted(self._rules))

    def active(self) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.all() if rule.status == "active")

    def __len__(self) -> int:
        return len(self._rules)

    def __iter__(self) -> Iterator[Rule]:
        return iter(self.all())

    def __contains__(self, rule_id: object) -> bool:
        return rule_id in self._rules

    # ---------------------------------------------------------------- 灰度
    def without(self, disabled: Iterable[str]) -> "RuleRegistry":
        """去掉被禁用的规则，返回新登记表（原表不动）。

        单条规则的回滚 = 往 ``RULES_DISABLED`` 加一个编号，**不整批回滚提示词**。
        未登记的编号被忽略而不是报错：灰度配置比代码先改是常态，配置里多一个
        已退役的编号不该阻断运行。
        """
        drop = {str(item).strip() for item in disabled if str(item).strip()}
        if not drop:
            return self
        registry = RuleRegistry()
        for rule in self.all():
            if rule.id not in drop:
                registry.register(rule)
        return registry

    # ---------------------------------------------------------------- 自检
    def verify(self) -> list[str]:
        """返回全部问题；空列表表示登记表可用。"""
        issues: list[str] = []
        prompt_scope: dict[str, str] = {}
        for rule in self._rules.values():
            if rule.status != "active":
                continue
            if len(rule.tests) < 2:
                issues.append(
                    f"{rule.id} 状态为 active，但正反例测试不足两条（现有 {len(rule.tests)} 条）"
                )
            if rule.cluster and not rule.cluster.strip():
                issues.append(f"{rule.id} 的 cluster 是空白串")
            # 同一字段被两条 active 提示词规则占用：渲染时先到先得，后者的正文会
            # **静默丢失**（SPEC-18 的 `intended_model` 就这样丢过一次）。正文只渲染
            # 一次的前提是「一个字段只有一条规则说它」，故在启动期拦下。
            if not rule.enables_channel("prompt"):
                continue
            for field in rule.scope:
                if field == "*":
                    continue
                owner = prompt_scope.get(field)
                if owner is not None:
                    issues.append(
                        f"{rule.id} 与 {owner} 都声明了取证方向 {field!r}——"
                        "同一字段只能有一条提示词规则，否则后者的正文会被丢弃"
                    )
                else:
                    prompt_scope[field] = rule.id
        return issues

    def require_valid(self) -> "RuleRegistry":
        issues = self.verify()
        if issues:
            raise RuleTableError(issues)
        return self


_DEFAULT: RuleRegistry | None = None


def load() -> RuleRegistry:
    """从 ``clusters/`` 装载一份新登记表并自检。每次调用重新构建，供测试隔离。"""
    from .clusters import iter_rules

    registry = RuleRegistry()
    registry.extend(iter_rules())
    return registry.require_valid()


def default() -> RuleRegistry:
    """进程内共享的登记表。首次调用即完成「启动期自检」。"""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = load()
    return _DEFAULT


def reset_default() -> None:
    """清空进程内缓存（测试用）。"""
    global _DEFAULT
    _DEFAULT = None


def as_dicts(registry: RuleRegistry | None = None) -> list[dict[str, Any]]:
    """把登记表摊成可序列化的字典列表，供报告与对账脚本使用。"""
    from dataclasses import asdict

    source = registry if registry is not None else default()
    return [asdict(rule) for rule in source.all()]
