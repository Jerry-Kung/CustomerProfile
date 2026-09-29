"""``evidence_subagent._parse_json`` 的围栏容忍度（有意差异 W52）。

对应基线批次 `baseline-39-dsv41-noThink` 实测到的两例静默降级：
`15012891982` / `18003012053` 的微信主页截图分析把 JSON 放在一句说明之后，
原逻辑只认「整段以围栏开头」，parse 失败后返回空 dict——
与「该客户确实没有这项数据」在下游无法区分。
"""

from __future__ import annotations

import json

import pytest

from customer_profile.workflows.evidence_subagent import _extract_fenced_block, _parse_json

PAYLOAD = {"wechat_profile_info": {"nickname": "郑某", "wechat_id": "15012891982"}}


def _body(*, indent: int = 4) -> str:
    return json.dumps(PAYLOAD, ensure_ascii=False, indent=indent)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_body(), id="裸JSON"),
        pytest.param(f"```json\n{_body()}\n```", id="整段围栏"),
        pytest.param(f"```\n{_body()}\n```", id="整段围栏无语言标注"),
        # W52 修复的正是这两类：说明文字在前
        pytest.param(f"根据提供的图片，提取信息如下：\n\n```json\n{_body()}\n```", id="说明在前-json"),
        pytest.param(f"图片无法读取，以下是字段占位：\n```\n{_body()}\n```", id="说明在前-无标注"),
    ],
)
def test_parse_json_accepts_fence_at_any_position(text: str) -> None:
    assert _parse_json(text) == PAYLOAD


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="空串"),
        pytest.param("   \n  ", id="纯空白"),
        pytest.param(None, id="None"),
        pytest.param("完全没有 JSON 的一段散文分析。", id="纯散文"),
        pytest.param("```json\n{不是合法 JSON\n```", id="围栏内非法JSON"),
        pytest.param("[1, 2, 3]", id="顶层不是对象"),
        pytest.param('```json\n[1, 2, 3]\n```', id="围栏内顶层不是对象"),
    ],
)
def test_parse_json_still_returns_empty_dict(text) -> None:
    """容忍度放宽后，原有「拿不到就用空 dict」的语义必须原样保留。"""
    assert _parse_json(text) == {}


def test_parse_json_passes_through_dict() -> None:
    """上游按 Object 直传时原样返回，不经字符串路径。"""
    assert _parse_json(PAYLOAD) == PAYLOAD


def test_extract_fenced_block_returns_none_without_pair() -> None:
    """只有开围栏没有闭围栏时不猜——返回 None，由调用方沿用原文。"""
    assert _extract_fenced_block("说明\n```json\n{...}") is None
    assert _extract_fenced_block("没有任何围栏") is None
