"""L1 取证层 / L2 接线 / T5 兼容覆写的语义回归。

装置口径与 `tests/conftest.py` 一致：**不出网、不依赖批次产物、干净检出上全绿**。
真实的端到端验证在 R0/R1 跑批里做（`docs/runbooks/AB实验.md`），这里钉住纯代码判定。

为什么这几条都要有用例：

- ``localize_claims`` 会**丢弃**断言。丢弃是静默的——出错不会报错，只会让画像少几个字段，
  看起来像「模型没提取到」。因此每条丢弃口径都要正面钉住，同时要有「不该丢的没丢」的反例。
- ``build_l1_input`` 决定模型看得见什么。渠道分级标错，整条链的 ``claim_type`` 判定随之错。
- ``overwrite_notes_updates`` 要改 ``result1``。改坏了产物形状，两臂立刻不可比。
"""

from __future__ import annotations

import json

from customer_profile.workflows import customer_profile_pipeline as pipe


# ---------------------------------------------------------------- 夹具

def record(source_name, content, *, status="ok", record_id=None, kind="ocr",
           created_at="2026-09-21T11:27:16", source_class="declared"):
    return {
        "record_id": record_id or f"rec_{source_name}",
        "channel": source_name,
        "source_name": source_name,
        "source_class": source_class,
        "status": status,
        "created_at": created_at,
        "content": content,
        "content_kind": kind,
    }


def records_json(*records):
    return json.dumps(list(records), ensure_ascii=False)


def claims_json(*claims):
    return json.dumps({"claims": list(claims)}, ensure_ascii=False)


def claim(**kwargs):
    base = {
        "claim_id": "c_001",
        "subject": "customer",
        "attribute": "occupation",
        "value": "充电桩行业",
        "record_channel": "试驾录音信息",
        "anchor": "我以前做充电桩的",
        "claim_type": "fact",
        "confidence": 0.9,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------- localize_claims

def test_anchor_localizes_to_verbatim_quote():
    """锚点定位成功后，引文是**原文切片**，不是模型抄的句子。"""
    body = "行吧，我以前做充电桩的，后来就转行了。"
    result = pipe.localize_claims(records_json(record("试驾录音信息", body)), claims_json(claim()))
    claims = json.loads(result["claims_json"])["claims"]
    assert result["kept"] == 1 and result["dropped"] == 0
    assert claims[0]["quote"] == "我以前做充电桩的"
    start, end = claims[0]["span"]
    assert body[start:end] == claims[0]["quote"]


def test_quote_is_always_a_slice_of_source():
    """锚点可回溯率 100% 的含义：留下的引文必然能在原文里逐字找到。"""
    body = "客户说：我家两个孩子嘛，都在上小学。"
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", body)),
        claims_json(claim(attribute="family_structure", value="有孩子", anchor="我家两个孩子嘛，都")),
    )
    for entry in json.loads(result["claims_json"])["claims"]:
        assert entry["quote"] in body


def test_anchor_not_found_is_dropped():
    """抄错字的断言必须丢弃——它是编造引文的入口。"""
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", "我以前做充电桩的。")),
        claims_json(claim(anchor="我以前修充电桩的")),
    )
    assert result["kept"] == 0
    assert json.loads(result["dropped_detail"])[0]["reason"] == "anchor_not_found"


def test_short_anchor_is_dropped():
    """短锚点极易偶然命中，留下它换来的是噪声不是召回。"""
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", "好，行。")),
        claims_json(claim(anchor="好")),
    )
    assert result["kept"] == 0
    assert json.loads(result["dropped_detail"])[0]["reason"] == "anchor_too_short"


def test_unknown_channel_is_dropped():
    """渠道对不上记录层 → 丢弃（断言指不到任何一份记录）。"""
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", "我以前做充电桩的。")),
        claims_json(claim(record_channel="不存在的渠道")),
    )
    assert result["kept"] == 0
    assert json.loads(result["dropped_detail"])[0]["reason"] == "unknown_channel"


def test_whitespace_only_difference_still_localizes():
    """ASR 断字与空格差异不得误杀：忽略空白再匹配一次。"""
    body = "我 以前 做 充电桩 的"
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", body)),
        claims_json(claim(anchor="我以前做充电桩的")),
    )
    assert result["kept"] == 1
    quote = json.loads(result["claims_json"])["claims"][0]["quote"]
    assert quote.replace(" ", "") == "我以前做充电桩的"


