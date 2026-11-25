"""跟进履历（``follow_up``）：解析、装配、契约自检与跳过分支。

**这层要单独钉住的原因**：契约文档写明「校验失败返回 400，本次画像回写整体回滚」——
履历里一条非法时间、一个重复 ``id``，就会让这个客户已经算好的定级、Notes 与九个板块
一起作废。这些约束错了在本地**不会报错**，只会在真实回传那一刻出事。

另一条同级的风险在反方向：``follow_up_history: []`` 在契约里是「清空该客户全部履历」。
没有数据时发空数组不是「没写」，是**删除**。
"""

from __future__ import annotations

import json

import pytest

from customer_profile import follow_up as fu


# ---------------------------------------------------------------- 夹具

def _record(
    content: str,
    *,
    created_at: str = "2026-11-01 15:38:40",
    purpose: str = "邀请试驾",
    method: str = "电话",
    result: str = "仍需后续跟进",
    level: str = "A 级意向（高）",
    vehicle: str = "Max",
    consultant: str = "郑伊美",
    record_id: str = "rec-1",
    status: str = "ok",
    channel: str = fu.FOLLOW_CHANNEL,
    source_name: str = fu.FOLLOW_SOURCE_NAME,
) -> dict:
    """造一条 L0 记录。正文形态逐字模仿 ``ontology_payload.render_record``。"""
    lines = [
        f"跟进记录 ID：{record_id}",
        f"跟进创建时间：{created_at}",
        f"跟进结果：{result}",
        f"跟进内容：{content}",
        f"跟进目的：{purpose}",
        f"意向级别：{level}",
        "下次跟进时间：2026-11-02 00:00:00",
        "意向车型名称：" + vehicle,
        f"沟通方式：{method}",
        f"跟进顾问姓名：{consultant}",
        "是否转商机：否",
    ]
    return {
        "record_id": record_id,
        "channel": channel,
        "source_name": source_name,
        "status": status,
        "created_at": "2026-11-01T15:45:06",
        "content": "\n".join(lines),
        "source_key": record_id,
    }


def _lead_record(leads, *, record_id: str = "lead-1") -> dict:
    """造一条 ``OntologyLeadInfo`` 记录；一条 L0 记录里可含多条线索。"""
    blocks = []
    for index, (series, model) in enumerate(leads, start=1):
        block = "\n".join(
            [
                f"线索 ID：{index}",
                f"意向车型：{model}",
                f"意向车系：{series}",
            ]
        )
        blocks.append(block if len(leads) == 1 else f"[第 {index} 条]\n{block}")
    return {
        "record_id": record_id,
        "channel": "OntologyLeadInfo",
        "source_name": "线索信息",
        "status": "ok",
        "created_at": "2026-09-10T13:07:42",
        "content": "\n\n".join(blocks),
        "source_key": record_id,
    }


def _records_json(*records: dict) -> str:
    return json.dumps(list(records), ensure_ascii=False)


class _FakeLLM:
    """返回固定 JSON 的假客户端。``fail=True`` 时抛错，用来验兜底。"""

    def __init__(self, payload, *, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.calls: list[dict] = []

    async def llm_call(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.fail:
            raise RuntimeError("模型炸了")
        return self.payload


# ---------------------------------------------------------------- 解析

def test_parses_rendered_lines_back_into_fields():
    """渲染层写「中文名：值」，这里要能原样读回来。"""
    fields = fu.parse_record_lines(_record("电话没接通")["content"])
    assert fields["created_at"] == "2026-11-01 15:38:40"
    assert fields["content"] == "电话没接通"
    assert fields["purpose"] == "邀请试驾"
    assert fields["method"] == "电话"
    assert fields["vehicle"] == "Max"
    assert fields["consultant"] == "郑伊美"


def test_value_may_contain_a_colon():
    """值里再出现冒号不切——渲染层写的是「字段名：值」，只认第一处分隔。"""
    fields = fu.parse_record_lines("跟进内容：客户说：下周再看\n")
    assert fields["content"] == "客户说：下周再看"


def test_unknown_lines_are_dropped():
    """不认识的字段不进结果——渠道表将来加字段时不会悄悄漏进提示词。"""
    fields = fu.parse_record_lines("跟进内容：x\n某个新字段：y\n")
    assert "某个新字段" not in fields.values()
    assert set(fields) == {"content"}


def test_records_sort_by_business_time_not_by_ledger_time():
    """按「跟进创建时间」排，不按 L0 的 ``created_at``（落库时刻）。

    实测两者差几分钟到几十分钟；顺带证明同一时刻的两条按记录 ID 兜底排序、顺序稳定。
    """
    late = _record("后发生", created_at="2026-11-02 11:53:55", record_id="b")
    early = _record("先发生", created_at="2026-11-01 15:37:56", record_id="c")
    tie_a = _record("同刻 A", created_at="2026-11-01 15:37:56", record_id="a")

    parsed = fu.parse_records(_records_json(late, early, tie_a))
    assert [item["content"] for item in parsed] == ["同刻 A", "先发生", "后发生"]


def test_ignores_other_channels_and_failed_records():
    """别的渠道、以及 ``status`` 非 ok 的记录都不算跟进履历。"""
    other = _record("朋友圈", channel="WeChatMomentsScreenshot", source_name="朋友圈信息")
    broken = _record("坏记录", status="exception")
    good = _record("好记录")
    parsed = fu.parse_records(_records_json(other, broken, good))
    assert [item["content"] for item in parsed] == ["好记录"]


def test_record_without_content_and_time_is_dropped():
    """两个关键字段都没有的记录不拿来凑节点。"""
    bare = {
        "channel": fu.FOLLOW_CHANNEL,
        "source_name": fu.FOLLOW_SOURCE_NAME,
        "status": "ok",
        "content": "跟进记录 ID：x",
    }
    assert fu.parse_records(_records_json(bare)) == []


# ---------------------------------------------------------------- 时间归一

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-11-01 15:38:40", "2026-11-01T15:38:40"),
        ("2026-11-01 15:38", "2026-11-01T15:38:00"),
        ("2026-11-01T15:38:40", "2026-11-01T15:38:40"),
        ("2026-11-01", "2026-11-01"),
        ("2026-11-01 00:00:00", "2026-11-01T00:00:00"),
    ],
)
def test_parse_time_normalizes_to_contract_shapes(raw, expected):
    assert fu.parse_time(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "昨天下午", "2026-13-01 10:00:00", "2026-02-30 10:00:00", "11/01/2026", None],
)
def test_parse_time_rejects_what_it_cannot_confirm(raw):
    """解析不了给空串——契约里非法格式是 400，而 ``followed_at`` 本就允许缺省。"""
    assert fu.parse_time(raw) == ""


