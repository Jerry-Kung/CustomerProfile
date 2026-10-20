"""角色标注：分段展开、渠道约束、兜底与失败降级。

**这一层要守住的东西。** LLM 只输出「角色分段」，逐句归属由代码展开。代码这一侧的口径
直接决定错误会不会落到客户头上：

- 分段有缺口时补 ``未知来源``，**绝不**继承上一段——缺口继承会把整块未覆盖的销售话术
  记成客户方；
- 角色非法或渠道不允许时改记 ``未知来源``，并留痕；
- 判成客户方却命中销售自述句式的句子，单向改判 ``未知来源``；
- LLM 输出解析不了时整条录音退回未标注原文，不中断批次。

**正文里不得出现任何角色标记。** 角色按句单独交出（``role_lines``），因为 L1 抄锚点
时必须命中原话的连续切片，标记混进正文会让锚点对不上，也会把内部符号带进对客产出。

用例都只测纯函数，不出网。
"""

from __future__ import annotations

import json

from customer_profile.workflows import role_labelling as rl


def utterances(n: int) -> list[dict]:
    return [{"text": f"第{i}句", "additions": {"speaker": "1", "gender": "female"}} for i in range(n)]


def build(payload: dict, n: int = 5, file_url: str = "") -> dict:
    return rl.expand_annotation(
        json.dumps(payload, ensure_ascii=False),
        json.dumps(utterances(n), ensure_ascii=False),
        file_url,
    )


def bundle_of(result: dict) -> dict:
    return json.loads(result["transcript_bundle"])


def role_line(bundle: dict, index: int) -> dict:
    return next(item for item in bundle["role_lines"] if item["i"] == index)


# ---------------------------------------------------------------- 分段展开

def test_full_coverage_is_expanded_per_sentence():
    result = bundle_of(
        build({"segments": [{"from": 0, "to": 2, "role": "销售方"}, {"from": 3, "to": 4, "role": "客户方"}]})
    )
    assert result["annotated"] is True
    assert result["roles"] == {"销售方": 3, "客户方": 2, "未知来源": 0}
    assert role_line(result, 0)["role"] == "销售方"
    assert role_line(result, 4)["role"] == "客户方"


def test_content_carries_no_role_markers():
    """正文只留原话：角色标记会破坏 L1 锚点，也会漏进对客产出。"""
    result = bundle_of(
        build(
            {
                "segments": [{"from": 0, "to": 0, "role": "销售方"}, {"from": 1, "to": 4, "role": "客户方"}],
                "corrections": [{"i": 2, "text": "改后的第三句", "reason": "音近误识别"}],
            }
        )
    )
    content = result["content"]
    assert content.split("\n") == ["第0句", "第1句", "改后的第三句", "第3句", "第4句"]
    assert "销售方" not in content and "客户方" not in content
    assert "原识别" not in content and "音近误识别" not in content


def test_role_lines_carry_character_ranges():
    """句级区间是反查「这条证据出自谁口」的唯一依据，必须与正文严格对齐。"""
    result = bundle_of(
        build({"segments": [{"from": 0, "to": 1, "role": "销售方"}, {"from": 2, "to": 4, "role": "客户方"}]})
    )
    content = result["content"]
    for line in result["role_lines"]:
        assert content[line["start"] : line["end"]] == line["text"] if "text" in line else True
    first, last = result["role_lines"][0], result["role_lines"][-1]
    assert (first["start"], first["end"]) == (0, 3)  # 「第0句」
    assert content[last["start"] : last["end"]] == "第4句"


def test_gap_is_filled_with_unknown_not_inherited():
    """缺口补未知来源——不得沿用上一段角色。"""
    result = bundle_of(build({"segments": [{"from": 0, "to": 1, "role": "销售方"}]}))
    assert role_line(result, 0)["role"] == "销售方"
    assert role_line(result, 2)["role"] == "未知来源", "未覆盖的句子必须补未知来源，不能继承销售方"
    assert any("未被覆盖" in issue for issue in result["issues"])


def test_overlap_keeps_first_segment():
    result = bundle_of(
        build({"segments": [{"from": 0, "to": 2, "role": "销售方"}, {"from": 2, "to": 4, "role": "客户方"}]})
    )
    assert role_line(result, 2)["role"] == "销售方"
    assert any("重复覆盖" in issue for issue in result["issues"])


def test_illegal_role_falls_back_to_unknown():
    result = bundle_of(build({"segments": [{"from": 0, "to": 4, "role": "主持人"}]}))
    assert result["roles"]["未知来源"] == 5
    assert any("非法角色" in issue for issue in result["issues"])


def test_legacy_roles_map_to_unknown():
    """四分类的历史产物仍需解析，但新输出不再使用这两个词。"""
    result = bundle_of(
        build(
            {
                "segments": [
                    {"from": 0, "to": 1, "role": "其他人员"},
                    {"from": 2, "to": 4, "role": "存疑"},
                ]
            }
        )
    )
    assert result["roles"]["未知来源"] == 5


# ---------------------------------------------------------------- 渠道约束

def test_outbound_allows_unknown_source():
    """实测外呼里会混入车载语音助手，不能一刀切禁止未知来源。"""
    result = bundle_of(
        build(
            {"segments": [{"from": 0, "to": 2, "role": "未知来源"}, {"from": 3, "to": 4, "role": "客户方"}]},
            file_url="https://x/1/AIOutboundCallRecordingFile/a.mp3",
        )
    )
    assert result["roles"] == {"销售方": 0, "客户方": 2, "未知来源": 3}
    assert not result["issues"]


