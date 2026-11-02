"""知识库页面的只读返回体（K6）。

与 ``/rules``、``/standards`` 同一定位：**只读、不依赖服务实例、进程常量现拼**。
知识包是启动期装载的常量，与运行、数据库都无关，因此这里不调 ``get_service()``——
服务尚未就绪时也能回答「当前系统认的在售车型是哪几款」。

**没有写接口，这是定论不是第一版。** 要改内容就改文件重启，页面刷新一次即可。

一个刻意的取舍：价格**同时**给出主力价格带与全系包络。只显示包络会让人以为
「917 卖 110 万」，只显示主力带又与结构校验的口径对不上。两者并列、并附上
``price.note`` 原文，看页面的人才知道真实口径。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from . import current_pack
from .pack import EMPTY_PACK, ModelEntry, Pack

STATUS_LABELS = {"on_sale": "在售", "coming": "待上市", "discontinued": "已停售"}
POWERTRAIN_LABELS = {"bev": "纯电", "phev": "插电混动", "erev": "增程", "ice": "燃油"}


def build_payload(pack: Pack | None = None) -> dict[str, Any]:
    """页面接口的返回体。装载失败时返回空包形态而不是 500。"""
    resolved = pack or current_pack()
    if resolved is EMPTY_PACK or resolved.is_empty:
        return {
            "available": False,
            "reason": "未配置知识库或知识包装载失败",
            "pack_id": "",
            "display_name": "",
            "models": [],
            "tiers": [],
        }

    manifest = resolved.manifest
    return {
        "available": True,
        "reason": "",
        "pack_id": manifest.pack_id,
        "display_name": manifest.display_name,
        "vendor": manifest.vendor,
        "effective_from": manifest.effective_from,
        "effective_to": manifest.effective_to,
        "days_since_effective": _days_since(manifest.effective_from),
        "note": manifest.note,
        "content_signature": resolved.content_signature,
        "brands": [
            {
                "canonical": resolved.brand.our_brand.canonical,
                "aliases": list(resolved.brand.our_brand.aliases),
            }
        ],
        "competitors": [item.canonical for item in resolved.brand.competitors],
        "tiers": [
            {
                "tier": tier.tier,
                "label": tier.label,
                "models": list(tier.models),
                "budget_min_wan": tier.budget_min_wan,
                "budget_max_wan": tier.budget_max_wan,
            }
            for tier in manifest.tiers
        ],
        "models": [_model_row(item, resolved) for item in resolved.models],
        "total_models": len(resolved.models),
        "total_configs": sum(len(item.configs) for item in resolved.models),
        "competitor_library": _competitor_section(),
    }


def _competitor_section() -> dict[str, Any]:
    """竞品库概况。**未启用时返回 ``available: false``，不是缺字段。**

    页面要能回答「有没有竞品资料」这件事本身——缺字段会让人分不清「没配」与
    「配了但装载失败」。两者都用 ``available`` + ``reason`` 表达。
    """
    from . import competitors as kb

    library = kb.current_competitors()
    if library.is_empty:
        configured = str(
            getattr(kb.get_settings(), "knowledge_competitors", "") or ""
        ).strip()
        return {
            "available": False,
            "library_id": configured,
            "reason": (
                "未配置竞品库（KNOWLEDGE_COMPETITORS 为空）"
                if not configured
                else f"竞品库 {configured!r} 装载失败"
            ),
            "entries": [],
            "total": 0,
        }
    return {
        "available": True,
        "library_id": library.library_id,
        "reason": "",
        "effective_from": library.manifest.effective_from,
        "content_signature": library.content_signature,
        "note": library.manifest.note,
        "total": len(library.entries),
        "entries": [
            {
                "model_id": item.model_id,
                "canonical_name": item.canonical_name,
                "brand": item.brand,
                "aliases": list(item.aliases),
                "powertrain": list(item.powertrain),
                "powertrain_labels": [
                    POWERTRAIN_LABELS.get(x, x) for x in item.powertrain
                ],
                "body": item.body,
                "price": {
                    "min_wan": item.price.min_wan,
                    "max_wan": item.price.max_wan,
                    "as_of": item.price.as_of,
                    "note": item.price.note,
                },
                "positioning": item.positioning,
                "strengths": [
                    {"title": p.title, "text": p.text} for p in item.strengths
                ],
                "gaps": [{"title": p.title, "text": p.text} for p in item.gaps],
                "talking_points": [
                    {"title": p.title, "text": p.text}
                    for p in item.talking_points
                ],
                "source": item.source,
                "updated_at": item.updated_at,
            }
            for item in library.entries
        ],
    }


def _days_since(effective_from: str) -> int | None:
    try:
        start = date.fromisoformat(effective_from)
    except (ValueError, TypeError):
        return None
    return (date.today() - start).days


def _model_row(entry: ModelEntry, pack: Pack) -> dict[str, Any]:
    tier = pack.tier_of(entry.model_id)
    price = entry.price
    return {
        "model_id": entry.model_id,
        "canonical_name": entry.canonical_name,
        "aliases": list(entry.aliases),
        "asr_variants": list(entry.asr_variants),
        "series": entry.series,
        "status": entry.status,
        "status_label": STATUS_LABELS.get(entry.status, entry.status),
        "tier": tier.label if tier is not None else "",
        "price": {
            "min_wan": price.min_wan,
            # 主力价格带与全系包络并列：页面必须能同时看到这两个口径
            "core_max_wan": price.core_max,
            "max_wan": price.max_wan,
            "as_of": price.as_of,
            "note": price.note,
            "has_extended": price.max_wan > price.core_max + 0.01,
        },
        "powertrain": list(entry.powertrain),
        "powertrain_labels": [
            POWERTRAIN_LABELS.get(item, item) for item in entry.powertrain
        ],
        "body": entry.body,
        "seats": entry.seats,
        "audience": list(entry.audience),
        "scenarios": list(entry.scenarios),
        "positioning": entry.positioning,
        "highlights": [
            {"title": item.title, "text": item.text} for item in entry.highlights
        ],
        "speech": entry.speech,
        "configs": [
            {
                "name": item.name,
                "price_wan": item.price_wan,
                "promo_price_wan": item.promo_price_wan,
                "powertrain_labels": [
                    POWERTRAIN_LABELS.get(key, key) for key in item.powertrain
                ],
                "drive": item.drive,
                "range_km": item.range_km,
                "note": item.note,
            }
            for item in entry.configs
        ],
    }