def test_readable_time_gives_a_sales_facing_phrase():
    assert fu.readable_time("2026-11-01T15:38:40") == "2026年11月1日下午"
    assert fu.readable_time("2026-11-01T09:00:00") == "2026年11月1日上午"
    assert fu.readable_time("2026-11-01T21:00:00") == "2026年11月1日晚上"
    assert fu.readable_time("2026-11-01") == "2026年11月1日"


# ---------------------------------------------------------------- 兜底命名与 extra_info

def test_fallback_name_uses_method_and_purpose():
    item = {"method": "电话", "purpose": "邀请试驾"}
    assert fu.fallback_name(item) == "电话沟通·邀请试驾"
    assert fu.fallback_name({"method": "电话"}) == "电话沟通"
    assert fu.fallback_name({"purpose": "邀请试驾"}) == "邀请试驾"
    # 两者都缺时不编造动作。
    assert fu.fallback_name({}) == "客户跟进"


def test_fixed_fields_drop_placeholders_and_bare_codes():
    """占位串与未解码的裸字典码不进对客产物（docs/README.md 的对客产出红线）。"""
    fields = fu.fixed_fields(
        {
            "consultant": "无",
            "vehicle": "90463",  # 未登记码会原样漏出
            "result": "仍需后续跟进",
            "level": "-",
        }
    )
    assert "follow_up_by" not in fields
    assert "related_model" not in fields
    assert "intent_level" not in fields
    assert fields["follow_up_result"] == "仍需后续跟进"


def test_fixed_fields_carries_channel_from_method():
    """跟进渠道是 1124-v2 版新增的第七项固定属性，取自沟通方式。"""
    assert fu.fixed_fields({"method": "电话"})["follow_up_channel"] == "电话"
    assert "follow_up_channel" not in fu.fixed_fields({"method": "无"})


def test_extra_info_keeps_only_open_tags():
    """固定属性提升为一级字段后，``extra_info`` 只留战败类型。

    「参与人」是历史版本自加的编外标签（契约从未有此键），2026-11-24 起不再产出。
    """
    extra = fu.fallback_extra(
        {
            "method": "电话",
            "consultant": "郑伊美",
            "vehicle": "Max",
            "result": "仍需后续跟进",
            "level": "A 级意向（高）",
            "to_business": "否",
            "next_follow": "2026-11-02 00:00:00",
            "defeat_type": "价格原因",
        }
    )
    assert extra == {"战败类型": "价格原因"}


def test_next_follow_up_at_folds_midnight_to_a_date():
    """下次跟进时间大量落在零点（上游按天填），给日期就够了。

    与 ``followed_at`` 不同，契约对 ``next_follow_up_at`` **不约束格式**，因此解析不了的
    模糊时间（「下周」）原样保留——它没有 400 回滚风险，丢掉反而少了一条交接信息。
    """
    item = {"next_follow": "2026-11-02 00:00:00"}
    assert fu.fixed_fields(item)["next_follow_up_at"] == "2026-11-02"
    item = {"next_follow": "2026-11-02 15:30:00"}
    assert fu.fixed_fields(item)["next_follow_up_at"] == "2026-11-02T15:30:00"
    assert fu.fixed_fields({"next_follow": "下周"})["next_follow_up_at"] == "下周"


# ---------------------------------------------------------------- 车型车系

def test_lead_vehicle_map_reads_every_lead_block():
    """一条 L0 线索记录里可能有多条线索，按 ``[第 N 条]`` 逐块解析。"""
    l0 = _records_json(
        _lead_record([("全新M817", "Ultra"), ("M817", "MAX+"), ("X700", "Max+")])
    )
    by_model, series = fu.lead_vehicle_map(l0)
    assert series == {"全新M817", "M817", "X700"}
    # 车型名称做大小写归一：MAX+ / Max+ 是同一个版本。
    assert by_model["ULTRA"] == {"全新M817"}
    assert by_model["MAX+"] == {"M817", "X700"}


