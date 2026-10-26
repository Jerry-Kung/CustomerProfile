"""checker 通道：语义判断交给模型，能不能用由代码说了算。

这一层的验收点不是「模型答得对」，而是**模型答错也进不了产物**：

- 编号不在送检清单 → 丢；
- 动作不在白名单（``reject`` / ``rewrite``）→ 丢；
- 带了取值 → 丢（checker 不负责定值）；
- 字段中文名对不上作用域 → 丢；
- 调用失败 → 什么都不改，管线照常往下走。

正例只有一条：真的存在不自洽时，收入被降级、取值保留。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from customer_profile.rules import NotesState, apply
from customer_profile.rules import checker
from customer_profile.workflows import customer_profile_pipeline as pipe


class FakeLlm:
    """记录调用次数，按预设返回。"""

    def __init__(self, payload, *, fail: bool = False):
        self.payload = payload
        self.fail = fail
        self.calls = 0
        self.prompts: list[str] = []

    async def llm_call(self, prompt, **kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("模型超时")
        return self.payload


class FakeTemplates:
    def render(self, name, variables, **kwargs):
        self.last = variables
        return "PROMPT"


MISMATCH = {"occupation": "网约车司机", "annual_income": "30-50 万"}
CONSISTENT = {"occupation": "网约车司机", "annual_income": "5-10 万"}


def notes_of(*entries):
    return json.dumps(list(entries), ensure_ascii=False)


def entry(key, value):
    return {
        "attribute_key": key,
        "attribute_value": value,
        "reasoning_detail": {"summary": "", "steps": [], "evidence": [], "confidence": 0.9},
    }


# ---------------------------------------------------------------- 组装

def test_briefs_use_chinese_names_only():
    """送检清单不得出现内部字段键以外的英文——模型看到什么就会抄什么。"""
    briefs = checker.briefs(MISMATCH)
    assert [item["rule"] for item in briefs] == ["INCOME-01"]
    names = [field["name"] for field in briefs[0]["fields"]]
    assert set(names) == {"职业", "年收入"}
    assert all(not any(ch.isascii() and ch.isalpha() for ch in name) for name in names)


def test_briefs_skip_rules_with_missing_field():
    """只有一个字段有值时不送检：没有第二个可比对的信息，问了也只能答「判不了」。"""
    assert checker.briefs({"occupation": "网约车司机"}) == []


def test_briefs_skip_placeholder_values():
    assert checker.briefs({"occupation": "未知", "annual_income": "30-50 万"}) == []


# ---------------------------------------------------------------- 复核

def verdict(*findings):
    return {"findings": list(findings)}


def test_income_01_fires_on_mismatched_pair():
    """正例：职业与收入量级不相容时，收入被降级、取值保留。"""
    briefs = checker.briefs(MISMATCH)
    actions, dropped = checker.recheck(
        verdict(
            {
                "rule": "INCOME-01",
                "hit": True,
                "fields": ["职业", "年收入"],
                "action": "downgrade",
                "explain": "职业为网约车司机，年收入记为 30-50 万，量级不相容",
            }
        ),
        briefs,
    )
    assert dropped == []
    assert {item.attribute for item in actions} == {"occupation", "annual_income"}
    assert all(item.action == "downgrade" for item in actions)

    state = NotesState(
        notes=[entry("occupation", "网约车司机"), entry("annual_income", "30-50 万")]
    )
    apply(state, actions, max_rounds=1)
    income = next(item for item in state.notes if item["attribute_key"] == "annual_income")
    assert income["attribute_value"] == "30-50 万"
    assert income["reasoning_detail"]["confidence"] == 0.5
    assert state.basis["annual_income"] == "inferred_low"


def test_income_01_silent_on_consistent_pair():
    """反例：同样的职业配 5-10 万不违反，不产生动作。"""
    briefs = checker.briefs(CONSISTENT)
    actions, dropped = checker.recheck(verdict(), briefs)
    assert actions == []
    assert dropped == []


# ---------------------------------------------------------------- 复核的边界

def test_recheck_drops_unknown_rule():
    actions, dropped = checker.recheck(
        verdict({"rule": "SPEC-01", "fields": ["职业"], "action": "downgrade", "explain": "x"}),
        checker.briefs(MISMATCH),
    )
    assert actions == []
    assert dropped[0]["reason"].startswith("规则不在本次送检清单")


def test_recheck_drops_disallowed_action():
    """checker 不该有删条目的权力。"""
    actions, dropped = checker.recheck(
        verdict(
            {"rule": "INCOME-01", "fields": ["年收入"], "action": "reject", "explain": "x"}
        ),
        checker.briefs(MISMATCH),
    )
    assert actions == []
    assert "白名单" in dropped[0]["reason"]


def test_recheck_drops_carried_value():
    """带取值的一律丢弃——即使理由写得再好。"""
    actions, dropped = checker.recheck(
        verdict(
            {
                "rule": "INCOME-01",
                "fields": ["年收入"],
                "action": "downgrade",
                "value": "5-10 万",
                "explain": "量级不相容",
            }
        ),
        checker.briefs(MISMATCH),
    )
    assert actions == []
    assert "新值" in dropped[0]["reason"]


def test_recheck_drops_field_outside_scope():
    actions, dropped = checker.recheck(
        verdict(
            {"rule": "INCOME-01", "fields": ["年龄"], "action": "downgrade", "explain": "x"}
        ),
        checker.briefs(MISMATCH),
    )
    assert actions == []
    assert "作用域" in dropped[0]["reason"]


# ---------------------------------------------------------------- 节点入口

async def test_run_check_disabled_makes_no_call():
    """开关关闭时零调用——在发起请求之前就返回。"""
    llm = FakeLlm(verdict())
    out = await checker.run_check(
        facts_json=json.dumps(MISMATCH, ensure_ascii=False),
        llm=llm,
        templates=FakeTemplates(),
        enabled=False,
    )
    assert llm.calls == 0
    assert out["checker_status"] == "disabled"
    assert out["checker_calls"] == 0


async def test_run_check_failure_does_not_raise():
    """一次调用失败不抛异常：规则层不能成为新的单点。"""
    out = await checker.run_check(
        facts_json=json.dumps(MISMATCH, ensure_ascii=False),
        llm=FakeLlm(None, fail=True),
        templates=FakeTemplates(),
        enabled=True,
    )
    assert out["checker_status"] == "failed"
    assert "模型超时" in out["checker_error"]


async def test_run_check_ok_passes_verdict_through():
    out = await checker.run_check(
        facts_json=json.dumps(MISMATCH, ensure_ascii=False),
        llm=FakeLlm(verdict()),
        templates=FakeTemplates(),
        enabled=True,
    )
    assert out["checker_status"] == "ok"
    assert out["checker_calls"] == 1
    assert json.loads(out["brief_json"])[0]["rule"] == "INCOME-01"


async def test_run_check_no_applicable_rule_makes_no_call():
    out = await checker.run_check(
        facts_json=json.dumps({"occupation": "教师"}, ensure_ascii=False),
        llm=FakeLlm(verdict()),
        templates=FakeTemplates(),
        enabled=True,
    )
    assert out["checker_status"] == "no_rule_applicable"


# ---------------------------------------------------------------- 落地节点

def test_rule_apply_writes_downgrade_into_product():
    briefs = checker.briefs(MISMATCH)
    out = pipe.rule_apply(
        verdict_json=json.dumps(
            verdict(
                {
                    "rule": "INCOME-01",
                    "fields": ["年收入"],
                    "action": "downgrade",
                    "explain": "职业为网约车司机，年收入 30-50 万偏高",
                }
            ),
            ensure_ascii=False,
        ),
        brief_json=json.dumps(briefs, ensure_ascii=False),
        notes_updates_json=notes_of(entry("annual_income", "30-50 万")),
        basis_json=json.dumps({"annual_income": "fact"}),
    )
    notes = json.loads(out["notes_updates_json"])
    assert notes[0]["reasoning_detail"]["confidence"] == 0.5
    assert json.loads(out["basis_json"])["annual_income"] == "inferred_low"
    assert out["checker_applied"] == 1


def test_rule_apply_is_noop_on_empty_verdict():
    out = pipe.rule_apply(
        verdict_json=json.dumps(verdict()),
        brief_json=json.dumps(checker.briefs(MISMATCH), ensure_ascii=False),
        notes_updates_json=notes_of(entry("annual_income", "30-50 万")),
        basis_json="{}",
    )
    assert out["checker_applied"] == 0
    assert json.loads(out["notes_updates_json"])[0]["attribute_value"] == "30-50 万"


# ---------------------------------------------------------------- 基线回放（离线）

BASELINE = Path(__file__).resolve().parents[2] / (
    "experiments/batches/rules-opt01-baseline-8-20261026"
)


def _baseline_facts():
    """逐案取 L2 投影的事实表。缺产物的案跳过（基线不是受控基线，可能不齐）。"""
    if not BASELINE.is_dir():
        pytest.skip("基线批次不在本机")
    out = {}
    for case in sorted((BASELINE / "runs").iterdir()):
        nodes_path = case / "nodes.json"
        if not case.is_dir() or not nodes_path.is_file():
            continue
        for entry in json.loads(nodes_path.read_text(encoding="utf-8")):
            if entry.get("node_id") == "pipeline_l2_project" and entry.get("status") == "succeeded":
                facts = entry.get("outputs", {}).get("facts_json")
                out[case.name] = json.loads(facts) if isinstance(facts, str) else {}
                break
    return out


def test_baseline_cases_without_fields_are_never_sent():
    """验收点：无职业、无收入或取值为占位串的案**一次也不送检**。

    命中率靠调用次数堆出来不算本事；这三案的共同点是「没有可比对的第二条信息」，
    送检只会换来一句「判不了」，那是白付一次调用。
    """
    facts_by_case = _baseline_facts()
    assert facts_by_case, "基线批次读不到任何事实表"
    sent = {name: checker.briefs(facts) for name, facts in facts_by_case.items()}
    # 职业为占位串或不存在的案：不得送检
    for name in ("15335772822", "15972379760", "18062597750"):
        if name in sent:
            assert sent[name] == [], f"{name} 的字段不足以判断，不该送检"


def test_baseline_never_exceeds_call_budget():
    """预算按**每案**算，不是整批加总——上限约束的是单次运行的花费。"""
    for name, facts in _baseline_facts().items():
        clusters = 1 if checker.briefs(facts) else 0
        assert clusters <= checker.CHECKER_BUDGET, (
            f"{name} 需要 {clusters} 次调用，超出每案上限 {checker.CHECKER_BUDGET}"
        )


def test_baseline_briefs_are_prompt_ready():
    """送检清单渲染成提示词时不含内部字段键——模型看到什么就会抄什么。"""
    from customer_profile.templates import TemplateRepository

    facts = next(iter(_baseline_facts().values()), {})
    brief_list = checker.briefs(facts) or checker.briefs(MISMATCH)
    prompt = TemplateRepository().render(
        "customer_profile_pipeline__pipeline_rule_check",
        {
            "arg1": "职业：网约车司机",
            # 必须走 prompt_view：原始 briefs() 带 key，那是给复核用的内部形态。
            "arg2": json.dumps(checker.prompt_view(brief_list), ensure_ascii=False),
        },
    )
    assert "occupation" not in prompt and "annual_income" not in prompt
    assert "{{" not in prompt


def test_prompt_view_strips_internal_keys():
    """``briefs()`` 带 ``key``（复核要用），``prompt_view()`` 必须剥干净。"""
    raw = checker.briefs(MISMATCH)
    assert all("key" in field for field in raw[0]["fields"])
    view = checker.prompt_view(raw)
    assert all(set(field) == {"name", "value"} for field in view[0]["fields"])
    # 复核路径仍拿得到内部键：两个形态各司其职
    assert checker.recheck(verdict(), raw)[0] is not None


# ---------------------------------------------------------------- 接线

def test_checker_nodes_are_between_review_and_level():
    nodes = {node.node_id: node for node in pipe.WORKFLOW.nodes}
    assert nodes[pipe.RULE_CHECK].predecessors == (pipe.L2_REVIEW,)
    assert nodes[pipe.RULE_APPLY].predecessors == (pipe.RULE_CHECK,)
    assert nodes[pipe.LEVEL_INPUT].predecessors == (pipe.RULE_APPLY,)


def test_downstream_reads_post_checker_notes():
    """表达与回传读的必须是 checker 执行后的 Notes，否则降级到不了产物。"""
    nodes = {node.node_id: node for node in pipe.WORKFLOW.nodes}
    pairs = {
        binding.target: binding.source for binding in nodes[pipe.PRODUCTION].bindings
    }
    assert pairs["notes_updates_json"] == (pipe.RULE_APPLY, "notes_updates_json")
    assert pairs["basis_json"] == (pipe.RULE_APPLY, "basis_json")
