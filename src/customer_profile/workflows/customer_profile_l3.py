"""L3 校验与表达层（设计稿 §7）。

**它替代的是 ``customer_profile_production``。** 那张图 12 个 LLM 节点各写各的，
9 个 section 与 Notes 两处各自生成、无人对齐——基线「卡片说 A、Notes 说 B」正是这么来的。

L3 的口径只有一条：**先定稿事实，再把事实写成话。**

```
L2 facts（代码投影，已定稿）
  ↓ l3_validate  代码校验：无依据确定值率 = 0、枚举合法、单多值、跨字段
  ↓ l3_express   1 次 LLM：把**同一份 facts**写成 9 个 section
  ↓ l3_assemble  代码装配：与 Notes 同源一致性检查 + 拼成 result1 形状
```

## 与 production 的三处根本差别

1. **同源**。Notes 来自 L2 的 ``facts``，9 个 section 也读**同一份** ``facts``。
   production 里 Notes 由 ``Notes生成`` 独立写、卡片由 7 个 LLM 独立写，两者无任何
   共享输入，冲突只能靠事后「Notes 审核」补救——那是治不好的。
2. **表达不新增事实**（§7.4）。LLM 只做组织与措辞；装配层逐条核对它引用的取值能否在
   ``facts`` 里找到出处，找不到即剔除并留痕。
3. **复用预置资产，不复制**。6 个业务资产按**原名**引用 production 的模板
   （见 :data:`PRODUCTION_ASSET`），仓库里只有一份正文。
   复制到本模块的 slug 下会造出第二份副本，日后必然漂移。
"""

from __future__ import annotations

import json
from typing import Any

from .. import readable as rdb
from .. import text_utils as tut
from ..rules.clusters import hard as hard_rules
from ..definitions import WorkflowDef, make_node

WORKFLOW_ID = "customer_profile_l3"
DISPLAY_NAME = "L3 校验与表达"

START = "l3_start"
END = "l3_end"

VALIDATE = "l3_validate"
EXPRESS = "l3_express"
ASSEMBLE = "l3_assemble"

SLUG = WORKFLOW_ID
SYSTEM_TEXT = "You are a helpful AI assistant."


def template_name(node_id: str) -> str:
    return f"{SLUG}__{node_id}"


def PRODUCTION_ASSET(node_id: str) -> str:
    """按**原名**取生产图里的预置资产模板。

    「必须原样保留的业务资产」共 6 份（设计稿 §7.5），它们属于**业务资产**而非
    某张图的私有物。本模块直接引用生产图那 6 个文件，不在 ``customer_profile_l3__*``
    下另存副本——一份正文、一处维护，才是「一字不改」能被验证的前提。
    """
    return f"customer_profile_production__{node_id}"


EXPRESS_TEMPLATE = template_name(EXPRESS)

# ---- 6 个预置资产（节点 ID 与生产图逐字相同，模板文案指向同一份文件）
TAG_LIBRARY = "1776755019648"
EVIDENCE_FORMAT = "1776828906716"
PROFILE_FORMAT = "1776829070283"
PRODUCT_INFO = "1776853902103"
PRODUCT_INFO_SHORT = "1776855531118"
INVITE_STRATEGY = "1776934470389"

PRESET_ASSETS: tuple[tuple[str, str], ...] = (
    (TAG_LIBRARY, "标签库预置"),
    (EVIDENCE_FORMAT, "证据线索数据格式预置"),
    (PROFILE_FORMAT, "特征画像数据格式预置"),
    (PRODUCT_INFO, "产品信息及卖点预置"),
    (PRODUCT_INFO_SHORT, "产品信息及卖点预置（速查简化版）"),
    (INVITE_STRATEGY, "邀约策略&话术预置"),
)

