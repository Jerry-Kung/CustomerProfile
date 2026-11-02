"""竞品库（第 4 期骨架）：装载、校验、解析、视图接入。

## 这一期做了什么、没做什么

**做了**：结构、校验、解析、K4 接入、`/knowledge` 暴露。全部可在无竞品资料的情况下
测通——默认配置为空串，行为与没有这个模块时逐字节一致。

**没做**：竞品内容本身。它需要业务方提供资料来源（``docs/feature-knowledge-base/07``
Q8），目前仓库里只有一处「与享界G9同源」的技术来源描述，不构成竞品情报。

## 三条最容易被写错的判据

1. **空库输出空串**——这是整个竞品能力的回滚口；
2. **竞品库与知识包分开配置**——换合作方时竞品库不该跟着换；
3. **竞品资料不是事实**——视图必须自述这一点，否则模型会把竞品参数写成客户说过的话。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from customer_profile.knowledge import competitors as kb
from customer_profile.knowledge import views


# ---------------------------------------------------------------- 夹具

def _entry(**overrides):
    base = {
        "model_id": "c-1",
        "canonical_name": "测试竞品A",
        "brand": "测试厂",
        "aliases": ["竞品A"],
        "price": {"min_wan": 25.0, "max_wan": 35.0, "as_of": "2026-10-30"},
        "powertrain": ["phev"],
        "body": "SUV",
        "positioning": "一句话定位",
        "strengths": [{"title": "轴距", "text": "3 米"}],
        "gaps": [{"title": "无纯电", "text": "只有插混"}],
        "talking_points": [{"title": "对比点", "text": "按需说明"}],
    }
    base.update(overrides)
    return base


def _library(tmp_path: Path, entries, *, manifest=None):
    root = tmp_path / "lib"
    root.mkdir(parents=True, exist_ok=True)
    (root / "competitors.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )
    data = {
        "library_id": "test-lib",
        "display_name": "测试竞品库",
        "effective_from": "2026-10-30",
    }
    if manifest:
        data.update(manifest)
    (root / "manifest.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return kb.load_library(root)


# ---------------------------------------------------------------- 退化

def test_empty_library_is_empty():
    """默认未配置 → 空库。这是骨架能先落地的前提。"""
    assert kb.EMPTY_LIBRARY.is_empty
    assert kb.resolve_competitor("坦克700", kb.EMPTY_LIBRARY) is None
    assert kb.mentioned_competitors(["坦克700"], kb.EMPTY_LIBRARY) == []


def test_unconfigured_setting_returns_empty_library():
    """``KNOWLEDGE_COMPETITORS`` 缺省空串 → 空库，不抛。"""
    from customer_profile.settings import Settings

    settings = Settings(llm_base_url="http://x", llm_api_key="k", llm_model="m")
    assert settings.knowledge_competitors == ""
    assert kb.current_competitors(settings).is_empty


def test_broken_library_degrades_to_empty(tmp_path):
    """装不起来只退化，不抛——竞品资料缺了不该让整张卡作废。"""
    from customer_profile.settings import Settings

    root = tmp_path / "broken"
    root.mkdir()
    (root / "manifest.json").write_text("{not json", encoding="utf-8")
    settings = Settings(
        llm_base_url="http://x",
        llm_api_key="k",
        llm_model="m",
        knowledge_competitors="broken",
        knowledge_competitors_dir=str(tmp_path),
    )
    kb.reset_cache()
    assert kb.current_competitors(settings).is_empty


# ---------------------------------------------------------------- 装载与校验

def test_library_loads(tmp_path):
    library = _library(tmp_path, [_entry()])
    assert not library.is_empty
    assert library.library_id == "test-lib"
    assert library.content_signature, "指纹缺失，两批差异将无法归因"
    assert library.entry("c-1") is not None


def test_duplicate_model_id_is_rejected(tmp_path):
    with pytest.raises(kb.CompetitorError) as exc:
        _library(tmp_path, [_entry(), _entry()])
    assert "重复" in str(exc.value)


def test_duplicate_alias_is_rejected(tmp_path):
    with pytest.raises(kb.CompetitorError) as exc:
        _library(
            tmp_path,
            [_entry(), _entry(model_id="c-2", canonical_name="测试竞品B", aliases=["竞品A"])],
        )
    assert "认领" in str(exc.value)


def test_price_inversion_is_rejected(tmp_path):
    with pytest.raises(kb.CompetitorError) as exc:
        _library(
            tmp_path,
            [_entry(price={"min_wan": 40.0, "max_wan": 30.0, "as_of": "2026-10-30"})],
        )
    assert "倒置" in str(exc.value)


def test_blank_positioning_is_rejected(tmp_path):
    with pytest.raises(kb.CompetitorError) as exc:
        _library(tmp_path, [_entry(positioning="   ")])
    assert "定位" in str(exc.value)


def test_unknown_powertrain_is_rejected(tmp_path):
    with pytest.raises(kb.CompetitorError) as exc:
        _library(tmp_path, [_entry(powertrain=["hydrogen"])])
    assert "动力形式" in str(exc.value)


def test_unknown_powertrain_value_is_allowed(tmp_path):
    """``unknown`` 是合法取值：竞品参数查不到时不能猜成「没有」。"""
    library = _library(tmp_path, [_entry(powertrain=["unknown"])])
    assert library.entry("c-1").powertrain == ["unknown"]


def test_missing_manifest_is_rejected(tmp_path):
    root = tmp_path / "x"
    root.mkdir()
    with pytest.raises(kb.CompetitorError):
        kb.load_library(root)


# ---------------------------------------------------------------- 解析

def test_resolve_by_canonical_alias_and_normalized(tmp_path):
    library = _library(tmp_path, [_entry()])
    assert kb.resolve_competitor("测试竞品A", library) == "c-1"
    assert kb.resolve_competitor("竞品A", library) == "c-1"
    assert kb.resolve_competitor(" 测试竞品A ", library) == "c-1"


def test_no_fuzzy_match(tmp_path):
    """``某某700`` 不该命中近乎相同的名字——与车型解析同口径，不做编辑距离。"""
    library = _library(tmp_path, [_entry(canonical_name="某某700", aliases=[])])
    assert kb.resolve_competitor("某某7000", library) is None


def test_mentioned_competitors_dedupes(tmp_path):
    library = _library(tmp_path, [_entry()])
    hits = kb.mentioned_competitors(["测试竞品A", "竞品A", "没见过的车"], library)
    assert [h.model_id for h in hits] == ["c-1"]


# ---------------------------------------------------------------- 视图

def test_view_is_empty_without_library():
    """空库 → 空串。这是回滚口。"""
    assert views.render_competitor_brief({}, kb.EMPTY_LIBRARY) == ""


def test_view_is_empty_when_no_competitor_mentioned(tmp_path):
    library = _library(tmp_path, [_entry()])
    assert views.render_competitor_brief({"intended_model": "本方车型X"}, library) == ""


def test_view_renders_mentioned_competitor(tmp_path):
    library = _library(tmp_path, [_entry()])
    text = views.render_competitor_brief({"competing_model": "测试竞品A"}, library)
    assert "测试竞品A" in text
    assert "轴距" in text
    assert "应对要点" in text


def test_view_marks_competitor_data_as_external(tmp_path):
    """**必须自述「外部资料、不是客户事实」**，否则模型会把竞品参数写进事实位。"""
    library = _library(tmp_path, [_entry()])
    text = views.render_competitor_brief({"competing_model": "测试竞品A"}, library)
    assert "外部资料" in text
    assert "不是客户已表达的事实" in text
    assert "不得" in text


def test_view_only_lists_mentioned_competitors(tmp_path):
    """只讲客户提到的那几台，不主动列全目录。"""
    library = _library(
        tmp_path,
        [_entry(), _entry(model_id="c-2", canonical_name="测试竞品B", aliases=["竞品B"])],
    )
    text = views.render_competitor_brief({"competing_model": "测试竞品A"}, library)
    assert "测试竞品A" in text
    assert "测试竞品B" not in text