def test_related_model_is_prefixed_with_the_customer_series():
    """业务方要车系（M817 / X700），不只是版本名（Ultra / PRO）。"""
    records = fu.parse_records(_records_json(_record("到店看车", vehicle="Ultra")))
    l0 = _records_json(_lead_record([("全新M817", "Ultra"), ("M817", "PRO")]))
    models, series = fu.lead_vehicle_map(l0)
    nodes, _ = fu.assemble(records, None, lead_models=models, lead_series=series)
    assert nodes[0]["related_model"] == "全新M817 Ultra"


def test_related_model_keeps_trim_when_two_series_share_it():
    """同一个版本名落在两个车系下：不猜，保持只出版本名。"""
    records = fu.parse_records(_records_json(_record("到店看车", vehicle="Ultra")))
    l0 = _records_json(_lead_record([("X700", "Ultra"), ("全新M817", "Ultra")]))
    models, series = fu.lead_vehicle_map(l0)
    nodes, _ = fu.assemble(records, None, lead_models=models, lead_series=series)
    assert nodes[0]["related_model"] == "Ultra"


def test_series_alias_is_not_treated_as_ambiguous():
    """``全新M817`` 与 ``M817`` 是同一车系的两种写法，不构成歧义。"""
    records = fu.parse_records(_records_json(_record("到店看车", vehicle="MAX+")))
    l0 = _records_json(_lead_record([("全新M817", "MAX+"), ("M817", "MAX+")]))
    models, series = fu.lead_vehicle_map(l0)
    nodes, _ = fu.assemble(records, None, lead_models=models, lead_series=series)
    assert "M817" in nodes[0]["related_model"]
    assert nodes[0]["related_model"].endswith("MAX+")


def test_related_model_falls_back_to_the_only_series():
    """记录没填车型、客户只有一个车系时，车系本身就是最有用的信息。"""
    records = fu.parse_records(_records_json(_record("到店看车", vehicle="")))
    l0 = _records_json(_lead_record([("X700", "Ultra")]))
    models, series = fu.lead_vehicle_map(l0)
    nodes, _ = fu.assemble(records, None, lead_models=models, lead_series=series)
    assert nodes[0]["related_model"] == "X700"


def test_build_reads_series_from_the_same_l0_payload():
    """管线只给跟进节点一份 ``records_json``，线索记录也在里面，不需要改依赖。"""
    import asyncio

    l0 = _records_json(
        _record("到店看车", vehicle="Ultra"), _lead_record([("X700", "Ultra")])
    )
    out = asyncio.run(fu.build(l0, llm=None))
    node = json.loads(out["follow_up_json"])[0]
    assert node["related_model"] == "X700 Ultra"


# ---------------------------------------------------------------- 装配

def test_assemble_covers_every_record_when_model_says_nothing():
    """模型没给东西时按记录 1:1 出节点——**不丢记录**是这条产线的底线。"""
    records = fu.parse_records(_records_json(_record("一", record_id="a"), _record("二", record_id="b")))
    nodes, supplemented = fu.assemble(records, None)
    assert len(nodes) == 2
    assert supplemented == [0, 1]
    assert [n["sequence"] for n in nodes] == [1, 2]
    assert [n["id"] for n in nodes] == ["fu-1", "fu-2"]


def test_assemble_groups_by_model_and_backfills_the_rest():
    """模型覆盖到的记录成组，没覆盖到的由代码补，且回到时间序的正确位置。"""
    records = fu.parse_records(
        _records_json(
            _record("一", created_at="2026-11-01 09:00:00", record_id="a"),
            _record("二", created_at="2026-11-01 10:00:00", record_id="b"),
            _record("三", created_at="2026-11-01 11:00:00", record_id="c"),
        )
    )
    model = {
        "nodes": [
            {
                "records": [1, 2],
                "node_name": "连续两次邀约",
                "summary": "销售顾问连续两次电话联系客户邀请试驾，均未接通。",
                "next_step": "本次仍需后续跟进，约定次日再沟通。",
            }
        ]
    }
    nodes, supplemented = fu.assemble(records, model)
    assert supplemented == [2]
    assert len(nodes) == 2
    assert nodes[0]["node_name"] == "连续两次邀约"
    # 模型仍可能返回 ``next_step``，装配侧忽略它——卡片只出一段小结。
    assert nodes[0]["contents"] == ["销售顾问连续两次电话联系客户邀请试驾，均未接通。"]
    # 模型没覆盖的第三条回落到兜底，仍在时间序末尾。
    assert nodes[1]["contents"] == ["三"]


def test_contents_are_one_summary_paragraph_never_the_raw_text():
    """``contents`` 只有一段模型写的小结，销售原文不进卡片。

    2026-11-17 口径：原文大量是「电话没接通」「白天联系。。。。。。」这类碎片，与小结并列
    只是让读者把同一件事读两遍，因此整段去掉。这里钉住「原文一条都不出现」。

    2026-11-24 起 ``next_step`` 段整体删除：六项固定属性提为一级字段后，它实测 61/61
    个节点都在复述「跟进结果/意向级别/转商机/下次跟进时间」，净增信息为零，还常与小
    结写的沟通内容互相打架。这些值由字段标签承担。
    """
    records = fu.parse_records(
        _records_json(
            _record("电话没接通", record_id="a"),
            _record("用户忙无法接通", record_id="b"),
            _record("通话转入语音留言", record_id="c"),
        )
    )
    model = {
        "nodes": [
            {
                "records": [1, 2, 3],
                "node_name": "电话跟进",
                "summary": "销售顾问三次电话联系客户邀请试驾，均未接通。",
                "next_step": "本次仍需后续跟进，约定次日再联系。",
            }
        ]
    }
    nodes, _ = fu.assemble(records, model)
    assert nodes[0]["contents"] == ["销售顾问三次电话联系客户邀请试驾，均未接通。"]


