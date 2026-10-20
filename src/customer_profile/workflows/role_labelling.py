"""录音说话方标注：把 LLM 的角色分段展开成逐句标注文本。

**为什么单独一层。** AUC 的 ``speaker`` 编号在本批录音里不可信——实测 55 条有效录音中
41 条存在「同一编号下性别稳定矛盾」，说话人编号的连续段有 78% 长度为 1（逐句在跳）。
录音里的销售自述（「我们是4S店的」）一旦被错归到客户方，下游会把整个客户职业判错。
因此角色必须由**语义**重新判定，编号只作低置信参考。

LLM 只输出「角色分段」（``{"from": 0, "to": 4, "role": "销售方"}``），逐句展开由本模块
完成，理由是：

- **省 token**：补全量随「角色切换次数」而非句数增长（817 句的录音只有几十段）；
- **可校验**：分段是否无缝覆盖、角色是否合法，代码能逐条判，不依赖模型自觉；
- **可降级**：覆盖缺口补 ``未知来源``、非法角色归 ``未知来源``，任何一处坏掉都不会把句子
  错归到客户方——那正是本次要消灭的错误形态。

标注结果分两路交出：``content`` 是**纯正文**（勘误后的写法），``role_lines`` 是句级
角色与正文字符区间（``{i, role, start, end}``）。正文里**不放任何角色标记**——L1 抄锚点
时必须命中原话的连续切片，标记混进正文会让锚点对不上，也会把内部符号带进对客产出。
勘误明细另存在 ``corrections`` 里，供 L1 输入的分节头汇总展示。
"""

from __future__ import annotations

import json
from typing import Any

ROLE_SALES = "销售方"
ROLE_CUSTOMER = "客户方"
ROLE_UNKNOWN = "未知来源"

ROLES: tuple[str, ...] = (ROLE_SALES, ROLE_CUSTOMER, ROLE_UNKNOWN)

LEGACY_ROLES: dict[str, str] = {
    "其他人员": ROLE_UNKNOWN,
    "存疑": ROLE_UNKNOWN,
}
"""旧词表到新词表的映射。

四分类（其他人员 / 存疑）在业务上被合并为「未知来源」：两者的下游处置完全相同
（都不作强证据），分开只会让提示词的判据边界更容易抖。保留映射是为了历史产物与
缓存还能解析，不是为了让新输出继续使用这两个词。
"""

SALES_SELF_MARKERS: tuple[str, ...] = (
    "我们店",
    "咱店",
    "咱们店",
    "我这边是",
    "我们这边是",
    "我是销售",
    "销售顾问",
    "我们4S店",
    "我们是4S店",
    "给您报",
    "帮你报",
    "给你报",
)
"""销售自述句式（**单向兜底**，只用于把客户方纠正为未知来源）。

**它不是主判据。** 主判据是提示词里的「资源与承诺方向」：谁在给资源、给承诺、给安排，
谁是销售方。这组句式只是最后一层保险——把「判成客户方、却明摆着在报自家门店」的句子
改判未知来源。

只做客户方 → 未知来源一个方向：销售方句子若命中这些词是正常的，客户方句子命中则说明
大概率判错。反方向（未知来源 → 客户方）不做，避免把客户的真实自述误判成销售。
"""

DEFAULT_CHUNK_SIZE = 250
"""单次 LLM 调用的句数上限。

实测转写句数中位数 257、90 分位约 1,600、最长 3,941。250 句约对应 4,000–6,000 字，
既保住上下文（整通对话的走向），又不让单次输出过长。分块时把**上一块最后一句的角色**
作为倾向传给下一块，避免块边界处角色漂移。
"""

# ---------------------------------------------------------------- 渠道规则

CHANNEL_RULES: dict[str, str] = {
    "outbound": (
        "## 渠道规则：外呼录音\n\n"
        "这通常是**主叫电话**，绝大多数情况下参与者只有销售方和客户方两人"
        "（客户的家人代为接听时也算客户方）。\n\n"
        "- 默认按**两方**判断：听不出是谁的句子，回看上下文与问答结构，按它所在轮次的一方归属。\n"
        "- 但**允许**判出 `未知来源`：实测外呼录音里会混入车载语音助手、通话外闲聊、"
        "接听的旁人等。确实不是销售与客户对话的内容，照判 `未知来源`，不要硬塞给任何一方。"
    ),
    "test_drive": (
        "## 渠道规则：试驾录音\n\n"
        "这是门店/车内的连续录音，可能录到**多段对话与大量无关声音**"
        "（车载语音助手、店员闲聊、交接班、私人电话、背景插话）。\n\n"
        "三种取值都可以用。与购车无关或身份不明的内容一律判 `未知来源`。"
    ),
}

