"""L1 取证层 / L2 接线 / T5 兼容覆写的语义回归。

装置口径与 `tests/conftest.py` 一致：**不出网、不依赖批次产物、干净检出上全绿**。
真实的端到端验证在 R0/R1 跑批里做（`docs/runbooks/AB实验.md`），这里钉住纯代码判定。

为什么这几条都要有用例：

- ``localize_claims`` 定位失败时**降级保留**（v2 起，v1 是整条丢弃）。降级是静默的——
  出错不会报错，只会让一个本该够硬的字段滑到最低档，看起来像「模型没把握」。
  因此每条降级口径都要正面钉住，同时要有「该保住的仍是原档」的反例。
- ``build_l1_input`` 决定模型看得见什么。渠道分级标错，整条链的 ``claim_type`` 判定随之错。
"""

from __future__ import annotations

import json

from customer_profile import notes_contract as nc
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
    # v2 的字段级结论用 ``basis``（三分类）；未显式给出时按 v1 的 ``claim_type`` 等价映射，
    # 否则 `localize_claims` 会按 ``_basis_of`` 回推，用例就钉不住「传进来的档位有没有被尊重」。
    # v3.2：``prediction`` 对齐 ``_basis_of``，映射到 ``inferred_high``（第三方画像标签按
    # 可信的高置信推断处理，不再是最低档）。
    base.setdefault("basis", {"fact": "fact", "observation": "inferred_high",
                              "prediction": "inferred_high"}.get(base["claim_type"], "fact"))
    base.update(kwargs)
    return base


# ---------------------------------------------------------------- localize_claims

def test_anchor_localizes_to_verbatim_quote():
    """锚点定位成功后，引文是**原文切片**，不是模型抄的句子。"""
    body = "行吧，我以前做充电桩的，后来就转行了。"
    result = pipe.localize_claims(records_json(record("试驾录音信息", body)), claims_json(claim()))
    claims = json.loads(result["claims_json"])["fields"]
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
    for entry in json.loads(result["claims_json"])["fields"]:
        assert entry["quote"] in body


def test_anchor_not_found_is_downgraded_and_kept():
    """抄错字的断言**降级保留**，不丢字段。

    它仍是编造引文的入口——因此必须**同时**保证两件事：切不出 ``quote``（不给它
    一个假的原文切片），且档位降到最低（不给它 ``fact`` 的分量）。v1 的做法是整条丢掉，
    代价是一个正确的字段值因为引用抄错就消失；v2 的做法是留着值、降它的档。
    """
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", "我以前做充电桩的。")),
        claims_json(claim(anchor="我以前修充电桩的")),
    )
    assert result["kept"] == 1 and result["dropped"] == 0
    entry = json.loads(result["claims_json"])["fields"][0]
    assert entry["basis"] == "inferred_low"
    assert "quote" not in entry and "span" not in entry
    assert json.loads(result["downgraded_detail"])[0]["reason"] == "anchor_not_found"


def test_ambiguous_short_anchor_is_downgraded_and_kept():
    """短锚偶然命中会引入歧义切片，因此**不给它引文**，但字段值仍保留在最低档。"""
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", "好，行。")),
        claims_json(claim(anchor="好")),
    )
    assert result["kept"] == 1 and result["dropped"] == 0
    entry = json.loads(result["claims_json"])["fields"][0]
    assert entry["basis"] == "inferred_low"
    assert "quote" not in entry
    assert json.loads(result["downgraded_detail"])[0]["reason"] == "anchor_too_short"


def test_unique_short_anchor_is_kept():
    """短但在原文里**只出现一次**的锚点应保留：长度只是「能否唯一定位」的代理指标。

    R1 实测：226 条被长度门丢掉的短锚里 163 条在原文中唯一出现，切出来是确定的原文切片。
    """
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", "我以前做充电桩的，这两年没做了。")),
        claims_json(claim(anchor="做充电桩")),
    )
    assert result["kept"] == 1
    assert "做充电桩" in json.loads(result["claims_json"])["fields"][0]["quote"]


