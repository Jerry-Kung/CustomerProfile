"""知识包审计：装载之后的「语义完整性」检查（T3-2）。

与 :func:`customer_profile.knowledge.pack.load_pack` 的分工：

- **装载期校验**回答「这份文件是不是合法的知识包」——形状、唯一性、区间自洽，
  任何一条不过就抛，因为坏数据不该进内存。
- **审计**回答「这份合法的知识包，有没有写了却用不上的东西」。这些问题不阻断装载：
  包能用，只是某部分永远不生效。它们属于**报出来给人看**，不属于抛异常。

四类「死知识」，每类都对应一个真实的失效方式：

1. **空分层**——`tiers` 里写了一层却没有车型。产品矩阵图会空一格，销售看到的
   「产品梯队」少一个价位段，而代码里没有任何地方会报错。
2. **分层预算带与车型价格带无交集**——按预算筛车时这一层永远选不到。最常见的原因是
   改价格时忘了改分层的 `budget_min_wan` / `budget_max_wan`。
3. **称谓解析不回自身**——条目声称客户会说「某某」，但四级解析匹配不上它。
   这类错会让 K5 把客户明说的本方车型判成「陌生称谓」，转而要求人工确认。
4. **分层仍在引用停售车型**——`status: discontinued` 的车还挂在在售矩阵里。
   它不会再被推荐（K4 跳过停售车），但分层声明与实际口径矛盾。

**审计不判「信息够不够丰富」**：卖点只有两条、缺车身尺寸，都不算错。知识库的价值在答得准，
不在答得多；把「不够详细」也算成问题，会逼出一堆为通过检查而写的填充内容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .pack import Pack
from .resolve import resolve_model


@dataclass(frozen=True, slots=True)
class Finding:
    """一条审计发现。``kind`` 是机器可判的类别，``detail`` 是给人看的话。"""

    kind: str
    detail: str
    subject: str = ""
    """出问题的对象（车型 ID 或分层 ID），便于按条目聚合。"""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class AuditReport:
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.findings

    def by_kind(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = {}
        for item in self.findings:
            out.setdefault(item.kind, []).append(item)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "count": len(self.findings),
            "findings": [item.as_dict() for item in self.findings],
        }


def audit_pack(pack: Pack) -> AuditReport:
    """跑完全部审计项。**空包返回空报告**——「没配知识」不是缺陷，是刻意的回滚口。"""
    if pack.is_empty:
        return AuditReport()

    findings: list[Finding] = []
    findings.extend(_audit_tiers(pack))
    findings.extend(_audit_names(pack))
    return AuditReport(findings=tuple(findings))


def _audit_tiers(pack: Pack) -> list[Finding]:
    out: list[Finding] = []
    for tier in pack.manifest.tiers:
        if not tier.models:
            out.append(
                Finding(
                    kind="empty_tier",
                    subject=tier.tier,
                    detail=f"分层「{tier.label}」（{tier.tier}）没有挂任何车型，产品矩阵会空一格",
                )
            )
            continue

        for model_id in tier.models:
            entry = pack.model(model_id)
            if entry is None:
                # 悬空引用已由装载期校验拦下，这里只是不再重复报
                continue
            if entry.status == "discontinued":
                out.append(
                    Finding(
                        kind="discontinued_in_tier",
                        subject=model_id,
                        detail=(
                            f"分层「{tier.label}」仍挂着已停售的 {entry.canonical_name}"
                            "——它不会被推荐，但矩阵声明与实际口径矛盾"
                        ),
                    )
                )

        bounds = _tier_bounds(tier)
        if bounds is None:
            continue
        low, high = bounds
        if not any(
            _overlaps(pack.model(model_id), low, high)
            for model_id in tier.models
            if pack.model(model_id) is not None
        ):
            out.append(
                Finding(
                    kind="tier_budget_gap",
                    subject=tier.tier,
                    detail=(
                        f"分层「{tier.label}」声明的预算带 {low:g}–{high or '∞'} 万"
                        "与本层全部车型的价格带都不重叠——按预算筛车时这一层永远选不到"
                    ),
                )
            )
    return out


def _audit_names(pack: Pack) -> list[Finding]:
    """每个可被客户说出口的称谓，都必须能被 :func:`resolve_model` 解析回自己。"""
    out: list[Finding] = []
    for entry in pack.models:
        for name in entry.all_names():
            hit = resolve_model(name, pack)
            if hit != entry.model_id:
                out.append(
                    Finding(
                        kind="unresolvable_alias",
                        subject=entry.model_id,
                        detail=(
                            f"{entry.canonical_name} 声称客户会说「{name}」，"
                            f"但解析结果是 {hit or '未命中'}"
                            "——K5 会把它判成陌生称谓并要求人工确认"
                        ),
                    )
                )
    return out


def _tier_bounds(tier: Any) -> tuple[float, float] | None:
    low = tier.budget_min_wan
    if low is None:
        return None
    high = tier.budget_max_wan
    return float(low), float("inf") if high is None else float(high)


def _overlaps(entry: Any, low: float, high: float) -> bool:
    if entry is None:
        return False
    return entry.price.min_wan <= high and entry.price.max_wan >= low