def test_next_step_from_the_model_is_ignored():
    """模型仍返回 ``next_step``（旧提示词、外部调用方）时整段丢弃。"""
    records = fu.parse_records(_records_json(_record("电话没接通")))
    model = {
        "nodes": [
            {
                "records": [1],
                "node_name": "首次电话邀约",
                "summary": "销售顾问电话联系客户，客户未接听。",
                "next_step": "本次仍需后续跟进，约定次日再联系。",
            }
        ]
    }
    nodes, _ = fu.assemble(records, model)
    assert nodes[0]["contents"] == ["销售顾问电话联系客户，客户未接听。"]


def test_next_step_alone_does_not_replace_the_summary():
    """只给了 ``next_step``、没给 ``summary`` 时回落销售原文，不把模板句当正文。"""
    records = fu.parse_records(_records_json(_record("电话没接通")))
    model = {
        "nodes": [
            {
                "records": [1],
                "node_name": "首次电话邀约",
                "next_step": "本次仍需后续跟进，约定次日再联系。",
            }
        ]
    }
    nodes, _ = fu.assemble(records, model)
    assert nodes[0]["contents"] == ["电话没接通"]


def test_model_summary_may_arrive_as_a_list():
    """模型偶尔把小结给成数组：内容非空就照收，不因形态不合预期整句丢掉。"""
    records = fu.parse_records(_records_json(_record("电话没接通")))
    model = {
        "nodes": [
            {
                "records": [1],
                "node_name": "首次电话邀约",
                "summary": ["销售顾问电话联系客户，客户未接听。"],
                "next_step": ["约定次日再联系。"],
            }
        ]
    }
    nodes, _ = fu.assemble(records, model)
    assert nodes[0]["contents"] == ["销售顾问电话联系客户，客户未接听。"]


def test_extra_info_reflects_the_latest_record_of_a_merged_node():
    """合并节点下方的标签取**最新**那条记录，与正文写的结论对齐。

    实测踩到：节点把两次未接通合并成一条，正文按最新那条写「A 级、11 月 3 日」，
    而标签取首条显示「C 级、2026-11-01」——同一张卡片上两个说法。
    """
    records = fu.parse_records(
        _records_json(
            _record(
                "客户忙",
                created_at="2026-10-31 12:54:05",
                level="C 级意向（中）",
                record_id="a",
            ),
            _record(
                "客户没接听",
                created_at="2026-10-31 12:54:09",
                level="A 级意向（高）",
                record_id="b",
            ),
        )
    )
    model = {
        "nodes": [
            {
                "records": [1, 2],
                "node_name": "两次未接通",
                "summary": "销售顾问两次电话联系客户邀请试驾，均未接通。",
                "next_step": "本次仍需后续跟进，意向级别为 A 级（高）。",
            }
        ]
    }
    nodes, _ = fu.assemble(records, model)
    assert nodes[0]["intent_level"] == "A 级意向（高）"
    assert "extra_info" not in nodes[0]
    # 时间轴位置仍按首条记录。
    assert nodes[0]["followed_at"] == "2026-10-31T12:54:05"


def test_merged_node_keeps_every_consultant_who_took_part():
    """合并节点不再把顾问并集写进 ``extra_info``——「参与人」已移除。

    顾问信息由 ``follow_up_by``（取最新一条）承载，见下一个测试。
    """
    records = fu.parse_records(
        _records_json(
            _record("一", created_at="2026-11-01 09:00:00", consultant="李凡", record_id="a"),
            _record("二", created_at="2026-11-01 10:00:00", consultant="王宇昕", record_id="b"),
        )
    )
    model = {"nodes": [{"records": [1, 2], "node_name": "两次邀约", "summary": "两次电话邀约。"}]}
    nodes, _ = fu.assemble(records, model)
    assert "extra_info" not in nodes[0]


def test_model_groups_reject_out_of_range_and_duplicate_indexes():
    """越界索引、重复索引、坏类型一律丢弃——模型说话不规矩不该让整条链路失败。"""
    records = fu.parse_records(_records_json(_record("一"), _record("二")))
    model = {
        "nodes": [
            {"records": [1, 1, 99, "x"], "node_name": "A"},
            {"records": [], "node_name": "空组"},
            {"records": [2], "node_name": "B"},
        ]
    }
    groups = fu.model_groups(model, len(records))
    assert [g["indexes"] for g in groups] == [[0], [1]]


def test_raw_content_is_kept_verbatim_including_inner_whitespace():
    """兜底路径展示的是**销售原文**，连空白都不改。

    实测有销售在跟进内容里连打多个空格（「女士关注了x700   目前在展会上…」）。
    把它折叠成单空格更好看，但那就不是他写下的话了——而兜底展示时那是读者唯一能看到的凭据。
    """
    raw = "女士关注了x700   目前在展会上表示下周可以到店看车 请跟进 "
    records = fu.parse_records(_records_json(_record(raw)))
    nodes, _ = fu.assemble(records, None)
    assert nodes[0]["contents"][0] == raw.strip()