def test_repeated_short_anchor_is_downgraded_and_kept():
    """短锚**出现多次**时定位有歧义 → 降级（这是原判据真正要防的情形）。"""
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", "好的好的，我下周来，好的。")),
        claims_json(claim(anchor="好的")),
    )
    assert result["kept"] == 1 and result["dropped"] == 0
    entry = json.loads(result["claims_json"])["fields"][0]
    assert entry["basis"] == "inferred_low" and "quote" not in entry
    assert json.loads(result["downgraded_detail"])[0]["reason"] == "anchor_too_short"


def test_anchor_below_floor_is_downgraded_even_if_unique():
    """短于下限（4 字）的锚点即使唯一也没有证据价值 → 降级，不给引文。"""
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", "下周一见面，下周一定来。")),
        claims_json(claim(anchor="下周")),
    )
    assert result["kept"] == 1 and result["dropped"] == 0
    entry = json.loads(result["claims_json"])["fields"][0]
    assert entry["basis"] == "inferred_low" and "quote" not in entry
    assert json.loads(result["downgraded_detail"])[0]["reason"] == "anchor_too_short"


def test_occurrences_matches_locate_whitespace_fallback():
    """计数与定位必须同口径，否则会出现「定位得到、计数数不到」的错位而白丢短锚。"""
    assert pipe._occurrences("我 下 周 来 看 车", "我下周来看车") == 1
    assert pipe._occurrences("好的好的", "好的") == 2


def test_unknown_channel_is_downgraded_and_kept():
    """渠道对不上记录层 → 降级保留，并**清空** ``record_channel``。

    清空是必须的：一个指不到任何记录的渠道名若留着，下游按渠道判来源档时会把它
    当成一条真的取证通道，反而抬高了这条断言的份量。
    """
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", "我以前做充电桩的。")),
        claims_json(claim(record_channel="不存在的渠道")),
    )
    assert result["kept"] == 1 and result["dropped"] == 0
    entry = json.loads(result["claims_json"])["fields"][0]
    assert entry["basis"] == "inferred_low"
    assert entry["record_channel"] == ""
    assert json.loads(result["downgraded_detail"])[0]["reason"] == "unknown_channel"


def test_whitespace_only_difference_still_localizes():
    """ASR 断字与空格差异不得误杀：忽略空白再匹配一次。"""
    body = "我 以前 做 充电桩 的"
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", body)),
        claims_json(claim(anchor="我以前做充电桩的")),
    )
    assert result["kept"] == 1
    quote = json.loads(result["claims_json"])["fields"][0]["quote"]
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


def test_l1_input_empty_records_still_carries_guide():
    """无记录时正文为空，但**前置清单恒在**——它是字段对照表，不随记录有无而变。"""
    built = pipe.build_l1_input("[]")
    assert built["section_count"] == 0
    assert built["output"] == pipe.l1_guide()
    assert built["guide_chars"] > 0
    # 清单必须**同时**含字段清单与合法取值两张表：只有枚举键时，模型不知道
    # 一共有哪些字段、每个字段的口径是什么，覆盖要求就无从落实。
    assert "目标字段清单" in built["output"] and "可选字段的合法取值" in built["output"]


def test_field_inference_guide_covers_all_contract_fields():
    """《字段清单》必须逐条列出契约里**全部**字段，并在每个字段后带上契约原文口径。"""
    guide = pipe.field_inference_guide()
    listed = {
        line.split("`")[1]
        for line in guide.splitlines()
        if line.startswith("- `") and line.count("`") >= 2
    }
    assert listed == set(nc.ALL_KEYS)
    assert len(listed) == len(nc.ALL_KEYS) == 47
    # 口径取自契约：抽两条逐字比对，钉住「不手抄」这条约束。
    for key in ("planned_purchase_time", "core_concerns"):
        description = (nc.by_key(key).get("description") or "").strip()
        assert f"- `{key}`" in guide
        assert description in guide