CHANNEL_ALLOWED: dict[str, tuple[str, ...]] = {
    "outbound": ROLES,
    "test_drive": ROLES,
}
"""各渠道允许的角色。三档统一放开——实测外呼也会混入车载语音，一刀切禁止只会逼模型硬猜。"""


def channel_of(file_url: str) -> str:
    """按录音直链判渠道。直链形如 ``.../<号码>/<渠道>/<文件>``。"""
    return "test_drive" if "TestDrive" in str(file_url or "") else "outbound"


# ---------------------------------------------------------------- 节点入参


def prepare_prompt(auc_result: Any, file_url: str = "") -> dict[str, Any]:
    """``code`` 节点入口：给角色标注 LLM 组装模板变量。

    只传判断角色必需的 ``index/speaker/gender/text``：情绪、语速、音量对判角色没有
    价值，传进去只会推高输入长度。
    """
    channel = channel_of(file_url)
    return {
        "total": str(len(_utterances(auc_result))),
        "prior": prior_hint(None, 0),
        "channel_rule": CHANNEL_RULES[channel],
        "utterances": chunk_payload(_utterances(auc_result), 0, len(_utterances(auc_result))),
    }


def expand_annotation(payload: Any, auc_result: Any, file_url: str = "") -> dict[str, Any]:
    """``code`` 节点入口：把 LLM 的分段与勘误展开成逐句标注文本。

    ``payload`` 是 LLM 节点 ``text`` 字段（JSON 字符串）。解析失败时**不抛**，而是
    交出未标注的原文并标 ``annotated=false``——一条录音的标注失败不该中断整批号码，
    而原文照常进 L0，下游只会少掉角色线索，不会拿到错归属。
    """
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except ValueError:
            data = None
    else:
        data = payload

    utterances = _utterances(auc_result)
    channel = channel_of(file_url)
    allowed = CHANNEL_ALLOWED[channel]
    if not utterances:
        return assemble_bundle(auc_result, {}, {}, annotated=False, issues=["转写为空"])

    roles, issues = apply_segments(data, utterances, 0, len(utterances))
    for index, role in roles.items():
        if role not in allowed:
            issues.append(f"第 {index} 句角色 {role} 不在本渠道允许范围，改记未知来源")
            roles[index] = ROLE_UNKNOWN
    corrections, correction_issues = apply_corrections(
        data, utterances, 0, len(utterances)
    )
    issues.extend(correction_issues)
    # 单向兜底：判成客户方却在明说自家的句子改判未知来源（见 :func:`guard_customer_claims`）。
    roles, guard_issues = guard_customer_claims(roles, utterances, corrections)
    issues.extend(guard_issues)
    return assemble_bundle(
        auc_result,
        roles,
        corrections,
        annotated=data is not None,
        issues=issues,
        model_text=payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
    )



def _utterances(auc_result: Any) -> list[dict[str, Any]]:
    """把 ``auc_result``（JSON 数组文本或其序列化形态）解析成逐句字典。

    解析不动就返回空列表：下游据此走「未标注」降级路径，原文照常进 L0。
    """
    if isinstance(auc_result, (list, tuple)):
        items = list(auc_result)
    else:
        text = str(auc_result or "").strip()
        if not text:
            return []
        try:
            items = json.loads(text)
        except ValueError:
            return []
    if not isinstance(items, list):
        return []
    return [item if isinstance(item, dict) else {"text": str(item)} for item in items]


def chunk_ranges(count: int, size: int = DEFAULT_CHUNK_SIZE) -> list[tuple[int, int]]:
    """把 ``0..count-1`` 切成左闭右开的块区间。"""
    if count <= 0:
        return []
    size = max(1, size)
    return [(i, min(i + size, count)) for i in range(0, count, size)]


