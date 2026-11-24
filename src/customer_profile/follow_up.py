"""客户跟进履历：把「销售跟进记录」整理成回传体 ``follow_up_history``。

业务契约见 ``docs/reference/AI画像回传接口变更说明-客户跟进履历-1124-v2.md``；本仓库
这一版升级的取舍见 ``docs/history/feature-followup-record/跟进履历固定字段升级-方案.md``。
这里只记**代码为什么长这样**。

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
给节点起名、把跟进过程写成一段小结，记录里有明确的下一步时并进小结最后一句。

**模型不输出 ``extra_info``**：渠道、车型、跟进结果、意向级别、战败类型全都能从
记录字段确定性取到，交给模型只会引入「把码值抄错」这一种新失败模式。

## 七项固定属性是一级字段（1124-v2 版契约）

跟进人、跟进渠道、关联车型、意向级别、跟进结果、转商机、下次跟进时间在 1124-v2 版
契约里从 ``extra_info`` 提升为节点一级字段。它们仍然**由代码从记录字段确定性取值**
（理由同上），``extra_info`` 只留开放标签（战败类型）。服务端提取固定字段后会从
``extra_info`` 去重，我们这边先不重复，等于同一事实只发一遍。

**「参与人」已移除**（2026-11-24，业务方要求）：它是历史版本自加的编外标签，契约从未
有此键，下游也没有消费方。装配侧不再产出，:func:`promote_fixed_fields` 同时剥离旧批次
与外部调用方节点里的残留。

:func:`sanitize` 同时承担旧形状的**提升**：历史产物与外部调用方给的节点里，七项可能
还在 ``extra_info``（别名见 :data:`_FIXED_FIELDS`），自检时提到一级并从 ``extra_info``
删掉。我们旧版把下次跟进写成 ``下一步跟进``——它不在服务端的别名表里，只有这里的
提升认得，回放旧批次时靠它保住这个值。

## 渠道与车系的两处取值口径（2026-11-24）

**渠道冲突以正文为准**：``communicationmethods`` 大面积错填 90441（电话）而正文写微信
（11-02 批次 972 条里冲突 64 条）。码值与这条记录的正文冲突、正文只提到一个渠道时，
取正文渠道；正文提到多个渠道（「电话没接，又发了微信」）或没有渠道词时仍不出这个
标签。兜底后的值再受节点级守卫约束，与模型写的小结矛盾时同样丢弃。

**``related_model`` 带车系**：跟进记录只有版本名（Ultra / PRO），车系（X700 / M817）
在 ``OntologyLeadInfo``。同一客户的线索里版本名能唯一落到车系时拼成「车系 版本」
（如 ``M817 PRO``）；多义时只留版本名；记录没填版本、客户只有一个车系时只出车系。
映射必须按客户做——``Ultra`` 在全批次里既属 X700 也属 M817，全局表必错。

## ``contents``：只有一段跟进小结，由模型写

``_node`` 里的 ``contents`` 是 ``[summary]``，销售手写的原文不进 ``contents``。

**``next_step``（后续安排）段为什么删除**（2026-11-24）：固定属性提为一级字段后，
第二段实测 61/61 个节点都在复述「本次仍需后续跟进，意向级别为 X，约定下次跟进时间为 X」
——每个成分都能在字段里原样找到，净增信息为零。它还常与小结写的沟通内容互相打架
（客户已提车、字段仍填「仍需后续跟进」，两段并排出现）。结论、等级、转商机、时间由
字段标签承担；记录里有明确的下一步时，提示词要求写进小结的最后一句。模型若仍返回
``next_step``（旧提示词、外部调用方），装配侧整段丢弃。

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

LEAD_CHANNEL = "OntologyLeadInfo"
"""车系所在的线索渠道。