def test_field_inference_guide_labels_categories():
    """六个字段大类都要出现——清单按类分组，模型才看得出口径的归属。"""
    guide = pipe.field_inference_guide()
    for category in (nc.BASE_IDENTITY, nc.PURCHASE_NEED, nc.BUDGET_FINANCE,
                     nc.DECISION_RHYTHM, nc.PREFERENCE_NOTE, nc.COMPETITOR):
        assert f"### {category}" in guide


def test_field_source_notes_keys_are_real_contract_fields():
    """来源补充说明的键必须是契约真字段名。

    这是**防漂移**的机制：字段改名或下线后，这张表若不跟着改，那段说明就会静默失效——
    模型既看不到它，也没人会发现它已经不生效了。
    """
    keys = set(pipe.FIELD_SOURCE_NOTES)
    assert keys, "来源补充表不得为空"
    assert keys <= set(nc.ALL_KEYS), f"不在契约里的字段名：{sorted(keys - set(nc.ALL_KEYS))}"
    guide = pipe.field_inference_guide()
    for key in keys:
        assert f"取证范围：" in guide.split(f"- `{key}`")[1].split("\n")[0] or \
            "取证范围：" in guide


def test_field_inference_guide_renders_source_notes_inline():
    """补充说明要**贴着字段**渲染（缩进子项），而不是另起一节——另起一节模型容易漏读。"""
    guide = pipe.field_inference_guide()
    lines = guide.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("- `planned_purchase_time`"))
    assert lines[index + 1].startswith("  - 取证范围：")


# ---------------------------------------------------------------- 枚举键附录（R0 §5.1 修复）

def test_enum_key_guide_covers_every_enum_field():
    """附录必须逐条列出契约里**全部** 21 个枚举字段，不多不少。"""
    guide = pipe.enum_key_guide()
    listed = {
        line.split("`")[1]
        for line in guide.splitlines()
        if line.startswith("- `") and line.count("`") >= 2
    }
    expected = {k for k in nc.ALL_KEYS if nc.is_enum(k)}
    assert listed == expected
    assert len(expected) == 21


def test_enum_key_guide_marks_structured_input_value():
    """结构化枚举要标出随带填写的字段名，否则 L2 只能补空串。"""
    guide = pipe.enum_key_guide()
    assert "有孩子（**需随带填孩子人数**）" in guide
    assert "有（**需随带填驾龄年限**）" in guide
    assert "需大空间（**需随带填乘坐人数**）" in guide
    assert "有（**需随带填旧车品牌**）" in guide


def test_enum_key_guide_labels_multi_value_fields():
    """多选字段要标出「多个值用 || 连接」，与 L2 的落库序列化口径一致。"""
    guide = pipe.enum_key_guide()
    for key in ("purchase_purpose", "core_concerns", "driving_preference", "emotion", "frequent_social_media"):
        line = next(l for l in guide.splitlines() if l.startswith(f"- `{key}`"))
        assert "多选，多个值用 || 连接" in line
    assert "多选" not in next(l for l in guide.splitlines() if l.startswith("- `purchase_type`"))


def test_enum_key_guide_is_generated_from_contract_not_handcopied():
    """附录是契约快照的投影：改契约则附录跟着变（钉住「不手抄」这一约束）。"""
    guide = pipe.enum_key_guide()
    for key in ("fuel_efficiency", "hate_being_rushed", "large_space", "needs_license"):
        assert key in guide
    # 展示名也在，模型才可能把口语对上
    assert "油耗" in guide and "讨厌被催" in guide and "限牌城市" in guide


def test_l1_input_places_guide_before_records():
    """附录置于正文**之前**，让记录原文成为模型最后读到的内容。

    R1 实测：附录放在末尾时，末尾那串短 token（``Mr=先生 / Ms=女士``）会把 anchor 也
    诱导成同样短的裸串（同案中位锚长 16 → 6），大量正确断言因此过不了长度门。
    """
    body = "（客户）我上次说的那个价格……"
    built = pipe.build_l1_input(records_json(record("外呼录音信息", body, kind="transcript")))
    assert built["output"].index("可选字段的合法取值") < built["output"].index(body)
    assert built["guide_chars"] == len(pipe.l1_guide())
    # 记录原文必须是**最后一段**：模型对上下文尾部最敏感。
    assert built["output"].rstrip().endswith(body)


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