def test_both_channels_allow_all_three_roles():
    for url in ("https://x/1/AIOutboundCallRecordingFile/a.mp3", "https://x/1/TestDriveRecordingFile/a.mp3"):
        channel = rl.channel_of(url)
        assert set(rl.CHANNEL_ALLOWED[channel]) == {"销售方", "客户方", "未知来源"}


def test_channel_detection_from_url():
    assert rl.channel_of("https://x/1/TestDriveRecordingFile/a.mp3") == "test_drive"
    assert rl.channel_of("https://x/1/AIOutboundCallRecordingFile/a.mp3") == "outbound"


# ---------------------------------------------------------------- 单向兜底

def test_customer_sentence_with_sales_self_marker_becomes_unknown():
    """「销售内容不得归给客户」的最后一层保险。"""
    data = [{"text": "我们是4S店的", "additions": {"speaker": "1"}}, {"text": "哦你在看哪款"}]
    result = json.loads(
        rl.expand_annotation(
            json.dumps({"segments": [{"from": 0, "to": 1, "role": "客户方"}]}, ensure_ascii=False),
            json.dumps(data, ensure_ascii=False),
            "",
        )["transcript_bundle"]
    )
    assert role_line(result, 0)["role"] == "未知来源"
    assert role_line(result, 1)["role"] == "客户方", "兜底只作用于命中句式的那一句"
    assert any("销售自述" in issue for issue in result["issues"])


def test_guard_does_not_touch_sales_or_unknown():
    roles = {0: "销售方", 1: "未知来源", 2: "客户方"}
    data = [{"text": "我们店现在有活动"}, {"text": "我们店那边"}, {"text": "你们店在哪"}]
    adjusted, issues = rl.guard_customer_claims(roles, data, {})
    assert adjusted == {0: "销售方", 1: "未知来源", 2: "客户方"}
    assert issues == []


def test_guard_reads_corrected_text():
    """勘误把话改清楚之后仍命中，才更有把握。"""
    roles = {0: "客户方"}
    data = [{"text": "唔们电"}]
    adjusted, issues = rl.guard_customer_claims(roles, data, {0: {"text": "我们店", "reason": "音近"}})
    assert adjusted[0] == "未知来源"
    assert issues


# ---------------------------------------------------------------- 降级

def test_unparseable_output_returns_raw_content_and_marks_annotated_false():
    """LLM 输出坏掉：不抛，原文照常交出，只是没有角色。"""
    raw = json.dumps(utterances(3), ensure_ascii=False)
    result = rl.expand_annotation("这不是 JSON", raw, "")
    bundle = json.loads(result["transcript_bundle"])
    assert bundle["annotated"] is False
    assert bundle["content"] == raw
    assert bundle["role_lines"] == []
    assert result["annotated"] == "false"


def test_missing_segments_marks_every_sentence_unknown():
    bundle = bundle_of(build({"corrections": []}))
    assert bundle["annotated"] is True
    assert bundle["roles"]["未知来源"] == 5


def test_empty_transcript_is_not_annotated():
    result = rl.expand_annotation("{}", "[]", "")
    assert json.loads(result["transcript_bundle"])["annotated"] is False


# ---------------------------------------------------------------- 勘误

def test_correction_substitutes_body_and_records_detail():
    """勘误改正文（L1 锚点必须命中同一份文本），明细另存在 corrections 里。"""
    result = bundle_of(
        build(
            {
                "segments": [{"from": 0, "to": 4, "role": "销售方"}],
                "corrections": [{"i": 1, "text": "改后的第二句", "reason": "音近误识别"}],
            }
        )
    )
    assert result["content"].split("\n")[1] == "改后的第二句"
    assert result["corrections"] == [{"i": 1, "text": "改后的第二句", "reason": "音近误识别"}]


def test_corrected_text_length_shifts_later_offsets():
    result = bundle_of(
        build(
            {
                "segments": [{"from": 0, "to": 4, "role": "销售方"}],
                "corrections": [{"i": 1, "text": "很长很长的一句替代文本", "reason": "x"}],
            }
        )
    )
    content = result["content"]
    assert content[role_line(result, 2)["start"] : role_line(result, 2)["end"]] == "第2句"


def test_identical_correction_is_dropped():
    result = bundle_of(
        build(
            {
                "segments": [{"from": 0, "to": 4, "role": "销售方"}],
                "corrections": [{"i": 2, "text": "第2句", "reason": "确认无误"}],
            }
        )
    )
    assert result["corrections"] == []
    assert any("与原文相同" in issue for issue in result["issues"])


def test_out_of_range_correction_is_dropped():
    result = bundle_of(
        build(
            {
                "segments": [{"from": 0, "to": 4, "role": "销售方"}],
                "corrections": [{"i": 99, "text": "越界", "reason": "x"}],
            }
        )
    )
    assert result["corrections"] == []
    assert "越界" not in result["content"]
    assert any("越界" in issue for issue in result["issues"])


# ---------------------------------------------------------------- 提示词入参

def test_prepare_prompt_passes_only_role_relevant_fields():
    """情绪/语速/音量对判角色无价值，不得进输入。"""
    raw = json.dumps(
        [{"text": "喂", "additions": {"speaker": "2", "gender": "male", "emotion": "happy", "volume": 70}}],
        ensure_ascii=False,
    )
    out = rl.prepare_prompt(raw, "https://x/1/TestDriveRecordingFile/a.mp3")
    payload = json.loads(out["utterances"])
    assert payload == [{"index": 0, "speaker": "2", "gender": "male", "text": "喂"}]
    assert out["total"] == "1"
    assert "试驾录音" in out["channel_rule"]