跟进记录只有**车型版本**（``vehiclemodelname``：Ultra / Max+ / PRO…），车系
（X700 / M817…）只在 ``OntologyLeadInfo.vehicle_series_name``。业务方要求
``related_model`` 带车系，只能从同客户线索里取。
"""

FOLLOW_SOURCE_NAME = "销售跟进记录"
"""渠道中文名，与 ``CHANNELS`` 表一致。按 ``channel`` 匹配不到时的兜底判据。"""

MAX_NODES = 100
"""契约 §3.3：单客户最多 100 个节点。"""

MAX_CONTENTS = 20
"""契约 §3.3：``contents`` 最多 20 条。当前只有一段跟进小结，远低于上限。"""

MAX_CONTENT_CHARS = 1000
MAX_NODE_NAME_CHARS = 100
MAX_EXTRA_BYTES = 64 * 1024
"""契约 §3.3 的字段上限，逐条对应，改这里之前先回看契约。"""

MAX_FOLLOW_UP_BY_CHARS = 100
MAX_FOLLOW_UP_CHANNEL_CHARS = 50
MAX_RELATED_MODEL_CHARS = 100
MAX_INTENT_LEVEL_CHARS = 50
MAX_FOLLOW_UP_RESULT_CHARS = 100
MAX_CONVERTED_CHARS = 50
MAX_NEXT_FOLLOW_UP_CHARS = 100
"""1124-v2 版契约 §3.2 七项固定属性的上限，逐条对应。"""

MIN_COMPLETION_TOKENS = 30
"""输出量下限。与 L2 兜底同思路：非空输入却只回几 token，等于模型没干活。

取值保守：一条节点正常也要三十多个 token，这里只挡「明显空转」，不参与质量判断。"""

PLACEHOLDER_VALUES = frozenset(
    {"", "-", "--", "无", "未知", "暂无", "null", "None", "undefined"}
)
"""渲染层可能给出的占位串。进 ``extra_info`` 会让卡片出现「跟进渠道：未知」这类行。"""

_BARE_CODE = re.compile(r"^\d{4,8}$")
"""未登记的字典码（渲染层原样输出）。它不是业务事实，不进对客产物。"""

_LEGACY_OPEN_TAGS = ("参与人",)
"""历史版本自加的开放标签，契约从未有此键，2026-11-24 业务方要求移除。

装配侧已不再产出；自检再从旧批次与外部调用方给的节点里剥离，保证回写体里不再出现。
"""

_DATE_PATTERN = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?$"
)

_LEAD_BLOCK = re.compile(r"^\s*\[第\s*\d+\s*条\]\s*$")
"""``OntologyLeadInfo`` 一条 L0 记录里可能渲染多条线索，用 ``[第 N 条]`` 分块。"""

_MODEL_ALIASES = {"全新M817": "M817"}
"""车系命名归一：同一车系的两种写法，判歧义时视为同一个。展示仍用上游原值。"""

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

_FIXED_FIELDS: tuple[tuple[str, str, int, tuple[str, ...]], ...] = (
    # (回传字段, 记录里的来源键, 契约上限, extra_info 历史别名)
    ("follow_up_by", "consultant", MAX_FOLLOW_UP_BY_CHARS, ("跟进人", "销售顾问")),
    ("follow_up_channel", "method", MAX_FOLLOW_UP_CHANNEL_CHARS, ("跟进渠道", "渠道")),
    ("related_model", "vehicle", MAX_RELATED_MODEL_CHARS, ("关联车型",)),
    ("intent_level", "level", MAX_INTENT_LEVEL_CHARS, ("意向级别", "意向等级")),
    ("follow_up_result", "result", MAX_FOLLOW_UP_RESULT_CHARS, ("跟进结果",)),
    (
        "converted_to_opportunity",
        "to_business",
        MAX_CONVERTED_CHARS,
        ("转商机", "是否转商机"),
    ),
    # ``下一步跟进`` 是我们 1124 版之前的输出键，只在自检提升时认，不指望服务端认得。
    (
        "next_follow_up_at",
        "next_follow",
        MAX_NEXT_FOLLOW_UP_CHARS,
        ("下次跟进时间", "下次跟进", "下一步跟进"),
    ),
)
"""七项固定属性：回传字段、记录来源键、上限、``extra_info`` 里的历史别名。

