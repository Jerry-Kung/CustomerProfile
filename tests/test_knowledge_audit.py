"""知识包审计（T3-2）：文件合法之后的「语义完整性」。

与 ``test_knowledge_pack`` 的分工：那里钉装载期校验（不合法就抛），这里钉**审计**
（包合法但有一部分永远不生效）。两类问题的处置不同，因此分两个文件：

- 装载期问题让管线拿不到知识 → 必须抛；
- 审计问题是「写了却用不上」 → 只报给人看，不阻断。

四类死知识各配一个反例。审计的价值全在「能不能发现」，所以每条都要有一个会红的构造。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from customer_profile.knowledge.audit import audit_pack
from customer_profile.knowledge.pack import EMPTY_PACK, load_pack

DEFAULT_PACK_DIR = Path(__file__).resolve().parents[1] / "src" / "customer_profile" / "knowledge" / "packs"


# ---------------------------------------------------------------- 夹具

def _model(model_id="m-1", name="测试车A", price=(10.0, 20.0), status="on_sale"):
    return {
        "model_id": model_id,
        "canonical_name": name,
        "aliases": [],
        "series": "A",
        "status": status,
        "price": {"min_wan": price[0], "max_wan": price[1], "as_of": "2026-10-29"},
        "powertrain": ["phev"],
        "configs": [{"name": "标准版", "price_wan": price[0]}],
        "positioning": "定位",
        "highlights": [{"title": "卖点"}],
        "speech": "话术",
    }


def _pack(tmp_path: Path, models, *, tiers=None):
    root = tmp_path / "pack"
    root.mkdir(parents=True, exist_ok=True)
    (root / "models.json").write_text(json.dumps(models, ensure_ascii=False), encoding="utf-8")
    (root / "brand.json").write_text(
        json.dumps({"our_brand": {"canonical": "测试品牌"}, "competitors": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    manifest = {
        "pack_id": "test-pack",
        "display_name": "测试",
        "vendor": "测试方",
        "brands": ["测试品牌"],
        "effective_from": "2026-10-29",
    }
    if tiers is not None:
        manifest["tiers"] = tiers
    (root / "pack.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return load_pack(root)


# ---------------------------------------------------------------- 正式包

def test_shipped_pack_passes_audit():
    """仓库里的正式包必须干净——它同时是审计「不误报」的基准。"""
    report = audit_pack(load_pack(DEFAULT_PACK_DIR / "dfms-mengshi"))
    assert report.ok, [f.detail for f in report.findings]


def test_empty_pack_is_not_a_finding():
    """空包表示「未启用知识」，是刻意的回滚口，不是缺陷。"""
    report = audit_pack(EMPTY_PACK)
    assert report.ok
    assert report.findings == ()


# ---------------------------------------------------------------- 四类死知识

def test_empty_tier_is_reported(tmp_path):
    """规则：分层写了却没挂车型 → 产品矩阵空一格，代码没有任何地方会报错。

    **夹具必须另配一层把车型挂上**：装载期规则 9 要求每款车至少被一层覆盖，只有一个
    空分层的包根本装不进内存——那种情况下报错的是装载期，轮不到审计。
    真实场景也正是这样：新开了一个价位段先占位，车型还没入。
    """
    pack = _pack(
        tmp_path,
        [_model()],
        tiers=[
            {"tier": "t", "label": "空层", "models": []},
            {"tier": "main", "label": "主销", "models": ["m-1"],
             "budget_min_wan": 5, "budget_max_wan": 25},
        ],
    )
    kinds = {f.kind for f in audit_pack(pack).findings}
    assert "empty_tier" in kinds


def test_tier_budget_gap_is_reported(tmp_path):
    """规则：分层预算带与本层车型价格带无交集 → 按预算筛车永远选不到它。

    这是改价格时忘了改分层预算的典型后果。
    """
    pack = _pack(
        tmp_path,
        [_model(price=(10.0, 20.0))],
        tiers=[{"tier": "t", "label": "L", "models": ["m-1"],
                "budget_min_wan": 60, "budget_max_wan": 90}],
    )
    report = audit_pack(pack)
    assert [f.kind for f in report.findings] == ["tier_budget_gap"]


def test_overlapping_budget_is_not_reported(tmp_path):
    """反例：预算带与价格带有交集就不该报——审计不是一刀切。"""
    pack = _pack(
        tmp_path,
        [_model(price=(10.0, 20.0))],
        tiers=[{"tier": "t", "label": "L", "models": ["m-1"],
                "budget_min_wan": 15, "budget_max_wan": 25}],
    )
    assert audit_pack(pack).ok


def test_unresolvable_alias_is_reported(tmp_path):
    """规则：条目声称客户会说某个称谓，解析却回不到自己。

    这类错会让 K5 把客户明说的本方车型判成陌生称谓、转而要求人工确认。

    **怎么造出来**：装载期的别名唯一性检查会跳过归一化后为空的称谓（``if not key:
    continue``），因此一个纯空白的别名能通过装载，却永远解析不命中——这正是审计
    要比装载更严的那一寸。
    """
    model = _model()
    model["aliases"] = ["   "]  # 归一化后为空，装载期不拦
    pack = _pack(tmp_path, [model])
    findings = audit_pack(pack).findings
    assert [f.kind for f in findings] == ["unresolvable_alias"]
    assert "K5" in findings[0].detail


def test_discontinued_model_in_tier_is_reported(tmp_path):
    """规则：分层仍挂着已停售车型 → 它不会被推荐，但矩阵声明与实际口径矛盾。"""
    pack = _pack(
        tmp_path,
        [_model(status="discontinued")],
        tiers=[{"tier": "t", "label": "L", "models": ["m-1"],
                "budget_min_wan": 5, "budget_max_wan": 25}],
    )
    kinds = {f.kind for f in audit_pack(pack).findings}
    assert "discontinued_in_tier" in kinds


# ---------------------------------------------------------------- 报告形态

def test_report_shape_is_stable(tmp_path):
    """报告要能被预检与页面直接消费，形状固定。"""
    pack = _pack(
        tmp_path,
        [_model()],
        tiers=[
            {"tier": "t", "label": "空层", "models": []},
            {"tier": "main", "label": "主销", "models": ["m-1"],
             "budget_min_wan": 5, "budget_max_wan": 25},
        ],
    )
    payload = audit_pack(pack).as_dict()
    assert set(payload) == {"ok", "count", "findings"}
    assert payload["count"] == len(payload["findings"])
    for item in payload["findings"]:
        assert set(item) == {"kind", "subject", "detail"}


def test_by_kind_groups_findings(tmp_path):
    pack = _pack(
        tmp_path,
        [_model()],
        tiers=[
            {"tier": "a", "label": "空A", "models": []},
            {"tier": "b", "label": "空B", "models": []},
            {"tier": "main", "label": "主销", "models": ["m-1"],
             "budget_min_wan": 5, "budget_max_wan": 25},
        ],
    )
    grouped = audit_pack(pack).by_kind()
    assert len(grouped["empty_tier"]) == 2