# ---------------------------------------------------------------- v2.1 R3 渠道塌陷修复

def test_inferred_high_without_channel_keeps_basis():
    """`inferred_*` 类本就允许留空渠道，**不得**因「没给渠道」被降级。

    v2 曾把「压根没给渠道」当成「渠道对不上记录层」，把 8 案 209 条断言（92% 的降级）
    一并压成 ``inferred_low``——三分类在字段级塌陷的根因（设计稿 §2.3）。
    """
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", "我以前做充电桩的。")),
        claims_json(
            claim(
                attribute="annual_income",
                value="30-50万",
                record_channel="",
                basis="inferred_high",
                basis_of_inference=["职业为互联网中层"],
            )
        ),
    )
    entry = json.loads(result["claims_json"])["fields"][0]
    assert entry["basis"] == "inferred_high"
    reasons = [d["reason"] for d in json.loads(result["downgraded_detail"])]
    assert "unknown_channel" not in reasons


def test_fact_without_channel_is_still_downgraded():
    """反面：``fact`` 无渠道、无锚点可定位 → 仍降级（事实必须能回溯）。"""
    result = pipe.localize_claims(
        records_json(record("试驾录音信息", "我以前做充电桩的。")),
        claims_json(claim(attribute="occupation", value="充电桩行业", record_channel="")),
    )
    entry = json.loads(result["claims_json"])["fields"][0]
    assert entry["basis"] == "inferred_low"
    assert "unknown_channel" in [d["reason"] for d in json.loads(result["downgraded_detail"])]


# ---------------------------------------------------------------- v2.1 R2 空值占位 KPI

def test_blank_value_claims_are_counted_and_dropped():
    """``value`` 为空的占位断言被拒收并计入 KPI，而不是伪装成一条有效结论。"""
    result = pipe.project_claims(
        claims_json(
            claim(attribute="occupation", value="教师"),
            claim(attribute="hobbies", value="", claim_id="c_002"),
            claim(attribute="pain_points", value="[]", claim_id="c_003"),
            claim(attribute="douyin_id", value="   ", claim_id="c_004"),
        )
    )
    assert result["empty_claims"] == 3
    assert set(json.loads(result["empty_claim_names"])) == {"hobbies", "pain_points", "douyin_id"}
    assert json.loads(result["facts_json"]).get("occupation") == "教师"


# ---------------------------------------------------------------- v3 接口一：坐标形态透传

def test_localize_carries_normalized_anchor_kind():
    """当归型事实（ASR 纠错/归一）：``anchor_kind`` 与 ``source_quote`` 一路透传到出口。"""
    result = pipe.localize_claims(
        records_json(
            record(
                "外呼录音信息",
                "我开的是安安I60",
                kind="transcript",
                source_class="primary_interaction",
            )
        ),
        claims_json(
            {
                "attribute": "intended_model",
                "value": "埃安",
                "record_channel": "外呼录音信息",
                "basis": "fact",
                "anchor": "我开的是安安I60",
                "anchor_kind": "normalized",
                "source_quote": "安安I60",
            }
        ),
    )
    entry = json.loads(result["claims_json"])["fields"][0]
    assert entry["anchor_kind"] == "normalized"
    assert entry["source_quote"] == "安安I60"


def test_localize_defaults_to_verbatim():
    """未声明当归型时，坐标形态按锚定型处理。"""
    result = pipe.localize_claims(
        records_json(
            record(
                "外呼录音信息",
                "我现在开的是一九年的君威",
                kind="transcript",
                source_class="primary_interaction",
            )
        ),
        claims_json(
            {
                "attribute": "old_car_model",
                "value": "2019款别克君威",
                "record_channel": "外呼录音信息",
                "basis": "fact",
                "anchor": "我现在开的是一九年的君威",
            }
        ),
    )
    entry = json.loads(result["claims_json"])["fields"][0]
    assert entry["anchor_kind"] == "verbatim"


