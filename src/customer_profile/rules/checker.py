"""llm_checker 通道：把「写成规则表太硬、写成提示词又没人执行」的语义判断交给模型。

这一通道只做一件事：拿可读化事实 + 少量规则说明，问模型「这几条规则有没有被违反」，
再由代码复核它答得能不能用。**它不产生新取值**——动作白名单只有降级与转确认，
任何带 ``value`` 的裁决一律丢弃。这是设计 §4.3 的硬边界：checker 是「查」，不是「补」。

## 为什么是 code 节点而不是 llm 节点

调度器对失败节点会**阻塞整棵下游子树**（``scheduler._block_descendants``）。若把这一
步做成 ``llm`` 节点，一次调用失败就会连带挡掉定级、表达与回传——规则层成了新的单点。
因此本模块以 ``code`` 节点落地，内部调用统一 LLM 层并**吞掉全部异常**：失败只留一条
``checker_failed``，管线照常往下走。LLM 客户端由 ``rt.extra['code_functions']`` 注入，
code 函数本身保持可单测（不传 ``llm`` 即视为「未启用」）。

## 关闭即零 token

``RULE_CHECKER_ENABLED`` 缺省 false。关闭时 ``run_check`` 在**发起调用之前**返回，
不渲染、不请求、不重试——开关的意义就是省钱，走到调用那一步再判断等于没关。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

from .. import internal_fields as inf
from .. import text_utils as tut
from . import actions as rule_actions
from .registry import RuleRegistry, default as default_registry

CHECKER_BUDGET = 2
"""每案 checker 调用上限。超出记 ``budget_exceeded`` 并把剩余簇转人工。

M5 首批只挂一条规则、一个簇，实际上限远用不满；这个常量是给后续加簇留的闸门——
「多挂几条规则」不等于「多调几次模型」，超了就是组装粒度出了问题。
"""

ALLOWED_ACTIONS: frozenset[str] = frozenset({"downgrade", "require_confirm"})
"""checker 能提的动作。**不含 ``reject`` / ``rewrite``**：