别名前三组与服务端契约的兼容口径一致（它们决定服务端能否从旧形状里提取），最后一组
``下一步跟进`` 是本仓库旧版的输出键，服务端不认识，只能在本地自检时提升。
"""


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
    """模型给出的一段「小结」（``summary``）折成非空字符串列表。

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


def _lead_blocks(content: Any) -> list[str]:
    """把 ``OntologyLeadInfo`` 的渲染正文按 ``[第 N 条]`` 切成单条线索。"""
    blocks: list[list[str]] = [[]]
    for line in str(content or "").splitlines():
        if _LEAD_BLOCK.match(line):
            blocks.append([])
            continue
        blocks[-1].append(line)
    return ["\n".join(lines) for lines in blocks if lines]


def _norm_model(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def _norm_series(value: str) -> str:
    text = re.sub(r"\s+", "", value)
    return _MODEL_ALIASES.get(text, text)


def lead_vehicle_map(records_json: Any) -> tuple[dict[str, set[str]], set[str]]:
    """从 L0 线索记录里取「车型版本 → 车系」与客户的全部车系。

    映射**必须按客户**做：同一车型名称在不同客户下属于不同车系（11-02 批次
    ``Ultra`` 454 次属 X700、18 次属 M817），全局表必错。键做了大小写与空白归一
    （``MAX+`` / ``Max+`` 是同一个版本）。

    返回 ``(by_model, series)``：``by_model`` 的键是归一后的车型名称，值是该客户
    线索里出现过的车系原值；``series`` 是客户全部车系原值。
    """
    by_model: dict[str, set[str]] = {}
    series: set[str] = set()
    for record in _records(records_json):
        if _text(record.get("channel")) != LEAD_CHANNEL:
            continue
        if _text(record.get("status")) != "ok":
            continue
        for block in _lead_blocks(record.get("content")):
            fields: dict[str, str] = {}
            for line in block.splitlines():
                name, sep, raw = line.partition("：")
                if not sep:
                    continue
                fields[name.strip()] = raw.strip()
            model = fields.get("意向车型")
            serie = fields.get("意向车系")
            if serie:
                series.add(serie)
            if model and serie:
                by_model.setdefault(_norm_model(model), set()).add(serie)
    return by_model, series


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


CHANNEL_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    "电话": ("电话", "致电", "通话", "外呼"),
    "微信": ("微信",),
    "上门": ("上门",),
}
"""沟通方式码 → 正文里指代该方式的词。

**不含「到店」**：「邀约到店」是跟进目的，一次电话沟通同样可以邀约到店，
把它当渠道词会把大量正常记录判成冲突。
"""


def channels_in(texts: Iterable[Any]) -> set[str]:
    """正文里出现了哪些渠道词（判据见 :data:`CHANNEL_KEYWORDS`）。"""
    joined = "".join(_text(text) for text in texts)
    return {
        name
        for name, words in CHANNEL_KEYWORDS.items()
        if any(word in joined for word in words)
    }


def channel_conflicts(method: str, texts: Iterable[Any]) -> bool:
    """解码出的沟通方式与正文/小结说的是不是同一件事。

    码值不可信是实测出来的：11-02 批次 110 条含「微信」的跟进原文里，99 条的
    ``communicationmethods`` 是 ``90441``（解作「电话」），涉及 85 个客户。在拿到
    ``prefix_id = 9044`` 的字典原文之前，只能以正文为准——**正文与码值打架时不给这个
    标签**。宁可少一行标签，也不让卡片出现与节点名相反的小图标。

    判据有意宽松：正文只要提到别的渠道、而没提到码值那个渠道，就算冲突。
    正文同时提到两者（「电话联系后又加了微信」）不算冲突——那是同一件事的两个环节。
    """
    method = _text(method)
    if not method:
        return False
    mentioned = channels_in(texts)
    return bool(mentioned) and method not in mentioned