# ---------------------------------------------------------------- v3 P4 L2 审查核验层

def note(key, value, *, evidence=None, channel=""):
    return {
        "attribute_key": key,
        "attribute_value": value,
        "record_channel": channel,
        "reasoning_detail": {"summary": "", "steps": [], "evidence": evidence or [], "confidence": 0.7},
    }


def test_review_removes_placeholder_values():
    """「未提供」这类占位 Notes 直接去除——看着有值、点进去没内容的，等于没填。"""
    result = pipe.review_notes(
        json.dumps([note("hobbies", "未提供"), note("occupation", "教师")], ensure_ascii=False),
        "{}",
        "{}",
    )
    assert result["placeholder_removed"] == 1
    kept = json.loads(result["notes_updates_json"])
    assert [n["attribute_key"] for n in kept] == ["occupation"]


def test_review_flags_illegal_enum_not_downgraded():
    """枚举取值不在契约内且未降级 → 记 issue。"""
    result = pipe.review_notes(
        json.dumps([note("family_structure", "说不清")], ensure_ascii=False),
        "{}",
        "{}",
    )
    assert result["enum_illegal"] == 1


def test_review_allows_illegal_enum_when_downgraded():
    """已降为 ``inferred_low`` 的非法枚举值放行（P3 规则 d 保留的客户原话）。"""
    result = pipe.review_notes(
        json.dumps([note("family_structure", "说不清")], ensure_ascii=False),
        "{}",
        json.dumps({"family_structure": "inferred_low"}, ensure_ascii=False),
    )
    assert result["enum_illegal"] == 0


def test_review_flags_unbacked_fact():
    """``fact`` 却既无证据又无渠道 → 记 issue（编造的确定性）。"""
    result = pipe.review_notes(
        json.dumps([note("occupation", "教师")], ensure_ascii=False),
        json.dumps({"occupation": "教师"}, ensure_ascii=False),
        json.dumps({"occupation": "fact"}, ensure_ascii=False),
    )
    assert result["unbacked_fact"] == 1


def test_review_flags_evidence_self_reference():
    """证据位出现「推断为…」结论型措辞 → 记 issue。"""
    result = pipe.review_notes(
        json.dumps([note("age", "40-49岁", evidence=["推断为40-49岁"])], ensure_ascii=False),
        "{}",
        "{}",
    )
    assert result["issue_count"] >= 1
    assert any(i["rule"] == "evidence_self_reference" for i in json.loads(result["issue_json"]))


def test_review_keeps_clean_notes():
    """干净的 Notes 原样保留，不改动取值。"""
    result = pipe.review_notes(
        json.dumps([note("old_car_model", "2019款别克君威",
                         evidence=["外呼录音信息：我现在开的是一九年的君威"])], ensure_ascii=False),
        "{}",
        "{}",
    )
    assert result["issue_count"] == 0
    assert json.loads(result["notes_updates_json"])[0]["attribute_value"] == "2019款别克君威"


# ------------------------------------------- v3.1 改造三：锚点相关性门

def test_greeting_anchor_is_not_evidence():
    """寒暄语锚点（「喂，你好」）零信息量，不得进证据位（实测 13530559591 的 title）。

    它在原文里逐字存在且唯一，v3 的「能定位」判据因此放行——本门补的是「有没有信息」。
    """
    result = pipe.localize_claims(
        records_json(record("外呼录音信息", "（销售）喂，你好，我这边是猛士的。", kind="transcript")),
        claims_json(claim(attribute="title", value="Mr", record_channel="外呼录音信息",
                          anchor="喂，你好")),
    )
    entry = json.loads(result["claims_json"])["fields"][0]
    assert "quote" not in entry
    reasons = [d["reason"] for d in json.loads(result["downgraded_detail"])]
    assert "noisy_anchor" in reasons