def chunk_payload(
    utterances: list[dict[str, Any]], start: int, end: int
) -> str:
    """给 LLM 的块输入：只带判断角色必需的字段，``index`` 用全局句序号。

    情绪、语速、音量对判角色没有价值，且会显著推高输入长度，故不传。
    """
    rows = []
    for index in range(start, end):
        item = utterances[index]
        additions = item.get("additions") if isinstance(item.get("additions"), dict) else {}
        rows.append(
            {
                "index": index,
                "speaker": str(additions.get("speaker") or ""),
                "gender": str(additions.get("gender") or ""),
                "text": str(item.get("text") or ""),
            }
        )
    return json.dumps(rows, ensure_ascii=False)


def prior_hint(last_role: str | None, start: int) -> str:
    """块首的上下文提示。首块说明这是开头，其余块带上上一块末句的角色。"""
    if last_role is None:
        return "这是通话的开头。" if start == 0 else "这是通话的中间一段。"
    return (
        f"这是通话的中间一段，从第 {start} 句开始。"
        f"紧邻的上一段最后一句的角色是「{last_role}」，可作倾向参考，"
        "但若本段内容明确指向另一方，以内容为准。"
    )


def apply_segments(
    payload: Any,
    utterances: list[dict[str, Any]],
    start: int,
    end: int,
) -> tuple[dict[int, str], list[str]]:
    """把一块的 LLM 输出展开成 ``{句序号: 角色}``，返回（角色表，问题清单）。

    四条口径，都是为了让错误**不落到客户方**：

    - **角色非法**（含模型自造的词）→ 记 ``未知来源``，并留痕；
    - **越界分段**（``from``/``to`` 落在外段或本块之外）→ 裁到本块范围；
    - **重复覆盖** → 保留先出现的一段，后到的留痕；
    - **未被覆盖** → 补 ``未知来源``，留痕。缺口绝不继承上一段角色。
    """
    issues: list[str] = []
    segments = (payload or {}).get("segments") if isinstance(payload, dict) else None
    if not isinstance(segments, list) or not segments:
        issues.append("segments 缺失或为空，整块记未知来源")
        return {i: ROLE_UNKNOWN for i in range(start, end)}, issues

    roles: dict[int, str] = {}
    for segment in segments:
        if not isinstance(segment, dict):
            issues.append(f"非法分段：{segment!r}")
            continue
        try:
            lo = int(segment["from"])
            hi = int(segment["to"])
        except (KeyError, TypeError, ValueError):
            issues.append(f"分段缺少 from/to：{segment!r}")
            continue
        role = str(segment.get("role") or "").strip()
        role = LEGACY_ROLES.get(role, role)
        if role not in ROLES:
            issues.append(f"非法角色 {role!r}，按未知来源处理")
            role = ROLE_UNKNOWN
        for index in range(max(lo, start), min(hi, end - 1) + 1):
            if index in roles:
                issues.append(f"第 {index} 句重复覆盖，保留先出现的一段")
                continue
            roles[index] = role

    for index in range(start, end):
        if index not in roles:
            issues.append(f"第 {index} 句未被覆盖，补未知来源")
            roles[index] = ROLE_UNKNOWN
    return roles, issues


def guard_customer_claims(
    roles: dict[int, str],
    utterances: list[dict[str, Any]],
    corrections: dict[int, dict[str, str]],
) -> tuple[dict[int, str], list[str]]:
    """单向兜底：判成客户方、却在明说自家的句子，改判 ``未知来源``。

    「销售内容不得归给客户」是非对称代价——把销售的话记成客户，会直接写进画像；
    反过来只是少一条证据。提示词守不住时由这里补最后一层，规则取**高精度**的销售自述
    句式（「我们店」「给您报」等），宁可漏纠也不误杀客户的自述。

    只看改后正文：勘误把话改清楚之后仍命中，才更有把握。
    """
    issues: list[str] = []
    adjusted: dict[int, str] = dict(roles)
    for index, role in roles.items():
        if role != ROLE_CUSTOMER:
            continue
        fixed = corrections.get(index)
        text = str((fixed or {}).get("text") or utterances[index].get("text") or "")
        hit = next((marker for marker in SALES_SELF_MARKERS if marker in text), "")
        if hit:
            adjusted[index] = ROLE_UNKNOWN
            issues.append(f"第 {index} 句判为客户方但含销售自述「{hit}」，改判未知来源")
    return adjusted, issues