def _next_follow_up(raw: Any) -> str:
    """下次跟进时间：能解析就归一，解析不了原样保留。

    ``next_follow_up_at`` 在契约里只是一个 ≤100 字的展示串，没有 ``followed_at`` 那种
    「格式非法即 400」的约束。因此这里的取舍与时间轴相反：宁可原样展示「下周」，也不要
    把一条交接信息弄丢。
    """
    if not isinstance(raw, str):
        return ""
    text = parse_time(raw)
    if not text:
        text = _literal(raw)
    if not text:
        return ""
    # 下次跟进时间大量落在零点（上游按天填），给日期就够了。
    return text[:10] if text.endswith("T00:00:00") else text


def _fixed_value(key: str, raw: Any) -> str:
    """把一项固定属性折成回传值；取不到给空串。

    只认字符串（转商机额外认布尔）——契约的七项都是字符串列，把数字或结构体 ``str()``
    成 ``"{'key': ...}"`` 等于把内部形态印到卡片上。
    """
    if key == "converted_to_opportunity":
        if isinstance(raw, bool):
            return "是" if raw else "否"
        return _literal(raw) if isinstance(raw, str) else ""
    if key == "next_follow_up_at":
        return _next_follow_up(raw)
    return _literal(raw) if isinstance(raw, str) else ""


def fixed_fields(item: Mapping[str, Any]) -> dict[str, str]:
    """从一条记录确定性产出七项固定属性（1124-v2 版契约 §3.2）。

    空值、占位串与未解码的裸码不出键——契约允许缺省，卡片上也不该出现「跟进人：无」。
    """
    out: dict[str, str] = {}
    for key, record_key, limit, _ in _FIXED_FIELDS:
        value = _fixed_value(key, item.get(record_key))
        if value:
            out[key] = value[:limit]
    return out


def fallback_extra(item: Mapping[str, str]) -> dict[str, Any]:
    """从记录字段确定性取 ``extra_info`` 的**开放标签**。

    1124-v2 版契约把七项固定属性提成一级字段（:func:`fixed_fields`），这里只留契约
    没有结构化承载的战败类型；取不到就整个键不出现，不留空串。

    「参与人」是历史版本自加的编外标签（契约从未有此键），2026-11-24 业务方要求移除，
    这里不再产出；旧数据里的残留由 :func:`promote_fixed_fields` 剥离。
    """
    extra: dict[str, Any] = {}
    defeat = _literal(item.get("defeat_type"))
    if defeat:
        extra["战败类型"] = defeat
    return extra


# ---------------------------------------------------------------- 清洗