def test_short_transcript_is_not_dropped():
    """13530559591 那条 256 字符短转写不得因为「短」被丢——短不等于无效。"""
    body = "（客户）喂？ （销售）您好，我是猛士的销售，上次您来试驾过。" * 6
    result = pipe.localize_claims(
        records_json(record("外呼录音信息", body, kind="transcript", source_class="primary_interaction")),
        claims_json(claim(record_channel="外呼录音信息", anchor="上次您来试驾过。")),
    )
    assert result["kept"] == 1


def test_non_json_claims_reported_not_silently_empty():
    """L1 返回非 JSON 时，必须与「确实零断言」可区分。"""
    result = pipe.localize_claims(records_json(record("试驾录音信息", "原文")), "这不是 JSON")
    assert result["parse_ok"] is False
    assert result["kept"] == 0


def test_claims_with_empty_list_is_parsed_ok():
    result = pipe.localize_claims(records_json(record("试驾录音信息", "原文")), '{"claims": []}')
    assert result["parse_ok"] is True and result["kept"] == 0


def test_channel_alias_locates():
    """渠道名写成「试驾录音」这类别名时按包含关系就近匹配。"""
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", "我以前做充电桩的。")),
        claims_json(claim(record_channel="试驾录音")),
    )
    assert result["kept"] == 1


# ---------------------------------------------------------------- build_l1_input

def test_only_ok_channels_enter_l1_context():
    """空渠道在记录层留痕，但不进 L1 上下文。"""
    built = pipe.build_l1_input(
        records_json(
            record("试驾录音信息", "有内容的一段话"),
            record("朋友圈截图内容信息", "", status="exception"),
        )
    )
    assert built["section_count"] == 1
    assert "朋友圈截图内容信息" not in built["output"]


def test_l1_input_labels_source_tier():
    """来源分级由代码标注，不由模型猜。"""
    built = pipe.build_l1_input(
        records_json(record("试驾录音信息", "内容", source_class="primary_interaction"))
    )
    assert "来源分级：一手交互" in built["output"]
    assert "记录编号：" in built["output"]


def test_l1_input_orders_registry_before_firsthand_before_prediction():
    """关键段前置：登记档在录音之前，录音在第三方预测之前。"""
    built = pipe.build_l1_input(
        records_json(
            record("第三方画像数据", "预测职业为货车司机", source_class="prediction"),
            record("试驾录音信息", "录音原文", source_class="primary_interaction"),
            record("销售人员人工确认的客户Notes信息", "人工确认内容"),
        )
    )
    text = built["output"]
    assert text.index("销售人员人工确认的客户Notes信息") < text.index("试驾录音信息")
    assert text.index("试驾录音信息") < text.index("第三方画像数据")


def test_l1_input_keeps_transcript_verbatim():
    """转写逐字进入上下文，中间不插任何压缩。"""
    body = "（客户）我上次说的那个价格……"
    built = pipe.build_l1_input(records_json(record("外呼录音信息", body, kind="transcript")))
    assert body in built["output"]


def test_l1_input_empty_records():
    built = pipe.build_l1_input("[]")
    assert built["section_count"] == 0 and built["output"] == ""


# ---------------------------------------------------------------- project_claims

def test_project_claims_enforces_attribution():
    """「我们单位」那类主体非客户本人的断言不得写入身份字段。"""
    result = pipe.project_claims(
        claims_json(claim(attribute="company", value="某单位", subject="unknown", anchor="我们单位也要买一个"))
    )
    assert "company" not in json.loads(result["facts_json"])


def test_project_claims_without_subject_is_filtered():
    """主体缺失不回落成 customer，否则该拦下的会被放行。"""
    result = pipe.project_claims(claims_json(claim(attribute="occupation", subject="", value="教师")))
    assert "occupation" not in json.loads(result["facts_json"])


def test_project_claims_deregistered_company_not_filled():
    """企业已注销不得当作在职（基线 13926073386 的缺陷）。"""
    result = pipe.project_claims(
        claims_json(
            claim(attribute="company", value="广州某某装饰工程有限公司（已注销）", anchor="该公司已注销"),
            claim(attribute="company_type", value="民企", claim_id="c_002", anchor="民企"),
        )
    )
    facts = json.loads(result["facts_json"])
    assert "company" not in facts and "company_type" not in facts