def apply_corrections(
    payload: Any,
    utterances: list[dict[str, Any]],
    start: int,
    end: int,
) -> tuple[dict[int, dict[str, str]], list[str]]:
    """收取勘误建议。下标越界、空正文或与原文相同的条目丢弃并留痕。

    **与原文相同**的条目没有信息量——实测模型会把「确认无误」也写进 corrections，
    原样保留会在正文后面挂一串空注释。
    """
    issues: list[str] = []
    result: dict[int, dict[str, str]] = {}
    corrections = (payload or {}).get("corrections") if isinstance(payload, dict) else None
    if not isinstance(corrections, list):
        return result, issues
    for item in corrections:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if not start <= index < end:
            issues.append(f"勘误下标 {index} 越界，丢弃")
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            issues.append(f"勘误下标 {index} 无正文，丢弃")
            continue
        original = str(utterances[index].get("text") or "").strip()
        if text == original:
            issues.append(f"勘误下标 {index} 与原文相同，丢弃")
            continue
        result[index] = {"text": text, "reason": str(item.get("reason") or "").strip()}
    return result, issues


def render(
    utterances: list[dict[str, Any]],
    roles: dict[int, str],
    corrections: dict[int, dict[str, str]],
) -> tuple[str, list[dict[str, Any]]]:
    """逐句渲染成**纯正文**，并把角色另存为句级数组。

    **正文里不放任何角色标记。** 早先的形态是 ``[序号|角色] 原文``，但它有两处硬伤：
    L1 抄锚点时会连标记一起抄进去，销售看到的证据就带着内部符号；而定位又要求锚点是
    原文的连续切片，标记一旦被抄进去就对不上。因此正文只留原话（勘误后的写法），
    角色按句号与字符区间单独交出，供 L1 输入装配与投影层的证据标注使用。

    返回 ``(正文, 句级角色表)``；句级角色表每项含 ``i`` / ``role`` / ``start`` / ``end``，
    区间是**该句在正文中的字符位置**（闭开区间），供反查「这条证据出自谁的口」。
    """
    lines: list[str] = []
    role_lines: list[dict[str, Any]] = []
    cursor = 0
    for index, item in enumerate(utterances):
        original = str(item.get("text") or "")
        fixed = corrections.get(index)
        body = fixed["text"] if fixed else original
        role_lines.append(
            {
                "i": index,
                "role": roles.get(index, ROLE_UNKNOWN),
                "start": cursor,
                "end": cursor + len(body),
            }
        )
        lines.append(body)
        cursor += len(body) + 1  # 换行符占一个字符
    return "\n".join(lines), role_lines


def role_counts(roles: dict[int, str]) -> dict[str, int]:
    return {role: sum(1 for value in roles.values() if value == role) for role in ROLES}


def assemble_bundle(
    auc_result: Any,
    roles: dict[int, str],
    corrections: dict[int, dict[str, str]],
    *,
    annotated: bool,
    issues: list[str],
    model_text: str = "",
) -> dict[str, Any]:
    """子流程的出口产物。

    ``content`` 是**纯正文**（勘误后的写法），``raw`` 是六字段原文，``role_lines`` 是句级
    角色与字符区间，``corrections`` 是勘误明细（供 L1 输入在渠道末尾汇总展示）。
    ``annotated=False`` 时 ``content`` 回落到原文、``role_lines`` 为空——标注失败不阻断
    录音链路，下游只会少掉角色线索。
    """
    utterances = _utterances(auc_result)
    if annotated:
        content, role_lines = render(utterances, roles, corrections)
    else:
        content, role_lines = str(auc_result or ""), []
    bundle = {
        "content": content,
        "raw": str(auc_result or ""),
        "annotated": bool(annotated),
        "roles": role_counts(roles),
        "role_lines": role_lines,
        "corrections": [
            {"i": index, "text": item["text"], "reason": item["reason"]}
            for index, item in sorted(corrections.items())
        ],
        "issues": issues[:20],
    }
    return {
        "transcript_bundle": json.dumps(bundle, ensure_ascii=False),
        "annotated": "true" if annotated else "false",
        "role_summary": json.dumps(bundle["roles"], ensure_ascii=False),
        "model_text": model_text,
    }
