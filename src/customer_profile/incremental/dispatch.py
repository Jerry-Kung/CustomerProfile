"""增量调度的纯代码部分：读基线、定档、组装增量上下文、T0 副本换批次号。

**为什么档位判定全在代码里**：任务侧不带增量语义，是否增量由我们自判（``01`` §7），
因此「这次为什么走全量」只能从这里的产物回答。判定结果、差分计数、兼容键比对结果
作为节点输出留在运行树上，就是审计留痕。

P2 起放开 **T1 / T2**：判据命中即带增量上下文交给现行管线；``enabled_tiers`` 仍是
灰度口，把 ``T1``/``T2`` 摘掉即回到「一律落 T3」（``03`` T2-7 的回滚方式）。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from ..persistence.profile_baselines import ProfileBaseline
from . import context as ctx_module

TIER_T0 = "T0"
TIER_T1 = "T1"
TIER_T2 = "T2"
TIER_T3 = "T3"

TIER_LABEL = {
    TIER_T0: "无变化，原样回传",
    TIER_T1: "文本增量",
    TIER_T2: "含录音增量",
    TIER_T3: "全量重建",
}
"""档位的中文名。只进运行台与日志，**不进对客产物**（``01`` §7）。"""

DEFAULT_TIERS: tuple[str, ...] = (TIER_T0, TIER_T1, TIER_T2)

TEST_DRIVE_CHANNEL = "TestDriveRecordingFile"
AUDIO_CHANNEL_SUFFIX = "RecordingFile"


def _rows(history_body: Any) -> list[dict]:
    """把 history 响应解析成行列表。解析失败按「无记录」处理，与 ``_manifest_index`` 同口径。"""
    if history_body is None:
        return []
    if isinstance(history_body, (list, tuple)):
        return [row for row in history_body if isinstance(row, dict)]
    text = str(history_body).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        return []
    return [row for row in parsed if isinstance(row, dict)] if isinstance(parsed, list) else []


def _callback_text(baseline: ProfileBaseline, batch_id: str) -> str:
    """T0：副本原样回传，只换 ``batch_id``。

    其余键一个字节都不动——这正是「不变则不变」的兑现方式；重造一遍回传体就必然
    带上模型随机性（实测同输入两次全量平均只有 66.4% 字段一致）。
    """
    payload = dict(baseline.callback)
    payload["batch_id"] = str(batch_id or "")
    return json.dumps(payload, ensure_ascii=False)


def stage_of(rows: Iterable[dict]) -> str:
    """记录里是否存在**有实质内容**的试驾录音。

    与管线 :func:`level_stage` 同判据（``_has_test_drive``）：渠道对、状态 ok、正文非空。
    P1 时期这里只看渠道名，空录音也会被判成「已进商机阶段」；P2 接上记录层后按正文判，
    空录音、转写失败都不再误触发 ``01`` §4 的阶段跃迁。
    """
    for row in rows:
        if str(row.get("channel") or "") != TEST_DRIVE_CHANNEL:
            continue
        if str(row.get("status") or "").strip() not in ("", "ok"):
            continue
        raw = row.get("raw_payload")
        if isinstance(raw, Mapping) and raw.get("operation") == "removed":
            continue
        # 清单行没有「正文」字段；有 media_urls 即视为有一条录音记录。是否真有内容由
        # 管线的记录层（转写结果非空）判定——那里拿得到逐字原文，这里拿不到。
        if row.get("media_urls") or (isinstance(raw, Mapping) and raw.get("recording_url")):
            return "opportunity"
        if isinstance(raw, Mapping) and raw:
            return "opportunity"
    return "lead"


def level_of(callback: Mapping[str, Any]) -> tuple[str, str]:
    """从回传体里取上一版等级与它的推理链（后者原样转文本，供定级锚点）。"""
    analysis = (callback or {}).get("analysis_result")
    if not isinstance(analysis, Mapping):
        return "", ""
    level = ((analysis.get("ai_insight") or {}).get("purchase_intent_level") or {})
    value = str(level.get("value") or "")
    detail = level.get("reasoning_detail") or {}
    return value, json.dumps(detail, ensure_ascii=False) if detail else ""


def _new_parts(delta: Mapping[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """从差分结果取出「新记录的物理键 / 逻辑键 / 渠道」。

    基线只存物理键，改版（版本递增）在本轮表现为「旧键消失 + 新键出现」。因此这里把
    ``added`` 一律当新记录；``changed`` 在当前差分实现下恒空，保留位置备改版判定接入。
    """
    records = list(delta.get("added_records") or []) + list(delta.get("changed_records") or [])
    keys: list[str] = []
    logical: list[str] = []
    channels: list[str] = []
    from .diff import logical_key, record_key

    for row in records:
        keys.append(record_key(row))
        logical.append(logical_key(row))
        channels.append(str(row.get("channel") or ""))
    return keys, logical, channels


def decide(
    *,
    history_body: Any,
    phone: str,
    batch_id: str,
    baseline: ProfileBaseline | None,
    current_compat_key: str,
    profile_body: Any = None,
    enabled_tiers: tuple[str, ...] = DEFAULT_TIERS,
) -> dict[str, Any]:
    """档位判定（纯函数）。返回节点出参字典。

    判定顺序自上而下、先命中先生效（``01`` §4）：无基线 → T3；兼容键不符 → T3；
    键集合无差异 → T0；新增含试驾录音 → T3（阶段跃迁）；含其他录音 → T2；仅文本 → T1。
    ``enabled_tiers`` 之外的档位落 T3 并写明理由。
    """
    from .diff import diff_by_keys

    rows = _rows(history_body)
    delta = diff_by_keys(baseline.consumed_keys if baseline else None, rows)
    compat_ok = bool(baseline) and baseline.compat_key == current_compat_key

    if baseline is None:
        tier, reason = TIER_T3, "无基线：首次运行或基线缺失"
    elif not compat_ok:
        tier, reason = TIER_T3, "管线兼容键不符：契约/规则/板块已变更，旧结论不可沿用"
    elif not delta["added"] and not delta["removed"]:
        tier, reason = TIER_T0, "记录身份键集合无差异"
    else:
        channels = {
            str(row.get("channel") or "")
            for row in delta["added_records"]
        }
        if TEST_DRIVE_CHANNEL in channels:
            tier, reason = TIER_T3, "首次出现试驾录音：阶段跃迁"
        elif any(ch.endswith(AUDIO_CHANNEL_SUFFIX) for ch in channels):
            tier, reason = TIER_T2, "新增记录含录音渠道"
        else:
            tier, reason = TIER_T1, "新增记录仅文本"

    if tier not in enabled_tiers:
        reason = f"{reason}；该档本期未放开，按全量处理"
        tier = TIER_T3

    callback = ""
    if tier == TIER_T0 and baseline is not None:
        callback = _callback_text(baseline, batch_id)

    stage = stage_of(rows)
    level, level_detail = ("", "")
    if baseline is not None:
        level = baseline.deal_level
        level_detail = baseline.deal_level_detail

    incremental_ctx = ""
    base_stats_json = ""
    if tier in (TIER_T1, TIER_T2) and baseline is not None:
        incremental_ctx, base_stats_json = _build_ctx(
            baseline=baseline,
            delta=delta,
            rows=rows,
            profile_body=profile_body,
            previous_level=level,
            previous_level_detail=level_detail,
        )

    return {
        "tier": tier,
        "tier_label": TIER_LABEL[tier],
        "tier_reason": reason,
        "baseline_found": "true" if baseline is not None else "false",
        "compat_ok": "true" if compat_ok else "false",
        "added_count": len(delta["added"]),
        "removed_count": len(delta["removed"]),
        "unchanged_count": len(delta["unchanged"]),
        "touched_channels": ",".join(
            sorted({str(row.get("channel") or "") for row in delta["added_records"]})
        ),
        "stage": stage,
        "previous_level": level,
        "previous_level_detail": level_detail,
        "callback_json": callback,
        "incremental_ctx": incremental_ctx,
        # 底座组装的三条规则各命中几次（same / differ / from_b_only）。灰度期统计
        # 「补缺字段数」靠它：补缺比例过高说明接口画像持续读不全，不是增量的功劳。
        "base_stats_json": base_stats_json,
    }


def _build_ctx(
    *,
    baseline: ProfileBaseline,
    delta: Mapping[str, Any],
    rows: list[dict],
    profile_body: Any,
    previous_level: str,
    previous_level_detail: str,
) -> tuple[str, str]:
    """组装增量上下文：新记录键 + 上一版已定稿事实 + 上一版等级。

    返回 ``(上下文, 底座统计)``。底座组装失败时**都返回空串**：管线随即按全量跑，只是
    这次没省下成本——比拿着半份底座算出一份错误的画像强。统计为空也说明「这一轮没有
    底座可用」，与「补缺为 0」是两件事，不要混在一起读。
    """
    from .base_claims import assemble_base_claims, base_facts_signature

    try:
        base = assemble_base_claims(
            profile_body,
            baseline.callback,
            updated_at_ms=baseline.updated_at_ms,
        )
    except Exception:  # noqa: BLE001 —— 底座坏了退回全量，不阻断运行
        return "", ""
    stats = json.dumps(base.get("stats") or {}, ensure_ascii=False)

    keys, logical, channels = _new_parts(delta)
    alive = sorted({str(row.get("channel") or "") for row in rows if row.get("channel")})
    facts = base.get("facts") or {}
    text = ctx_module.dumps(
        ctx_module.build(
            new_keys=keys,
            new_logical=logical,
            new_channels=channels,
            base_fields=base.get("fields") or [],
            base_facts=facts,
            base_sections=base.get("sections") or {},
            previous_level=previous_level,
            previous_level_detail=previous_level_detail,
            base_updated_at=_iso(baseline.updated_at_ms),
            facts_sig=base_facts_signature(facts),
            alive_channels=alive,
        )
    )
    return text, stats


def _iso(ms: int) -> str:
    if not ms:
        return ""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M:%S")
