"""**喂给模型的事实必须是中文可读的**——内部字段键与英文枚举键不得漏进提示词。

这条守卫来自一次真实缺陷（2026-10-20）：商机定级的证据里出现
「已定稿事实显示牌照状态为needs_license、付款方式为loan」，
「planned_purchase_time为within_3_months」。根因不是某一个模板写错了，
而是 ``facts`` 是机器形态（英文键 + 英文枚举键），凡原样 ``json.dumps`` 喂模型的
装配点都会漏。

这里不逐个断言具体文案（那是脆弱的），只钉一件事：**所有面向模型的装配产物里
都不出现 ``snake_case`` 形态的内部键**。新增泄漏点会直接把用例打红。

例外：``L1-1c`` 的输入**刻意保留**英文键（它的输出就是枚举键），不在本用例范围。
"""

from __future__ import annotations

import json
import re

import pytest

from customer_profile import readable as rdb
from customer_profile.workflows import customer_profile_l3 as l3
from customer_profile.workflows import customer_profile_pipeline as pipe

SNAKE_CASE = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b")
"""``snake_case`` 形态的内部键：``needs_license``、``planned_purchase_time`` 等。

只标记「小写字母开头且含下划线」的整词，中文与「15-25 万」这类取值不受影响。
"""


def _english_leaks(text: str) -> list[str]:
    return sorted(set(SNAKE_CASE.findall(text)))


def _facts() -> dict:
    """一份覆盖四种取值形态的事实（单值枚举 / 多选枚举 / 结构化项 / 自由文本）。"""
    return {
        "customer_name": "张三",
        "license_plate_status": "needs_license",
        "payment_method": "loan",
        "planned_purchase_time": "within_3_months",
        "core_concerns": "safety||fuel_efficiency",
        "driver_license_status": {"key": "yes", "input_value": "5年"},
        "budget_range": "15-25万",
        "business_opp_stage": "线索邀约阶段",
        "deal_level": "B",
        "deal_level_detail": {
            "summary": "需求明确但推进动作不足",
            "steps": ["需求真实", "未约到店"],
            "evidence": ["通话中询问落地价"],
            "confidence": 0.7,
        },
    }


# ---------------------------------------------------------------- 渲染器本身

def test_readable_facts_translates_keys_and_enum_values():
    text = rdb.readable_facts(_facts())
    assert _english_leaks(text) == []
    assert "牌照指标：需办理" in text
    assert "付款方式：贷款" in text
    assert "计划时间：3个月内" in text
    # 多选枚举逐项翻展示名后用顿号连接
    assert "核心关注点：安全、油耗" in text
    # 结构化项带随带值
    assert "驾照情况：有驾照（5年）" in text or "驾照情况" in text
    # 嵌套结构（定级详情）的键也翻中文
    assert "商机等级：B" in text
    assert "结论：需求明确但推进动作不足" in text
    assert "置信度：0.7" in text


def test_readable_facts_keeps_free_text_verbatim():
    """自由文本是客户原话（本就是中文），渲染不得改写它。"""
    text = rdb.readable_facts({"budget_range": "15-25万，落地价再谈"})
    assert "15-25万，落地价再谈" in text


def test_readable_facts_empty_is_explicit():
    """空事实要给一句明确的占位，不能让模型看到一片空白自行发挥。"""
    assert rdb.readable_facts({}).strip()


def test_readable_facts_is_stable():
    """同一份事实两次渲染必须一致（提示词缓存友好，测试可稳定断言）。"""
    facts = _facts()
    assert rdb.readable_facts(facts) == rdb.readable_facts(facts)


# ---------------------------------------------------------------- 三处装配点

LEAD_RECORDS = json.dumps(
    [
        {
            "record_id": "rec_chat",
            "channel": "企业微信聊天记录",
            "source_name": "企业微信聊天记录",
            "source_class": "declared",
            "status": "ok",
            "created_at": "2026-10-19T10:00:00",
            "content": "客户说这个月内定，问落地价",
            "content_kind": "text",
        }
    ],
    ensure_ascii=False,
)


def _bindings(node) -> dict[str, tuple[str, str]]:
    """``{目标参数名: (上游节点, 输出字段)}``。"""
    return {b.target: b.source for b in node.bindings}


def test_level_input_has_no_internal_keys():
    """商机定级输入：事实表必须是中文名与非英文取值（本次缺陷的直接现场）。

    只扫「已定稿事实」这一段——原始记录里的记录编号（``rec_chat``）是数据标识，
    不是字段键，不该被本守卫误伤。
    """
    out = pipe.build_level_input(LEAD_RECORDS, facts_json=json.dumps(_facts(), ensure_ascii=False))
    text = out["output"]
    head, _, _ = text.partition("【客户原始信息记录")
    leaks = _english_leaks(head)
    assert leaks == [], f"定级输入漏出内部键：{leaks}"


def test_project_claims_exposes_readable_facts():
    """L2 投影层要给 LLM 一个可读化事实出口，且它不含内部键。"""
    claims = json.dumps(
        [
            {
                "attribute": "planned_purchase_time",
                "value": "within_3_months",
                "record_channel": "外呼录音信息",
                "subject": "customer",
                "basis": "fact",
                "quote": "这个月内定",
            }
        ],
        ensure_ascii=False,
    )
    out = pipe.project_claims(claims, tier="sufficient")
    assert "facts_readable" in out
    assert _english_leaks(out["facts_readable"]) == []
    # 给代码消费的 facts_json 仍保留机器形态（回传与同源核对依赖它）
    assert json.loads(out["facts_json"]).get("planned_purchase_time") == "within_3_months"


def test_l3_express_reads_readable_facts():
    """L3 表达层的事实输入必须是可读化那份，否则画像卡会照抄英文值。"""
    facts_json = json.dumps(_facts(), ensure_ascii=False)
    out = l3.validate_l3("[]", facts_json, "[]")
    assert "facts_readable" in out
    assert _english_leaks(out["facts_readable"]) == []
    # 同源核对用的 facts_json 不得被替换成中文——否则关键字匹配会失真
    assert json.loads(out["facts_json"]).get("payment_method") == "loan"

    express = next(node for node in l3.WORKFLOW.nodes if node.node_id == l3.EXPRESS)
    assert _bindings(express)["facts_output"] == (l3.VALIDATE, "facts_readable")


@pytest.mark.parametrize(
    "node_id, input_name",
    [(pipe.L2_FALLBACK, "arg2")],
)
def test_llm_fallback_reads_readable_facts(node_id, input_name):
    """L2 兜底的事实输入同理：理由是给销售的，不能出现内部键。"""
    node = next(n for n in pipe.WORKFLOW.nodes if n.node_id == node_id)
    assert _bindings(node)[input_name] == (pipe.L2_PROJECT, "facts_readable")
