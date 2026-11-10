"""合并账本：底座断言 + 本轮新断言 → 一份可直接喂 ``project_claims`` 的账本。

三件事（``01`` §6.2 / ``02`` §1.4）：

1. **退场**：源记录已被移除的底座断言不再进入账本——它依赖的那条记录不存在了；
2. **取舍**：同字段的底座断言与新断言按 :mod:`customer_profile.rules.clusters.recency`
   判谁留下（进程字段后到者胜，稳态字段弱信号不翻旧结论）；
3. **标注**：每条补 ``observed_at``（底座取基线更新时间，新断言取记录业务时间）。

**不做冲突消解**——同一字段有多条断言时怎么取，是投影层的事。本模块只决定「哪些断言
有资格进入投影」，不做取值裁决，因此 :func:`merge_claims` 的产物与 ``localize_claims``
完全同形。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

from ..rules.clusters import recency as recency_rules
from .base_claims import BASE_CHANNEL

REPORT_LIMIT = 200
"""留痕条目上限。合并账本的 trace 进运行台，不让它无限膨胀。"""


def _payload(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None
    return value


def _claims(value: Any) -> list[dict]:
    data = _payload(value)
    if isinstance(data, dict):
        data = data.get("fields", data.get("claims"))
    if not isinstance(data, (list, tuple)):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _removed_attributes(
    base_claims: Sequence[dict],
    *,
    base_records: Sequence[dict] | None,
    current_rows: Sequence[dict],
) -> dict[str, str]:
    """底座断言来自哪条记录、那条记录是否已从本轮数据里消失。

    「哪些底座断言该退场」不能只看字段名：一条断言依赖的渠道记录被上游删除后，这条断言
    就失去了依据（``01`` §5「键消失视为移除，依赖该记录的结论退场」）。

    底座断言没有存记录键（基线只存**已消费键集合**，断言与键的对应关系在 ``basic_notes``
    的渠道名里）。因此判据退回**渠道层**：底座断言记录的渠道若整条从本轮清单里消失，
    该渠道上的底座断言退场。``base_records`` 为 ``None`` 时不判（无渠道信息）。
    """
    if not base_records:
        return {}
    alive_channels = {str(row.get("channel") or "") for row in current_rows if isinstance(row, dict)}
    gone: dict[str, str] = {}
    for claim in base_claims:
        channel = str(claim.get("record_channel") or "").strip()
        if not channel:
            continue
        manifest = _channel_key_of(channel)
        if manifest and manifest not in alive_channels:
            gone.setdefault(channel, manifest)
    return gone


def _channel_key_of(source_name: str) -> str:
    """断言里的渠道名（中文来源名）→ 清单渠道键。取不到返回空串（判据不生效）。

    从管线渠道表读而不是另抄一份：两处口径一旦分叉，退场判据就会把还在的渠道判成已删。
    """
    from ..workflows import customer_profile_pipeline as pipeline

    for spec in pipeline.CHANNELS:
        if spec["source_name"] == source_name:
            return str(spec.get("manifest_channel") or "")
    return ""


def _observed_of(claim: Mapping[str, Any], default: str) -> str:
    return str(claim.get("observed_at") or default or "")


def merge_claims(
    *,
    base_fields: Any,
    new_fields: Any,
    current_rows: Sequence[dict] | None = None,
    base_updated_at: str = "",
    disabled_rules: Iterable[str] = (),
) -> dict[str, Any]:
    """合并账本。

    ``base_fields`` 是 :func:`customer_profile.incremental.base_claims.assemble_base_claims`
    的 ``fields_json``（或 ``{"fields": [...]}``）；``new_fields`` 是本轮 L1 定坐标后的
    ``claims_json``。``current_rows`` 是本轮 history 行，用于判断渠道退场。
    """
    base = _claims(base_fields)
    new = _claims(new_fields)
    rows = list(current_rows or [])
    off = {str(item).strip() for item in disabled_rules if str(item).strip()}

    gone = _removed_attributes(base, base_records=rows or None, current_rows=rows)
    retired: list[dict] = []
    kept_base: list[dict] = []
    for claim in base:
        channel = str(claim.get("record_channel") or "").strip()
        if channel in gone:
            retired.append(
                {
                    "attribute": claim.get("attribute"),
                    "value": claim.get("value"),
                    "channel": channel,
                    "reason": f"渠道 {channel} 的本轮数据已不存在，依赖它的上一版结论退场",
                }
            )
            continue
        kept_base.append(dict(claim))

    by_attr: dict[str, list[dict]] = {}
    for claim in kept_base:
        by_attr.setdefault(str(claim.get("attribute") or ""), []).append(claim)

    new_by_attr: dict[str, list[dict]] = {}
    for claim in new:
        attribute = str(claim.get("attribute") or "").strip()
        if not attribute:
            continue
        new_by_attr.setdefault(attribute, []).append(dict(claim))

    merged: list[dict] = []
    trace: list[dict] = []
    order = sorted(set(by_attr) | set(new_by_attr))
    for attribute in order:
        base_group = by_attr.get(attribute) or []
        new_group = new_by_attr.get(attribute) or []
        if base_group and len(base_group) == 1:
            # rule: RECENT-01 稳态字段：新弱信号不翻旧强事实
            # rule: RECENT-02 进程字段：同档后到者胜，弱档须带推翻标注
            decision = recency_rules.decide_field(
                attribute=attribute,
                base_claim=base_group[0],
                new_claims=new_group,
                disabled=off,
            )
            trace.extend(decision["trace"])
            for claim in decision["claims"]:
                claim = dict(claim)
                claim["observed_at"] = _observed_of(claim, base_updated_at)
                merged.append(claim)
            continue
        # 底座同一字段有多条（多值/多来源）：不做字段级取舍，全部放行，交给投影层。
        for claim in base_group:
            claim = dict(claim)
            claim["observed_at"] = _observed_of(claim, base_updated_at)
            merged.append(claim)
        for claim in new_group:
            claim = dict(claim)
            claim["observed_at"] = _observed_of(claim, "")
            merged.append(claim)

    merged.sort(key=lambda c: (str(c.get("attribute") or ""), str(c.get("observed_at") or ""), str(c.get("record_channel") or "")))
    return {
        "claims_json": json.dumps({"fields": merged}, ensure_ascii=False, default=str),
        "total": len(merged),
        "base_count": len(kept_base),
        # ``new_count`` 按「本轮断言里有多少条最终进了账本」算：直接数
        # ``new_by_attr`` 会漏掉被时近性规则挡下的那些，那不是「进来多少」而是
        # 「提出多少」。这里用原始条数减去被替代的条数。
        "new_count": sum(
            len(new_by_attr.get(attr) or [])
            for attr in order
            if any(c.get("record_channel") != BASE_CHANNEL for c in merged if c.get("attribute") == attr)
        ),
        "retired": len(retired),
        "retired_json": json.dumps(retired, ensure_ascii=False, default=str),
        "rule_trace_json": json.dumps(trace[:REPORT_LIMIT], ensure_ascii=False, default=str),
        "rule_trace_count": len(trace),
    }

