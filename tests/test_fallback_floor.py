"""L2 兜底的总量闸门：候选为空不调用、候选非空才判输出量、失败不阻断。

三条判据都来自 2026-10-28 的 148 次兜底调用实测：

- 候选为 0 的 35 次调用稳定回 ``{"verdicts": []}``（7 token）——那是**正确结果**，
  不该被判成截断，也不该白烧一次调用；
- 候选 > 0 的正常输出最小 65 token，而 31 次只回 7–8 token，全部是空结论；
- 兜底原先是个 ``llm`` 节点，失败会阻塞整棵下游子树，把「兜底没干活」升级成「整案失败」。

因此闸门不能是静态的：同样 7 token，候选为 0 时正确、候选为 30 时是截断。

"""

from __future__ import annotations

import json

import pytest

from customer_profile.workflows import customer_profile_pipeline as pipe


def cand(key="occupation"):
    return json.dumps(
        [
            {
                "attribute_key": key,
                "current_value": None,
                "current_basis": "fact",
                "candidates": ["教师"],
                "options": [{"value": "教师", "channel": "外呼录音信息", "tier": "一手交互"}],
                "missing": False,
                "reason": "同来源档给出 1 个值",
            }
        ],
        ensure_ascii=False,
    )


class FakeLLM:
    """记录调用、按队列返回的假客户端（可注入异常）。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def llm_call(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if not self.responses:
            raise AssertionError("超出预期的调用次数")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------- 候选为空不调用

async def test_empty_candidates_make_no_call():
    """候选为空 → 零 token 返回空裁决，绝不发请求。

    归档里这类空调用占两成（35/148），每一次都是白烧的钱。
    """
    llm = FakeLLM()
    out = await pipe.fallback_verdict(candidates_json="[]", facts_readable="", llm=llm)
    assert out["fallback_status"] == "no_candidate"
    assert out["fallback_calls"] == 0
    assert out["candidate_count"] == 0
    assert llm.calls == []
    # 下游 L2_APPLY 读这个键，形态必须保持「JSON 串」且是合法的空裁决。
    assert json.loads(out["result"]) == {"verdicts": []}


async def test_empty_candidates_need_no_llm_client():
    """连 LLM 客户端都没有时，候选为空也必须正常返回（关闭/离线场景）。"""
    out = await pipe.fallback_verdict(candidates_json="[]", llm=None)
    assert out["fallback_status"] == "no_candidate"
    assert json.loads(out["result"]) == {"verdicts": []}


# ---------------------------------------------------------------- 下限只在候选非空时生效

async def test_floor_is_passed_only_when_candidates_exist():
    """候选非空时把下限交给调用层；候选为空时一次调用都不发（因此也谈不上下限）。"""
    llm = FakeLLM({"verdicts": []})
    await pipe.fallback_verdict(candidates_json=cand(), facts_readable="", llm=llm)
    assert llm.calls[0]["min_completion_tokens"] == pipe.L2_FALLBACK_MIN_TOKENS


def test_floor_sits_between_observed_short_and_normal_outputs():
    """阈值必须落在实测的空结论（7–8）与正常最小（65）之间。

    这条用例是阈值的锚：改常量时如果越界，会在这里打红，而不是等到线上误杀。
    """
    assert 8 < pipe.L2_FALLBACK_MIN_TOKENS < 65


# ---------------------------------------------------------------- 正常与失败

async def test_normal_verdict_passes_through():
    verdict = {"verdicts": [{"attribute_key": "occupation", "value": "教师", "reason": "x"}]}
    out = await pipe.fallback_verdict(candidates_json=cand(), facts_readable="", llm=FakeLLM(verdict))
    assert out["fallback_status"] == "ok"
    assert out["fallback_calls"] == 1
    assert json.loads(out["result"]) == verdict


async def test_failure_does_not_raise_and_reports_failed():
    """调用失败 → 空裁决 + ``failed``，不抛。抛出去会阻塞下游子树。"""
    out = await pipe.fallback_verdict(
        candidates_json=cand(), facts_readable="", llm=FakeLLM(RuntimeError("boom"))
    )
    assert out["fallback_status"] == "failed"
    assert "boom" in out["fallback_error"]
    assert out["fallback_calls"] == 1
    assert json.loads(out["result"]) == {"verdicts": []}


async def test_missing_llm_client_is_reported_not_raised():
    out = await pipe.fallback_verdict(candidates_json=cand(), facts_readable="", llm=None)
    assert out["fallback_status"] == "failed"
    assert json.loads(out["result"]) == {"verdicts": []}


# ---------------------------------------------------------------- 提示词装配

async def test_prompt_carries_candidates_and_readable_facts():
    llm = FakeLLM({"verdicts": []})
    await pipe.fallback_verdict(
        candidates_json=cand(), facts_readable="职业：教师", llm=llm
    )
    prompt = llm.calls[0]["prompt"]
    assert "occupation" in prompt
    assert "职业：教师" in prompt


async def test_system_text_is_kept():
    """改由 code 承载后系统提示不能丢——llm 节点原先靠 ``system_text`` 携带它。"""
    llm = FakeLLM({"verdicts": []})
    await pipe.fallback_verdict(candidates_json=cand(), facts_readable="", llm=llm)
    assert llm.calls[0]["system"] == pipe.SYSTEM_TEXT


# ---------------------------------------------------------------- 留痕

def test_attempt_ref_carries_what_the_recorder_reads():
    """留痕层只按 getattr 读 ``run_id`` / ``node_id`` / ``call_path``。"""
    from customer_profile.execution import executors_misc

    class Ctx:
        run_id = "run-1"
        call_path = "/iter:1"

    class Node:
        node_id = "n1"
        title = "测试节点"

    ref = executors_misc._attempt_ref(Ctx(), Node())
    assert ref.run_id == "run-1"
    assert ref.node_id == "n1"
    assert ref.call_path == "/iter:1"


def test_llm_calling_code_functions_accept_node_ref():
    """会调 LLM 的 code 节点必须能接住 ``node_ref``，否则调用不留痕。

    范围限于本次改写的两个节点。``rule_check``（checker）至今没有该参数，是**既有缺口**
    ——它在改造前就没有留痕，不是本次引入的；补它需要连同 ``checker.run_check`` 一起改，
    留待单独处理。
    """
    import inspect

    for name in ("fallback_verdict", "enum_map"):
        func = getattr(pipe, name)
        assert "node_ref" in inspect.signature(func).parameters, (
            f"{name} 未声明 node_ref 参数，调用 LLM 时不会留痕"
        )
