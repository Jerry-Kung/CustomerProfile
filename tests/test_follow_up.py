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


def test_extra_info_drops_placeholders_and_bare_codes():
    """占位串与未解码的裸字典码不进对客产物（docs/README.md 的对客产出红线）。"""
    extra = fu.fallback_extra(
        {
            "method": "未知",
            "consultant": "无",
            "vehicle": "90463",  # 未登记码会原样漏出
            "result": "仍需后续跟进",
            "level": "-",
        }
    )
    assert "跟进渠道" not in extra
    assert "参与人" not in extra
    assert "关联车型" not in extra
    assert "意向级别" not in extra
    assert extra["跟进结果"] == "仍需后续跟进"


def test_extra_info_carries_next_step_as_date_when_midnight():
    """下次跟进时间大量落在零点（上游按天填），给日期就够了。"""
    item = {"next_follow": "2026-11-02 00:00:00"}
    assert fu.fallback_extra(item)["下一步跟进"] == "2026-11-02"
    item = {"next_follow": "2026-11-02 15:30:00"}
    assert fu.fallback_extra(item)["下一步跟进"] == "2026-11-02T15:30:00"


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
    model = {"nodes": [{"records": [1, 2], "node_name": "连续两次邀约", "readable": ["两次都没接通。"]}]}
    nodes, supplemented = fu.assemble(records, model)
    assert supplemented == [2]
    assert len(nodes) == 2
    assert nodes[0]["node_name"] == "连续两次邀约"
    # 组内两条原文都在（两段式的首段）。
    assert nodes[0]["contents"][:2] == ["一", "二"]
    # 第三条回到时间序末尾，而不是被挤到最前。
    assert nodes[1]["contents"] == ["三"]


def test_assemble_keeps_every_raw_line_from_a_group():
    """一句可读陈述**不能**把组内多条原文吃掉——这是最容易溜过去的退化路径。"""
    records = fu.parse_records(
        _records_json(
            _record("电话没接通", record_id="a"),
            _record("用户忙无法接通", record_id="b"),
            _record("通话转入语音留言", record_id="c"),
        )
    )
    model = {"nodes": [{"records": [1, 2, 3], "node_name": "电话跟进", "readable": ["多次联系未接通。"]}]}
    nodes, _ = fu.assemble(records, model)
    assert nodes[0]["contents"] == ["电话没接通", "用户忙无法接通", "通话转入语音留言", "多次联系未接通。"]


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
    """首段是**销售原文**，连空白都不改。

    实测有销售在跟进内容里连打多个空格（「女士关注了x700   目前在展会上…」）。
    把它折叠成单空格更好看，但那就不是他写下的话了——而这段原文是交接时的凭据
    （``方案设计.md`` §7 第 3 条「逐字保留」）。
    """
    raw = "女士关注了x700   目前在展会上表示下周可以到店看车 请跟进 "
    records = fu.parse_records(_records_json(_record(raw)))
    nodes, _ = fu.assemble(records, None)
    assert nodes[0]["contents"][0] == raw.strip()


def test_model_readable_text_is_whitespace_collapsed():
    """次段（模型陈述）反而要折叠空白：那里是模型偶发的换行与多余空格。"""
    records = fu.parse_records(_records_json(_record("原文")))
    model = {"nodes": [{"records": [1], "node_name": "A", "readable": ["第一句。\n\n   第二句。"]}]}
    nodes, _ = fu.assemble(records, model)
    assert nodes[0]["contents"][1] == "第一句。 第二句。"


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


def test_happy_path_uses_model_names_and_keeps_raw_text():
    import asyncio

    llm = _FakeLLM(
        {
            "nodes": [
                {
                    "records": [1],
                    "node_name": "首次电话邀约",
                    "readable": ["销售顾问电话联系客户，客户未接听，约定次日再沟通。"],
                }
            ]
        }
    )
    out = asyncio.run(fu.build(_records_json(_record("电话没接通")), llm=llm))
    nodes = json.loads(out["follow_up_json"])
    assert out["follow_status"] == "ok"
    assert out["fallback_used"] == "false"
    assert nodes[0]["node_name"] == "首次电话邀约"
    # 两段式：原文在，模型的可读陈述在。
    assert nodes[0]["contents"][0] == "电话没接通"
    assert "未接听" in nodes[0]["contents"][1]
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