def test_model_summary_is_whitespace_collapsed():
    """模型写的小结反而要折叠空白：那里是模型偶发的换行与多余空格。"""
    records = fu.parse_records(_records_json(_record("原文")))
    model = {
        "nodes": [
            {
                "records": [1],
                "node_name": "A",
                "summary": "第一句。\n\n   第二句。",
                "next_step": "约定次日再联系。",
            }
        ]
    }
    nodes, _ = fu.assemble(records, model)
    assert nodes[0]["contents"] == ["第一句。 第二句。"]


def test_node_caps_at_contract_limits():
    """契约 §3.3 的条数与字数上限在装配时就生效。"""
    long_text = "长" * (fu.MAX_CONTENT_CHARS + 500)
    records = fu.parse_records(_records_json(_record(long_text)))
    nodes, _ = fu.assemble(records, None)
    assert len(nodes[0]["contents"][0]) == fu.MAX_CONTENT_CHARS


def test_node_without_any_content_gets_an_explanation_not_an_empty_array():
    """``contents`` 允许空数组，但卡片上会是一片空白；给一条说明更有用。"""
    records = [{"created_at": "2026-11-01 10:00:00", "content": ""}]
    nodes, _ = fu.assemble(records, None)
    assert nodes[0]["contents"] == ["销售留下了跟进记录，但没有填写具体内容"]


# ---------------------------------------------------------------- 契约自检

def test_sanitize_renumbers_ids_and_sequences():
    """``id`` / ``sequence`` 由代码重新发号——唯一性是契约硬约束（重复即 400）。"""
    raw = [
        {"node_name": "A", "contents": ["x"], "id": "dup", "sequence": 7},
        {"node_name": "B", "contents": ["y"], "id": "dup", "sequence": 7},
    ]
    kept, dropped = fu.sanitize(raw)
    assert dropped == []
    assert [n["id"] for n in kept] == ["fu-1", "fu-2"]
    assert [n["sequence"] for n in kept] == [1, 2]


def test_sanitize_drops_only_the_bad_time_field_not_the_node():
    """时间坏了只丢这个字段：契约允许 ``followed_at`` 缺省，节点本身仍有价值。"""
    raw = [{"node_name": "A", "contents": ["x"], "followed_at": "昨天下午"}]
    kept, dropped = fu.sanitize(raw)
    assert len(kept) == 1
    assert "followed_at" not in kept[0]
    assert dropped and "时间格式非法" in dropped[0]["reason"]


def test_sanitize_rejects_node_without_name_or_contents():
    kept, dropped = fu.sanitize(
        [
            {"contents": ["x"]},
            {"node_name": "有名字", "contents": []},
            {"node_name": "正常", "contents": ["ok"]},
        ]
    )
    assert len(kept) == 1
    assert len(dropped) == 2


def test_sanitize_caps_node_count():
    raw = [{"node_name": f"N{i}", "contents": [f"c{i}"]} for i in range(fu.MAX_NODES + 5)]
    kept, dropped = fu.sanitize(raw)
    assert len(kept) == fu.MAX_NODES
    assert len(dropped) == 5


def test_sanitize_rejects_oversized_extra_info():
    """``extra_info`` 序列化超过 64KB 整体丢弃该键，而不是发出去赌接口接受。"""
    raw = [
        {
            "node_name": "A",
            "contents": ["x"],
            "extra_info": {"大字段": "长" * (fu.MAX_EXTRA_BYTES)},
        }
    ]
    kept, _ = fu.sanitize(raw)
    assert "extra_info" not in kept[0]


def test_sanitize_tolerates_non_array_input():
    assert fu.sanitize("不是数组") == ([], [{"index": 0, "reason": "不是数组"}])


# ---------------------------------------------------------------- 1124 版固定字段

def test_node_carries_the_six_fixed_fields_from_the_latest_record():
    """六项固定属性是节点一级字段，全部取组内**最新**那条记录。

    1124 版契约把它们从 ``extra_info`` 提出来单独成列，前端按结构化字段渲染。
    """
    records = fu.parse_records(
        _records_json(
            _record(
                "客户忙",
                created_at="2026-10-31 12:54:05",
                level="C 级意向（中）",
                record_id="a",
            ),
            _record(
                "客户没接听",
                created_at="2026-10-31 12:54:09",
                level="A 级意向（高）",
                consultant="李凡",
                record_id="b",
            ),
        )
    )
    nodes, _ = fu.assemble(
        records,
        {"nodes": [{"records": [1, 2], "node_name": "两次跟进", "summary": "两次电话跟进。"}]},
    )
    node = nodes[0]
    assert node["related_model"] == "Max"
    assert node["intent_level"] == "A 级意向（高）"
    assert node["follow_up_result"] == "仍需后续跟进"
    assert node["converted_to_opportunity"] == "否"
    assert node["next_follow_up_at"] == "2026-11-02"
    assert node["followed_at"] == "2026-10-31T12:54:05"


def test_extra_info_is_absent_when_there_are_no_open_tags():
    """固定属性都已是一级字段；无开放标签时 ``extra_info`` 整个不出键。"""
    records = fu.parse_records(_records_json(_record("电话没接通")))
    nodes, _ = fu.assemble(records, None)
    assert "extra_info" not in nodes[0]
    for legacy_key in (
        "跟进渠道",
        "渠道",
        "参与人",
        "关联车型",
        "跟进结果",
        "意向级别",
        "是否转商机",
        "下一步跟进",
    ):
        assert legacy_key not in nodes[0]