- ``reject`` 意味着删条目，那是掌握确定证据才该做的判断，模型只有可读化事实；
- ``rewrite`` 要带新值，而 checker 不产生取值（见模块说明）。
"""

MAX_EXPLAIN = 200
"""``explain`` 长度上限。它是写给人看的理由，超长基本是模型把整份事实复述了一遍。"""


def _text(value: Any) -> str:
    return tut.as_text(value).strip()


def _payload(value: Any) -> Any:
    return tut.as_payload(value)


# ====================================================================
# 组装：哪些规则这一案要查
# ====================================================================


def enabled_rules(
    registry: RuleRegistry | None = None, disabled: Iterable[str] = ()
) -> list[Any]:
    """挂了 ``llm_checker`` 通道的 ``active`` 规则（去掉灰度关闭的）。"""
    # rule: INCOME-01 职业与收入量级不自洽（判定在模型，落地在 recheck 的动作白名单）
    table = registry if registry is not None else default_registry()
    drop = {str(item).strip() for item in disabled if str(item).strip()}
    return [
        rule
        for rule in table.active()
        if rule.enables_channel("llm_checker") and rule.id not in drop
    ]


def briefs(
    facts: Mapping[str, Any],
    *,
    registry: RuleRegistry | None = None,
    disabled: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """本簇待检规则的说明，只含**中文字段名**。

    模型不得看见内部字段键（设计 §4.3 硬边界 1）：它一旦看到 ``annual_income``，
    写理由时就会照抄进对客文案。因此这里用 :func:`internal_fields.name_of` 翻成中文名，
    复核时再翻回来。

    作用域内字段**全部缺失或全是占位串**的规则不送检——没有可比对的两条信息，
    问模型只会得到一条「无法判断」，白烧一次调用。
    """
    picked: list[dict[str, Any]] = []
    for rule in enabled_rules(registry, disabled):
        present: list[dict[str, str]] = []
        for key in rule.scope:
            if key == "*":
                continue
            value = facts.get(key)
            if value is None:
                continue
            text = _text(value)
            if not text or text in {"未知", "未提供", "无", "暂无"}:
                continue
            present.append({"key": key, "name": inf.name_of(key), "value": text})
        if len(present) < 2:
            continue
        picked.append(
            {
                "rule": rule.id,
                "title": rule.title,
                "failure_mode": rule.message_internal,
                "actions": sorted(ALLOWED_ACTIONS & {rule.default_action})
                or ["downgrade"],
                "fields": present,
            }
        )
    return picked


def brief_json(brief_list: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(list(brief_list), ensure_ascii=False)


# ====================================================================
# 复核：模型答的东西能不能用
# ====================================================================


def recheck(
    verdict_json: Any,
    brief_list: Sequence[Mapping[str, Any]],
    *,
    stage: str = "project",
) -> tuple[list[rule_actions.Action], list[dict[str, Any]]]:
    """把模型裁决过成动作。返回 ``(动作, 丢弃明细)``。

    四条复核，逐条对应一种可预见的胡来：

    1. **编号必须在本次送检清单里**——模型会顺手评论没让它看的规则；
    2. **字段必须属于该规则的作用域**——防止把 A 规则的意见挂到 B 字段上；
    3. **动作必须在白名单内**——它不该有删条目、改取值的权力；
    4. **不得携带新值**——带 ``value`` 的直接丢弃，不看它写得多合理。

    字段用中文名回传，这里翻回内部键；翻不回来的同样丢弃——宁可少一条提示，
    也不让一条对不上字段的裁决进产物。
    """
    by_id = {str(item["rule"]): item for item in brief_list}
    raw = _payload(verdict_json)
    if isinstance(raw, Mapping):
        raw = raw.get("findings") or raw.get("verdicts") or []
    if not isinstance(raw, (list, tuple)):
        raw = []

    accepted: list[rule_actions.Action] = []
    dropped: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            dropped.append({"reason": "条目不是对象", "item": str(item)[:80]})
            continue
        rule_id = _text(item.get("rule"))
        brief = by_id.get(rule_id)
        if brief is None:
            dropped.append({"reason": "规则不在本次送检清单", "rule": rule_id})
            continue
        if "value" in item or "new_value" in item:
            dropped.append({"reason": "checker 不得携带新值", "rule": rule_id})
            continue
        action = _text(item.get("action")) or "downgrade"
        if action not in (brief.get("actions") or ALLOWED_ACTIONS):
            dropped.append({"reason": f"动作 {action} 不在白名单", "rule": rule_id})
            continue
        explain = _text(item.get("explain"))
        if not explain or len(explain) > MAX_EXPLAIN:
            dropped.append({"reason": "理由缺失或过长", "rule": rule_id})
            continue

        names = item.get("fields")
        if isinstance(names, str):
            names = [names]
        keys = _resolve_fields(names, brief)
        if not keys:
            dropped.append({"reason": "字段对不上规则作用域", "rule": rule_id})
            continue

        for key in keys:
            accepted.append(
                rule_actions.Action(
                    rule=rule_id,
                    attribute=key,
                    action=action,
                    severity="repair",
                    reason=explain,
                    customer_reason=explain if action == "require_confirm" else "",
                    target="note",
                    stage=stage,
                )
            )
    return accepted, dropped


def _resolve_fields(names: Any, brief: Mapping[str, Any]) -> list[str]:
    """中文名 → 内部键。只在该规则自己的字段里找。"""
    if not isinstance(names, (list, tuple)):
        return []
    scope = {str(field["name"]): str(field["key"]) for field in brief.get("fields") or []}
    keys: list[str] = []
    for name in names:
        key = scope.get(_text(name))
        if key and key not in keys:
            keys.append(key)
    return keys


# ====================================================================
# 节点入口
# ====================================================================


async def run_check(
    facts_json: Any = "{}",
    notes_updates_json: Any = "[]",
    basis_json: Any = "{}",
    llm: Any = None,
    templates: Any = None,
    enabled: bool | None = None,
    template_name: str = "customer_profile_pipeline__pipeline_rule_check",
    registry: RuleRegistry | None = None,
    disabled: Iterable[str] = (),
) -> dict[str, Any]:
    """RULE_CHECK 节点：组装提示词、调一次模型、原样交出裁决。

    **失败一律不抛**：调用异常、JSON 解析失败、模型返回空，都只体现为
    ``checker_status`` 与 ``verdict_json`` 为空。管线靠这一点做到「规则层不是新单点」。
    """
    facts = _payload(facts_json) if facts_json is not None else {}
    if not isinstance(facts, Mapping):
        facts = {}
    notes = _payload(notes_updates_json)
    if not isinstance(notes, (list, tuple)):
        notes = []
    basis = _payload(basis_json) if basis_json is not None else {}
    if not isinstance(basis, Mapping):
        basis = {}

    switch = _enabled(enabled)
    brief_list = briefs(facts, registry=registry, disabled=disabled) if switch else []
    if not switch:
        return _result("disabled", [], 0, notes)
    if not brief_list:
        return _result("no_rule_applicable", [], 0, notes)
    if llm is None:
        return _result("failed", [], 0, notes, error="未注入 LLM 客户端")
    if CHECKER_BUDGET < 1:
        return _result("budget_exceeded", [], 0, notes)

    prompt = _render(template_name, brief_list, facts, templates)
    try:
        verdict = await llm.llm_call(
            prompt, expect_json=True, max_attempts=1
        )
    except Exception as exc:  # noqa: BLE001 —— 失败不阻断是本通道的定义
        return _result("failed", brief_list, 1, notes, error=str(exc)[:200])
    return _result("ok", brief_list, 1, notes, verdict=verdict)


def _enabled(explicit: bool | None) -> bool:
    """``RULE_CHECKER_ENABLED`` 的运行时取值。显式传入优先（测试与离线回放用）。

    读配置失败按**关闭**处理：checker 是新增花费，配置读不出来时不该默认开。
    """
    if explicit is not None:
        return bool(explicit)
    try:
        from ..settings import get_settings

        return bool(get_settings().rule_checker_enabled)
    except Exception:
        return False


def prompt_view(brief_list: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """送检清单的**模型可见形态**：剥掉内部字段键 ``key``。

    ``briefs()`` 的产物带 ``key``，那是复核时把中文名翻回内部键用的；模型不该看见它
    （设计 §4.3 硬边界 1）——它一旦看到 ``annual_income``，写理由时就会照抄，而理由
    会进对客文案。两个用途指向两个形态，不能共用一份。
    """
    view: list[dict[str, Any]] = []
    for item in brief_list:
        view.append(
            {
                "rule": item.get("rule"),
                "title": item.get("title"),
                "failure_mode": item.get("failure_mode"),
                "actions": list(item.get("actions") or ()),
                "fields": [
                    {"name": field.get("name"), "value": field.get("value")}
                    for field in item.get("fields") or ()
                ],
            }
        )
    return view


def _render(
    template_name: str, brief_list: Sequence[Mapping[str, Any]], facts: Mapping[str, Any],
    templates: Any,
) -> str:
    from .. import readable as rdb

    repository = templates
    if repository is None:
        from ..templates import TemplateRepository

        repository = TemplateRepository()
    return repository.render(
        template_name,
        {
            "arg1": rdb.readable_facts(dict(facts)),
            "arg2": json.dumps(prompt_view(brief_list), ensure_ascii=False, indent=2),
        },
    )


def _result(
    status: str,
    brief_list: Sequence[Mapping[str, Any]],
    calls: int,
    notes: Sequence[Any],
    *,
    verdict: Any = None,
    error: str = "",
) -> dict[str, Any]:
    payload = verdict if isinstance(verdict, Mapping) else {"findings": []}
    return {
        "verdict_json": json.dumps(payload, ensure_ascii=False),
        "brief_json": brief_json(brief_list),
        "checker_status": status,
        "checker_calls": calls,
        "checker_error": error,
        "notes_updates_json": json.dumps(list(notes), ensure_ascii=False, default=str),
    }