SECTION_KEYS: tuple[str, ...] = (
    "profile_summary",
    "inferred_tags",
    "usage_scenarios",
    "communication_strategies",
    "customer_overview",
    "purchase_motivations",
    "product_preferences",
    "resistances",
    "emotion_state",
)
"""卡片的 9 个 section（基线 ``analysis_result`` 的键，顺序即展示顺序）。

**这是对外产物契约，不是本模块的私有约定**——形状逐节对齐基线（见
``docs/business_opt01_v2/端到端方案设计.md`` §7.4 与 R1b 批次产物）。
"""


# ====================================================================
# l3_validate：代码校验（确定性，0 token）
# ====================================================================


_as_text = tut.as_text
"""取值 → 文本。实现见 :mod:`customer_profile.text_utils`。"""

_payload = tut.as_payload
"""JSON 串 / 对象的兼容读取。实现见 :mod:`customer_profile.text_utils`。"""


def _claims_of(claims_json: Any) -> list[dict]:
    data = _payload(claims_json)
    if isinstance(data, dict):
        data = data.get("fields", data.get("claims"))
    if not isinstance(data, (list, tuple)):
        return []
    return [c for c in data if isinstance(c, dict)]


def validate_l3(claims_json, facts_json, notes_updates_json, basis_json="{}") -> dict:
    """L3 代码校验：在**表达之前**证明这份事实是干净的（设计稿 §7.2）。

    三条硬判据，逐条都对应一个真实错误类型：

    1. **无依据确定值率 = 0**。``fact`` 必须能指回原文——它要么有 ``quote``（L1 定位层
       切出的原文片段），要么来源渠道是登记档（登记系统直接给出，本就不需要原文锚点）。
       指不出来的 ``fact`` 是「编造的确定性」，这是本次要杜绝的头号错误。
    2. **枚举合法**。枚举字段的值须是该字段的合法键（多选用 ``||`` 连接）——
       合法键来自契约与编外登记表**并集**（见 :func:`enum_is_known`），编外枚举照样要校验。
       **但已降级为 ``inferred_low`` 的取值除外**：P3 规则 d 刻意保留归不到合法键的
       客户原话（「想上广州那边的牌」「裸金价格」），标低置信并转人工确认。那种取值
       本就不是「确定值」，拦它等于把 P3 的降级策略又废掉一遍。降级的记为
       **warning**（仍可见、仍待人工归一键），未降级的才是 error。
    3. **Notes 与 facts 同源**。``notes_updates`` 里的每个取值都必须来自 ``facts``——
       它本就是 ``facts`` 的投影，对不上即说明有人绕过 L2 改了 Notes。

    返回报告中 ``blocking`` 为真时，调用方**不应**继续表达：宁可这一步失败，
    也不能把一份带编造确定性的画像交给销售。
    """
    claims = _claims_of(claims_json)
    basis = _payload(basis_json)
    if not isinstance(basis, dict):
        basis = {}
    facts = _payload(facts_json)
    if not isinstance(facts, dict):
        facts = {}
    updates = _payload(notes_updates_json)
    if isinstance(updates, dict):
        updates = updates.get("basic_notes_updates", [])
    if not isinstance(updates, (list, tuple)):
        updates = []

    issues: list[dict] = []

    # ---- 1 无依据确定值率
    unbacked = []
    facts_n = 0
    for claim in claims:
        if str(claim.get("basis") or "").strip() != "fact":
            continue
        facts_n += 1
        has_quote = bool(str(claim.get("quote") or "").strip())
        channel = str(claim.get("record_channel") or "").strip()
        # 判据与 ``review_notes`` 共用一份实现（HARD-03）。两边输入来源不同——
        # 这里读 ``claim.quote``，Notes 侧读证据串里有没有原文标记——但判定相同。
        if hard_rules.is_unbacked_fact(has_quote, channel):
            unbacked.append(
                {
                    "attribute": claim.get("attribute"),
                    "value": claim.get("value"),
                    "channel": channel,
                }
            )
    if unbacked:
        issues.append(
            {
                "rule": "unbacked_fact",
                "severity": "error",
                "reason": f"{len(unbacked)} 条 fact 指不出原文，也不是登记档来源",
                "detail": unbacked[:10],
            }
        )

    # ---- 2 枚举合法（降级值除外，见函数说明）
    illegal = []
    downgraded_illegal = []
    for key, value in facts.items():
        if not enum_is_known(key):
            continue
        soft = str(basis.get(key) or "") == INFERRED_LOW
        for item in _enum_items(value):
            if enum_is_legal(key, item):
                continue
            entry = {"attribute": key, "value": item}
            (downgraded_illegal if soft else illegal).append(entry)
    if illegal:
        issues.append(
            {
                "rule": "illegal_enum",
                "severity": "error",
                "reason": f"{len(illegal)} 个枚举取值不在契约内且未被降级",
                "detail": illegal[:10],
            }
        )
    if downgraded_illegal:
        issues.append(
            {
                "rule": "illegal_enum_downgraded",
                "severity": "warning",
                "reason": (
                    f"{len(downgraded_illegal)} 个枚举取值不在契约内，但已降为 inferred_low "
                    "并转人工确认（P3 规则 d 保留的客户原话）"
                ),
                "detail": downgraded_illegal[:10],
            }
        )

    # ---- 3 Notes 与 facts 同源
    drifted = []
    for entry in updates:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("attribute_key") or "")
        if key and key not in facts:
            drifted.append({"attribute": key, "value": entry.get("attribute_value")})
    if drifted:
        issues.append(
            {
                "rule": "notes_not_from_facts",
                "severity": "error",
                "reason": f"{len(drifted)} 个 Notes 字段在 facts 里没有出处",
                "detail": drifted[:10],
            }
        )

    blocking = any(item["severity"] == "error" for item in issues)
    return {
        "facts_json": json.dumps(facts, ensure_ascii=False, default=str),
        # 表达层读的是这一份：事实喂给模型时必须翻成中文名与非英文取值，
        # 否则模型会把 ``payment_method=loan`` 这类内部形态照抄进画像卡正文。
        "facts_readable": rdb.readable_facts(facts),
        "issue_json": json.dumps(issues, ensure_ascii=False, default=str),
        "issue_count": len(issues),
        "fact_count": facts_n,
        "unbacked_count": len(unbacked),
        "valid": not blocking,
    }