def test_merged_node_follow_up_by_takes_the_latest():
    """``follow_up_by`` 单值，取最新一条的顾问；``extra_info`` 不再承载顾问并集。"""
    records = fu.parse_records(
        _records_json(
            _record("一", created_at="2026-11-01 09:00:00", consultant="李凡", record_id="a"),
            _record("二", created_at="2026-11-01 10:00:00", consultant="王宇昕", record_id="b"),
        )
    )
    model = {"nodes": [{"records": [1, 2], "node_name": "两次邀约", "summary": "两次电话邀约。"}]}
    nodes, _ = fu.assemble(records, model)
    assert nodes[0]["follow_up_by"] == "王宇昕"
    assert "extra_info" not in nodes[0]


def test_sanitize_promotes_legacy_extra_info_fields():
    """旧形状节点（七项都在 ``extra_info``）经自检后提升为一级字段，并去掉重复键。"""
    raw = [
        {
            "node_name": "首次到店",
            "contents": ["客户到店看车。"],
            "extra_info": {
                "跟进渠道": "到店",
                "参与人": ["销售顾问张三", "客户本人"],
                "关联车型": "X700",
                "意向级别": "B 级意向（中）",
                "跟进结果": "预约试驾",
                "是否转商机": "是",
                "下次跟进时间": "2026-05-22 00:00:00",
                "战败类型": "价格原因",
            },
        }
    ]
    kept, dropped = fu.sanitize(raw)
    assert dropped == []
    node = kept[0]
    assert node["related_model"] == "X700"
    assert node["intent_level"] == "B 级意向（中）"
    assert node["follow_up_result"] == "预约试驾"
    assert node["converted_to_opportunity"] == "是"
    assert node["next_follow_up_at"] == "2026-05-22"
    assert node["follow_up_channel"] == "店内沟通"
    assert node["extra_info"] == {"战败类型": "价格原因"}


def test_sanitize_strips_legacy_participants_from_open_tags():
    """「参与人」是历史遗留的编外标签：旧批次/外部节点带着它时，自检直接剥掉。"""
    raw = [
        {
            "node_name": "首次到店",
            "contents": ["客户到店看车。"],
            "extra_info": {
                "参与人": ["销售顾问张三", "客户本人"],
                "战败类型": "价格原因",
            },
        },
        {
            "node_name": "电话跟进",
            "contents": ["电话没接通。"],
            "extra_info": {"参与人": ["客户本人"]},
        },
    ]
    kept, _ = fu.sanitize(raw)
    assert kept[0]["extra_info"] == {"战败类型": "价格原因"}
    # 剥离后不剩任何键：与固定字段提升的既有口径一致，保留空对象。
    assert kept[1]["extra_info"] == {}


def test_sanitize_recognizes_our_legacy_next_step_key():
    """旧版我们把下次跟进写成 ``下一步跟进``，它不在服务端别名表里，自检要认。"""
    raw = [
        {
            "node_name": "电话跟进",
            "contents": ["电话没接通。"],
            "extra_info": {"下一步跟进": "2026-11-02"},
        }
    ]
    kept, _ = fu.sanitize(raw)
    assert kept[0]["next_follow_up_at"] == "2026-11-02"
    assert "下一步跟进" not in kept[0]["extra_info"]


def test_sanitize_caps_fixed_field_lengths():
    """六项固定字段按契约各自限长，超长截断而不是整条丢弃。"""
    raw = [
        {
            "node_name": "A",
            "contents": ["x"],
            "follow_up_by": "人" * 120,
            "follow_up_channel": "道" * 80,
            "related_model": "车" * 120,
            "intent_level": "级" * 80,
            "follow_up_result": "果" * 120,
            "converted_to_opportunity": "是" * 80,
            "next_follow_up_at": "日" * 120,
        }
    ]
    kept, _ = fu.sanitize(raw)
    node = kept[0]
    assert len(node["follow_up_by"]) == fu.MAX_FOLLOW_UP_BY_CHARS
    assert len(node["follow_up_channel"]) == fu.MAX_FOLLOW_UP_CHANNEL_CHARS
    assert len(node["related_model"]) == fu.MAX_RELATED_MODEL_CHARS
    assert len(node["intent_level"]) == fu.MAX_INTENT_LEVEL_CHARS
    assert len(node["follow_up_result"]) == fu.MAX_FOLLOW_UP_RESULT_CHARS
    assert len(node["converted_to_opportunity"]) == fu.MAX_CONVERTED_CHARS
    assert len(node["next_follow_up_at"]) == fu.MAX_NEXT_FOLLOW_UP_CHARS


def test_sanitize_converts_boolean_to_opportunity():
    """契约允许转商机给布尔值；出口统一成「是/否」，与记录解码后的形态一致。"""
    raw = [
        {"node_name": "A", "contents": ["x"], "converted_to_opportunity": True},
        {"node_name": "B", "contents": ["y"], "converted_to_opportunity": False},
    ]
    kept, _ = fu.sanitize(raw)
    assert kept[0]["converted_to_opportunity"] == "是"
    assert kept[1]["converted_to_opportunity"] == "否"


