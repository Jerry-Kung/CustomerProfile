"""客户跟进履历：把「销售跟进记录」整理成回传体 ``follow_up_history``。

业务契约见 ``docs/reference/AI画像回传接口变更说明-客户跟进履历.md``，方案与本仓库的
取舍见 ``docs/feature-followup-record/方案设计.md``。这里只记**代码为什么长这样**。

## 为什么单独一次 LLM 调用（而不是塞进 L1）

跟进履历服务的是「销售交接/续跟时看懂这个客户被跟到哪一步」，与客户自身人设相关性弱。
把它混进 L1 的字段清单，会让一次只拿得到十几个字的证据位去承担「时间线叙事」，
两头都做不好。单独一条旁支、独占一次调用、只吃跟进渠道，边界最清楚。

## 为什么是 code 节点而不是 llm 节点

调度器对失败节点会阻塞整棵下游子树（``scheduler._block_descendants``）。做成
``llm`` 节点，一次调用失败就挡掉回传，把「履历没生成」升级成「整案失败」。因此本模块
以 ``code`` 节点落地、内部调统一 LLM 层并吞掉全部异常，与 ``rules.checker`` /
L2 兜底裁决同一骨架。

## 分工：代码管结构，模型管措辞

契约写明的硬约束（``id``/``sequence`` 唯一、节点数上限、各字段长度、``followed_at``
格式）**错一条就是整次回写 400 回滚**——连带已算好的定级、Notes 与九个板块一起作废。
这些判据全部可机器验证，因此一律留在代码里；模型负责所有要读懂人话才能做的事：
给节点起名、把跟进过程写成小结、写清这次跟进的下一步。

**模型不输出 ``extra_info``**：渠道、参与人、车型、跟进结果、意向级别、下一步全都能从
记录字段确定性取到，交给模型只会引入「把码值抄错」这一种新失败模式。（``next_step``
是**叙述**而非字段值：它要把结论与约定时间讲成一句连贯的话，这是模型的事；取值仍以
记录字段为准，提示词已就此立规矩。）

## ``contents``：跟进小结 + 后续安排，两段都由模型写

``_node`` 里的 ``contents`` 是 ``[summary, next_step]``，销售手写的原文不进 ``contents``。

**为什么原文不是第一段**：原文是取证凭据，小结是交接读物，两者服务的人不同。卡片的使用者是
接手的销售，而销售手写的原文大量是「电话没人接」「白天联系。。。。。。」这类碎片，与模型
写的陈述并列时只是把同一件事说两遍。这一版取交接读物。原文只在模型对这一组什么都没写时
兜底出现（见 :func:`_node`）——所以「原文一条不少」不再是本模块的约束，别按旧口径读装配。

这项取舍的过程与数据见 ``docs/changelog.md`` 与
``docs/history/feature-followup-record/跟进履历内容重复问题-定位与处置.md``。

## 无数据不出键

``follow_up_json`` 空数组表示「这次没有可写的履历」。回传体构造侧据此**不出这个键**，
绝不把空数组发给接口——契约里 ``[]`` 是「清空该客户全部履历」，而我们只是没取到。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

TEMPLATE_NAME = "customer_profile_pipeline__pipeline_follow_up"
"""模板资源名。与管线其它节点同一约定：``<slug>__<node_id>``。"""

SYSTEM_TEXT = "You are a helpful AI assistant."

FOLLOW_CHANNEL = "OntologyLeadFollowRecord"
"""跟进渠道在 L0 记录层里的 ``channel`` 键。数据源只有这一个。"""

FOLLOW_SOURCE_NAME = "销售跟进记录"
"""渠道中文名，与 ``CHANNELS`` 表一致。按 ``channel`` 匹配不到时的兜底判据。"""

MAX_NODES = 100
"""契约 §3.3：单客户最多 100 个节点。"""

MAX_CONTENTS = 20
"""契约 §3.3：``contents`` 最多 20 条。当前两段（跟进小结 + 后续安排），远低于上限。"""

MAX_CONTENT_CHARS = 1000
MAX_NODE_NAME_CHARS = 100
MAX_EXTRA_BYTES = 64 * 1024
"""契约 §3.3 的字段上限，逐条对应，改这里之前先回看契约。"""

MIN_COMPLETION_TOKENS = 30
"""输出量下限。与 L2 兜底同思路：非空输入却只回几 token，等于模型没干活。