def test_json_fragment_anchor_is_not_evidence():
    """OCR 的 JSON 残片不作证据（实测 13435844484 的 title / phone_number 证据）。"""
    result = pipe.localize_claims(
        records_json(record("微信手机号搜索界面信息", '{"nickname":"小林","gender": "男性"}', kind="ocr")),
        claims_json(claim(attribute="title", value="Mr", record_channel="微信手机号搜索界面信息",
                          anchor='gender": "男性')),
    )
    entry = json.loads(result["claims_json"])["fields"][0]
    assert "quote" not in entry
    assert "noisy_anchor" in [d["reason"] for d in json.loads(result["downgraded_detail"])]


def test_value_signal_must_appear_in_anchor():
    """声明的 ``value_signal`` 不在锚点里 → 锚点与取值不相关，收回引文（v3.1 改造三）。"""
    result = pipe.localize_claims(
        records_json(record("外呼录音信息", "（客户）我平时开货车跑长途。", kind="transcript")),
        claims_json(claim(attribute="title", value="Mr", record_channel="外呼录音信息",
                          anchor="我平时开货车跑长途", value_signal="女士")),
    )
    entry = json.loads(result["claims_json"])["fields"][0]
    assert "quote" not in entry
    assert "signal_mismatch" in [d["reason"] for d in json.loads(result["downgraded_detail"])]


def test_value_signal_matching_anchor_keeps_quote():
    """反面：``value_signal`` 确实是锚点子串时正常保留引文。"""
    result = pipe.localize_claims(
        records_json(record("外呼录音信息", "（销售）先生，您上次看的那台车。", kind="transcript")),
        claims_json(claim(attribute="title", value="Mr", record_channel="外呼录音信息",
                          anchor="先生，您上次看的那台车", value_signal="先生")),
    )
    entry = json.loads(result["claims_json"])["fields"][0]
    assert "先生" in entry["quote"]


def test_normal_quote_is_not_flagged_noisy():
    """普通长引文里夹一句寒暄不算噪音——本门只拦短锚。"""
    body = "你好，我上次来看的是那台黑色的，价格能再谈谈吗。"
    result = pipe.localize_claims(
        records_json(record("外呼录音信息", body, kind="transcript")),
        claims_json(claim(attribute="old_car_model", value="别克",
                          record_channel="外呼录音信息", anchor=body)),
    )
    entry = json.loads(result["claims_json"])["fields"][0]
    assert "quote" in entry


# ------------------------------------------- v3.1 改造一：推导链透传到出口

def test_reasoning_chain_reaches_notes_steps():
    """L1 给的 ``reasoning_chain`` 须一路传到 Notes 的 ``steps``（与 ``evidence`` 分料）。"""
    result = pipe.project_claims(
        claims_json(
            claim(
                attribute="annual_income",
                value="10-20万",
                record_channel="",
                anchor="",
                basis="inferred_high",
                basis_of_inference=["预算 9.5 万且贷款购车"],
                reasoning_chain=["预算 9.5 万且需贷款", "支付能力偏紧", "收入取中低档"],
            )
        )
    )
    note = next(
        n for n in json.loads(result["notes_updates_json"]) if n["attribute_key"] == "annual_income"
    )
    assert note["reasoning_detail"]["steps"] == ["预算 9.5 万且需贷款", "支付能力偏紧", "收入取中低档"]
    # 依据与推导链分料：evidence 是论据，steps 是论证，两者不得同集。
    assert set(note["reasoning_detail"]["steps"]) != set(note["reasoning_detail"]["evidence"])


def test_steps_do_not_restate_quote_when_no_chain():
    """旧产物没有 ``reasoning_chain`` 时，``steps`` **不得**再抄锚点原文（雷同的来源）。"""
    result = pipe.project_claims(
        claims_json(claim(attribute="occupation", value="教师", quote="我以前做充电桩的"))
    )
    note = next(n for n in json.loads(result["notes_updates_json"]) if n["attribute_key"] == "occupation")
    steps = note["reasoning_detail"]["steps"]
    assert all("我以前做充电桩的" not in s for s in steps), steps