def test_project_claims_chinese_display_normalized_to_key():
    result = pipe.project_claims(
        claims_json(claim(attribute="core_concerns", value="油耗||安全", anchor="油耗安全"))
    )
    assert json.loads(result["facts_json"])["core_concerns"] == ["fuel_efficiency", "safety"]


def test_project_claims_notes_shape_matches_baseline():
    """Notes 形状须与基线同形，否则 T5 覆写进去的产物两臂不可比。"""
    result = pipe.project_claims(claims_json(claim(attribute="occupation", value="教师", anchor="我是老师")))
    notes = json.loads(result["notes_updates_json"])
    assert notes, "应产出一条 Notes"
    assert set(notes[0]) == {"attribute_key", "attribute_value", "reasoning_detail"}
    assert set(notes[0]["reasoning_detail"]) == {"summary", "steps", "evidence", "confidence"}


def test_project_claims_thin_tier_keeps_registry_only():
    """thin 档只输出登记档坐实的字段。"""
    result = pipe.project_claims(
        claims_json(claim(attribute="occupation", value="教师", record_channel="小红书主页截图信息")),
        tier="thin",
    )
    assert result["notes_count"] == 0


def test_project_claims_empty_tier_outputs_nothing():
    result = pipe.project_claims(claims_json(claim(attribute="occupation", value="教师")), tier="empty")
    assert result["notes_count"] == 0


def test_project_claims_old_car_forces_replacement():
    """旧车证据 ⇒ 换购（基线多案正文写了旧车却仍填首购）。"""
    result = pipe.project_claims(
        claims_json(
            claim(attribute="old_car_model", value="2019 别克", anchor="旧车是2019年的别克"),
            claim(attribute="purchase_type", value="first", claim_id="c_002", anchor="第一次买车"),
        )
    )
    assert json.loads(result["facts_json"]).get("purchase_type") == "replacement"


# ---------------------------------------------------------------- overwrite_notes_updates

def test_overwrite_replaces_basic_notes_updates():
    payload = json.dumps(
        {"analysis_result": {"basic_notes_updates": [{"attribute_key": "旧"}], "profile_summary": {}},
         "batch_id": "", "phone": "13500000000"},
        ensure_ascii=False,
    )
    updates = json.dumps(
        [{"attribute_key": "occupation", "attribute_value": "教师",
          "reasoning_detail": {"summary": "s", "steps": ["a"], "evidence": [], "confidence": 0.7}}],
        ensure_ascii=False,
    )
    result = pipe.overwrite_notes_updates(payload, updates)
    assert result["overwrite_applied"] is True and result["notes_count"] == 1
    merged = json.loads(result["result1"])
    assert merged["analysis_result"]["basic_notes_updates"][0]["attribute_key"] == "occupation"
    assert merged["phone"] == "13500000000"
    assert "profile_summary" in merged["analysis_result"]


def test_overwrite_preserves_other_sections():
    payload = json.dumps(
        {"analysis_result": {"basic_notes_updates": [], "emotion_state": {"emotion": "平静"}}},
        ensure_ascii=False,
    )
    result = pipe.overwrite_notes_updates(payload, "[]")
    assert json.loads(result["result1"])["analysis_result"]["emotion_state"] == {"emotion": "平静"}


def test_overwrite_unparsable_result1_passes_through():
    """解不动就原样返回并留痕，不抛异常——notes 为空与整案失败是两件事。"""
    result = pipe.overwrite_notes_updates("{不是 JSON", "[]")
    assert result["overwrite_applied"] is False
    assert result["result1"] == "{不是 JSON"
    assert result["overwrite_reason"]


def test_overwrite_missing_analysis_result_passes_through():
    result = pipe.overwrite_notes_updates(json.dumps({"phone": "1"}, ensure_ascii=False), "[]")
    assert result["overwrite_applied"] is False


def test_overwrite_accepts_parsed_dict():
    """result1 已是对象时也要能覆写。"""
    result = pipe.overwrite_notes_updates({"analysis_result": {"basic_notes_updates": []}}, "[]")
    assert result["overwrite_applied"] is True