def test_sanitize_prefers_top_level_over_extra_info():
    """一级字段与旧别名同时出现时，一级字段说了算，别名一律从 ``extra_info`` 去掉。"""
    raw = [
        {
            "node_name": "A",
            "contents": ["x"],
            "intent_level": "A 级意向（高）",
            "extra_info": {"意向级别": "C 级意向（中）", "跟进渠道": "电话"},
        }
    ]
    kept, _ = fu.sanitize(raw)
    assert kept[0]["intent_level"] == "A 级意向（高）"
    # 意向级别被一级字段覆盖丢弃；跟进渠道没有一级值，按别名提升。
    assert kept[0]["follow_up_channel"] == "电话"
    assert kept[0]["extra_info"] == {}


def test_sanitize_promotes_channel_alias():
    """v2 契约把 ``渠道`` 也列为兼容别名，旧调用方给这个键同样能提升。"""
    raw = [{"node_name": "A", "contents": ["x"], "extra_info": {"渠道": "微信"}}]
    kept, _ = fu.sanitize(raw)
    assert kept[0]["follow_up_channel"] == "微信"
    assert kept[0]["extra_info"] == {}


def test_sanitize_drops_empty_and_placeholder_fixed_fields():
    """空串与占位串不出键——契约允许缺省，卡片上也不该出现「跟进人：无」。"""
    raw = [
        {
            "node_name": "A",
            "contents": ["x"],
            "follow_up_by": "  ",
            "related_model": "未知",
            "intent_level": "无",
        }
    ]
    kept, _ = fu.sanitize(raw)
    for key in ("follow_up_by", "related_model", "intent_level"):
        assert key not in kept[0]


# ---------------------------------------------------------------- 入口（跳过 / 兜底 / 正常）

def test_no_follow_records_short_circuits_before_any_llm_call():
    """没有这个渠道时零 token 返回——实测 245/791 案如此。"""
    import asyncio

    llm = _FakeLLM({"nodes": []})
    out = asyncio.run(fu.build(_records_json(), llm=llm))
    assert llm.calls == []
    assert out["follow_status"] == "no_record"
    assert out["follow_calls"] == 0
    assert json.loads(out["follow_up_json"]) == []


def test_llm_failure_falls_back_to_one_node_per_record():
    """调用失败仍产出可用区块：履历的价值首先在于「都在」。"""
    import asyncio

    records = _records_json(_record("一", record_id="a"), _record("二", record_id="b"))
    out = asyncio.run(fu.build(records, llm=_FakeLLM(None, fail=True)))
    nodes = json.loads(out["follow_up_json"])
    assert out["follow_status"] == "failed"
    assert out["fallback_used"] == "true"
    assert len(nodes) == 2
    assert out["follow_calls"] == 1


def test_missing_llm_also_falls_back():
    import asyncio

    out = asyncio.run(fu.build(_records_json(_record("一"))))
    assert out["follow_status"] == "no_llm"
    assert len(json.loads(out["follow_up_json"])) == 1


def test_happy_path_uses_model_names_and_writes_one_paragraph():
    import asyncio

    llm = _FakeLLM(
        {
            "nodes": [
                {
                    "records": [1],
                    "node_name": "首次电话邀约",
                    "summary": "销售顾问电话联系客户邀请试驾，客户未接听，计划次日再联系。",
                    # 旧提示词或外部调用方仍可能带回这一段；装配侧忽略。
                    "next_step": "本次仍需后续跟进，约定次日再沟通。",
                }
            ]
        }
    )
    out = asyncio.run(fu.build(_records_json(_record("电话没接通")), llm=llm))
    nodes = json.loads(out["follow_up_json"])
    assert out["follow_status"] == "ok"
    assert out["fallback_used"] == "false"
    assert nodes[0]["node_name"] == "首次电话邀约"
    # 单段：只有跟进小结，销售原文不上卡片，``next_step`` 不再出现。
    assert nodes[0]["contents"] == ["销售顾问电话联系客户邀请试驾，客户未接听，计划次日再联系。"]

    assert nodes[0]["followed_at"] == "2026-11-01T15:38:40"
    assert nodes[0]["followed_at_text"] == "2026年11月1日下午"


def test_prompt_carries_records_and_excludes_internal_ids():
    """提示词只给已解码字段：门店编码与记录 ID 这类内部标识不进输入。"""
    import asyncio

    llm = _FakeLLM({"nodes": []})
    asyncio.run(fu.build(_records_json(_record("电话没接通")), llm=llm))
    prompt = llm.calls[0]["prompt"]
    assert "电话没接通" in prompt
    assert "邀请试驾" in prompt
    assert "MSCHUB023" not in prompt


# ---------------------------------------------------------------- 渠道：模型判定 + 归一化

def _assemble_one(record: dict, node: dict) -> dict:
    nodes, _ = fu.assemble(
        fu.parse_records(_records_json(record)),
        {"nodes": [node]},
    )
    return nodes[0]