取值保守：一条节点正常也要三十多个 token，这里只挡「明显空转」，不参与质量判断。"""

PLACEHOLDER_VALUES = frozenset(
    {"", "-", "--", "无", "未知", "暂无", "null", "None", "undefined"}
)
"""渲染层可能给出的占位串。进 ``extra_info`` 会让卡片出现「跟进渠道：未知」这类行。"""

_BARE_CODE = re.compile(r"^\d{4,8}$")
"""未登记的字典码（渲染层原样输出）。它不是业务事实，不进对客产物。"""

_DATE_PATTERN = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?$"
)

# 记录正文的字段名 → 内部键。正文由 ``ontology_payload.render_record`` 生成，
# 格式是「中文名：值」逐行；这里是**读回自己写的格式**，不是解析第三方协议。
_FIELD_MAP: Mapping[str, str] = {
    "跟进创建时间": "created_at",
    "跟进内容": "content",
    "跟进目的": "purpose",
    "跟进结果": "result",
    "沟通方式": "method",
    "意向级别": "level",
    "意向车型名称": "vehicle",
    "跟进顾问姓名": "consultant",
    "跟进门店编码": "store",
    "下次跟进时间": "next_follow",
    "是否转商机": "to_business",
    "战败类型": "defeat_type",
    "到店时间": "arrive_time",
    "跟进记录 ID": "record_id",
}

_PROMPT_FIELDS: tuple[tuple[str, str], ...] = (
    ("时间", "created_at"),
    ("沟通方式", "method"),
    ("跟进目的", "purpose"),
    ("跟进结果", "result"),
    ("意向级别", "level"),
    ("意向车型", "vehicle"),
    ("跟进顾问", "consultant"),
    ("下次跟进", "next_follow"),
    ("转商机", "to_business"),
    ("战败类型", "defeat_type"),
    ("到店时间", "arrive_time"),
    ("跟进内容", "content"),
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _payload(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None
    return None


def _records(value: Any) -> list[dict]:
    data = _payload(value)
    if isinstance(data, dict):
        data = data.get("records")
    if not isinstance(data, (list, tuple)):
        return []
    return [item for item in data if isinstance(item, dict)]


def _usable(value: str) -> bool:
    """取值能不能进对客产物：占位串与未解码的裸字典码都不行。"""
    text = _text(value)
    if text in PLACEHOLDER_VALUES:
        return False
    return not _BARE_CODE.match(text)


def _literal(value: Any) -> str:
    """占位串与裸码统一折成空串——调用方按「没有这个信息」处理。"""
    text = _text(value)
    return text if _usable(text) else ""


def _paragraph(value: Any) -> list[str]:
    """模型给出的一个「段」（``summary`` / ``next_step``）折成非空字符串列表。

    正常情况下模型给的是一个字符串；给成数组、或者多给几段，都接受并保留——
    只要内容非空，装配侧照收，不因为形态不合预期就整段丢掉。
    """
    if isinstance(value, (list, tuple)):
        items: Iterable[Any] = value
    else:
        items = [value]
    return [text for text in (_text(item) for item in items) if text]


# ---------------------------------------------------------------- 解析


def parse_record_lines(content: Any) -> dict[str, str]:
    """把一条记录的渲染正文解析回字段表。

    只认「中文名：值」的行；不认识的行丢弃。用**第一个**冒号切分：
    渲染层写的是 ``字段名：值``，值本身可能含冒号。
    """
    out: dict[str, str] = {}
    for line in str(content or "").splitlines():
        name, sep, raw = line.partition("：")
        if not sep:
            continue
        key = _FIELD_MAP.get(name.strip())
        if key is None:
            continue
        value = raw.strip()
        if value:
            out[key] = value
    return out


def parse_records(records_json: Any) -> list[dict[str, str]]:
    """从 L0 记录层取出跟进记录，按**业务时刻**升序排好。

    用「跟进创建时间」（销售实际操作的那一刻）而不是 L0 的 ``created_at``（落库时刻）：
    实测两者相差几分钟到几十分钟，卡片上的时间线应以业务发生为准。

    同一时刻的多条（实测 15 案）按记录 ID 兜底排序，保证每次运行顺序一致——
    全量替换语义下，顺序抖动会让「其实没变的客户」在卡片上看起来变了。
    """
    parsed: list[dict[str, str]] = []
    for record in _records(records_json):
        channel = _text(record.get("channel"))
        source_name = _text(record.get("source_name"))
        if channel != FOLLOW_CHANNEL and source_name != FOLLOW_SOURCE_NAME:
            continue
        if _text(record.get("status")) != "ok":
            continue
        fields = parse_record_lines(record.get("content"))
        if not _text(fields.get("content")) and not _text(fields.get("created_at")):
            # 两个关键字段都没有，说明这条记录没有可用信息；不拿它凑节点。
            continue
        fields["source_key"] = _text(record.get("source_key"))
        parsed.append(fields)

    parsed.sort(key=lambda item: (_text(item.get("created_at")), _text(item.get("record_id"))))
    return parsed


# ---------------------------------------------------------------- 提示词输入


def render_input(records: Sequence[Mapping[str, str]]) -> str:
    """把解析后的记录渲染成给模型看的编号清单。

    只给**已解码**的字段（渲染层已把字典码译成中文），不给记录 ID 与门店编码这类
    内部标识——模型不需要它们，而它们一旦被抄进 ``contents`` 就是内部信息外泄。
    """
    blocks: list[str] = []
    for index, item in enumerate(records, start=1):
        lines = [f"[第 {index} 条]"]
        for label, key in _PROMPT_FIELDS:
            value = _text(item.get(key))
            if value:
                lines.append(f"{label}：{value}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- 时间的归一


def parse_time(value: Any) -> str:
    """把记录里的时间串归一成契约接受的形态；解析不了给空串。

    **只输出能确定的时间**：契约里格式非法会 400 并让整次回写回滚，所以宁可让这一条
    没有时间（``followed_at`` 本就允许缺省），也不能把原串透传出去。
    """
    text = _text(value)
    match = _DATE_PATTERN.match(text)
    if not match:
        return ""
    year, month, day, hour, minute, second = match.groups()
    try:
        stamp = datetime(
            int(year), int(month), int(day),
            int(hour or 0), int(minute or 0), int(second or 0),
        )
    except ValueError:
        return ""
    # 输出形态跟输入形态走：只有日期的就只回日期，不凭空补出零点时间。
    if hour is None:
        return stamp.strftime("%Y-%m-%d")
    return stamp.strftime("%Y-%m-%dT%H:%M:%S")


def readable_time(value: str) -> str:
    """给销售的模糊时间：「2026年11月1日下午」。解析不了给空串。"""
    text = _text(value)
    match = _DATE_PATTERN.match(text)
    if not match:
        return ""
    year, month, day, hour = match.groups()[:4]
    try:
        stamp = datetime(int(year), int(month), int(day), int(hour or 0))
    except ValueError:
        return ""
    head = f"{stamp.year}年{stamp.month}月{stamp.day}日"
    if hour is None:
        return head
    if stamp.hour < 6:
        return f"{head}凌晨"
    if stamp.hour < 12:
        return f"{head}上午"
    if stamp.hour < 18:
        return f"{head}下午"
    return f"{head}晚上"


# ---------------------------------------------------------------- 兜底命名


def fallback_name(item: Mapping[str, str]) -> str:
    """模型没给出节点名时的兜底：用「沟通方式 + 跟进目的」拼。

    两者都缺时用「客户跟进」，不编造动作——编出来的动作在交接时比空白更危险。
    """
    method = _literal(item.get("method"))
    purpose = _literal(item.get("purpose"))
    if method and purpose:
        return f"{method}沟通·{purpose}"
    if purpose:
        return purpose
    if method:
        return f"{method}沟通"
    return "客户跟进"


def fallback_extra(item: Mapping[str, str]) -> dict[str, Any]:
    """从记录字段确定性取 ``extra_info``。取不到的键不出现，不留空串。"""
    extra: dict[str, Any] = {}
    method = _literal(item.get("method"))
    if method:
        extra["跟进渠道"] = method
    consultant = _literal(item.get("consultant"))
    if consultant:
        extra["参与人"] = [f"销售顾问{consultant}", "客户本人"]
    vehicle = _literal(item.get("vehicle"))
    if vehicle:
        extra["关联车型"] = vehicle
    result = _literal(item.get("result"))
    if result:
        extra["跟进结果"] = result
    level = _literal(item.get("level"))
    if level:
        extra["意向级别"] = level
    to_business = _literal(item.get("to_business"))
    if to_business:
        extra["是否转商机"] = to_business
    defeat = _literal(item.get("defeat_type"))
    if defeat:
        extra["战败类型"] = defeat
    next_follow = parse_time(item.get("next_follow"))
    if next_follow:
        # 下次跟进时间大量落在零点（上游按天填），给日期就够了。
        extra["下一步跟进"] = next_follow[:10] if next_follow.endswith("T00:00:00") else next_follow
    return extra


# ---------------------------------------------------------------- 清洗


def _clean_contents(items: Iterable[Any]) -> list[str]:
    """``contents`` 清洗：去空、去重、限长、限条数。

    模型写的两段都走这里——折叠空白是对的，模型偶发换行与多余空格。
    销售原文只在兜底时才进 ``contents``，那条路径走 :func:`_clean_verbatim`。
    """
    out: list[str] = []
    for raw in items:
        text = re.sub(r"\s+", " ", _text(raw)).strip()
        if not text or text in out:
            continue
        out.append(text[:MAX_CONTENT_CHARS])
        if len(out) >= MAX_CONTENTS:
            break
    return out


def _clean_verbatim(items: Iterable[Any]) -> list[str]:
    """销售原文的清洗：只去首尾空白，**不折叠内部空白**。

    原文已不进 ``contents``（见模块文档「跟进小结 + 后续安排」）。只剩两处用它：
    兜底路径（模型没给出内容时退回原文）与 :func:`sanitize` 对成品的复核。
    实测有销售在内容里连打五个空格（``女士关注了x700   目前…``），兜底展示时也不替他改。
    """
    out: list[str] = []
    for raw in items:
        text = _text(raw)
        if not text or text in out:
            continue
        out.append(text[:MAX_CONTENT_CHARS])
        if len(out) >= MAX_CONTENTS:
            break
    return out


# ---------------------------------------------------------------- 模型输出


def model_groups(model_output: Any, total: int) -> list[dict[str, Any]]:
    """校验模型给的分组：索引必须存在、不得重复、组内不得为空。

    返回元素的 ``indexes`` 是 0 基、已排序且互斥。模型多给、给错的部分直接丢掉，
    少给的部分由代码补节点——**解析失败不报错**，这条链路不允许因为模型说话不规矩
    就整体失败。
    """
    data = _payload(model_output)
    if isinstance(data, dict):
        data = data.get("nodes")
    if not isinstance(data, (list, tuple)):
        return []

    seen: set[int] = set()
    groups: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        raw_indexes = entry.get("records")
        if not isinstance(raw_indexes, (list, tuple)):
            raw_indexes = [raw_indexes]
        indexes: list[int] = []
        for raw in raw_indexes:
            try:
                position = int(raw) - 1
            except (TypeError, ValueError):
                continue
            if position < 0 or position >= total or position in seen:
                continue
            seen.add(position)
            indexes.append(position)
        if not indexes:
            continue
        groups.append(
            {
                "indexes": sorted(indexes),
                "name": _text(entry.get("node_name")),
                "summary": _paragraph(entry.get("summary")),
                "next_step": _paragraph(entry.get("next_step")),
            }
        )
    return groups


# ---------------------------------------------------------------- 装配


def _node(
    members: Sequence[Mapping[str, str]],
    *,
    name: str,
    summary: Iterable[Any],
    next_step: Iterable[Any],
    position: int,
) -> dict[str, Any]:
    """构造一个契约形状的节点。

    ``contents`` 是模型写的两段：**跟进小结** 与 **后续安排**。销售原文不进 ``contents``
    （原因见模块文档）。

    唯一的原文返场：模型对这一组什么都没写时，把组内原文逐字放进来兜底。履历的价值首先
    在于「这一段不能空」——宁可让读者看到一句粗糙的原文，也不要一片空白。
    """
    written = _clean_contents([*summary, *next_step])
    if written:
        contents = written
    else:
        # 兜底走逐字清洗：这里展示的就是销售写下的话，内部空白也不替他改。
        contents = _clean_verbatim(_text(item.get("content")) for item in members)
    if not contents:
        # 契约要求 ``contents`` 是数组；给一条说明，而不是让卡片上的节点空着。
        contents = ["销售留下了跟进记录，但没有填写具体内容"]

    head = members[0]
    node: dict[str, Any] = {
        "id": f"fu-{position}",
        "sequence": position,
        "node_name": (name or fallback_name(head))[:MAX_NODE_NAME_CHARS],
        "contents": contents,
    }
    followed_at = parse_time(head.get("created_at"))
    if followed_at:
        node["followed_at"] = followed_at
        text = readable_time(followed_at)
        if text:
            node["followed_at_text"] = text
    # ``extra_info`` 取组内**最新**那条记录：一个节点合并了多次跟进时，卡片下方标签要反映
    # 这组跟进的最终状态（结果、意向级别、下次跟进时间），否则读者会看到正文写「A 级、
    # 11 月 3 日」而标签写「C 级、11 月 1 日」。时间轴位置仍用首条（``followed_at`` 不动）。
    tail = members[-1]
    extra = fallback_extra(tail)
    # 参与人是个例外：合并节点里每个顾问都参与过，只留最后一位会把前面的人抹掉。
    consultants = list(
        dict.fromkeys(
            value
            for value in (_literal(item.get("consultant")) for item in members)
            if value
        )
    )
    if len(consultants) > 1:
        extra["参与人"] = [f"销售顾问{person}" for person in consultants] + ["客户本人"]
    if extra:
        node["extra_info"] = extra
    return node


def assemble(
    records: Sequence[Mapping[str, str]], model_output: Any
) -> tuple[list[dict[str, Any]], list[int]]:
    """把模型的分组与原始记录装成契约节点列表。

    模型覆盖不到或压根没返回时，代码按记录 1:1 补节点——**不丢记录**是这条产线的
    底线（履历的价值首先在于「都在」）。补出来的节点回到时间序里的正确位置。

    返回 ``(nodes, supplemented)``：``supplemented`` 是被代码补成节点的记录下标（0 基），
    留痕用。
    """
    total = len(records)
    groups = model_groups(model_output, total)
    covered = {index for group in groups for index in group["indexes"]}

    slots: list[tuple[str, int, list[Mapping[str, str]], str, list[str], list[str]]] = []
    for group in groups:
        indexes = group["indexes"]
        members = [records[index] for index in indexes]
        slots.append(
            (
                _text(members[0].get("created_at")),
                indexes[0],
                members,
                group["name"],
                group["summary"],
                group["next_step"],
            )
        )

    supplemented: list[int] = []
    for index, item in enumerate(records):
        if index in covered:
            continue
        supplemented.append(index)
        # 代码补出来的节点没有模型措辞，``_node`` 会退回组内原文。
        slots.append((_text(item.get("created_at")), index, [item], "", [], []))

    # 跨组补充的记录按「本条记录的创建时间」定位，与模型分组共用同一时间轴；
    # 同一时刻再比记录下标，保证顺序稳定。
    slots.sort(key=lambda slot: (slot[0], slot[1]))

    nodes: list[dict[str, Any]] = []
    for position, (_, _, members, name, summary, next_step) in enumerate(
        slots[:MAX_NODES], start=1
    ):
        nodes.append(
            _node(
                members,
                name=name,
                summary=summary,
                next_step=next_step,
                position=position,
            )
        )
    return nodes, supplemented


# ---------------------------------------------------------------- 契约自检


def _extra_ok(extra: Any) -> bool:
    if not isinstance(extra, dict):
        return False
    try:
        encoded = json.dumps(extra, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return len(encoded.encode("utf-8")) <= MAX_EXTRA_BYTES


def sanitize(nodes: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """回写前的最后一道闸：把不合契约的节点挡在本地。

    契约写明校验失败返回 400、**整次回写回滚**——一条坏的 ``followed_at`` 会让这个客户
    的定级、Notes 与九个板块一起作废。因此这里的取舍一律是「去掉这一个字段/这一条」，
    而不是「照原样发出去赌接口接受」。

    ``id`` / ``sequence`` 在这里**重新发号**：唯一性是契约硬约束（重复即 400），
    而编号本就不该由模型或兜底逻辑决定。

    返回 ``(kept, dropped)``；``dropped`` 逐条记原因，进节点留痕。
    """
    if isinstance(nodes, str):
        nodes = _payload(nodes)
    if not isinstance(nodes, (list, tuple)):
        return [], [{"index": 0, "reason": "不是数组"}]

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []

    for raw_index, entry in enumerate(nodes):
        if len(kept) >= MAX_NODES:
            dropped.append({"index": raw_index, "reason": "超出节点上限"})
            continue
        if not isinstance(entry, dict):
            dropped.append({"index": raw_index, "reason": "不是对象"})
            continue

        name = re.sub(r"\s+", " ", _text(entry.get("node_name"))).strip()
        if not name:
            dropped.append({"index": raw_index, "reason": "缺节点名称"})
            continue

        contents = _clean_verbatim(entry.get("contents") or [])
        if not contents:
            dropped.append({"index": raw_index, "reason": "内容为空"})
            continue

        node: dict[str, Any] = {
            "id": "",
            "sequence": 0,
            "node_name": name[:MAX_NODE_NAME_CHARS],
            "contents": contents,
        }

        followed_at = _text(entry.get("followed_at"))
        if followed_at:
            normalized = parse_time(followed_at)
            if normalized:
                node["followed_at"] = normalized
            else:
                # 时间坏了只丢这个字段：契约允许缺省，节点本身仍然有价值。
                dropped.append(
                    {"index": raw_index, "reason": f"时间格式非法：{followed_at[:32]}"}
                )

        text = _text(entry.get("followed_at_text"))
        if text:
            node["followed_at_text"] = text[:MAX_NODE_NAME_CHARS]

        extra = entry.get("extra_info")
        if isinstance(extra, Mapping) and _extra_ok(dict(extra)):
            node["extra_info"] = dict(extra)

        kept.append(node)

    for position, node in enumerate(kept, start=1):
        node["id"] = f"fu-{position}"
        node["sequence"] = position

    return kept, dropped


# ---------------------------------------------------------------- 入口


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _out(
    nodes: Sequence[dict[str, Any]],
    *,
    status: str,
    calls: int,
    error: str,
    record_count: int,
    supplemented: Sequence[int],
    dropped: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "follow_up_json": _dump(list(nodes)),
        "follow_status": status,
        "follow_calls": calls,
        "follow_error": error,
        "record_count": record_count,
        "node_count": len(nodes),
        # 被代码补成节点的记录（1 基，逗号分隔）。空串＝模型全覆盖。
        "supplemented": ",".join(str(index + 1) for index in sorted(set(supplemented))),
        # 模型没干活或部分没干，都算用过兜底；留痕是为了让质量排查能定位到调用。
        # **没有记录时不叫兜底**：那条路径压根没有可兜的东西。
        "fallback_used": "true" if (record_count and (status != "ok" or supplemented)) else "false",
        # 自检丢掉了什么，直接进节点出参，排查时不必回看日志。
        "sanitize_dropped": _dump(list(dropped)) if dropped else "",
    }


async def build(
    records_json: Any = "",
    llm: Any = None,
    templates: Any = None,
    template_name: str = "",
    node_ref: Any = None,
) -> dict[str, Any]:
    """跟进履历节点本体：一次 LLM 调用，失败兜底，永远返回可用产物。

    三件事按顺序发生（与 L2 兜底同一骨架）：

    1. **没有跟进记录 → 在发起调用之前返回**。归档 791 案里 245 案没有这个渠道，
       对它们发起调用只会换来一份空结论，还白烧一次钱。
    2. **调用失败不抛**：按记录 1:1 出节点，状态如实记 ``failed``。履历的价值首先
       在于「都在」，读着糙好过让销售看到空白。
    3. **自检在装配之后、出口之前**：契约一错就整次回写回滚，这道闸不能省。
    """
    records = parse_records(records_json)
    if not records:
        return _out([], status="no_record", calls=0, error="", record_count=0,
                    supplemented=[], dropped=[])

    status = "ok"
    error = ""
    calls = 0
    output: Any = None

    if llm is None:
        status = "no_llm"
        error = "未注入 LLM 客户端"
    else:
        from .templates import TemplateRepository

        repository = templates or TemplateRepository()
        prompt = repository.render(template_name or TEMPLATE_NAME, {"arg1": render_input(records)})
        try:
            output = await llm.llm_call(
                prompt,
                system=SYSTEM_TEXT,
                expect_json=True,
                min_completion_tokens=MIN_COMPLETION_TOKENS,
                node_ref=node_ref,
            )
            calls = 1
        except Exception as exc:  # noqa: BLE001 —— 失败不阻断是本节点的定义
            status = "failed"
            error = str(exc)[:200]
            calls = 1

    nodes, supplemented = assemble(records, output)
    kept, dropped = sanitize(nodes)
    if not kept:
        return _out([], status="empty_after_sanitize", calls=calls,
                    error=error or "全部节点未通过契约自检", record_count=len(records),
                    supplemented=supplemented, dropped=dropped)

    return _out(kept, status=status, calls=calls, error=error,
                record_count=len(records), supplemented=supplemented, dropped=dropped)


__all__ = [
    "FOLLOW_CHANNEL",
    "MAX_CONTENTS",
    "MAX_NODES",
    "TEMPLATE_NAME",
    "assemble",
    "build",
    "fallback_extra",
    "fallback_name",
    "model_groups",
    "parse_record_lines",
    "parse_records",
    "parse_time",
    "readable_time",
    "render_input",
    "sanitize",
]