def _clean_contents(items: Iterable[Any]) -> list[str]:
    """``contents`` 清洗：去空、去重、限长、限条数。

    模型写的小结走这里——折叠空白是对的，模型偶发换行与多余空格。
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
            }
        )
    return groups


# ---------------------------------------------------------------- 装配


def _node(
    members: Sequence[Mapping[str, str]],
    *,
    name: str,
    summary: Iterable[Any],
    position: int,
    lead_models: Mapping[str, set[str]] | None = None,
    lead_series: Iterable[str] = (),
) -> dict[str, Any]:
    """构造一个契约形状的节点。

    ``contents`` 是模型写的一段**跟进小结**。销售原文不进 ``contents``（原因见模块文档）；
    后续安排不单独成段，记录里有明确下一步时写进小结最后一句，其余由七项固定字段承担。

    唯一的原文返场：模型对这一组什么都没写时，把组内原文逐字放进来兜底。履历的价值首先
    在于「这一段不能空」——宁可让读者看到一句粗糙的原文，也不要一片空白。
    """
    lead_series = tuple(series for series in lead_series if series)
    written = _clean_contents(summary)
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
    # 七项固定属性与 ``extra_info`` 取组内**最新**那条记录：一个节点合并了多次跟进时，
    # 卡片下方标签要反映这组跟进的最终状态（结果、意向级别、下次跟进时间），否则读者会
    # 看到正文写「A 级、11 月 3 日」而字段写「C 级、11 月 1 日」。时间轴位置仍用首条
    # （``followed_at`` 不动）。
    tail = members[-1]
    node.update(fixed_fields(tail))
    extra = fallback_extra(tail)
    # 渠道是**唯一**可能与正文打架的一项：其余键取自记录字段，与措辞无关。
    # 码值大面积错填 90441（电话）而正文写微信（见 :func:`channel_conflicts`）。
    # 冲突、且这条记录的正文只提到一个渠道时，以正文为准——正文是销售自己写的；
    # 正文提到多个渠道（「电话没接，又发了微信」）或没有渠道词时，码值同样拿不准，
    # 维持旧口径：不出这个标签。
    tail_text = _text(tail.get("content"))
    channel = node.get("follow_up_channel")
    if channel and channel_conflicts(channel, [tail_text]):
        mentioned = channels_in([tail_text])
        if len(mentioned) == 1:
            node["follow_up_channel"] = next(iter(mentioned))[:MAX_FOLLOW_UP_CHANNEL_CHARS]
        else:
            node.pop("follow_up_channel")
    # 兜底后的渠道仍要过节点级守卫：模型写的小结与它冲突时不出这个标签，
    # 免得卡片上的小图标与节点名写反。
    if "follow_up_channel" in node and channel_conflicts(
        node["follow_up_channel"], [node["node_name"], *contents]
    ):
        node.pop("follow_up_channel")
    # 业务方要车系（X700 / M817），不只是版本名（Ultra / PRO）：见
    # :func:`lead_vehicle_map`。车型名称在同客户线索里能唯一落到车系时拼接；
    # 多义时只用版本名，不猜。
    model = _text(node.get("related_model"))
    series_values = {_norm_series(value) for value in lead_series if value}
    if model and lead_models:
        candidates = lead_models.get(_norm_model(model)) or set()
        if len({_norm_series(value) for value in candidates}) == 1:
            node["related_model"] = (
                f"{sorted(candidates)[0]} {model}"[:MAX_RELATED_MODEL_CHARS]
            )
    elif not model and len(series_values) == 1:
        node["related_model"] = sorted(lead_series)[0][:MAX_RELATED_MODEL_CHARS]
    if not node.get("related_model"):
        node.pop("related_model", None)
    if extra:
        node["extra_info"] = extra
    return node


def assemble(
    records: Sequence[Mapping[str, str]],
    model_output: Any,
    *,
    lead_models: Mapping[str, set[str]] | None = None,
    lead_series: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[int]]:
    """把模型的分组与原始记录装成契约节点列表。

    模型覆盖不到或压根没返回时，代码按记录 1:1 补节点——**不丢记录**是这条产线的
    底线（履历的价值首先在于「都在」）。补出来的节点回到时间序里的正确位置。

    ``lead_models`` / ``lead_series`` 来自 :func:`lead_vehicle_map`，用于给
    ``related_model`` 补车系；不传时行为与旧版一致。

    返回 ``(nodes, supplemented)``：``supplemented`` 是被代码补成节点的记录下标（0 基），
    留痕用。
    """
    total = len(records)
    groups = model_groups(model_output, total)
    covered = {index for group in groups for index in group["indexes"]}

    slots: list[tuple[str, int, list[Mapping[str, str]], str, list[str]]] = []
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
            )
        )

    supplemented: list[int] = []
    for index, item in enumerate(records):
        if index in covered:
            continue
        supplemented.append(index)
        # 代码补出来的节点没有模型措辞，``_node`` 会退回组内原文。
        slots.append((_text(item.get("created_at")), index, [item], "", []))

    # 跨组补充的记录按「本条记录的创建时间」定位，与模型分组共用同一时间轴；
    # 同一时刻再比记录下标，保证顺序稳定。
    slots.sort(key=lambda slot: (slot[0], slot[1]))

    nodes: list[dict[str, Any]] = []
    for position, (_, _, members, name, summary) in enumerate(
        slots[:MAX_NODES], start=1
    ):
        nodes.append(
            _node(
                members,
                name=name,
                summary=summary,
                position=position,
                lead_models=lead_models,
                lead_series=lead_series,
            )
        )
    return nodes, supplemented


# ---------------------------------------------------------------- 契约自检


def promote_fixed_fields(entry: Mapping[str, Any]) -> dict[str, Any]:
    """把任意形状的节点提升成 1124-v2 版：七项固定属性提到一级，``extra_info`` 去重。

    处理两种来源：

    1. 本仓库旧版的产物（七项在 ``extra_info``，下次跟进叫 ``下一步跟进``）；
    2. 外部调用方或回放批次给的、服务端兼容口径的节点（别名见 :data:`_FIXED_FIELDS`）。

    一级字段优先于别名；别名无论提没提到值都从 ``extra_info`` 删掉——服务端提取后也会
    去重，这里先删等于同一事实只发一遍。返回副本，不改入参。
    """
    node = dict(entry)
    raw_extra = node.get("extra_info")
    extra = dict(raw_extra) if isinstance(raw_extra, Mapping) else None

    for key, _, limit, aliases in _FIXED_FIELDS:
        value = _fixed_value(key, node.get(key))
        for alias in aliases:
            if not value and extra is not None:
                value = _fixed_value(key, extra.get(alias))
            if extra is not None:
                extra.pop(alias, None)
        if value:
            node[key] = value[:limit]
        else:
            node.pop(key, None)

    if extra is not None:
        # 历史遗留的编外开放标签（如「参与人」）：契约从未有此键，业务方要求移除。
        # 回放旧批次与外部调用方给的节点可能仍带着它，一并剥掉。
        for legacy in _LEGACY_OPEN_TAGS:
            extra.pop(legacy, None)
        node["extra_info"] = extra
    return node


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

        # 七项固定属性：先提升（旧形状 → 一级字段），再逐项取限长后的值。
        # 空值与占位串不出键；``extra_info`` 里的同名旧键已在提升时删掉。
        normalized = promote_fixed_fields(entry)
        for key, _, _, _ in _FIXED_FIELDS:
            value = normalized.get(key)
            if value:
                node[key] = value

        extra = normalized.get("extra_info")
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

    # 车系在同客户线索记录里（``OntologyLeadInfo``）；L0 已经全量在手，不需要改依赖。
    lead_models, lead_series = lead_vehicle_map(records_json)
    nodes, supplemented = assemble(
        records, output, lead_models=lead_models, lead_series=lead_series
    )
    kept, dropped = sanitize(nodes)
    if not kept:
        return _out([], status="empty_after_sanitize", calls=calls,
                    error=error or "全部节点未通过契约自检", record_count=len(records),
                    supplemented=supplemented, dropped=dropped)

    return _out(kept, status=status, calls=calls, error=error,
                record_count=len(records), supplemented=supplemented, dropped=dropped)


__all__ = [
    "FOLLOW_CHANNEL",
    "MAX_CONVERTED_CHARS",
    "MAX_CONTENTS",
    "MAX_FOLLOW_UP_BY_CHARS",
    "MAX_FOLLOW_UP_CHANNEL_CHARS",
    "MAX_FOLLOW_UP_RESULT_CHARS",
    "MAX_INTENT_LEVEL_CHARS",
    "MAX_NEXT_FOLLOW_UP_CHARS",
    "MAX_NODES",
    "MAX_RELATED_MODEL_CHARS",
    "TEMPLATE_NAME",
    "assemble",
    "build",
    "channels_in",
    "fallback_extra",
    "fallback_name",
    "fixed_fields",
    "model_groups",
    "parse_record_lines",
    "parse_records",
    "parse_time",
    "promote_fixed_fields",
    "readable_time",
    "render_input",
    "sanitize",
]