def test_model_channel_is_adopted_over_decoded_code():
    """模型给出渠道时直接采用；码值只作缺省。

    UAT ``15903771774``：正文写「微信已经联系」，沟通方式码却是「电话」。
    2026-11-25 起渠道由模型判定，代码不再按正文关键词翻转。
    """
    node = _assemble_one(
        _record("微信已经联系说找个时间过来门店看一下具体的权益政策。", method="电话"),
        {
            "records": [1],
            "node_name": "微信邀约到店",
            "channel": "微信",
            "channel_basis": "微信已经联系说找个时间过来门店",
            "summary": "销售顾问通过微信联系客户，邀请其到门店了解具体权益政策。",
        },
    )
    assert node["follow_up_channel"] == "微信"
    assert node["intent_level"] == "A 级意向（高）"
    assert "channel_basis" not in node


def test_negated_record_text_does_not_override_model_channel():
    """UAT 18620663489 回归：正文「不加微信」、模型判「电话」→ 输出电话。

    旧守卫会把码值「电话」翻转成「微信」；守卫已移除，代码不再对正文做关键词改判。
    """
    node = _assemble_one(
        _record("不加微信，周末有时间来看看，周五联系。", method="电话"),
        {
            "records": [1],
            "node_name": "电话邀约到店",
            "channel": "电话",
            "channel_basis": "不加微信，周末有时间来看看",
            "summary": "销售顾问电话联系客户邀请试驾，客户表示不加微信，周末有时间来看看。",
        },
    )
    assert node["follow_up_channel"] == "电话"


def test_missing_model_channel_falls_back_to_decoded_method():
    """模型没给渠道时，用码值解码值兜底（与 LLM 缺席时的既有行为一致）。"""
    node = _assemble_one(
        _record("电话联系客户邀请试驾。", method="电话"),
        {"records": [1], "node_name": "邀约试驾", "summary": "销售顾问电话联系客户邀请试驾。"},
    )
    assert node["follow_up_channel"] == "电话"


def test_model_channel_placeholder_falls_back_to_decoded_method():
    """模型给占位串等于没给：回落码值解码。"""
    node = _assemble_one(
        _record("电话联系客户邀请试驾。", method="电话"),
        {
            "records": [1],
            "node_name": "邀约试驾",
            "channel": "未知",
            "summary": "销售顾问电话联系客户邀请试驾。",
        },
    )
    assert node["follow_up_channel"] == "电话"


def test_model_channel_alias_is_normalized_before_use():
    """语义正确但非规范枚举：归一化后采用，不回落码值。"""
    node = _assemble_one(
        _record("微信已经联系。", method="电话"),
        {
            "records": [1],
            "node_name": "微信沟通",
            "channel": "微信沟通",
            "summary": "销售顾问微信联系客户。",
        },
    )
    assert node["follow_up_channel"] == "微信"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("微信", "微信"),
        ("微信沟通", "微信"),
        ("微信联系", "微信"),
        ("微信语音", "微信"),
        ("电话", "电话"),
        ("电话联系", "电话"),
        ("致电", "电话"),
        ("外呼", "电话"),
        ("上门", "上门"),
        ("上门拜访", "上门"),
        ("到店", "店内沟通"),
        ("到店沟通", "店内沟通"),
        ("门店沟通", "店内沟通"),
        ("店内沟通", "店内沟通"),
        ("其他", "其他"),
        # 拿不准的一律原样保留：不猜、不丢、不改判
        ("电话和微信沟通", "电话和微信沟通"),
        ("邀约到店", "邀约到店"),
        ("未加微信", "未加微信"),
        ("本想加微信", "本想加微信"),
        ("下次再加微信", "下次再加微信"),
        ("客户说微信不方便", "客户说微信不方便"),
        ("短信沟通", "短信沟通"),
        ("", ""),
    ],
)
def test_normalize_channel_aliases(raw: str, expected: str):
    assert fu.normalize_channel(raw) == expected


def test_normalize_channel_is_idempotent():
    for raw in ("微信沟通", "致电", "到店沟通", "邀约到店", "未加微信", "其他"):
        once = fu.normalize_channel(raw)
        assert fu.normalize_channel(once) == once


def test_normalize_channel_respects_contract_length():
    raw = "短信" + "说明" * 40
    out = fu.normalize_channel(raw)
    assert len(out) <= fu.MAX_FOLLOW_UP_CHANNEL_CHARS
    assert out.startswith("短信")


def test_build_records_channel_trace_for_audit():
    """模型原值、归一化值、最终值与依据进留痕，便于抽检。"""
    import asyncio

    payload = json.dumps(
        {
            "nodes": [
                {
                    "records": [1],
                    "node_name": "微信沟通",
                    "channel": "微信沟通",
                    "channel_basis": "微信已经联系",
                    "summary": "销售顾问微信联系客户。",
                }
            ]
        },
        ensure_ascii=False,
    )
    llm = _FakeLLM(payload)
    out = asyncio.run(fu.build(_records_json(_record("微信已经联系。")), llm=llm))
    trace = json.loads(out["channel_trace"])
    assert trace[0]["model_channel"] == "微信沟通"
    assert trace[0]["normalized_channel"] == "微信"
    assert trace[0]["final_channel"] == "微信"
    assert trace[0]["basis"] == "微信已经联系"


def test_prompt_asks_model_for_channel_and_basis():
    """提示词必须给出渠道字段、语义口径与反例。"""
    import asyncio

    llm = _FakeLLM({"nodes": []})
    asyncio.run(fu.build(_records_json(_record("电话没接通")), llm=llm))
    prompt = llm.calls[0]["prompt"]
    assert '"channel"' in prompt
    assert "channel_basis" in prompt
    assert "不加微信" in prompt
    assert "秒挂" in prompt