def _enum_items(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        out: list[Any] = []
        for item in value:
            out.extend(_enum_items(item))
        return out
    if isinstance(value, str) and "||" in value:
        return [part.strip() for part in value.split("||") if part.strip()]
    if isinstance(value, dict):
        return [value.get("key")]
    return [value]


def enum_is_known(attribute: str) -> bool:
    """该字段是否为**枚举字段**（契约或编外——判定集合见 :mod:`customer_profile.internal_fields`）。

    不去问 :mod:`notes_contract` 直接问：

    - 编外枚举（``emotion`` / ``communication_rhythm`` / ``personality_and_preferences``
      / ``seating_requirement`` / ``driving_preference`` / ``purchase_nature``）已离开契约，
      用契约判定会把它们整条跳过——非法值既拦不下也不告警，而它们仍要喂
      ``decision_psychology`` 三个固定 key，脏值会一路带进回传体；
    - 与 L2 投影层同源：两处若用不同集合判定，会出现「投影层认、校验层不认」的
      自相矛盾，而这类不一致不报错，只是产物悄悄变样。
    """
    from .. import internal_fields as inf

    return inf.is_enum(attribute)


def enum_is_legal(attribute: str, value: Any) -> bool:
    """取值是否归一到该字段的合法键（合法键本身或展示名皆可）。"""
    from .. import internal_fields as inf

    if value is None:
        return False
    text = _as_text(value)
    if not text:
        return False
    return text in inf.legal_keys(attribute) or bool(inf.display_to_key(attribute, text))


INFERRED_LOW = "inferred_low"
"""低置信推断档。与 :mod:`customer_profile.projection` 的常量同值——
本模块是独立工作流，不导入那个模块，靠**用例**钉住两者一致。"""


REGISTRY_CHANNELS: frozenset[str] = hard_rules.REGISTRY_CHANNELS
"""登记档渠道：由系统直接给出的事实，本就不需要原文锚点。

正身已随 HARD-03 迁到 :mod:`customer_profile.rules.clusters.hard`，与 ``review_notes``
共用同一份——此前两处各写一份，正是「改一处漏一处」的来源。
"""


# ====================================================================
# l3_assemble：代码装配（同源检查 + 拼产物）
# ====================================================================


def assemble_l3(phone: str, batch_id: str, facts_json, notes_updates_json, express_json) -> dict:
    """把表达层的 9 个 section 与 Notes 装成 ``result1``（与基线同形）。

    **同源检查在这里做**：表达层是 LLM，它可以润色，但不得引入 ``facts`` 之外的事实。
    因此逐个 section 扫一遍，凡出现「看起来是取值、却在 facts 里找不到出处」的，
    记录在 ``new_fact_suspects`` 里。

    这里**不做字符串级的强校验**（9 个 section 是自然语言叙述，取值形态千变万化，
    强行比对会误杀正确的改写）。可判定的是「**数字与专有取值**是否凭空出现」——
    核对的取值口径与 ``facts`` 完全一致：数字串、以及任何出现在 facts 值里的
    2 字以上片段。核不出的**不拦、只留痕**，交人工复核。
    """
    facts = _payload(facts_json)
    if not isinstance(facts, dict):
        facts = {}
    notes = _payload(notes_updates_json)
    if isinstance(notes, dict):
        notes = notes.get("basic_notes_updates", [])
    if not isinstance(notes, (list, tuple)):
        notes = []

    sections = _payload(express_json)
    if isinstance(sections, dict):
        sections = sections.get("analysis_result", sections)

    analysis_result: dict[str, Any] = {}
    if isinstance(sections, dict):
        for key in SECTION_KEYS:
            if key in sections:
                analysis_result[key] = sections[key]

    # ---- 上提归位：把被嵌进别的 section 里的节搬回顶层。
    # 模型会偶发地把「第 6/7/8 节」理解为第 5 节（对象型）的子键，写进
    # ``customer_overview`` 内部。此前这种输出会被下面的补空静默抹平——内容还在，
    # 位置错了，卡片上就成了空白（v3.1 八案里有五案如此）。
    # 顶层的值优先（``setdefault`` 不覆盖），所以正常输出不受影响。
    hoisted: list[str] = []
    for outer in SECTION_KEYS:
        value = analysis_result.get(outer)
        if not isinstance(value, dict):
            continue
        for inner in SECTION_KEYS:
            if inner == outer or inner not in value:
                continue
            if inner in analysis_result:
                # 顶层已有该节：嵌套的那份是多余的，就地清除，**不覆盖**顶层取值。
                # 留着它会让该节的内容出现在两个地方，卡片上就是脏键。
                value.pop(inner)
                continue
            analysis_result[inner] = value.pop(inner)
            hoisted.append(inner)

    # ---- 实到节数在**补空之前**统计。补空之后每个键都在，恒等于 9，
    # 那样统计等于没统计——缺节必须是可见事件，而不是被补齐掩盖。
    arrived = [key for key in SECTION_KEYS if analysis_result.get(key)]
    missing = [key for key in SECTION_KEYS if not analysis_result.get(key)]

    # 表达层漏掉的节补空形态，**不编造**：缺就是缺，卡片渲染时自行留白。
    for key in SECTION_KEYS:
        analysis_result.setdefault(key, [] if key in LIST_SECTIONS else {})

    # ---- 商机等级由定级环节给定，表达层不得自行推断（v3.6）
    # 等级的推理链是一次独立、足量的调用产出的；若让表达层按同一份 facts 再推一遍，
    # 卡片上的等级与依据就会与 ``ai_insight`` 分叉——正是 production 时代的老毛病。
    # 这里以定级结果**覆盖**表达层的 ``business_opp_level``。
    from .. import projection as proj

    level = proj.normalize_level(facts.get("deal_level"))
    if level:
        overview = analysis_result.get("customer_overview")
        if not isinstance(overview, dict):
            overview = {}
        overview["business_opp_level"] = {
            "value": level,
            "reasoning_detail": _level_reasoning_detail(facts),
        }
        analysis_result["customer_overview"] = overview

    # ---- resistances **不写进 Notes**（2026-10-22）
    # 它在回传契约里已下线（``notes_contract`` 不含该键），回传体由 ``callback_build``
    # 从表达层 ``analysis_result.resistances`` 直接生成 ``deal_battle.loss_conditions``。
    # v3.3 曾在这里补过一条 Notes 条目，但那条记录不经过回传白名单，只是让排查时
    # 误以为已经覆盖——保留代码会掩盖真正生效的那条通路。

    analysis_result["basic_notes_updates"] = list(notes)

    suspects = _scan_new_facts(analysis_result, facts)
    result1 = {
        "phone": "" if phone is None else str(phone),
        "batch_id": "" if batch_id is None else str(batch_id),
        "analysis_result": analysis_result,
    }
    return {
        "result1": json.dumps(result1, ensure_ascii=False),
        "section_count": len(arrived),
        "sections_missing": json.dumps(missing, ensure_ascii=False),
        "sections_hoisted": json.dumps(hoisted, ensure_ascii=False),
        "new_fact_suspects": len(suspects),
        "suspects_json": json.dumps(suspects, ensure_ascii=False, default=str),
    }


LIST_SECTIONS: frozenset[str] = frozenset(
    {"inferred_tags", "usage_scenarios", "communication_strategies",
     "purchase_motivations", "product_preferences", "resistances"}
)
"""数组形态的 section。其余是对象形态（``profile_summary`` / ``customer_overview`` /
``emotion_state``）——形状逐节对齐基线。"""


def _level_reasoning_detail(facts: dict) -> dict[str, Any]:
    """商机等级的 ``reasoning_detail``：定级环节给了就用它的，否则给保守兜底。"""
    raw = facts.get("deal_level_detail")
    if isinstance(raw, dict):
        steps = [str(s) for s in (raw.get("steps") or []) if str(s).strip()]
        evidence = [str(s) for s in (raw.get("evidence") or []) if str(s).strip()]
        confidence = raw.get("confidence")
        if not isinstance(confidence, (int, float)):
            confidence = 0.6
        return {
            "summary": str(raw.get("summary") or "").strip(),
            "steps": steps,
            "evidence": evidence,
            "confidence": max(0.0, min(1.0, float(confidence))),
        }
    from .. import projection as proj

    level = proj.normalize_level(facts.get("deal_level")) or ""
    return {
        "summary": f"综合客户数据判定商机等级为{level}",
        "steps": ["由商机定级环节依客户全部记录与已定稿事实得出"],
        "evidence": [],
        "confidence": 0.6,
    }


def _scan_new_facts(analysis_result: dict, facts: dict) -> list[dict]:
    """扫出卡片里「facts 中无出处」的数字型取值，仅留痕不拦截。"""
    haystack = " ".join(_as_text(value) for value in facts.values())
    numbers_in_facts = set()
    for value in facts.values():
        for token in _as_text(value).replace("|", " ").split():
            numbers_in_facts.add(token)

    suspects: list[dict] = []
    for key in SECTION_KEYS:
        text = _as_text(analysis_result.get(key))
        if not text:
            continue
        # 只对**带单位的量**做核对：它们最可能是「编出来的事实」，而非措辞改写。
        for marker in ("万元", "%", "万", "岁"):
            index = text.find(marker)
            if index < 0:
                continue
            window = text[max(0, index - 12): index + len(marker)]
            if not any(token and token in haystack for token in (window.strip(),)):
                suspects.append({"section": key, "excerpt": window.strip(), "marker": marker})
                break
    return suspects[:20]


# ====================================================================
# 图定义
# ====================================================================


def _preset(node_id: str, title: str):
    """预置资产节点：只声明模板，无绑定（模板正文本身就是全部内容）。"""
    return make_node(
        node_id,
        title,
        "template-transform",
        after=(START,),
        template=PRODUCTION_ASSET(node_id),
    )


WORKFLOW: WorkflowDef = WorkflowDef(
    workflow_id=WORKFLOW_ID,
    display_name=DISPLAY_NAME,
    source_dsl=DISPLAY_NAME,
    entries=(START,),
    exits=(END,),
    outputs={"result1": "result1"},
    nodes=(
        make_node(
            START,
            "用户输入",
            "start",
            variables=(
                "phone",
                "batch_id",
                "facts_json",
                "basis_json",
                "notes_updates_json",
                "claims_json",
            ),
            required_phone=True,
            required_facts_json=True,
            required_notes_updates_json=True,
        ),
        # ---- 6 个预置资产（业务资产，正文一字不改）
        *(_preset(node_id, title) for node_id, title in PRESET_ASSETS),
        # ---- 代码校验
        make_node(
            VALIDATE,
            "L3 代码校验",
            "code",
            after=(START,),
            inputs={
                "claims_json": (START, "claims_json"),
                "facts_json": (START, "facts_json"),
                "basis_json": (START, "basis_json"),
                "notes_updates_json": (START, "notes_updates_json"),
            },
            outputs=(
                "facts_json",
                "facts_readable",
                "issue_json",
                "issue_count",
                "fact_count",
                "unbacked_count",
                "valid",
            ),
            function=f"customer_profile.workflows.{SLUG}:validate_l3",
        ),
        # ---- 表达（1 次 LLM）
        make_node(
            EXPRESS,
            "L3 表达（9 个 section）",
            "llm",
            after=(VALIDATE, *(node_id for node_id, _ in PRESET_ASSETS)),
            inputs={
                "facts_output": (VALIDATE, "facts_readable"),
                "tag_library": (TAG_LIBRARY, "output"),
                "profile_format": (PROFILE_FORMAT, "output"),
                "product_info": (PRODUCT_INFO, "output"),
                "product_info_short": (PRODUCT_INFO_SHORT, "output"),
                "invite_strategy": (INVITE_STRATEGY, "output"),
            },
            outputs=("result",),
            template=EXPRESS_TEMPLATE,
            output="result",
            expect_json=True,
            system_text=SYSTEM_TEXT,
        ),
        # ---- 装配
        make_node(
            ASSEMBLE,
            "L3 装配",
            "code",
            after=(EXPRESS,),
            inputs={
                "phone": (START, "phone"),
                "batch_id": (START, "batch_id"),
                "facts_json": (VALIDATE, "facts_json"),
                "notes_updates_json": (START, "notes_updates_json"),
                "express_json": (EXPRESS, "result"),
            },
            outputs=("result1", "section_count", "sections_missing",
                     "sections_hoisted", "new_fact_suspects", "suspects_json"),
            function=f"customer_profile.workflows.{SLUG}:assemble_l3",
        ),
        make_node(
            END,
            "输出",
            "end",
            after=(ASSEMBLE,),
            inputs={"result1": (ASSEMBLE, "result1")},
            outputs=("result1",),
        ),
    ),
)


# ====================================================================
# code 节点 -> 函数名映射
# ====================================================================

CODE_SPECS: dict[str, dict] = {
    VALIDATE: {"main": "validate_l3", "renames": {}},
    ASSEMBLE: {"main": "assemble_l3", "renames": {}},
}


def default_inputs(
    phone: str, batch_id: str = "", facts_json: str = "{}", notes_updates_json: str = "[]",
    claims_json: str = "[]",
) -> dict[str, Any]:
    """该工作流的对外入参契约。"""
    return {
        "phone": phone,
        "batch_id": batch_id,
        "facts_json": facts_json,
        "notes_updates_json": notes_updates_json,
        "claims_json": claims_json,
    }
