"""知识包装载与校验（K1）。

每条校验规则至少一个反例：装载期的严格性是这套知识库唯一的质量闸门——内容写错
不会报错，只会让输出慢慢偏掉，因此「能不能被拦住」必须逐条钉住。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from customer_profile import knowledge
from customer_profile.knowledge.pack import (
    DEFAULT_PACK_DIR,
    EMPTY_PACK,
    PackError,
    load_pack,
    normalize_name,
)

# ---------------------------------------------------------------- 夹具

def _model(**overrides):
    base = {
        "model_id": "m-1",
        "canonical_name": "测试车A",
        "aliases": ["A1"],
        "series": "A",
        "status": "on_sale",
        "price": {"min_wan": 10.0, "max_wan": 20.0, "as_of": "2026-10-29"},
        "powertrain": ["phev"],
        "body": "SUV",
        "seats": 5,
        "configs": [{"name": "标准版", "price_wan": 10.0}],
        "audience": ["家庭"],
        "scenarios": ["通勤"],
        "positioning": "一句话定位",
        "highlights": [{"title": "卖点", "text": "说明"}],
        "speech": "推介话术",
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, models, *, manifest=None, brand=None) -> Path:
    root = tmp_path / "pack"
    root.mkdir(parents=True, exist_ok=True)
    (root / "models.json").write_text(
        json.dumps(models, ensure_ascii=False), encoding="utf-8"
    )
    (root / "brand.json").write_text(
        json.dumps(brand or {"our_brand": {"canonical": "测试品牌"}, "competitors": []},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    data = {
        "pack_id": "test-pack",
        "display_name": "测试",
        "vendor": "测试方",
        "brands": ["测试品牌"],
        "effective_from": "2026-10-29",
    }
    if manifest:
        data.update(manifest)
    (root / "pack.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return root


# ---------------------------------------------------------------- 仓库内的正式包

def test_shipped_pack_loads():
    """仓库里的猛士包必须能装载——它是默认配置，装不上等于开箱即坏。"""
    pack = load_pack(DEFAULT_PACK_DIR / "dfms-mengshi")
    assert pack.pack_id == "dfms-mengshi"
    assert [m.model_id for m in pack.models] == ["dfms-x700", "dfms-m817", "dfms-917"]
    assert pack.content_signature, "指纹不能为空：换知识要能看出来"


def test_shipped_pack_prices_are_self_consistent():
    """三段式价格：min <= core_max <= max，且版本价全落在包络内。"""
    pack = load_pack(DEFAULT_PACK_DIR / "dfms-mengshi")
    for entry in pack.models:
        price = entry.price
        assert price.min_wan <= price.core_max <= price.max_wan
        for item in entry.configs:
            assert price.min_wan - 0.011 <= item.price_wan <= price.max_wan + 0.011


def test_shipped_pack_917_has_extended_price_band():
    """917 的主力带与全系包络必须分开：只留包络会被当成百万级车推给 80 万预算的客户。"""
    pack = load_pack(DEFAULT_PACK_DIR / "dfms-mengshi")
    entry = pack.model("dfms-917")
    assert entry is not None
    assert entry.price.core_max == pytest.approx(69.90)
    assert entry.price.max_wan == pytest.approx(109.80)


def test_shipped_pack_tiers_cover_every_model():
    """每款车都要被一个分层覆盖，否则 K4 永远选不到它。"""
    pack = load_pack(DEFAULT_PACK_DIR / "dfms-mengshi")
    for entry in pack.models:
        assert pack.tier_of(entry.model_id) is not None


# ---------------------------------------------------------------- 校验规则

def test_duplicate_alias_is_rejected(tmp_path):
    """规则 2：两个条目声称同一称谓——查表结果将取决于遍历顺序，必须硬拦。"""
    root = _write(
        tmp_path,
        [
            _model(model_id="m-1", canonical_name="车一", aliases=["X"]),
            _model(model_id="m-2", canonical_name="车二", aliases=["X"]),
        ],
    )
    with pytest.raises(PackError) as exc:
        load_pack(root)
    assert "同时被" in str(exc.value)


def test_alias_colliding_with_another_canonical_name_is_rejected(tmp_path):
    """规则 3：别名不得与另一条目的规范名撞车。"""
    root = _write(
        tmp_path,
        [
            _model(model_id="m-1", canonical_name="车一", aliases=["车二"]),
            _model(model_id="m-2", canonical_name="车二", aliases=[]),
        ],
    )
    with pytest.raises(PackError):
        load_pack(root)


def test_duplicate_model_id_is_rejected(tmp_path):
    root = _write(tmp_path, [_model(), _model(canonical_name="另一个")])
    with pytest.raises(PackError) as exc:
        load_pack(root)
    assert "model_id 重复" in str(exc.value)


def test_price_range_inversion_is_rejected(tmp_path):
    root = _write(
        tmp_path,
        [_model(price={"min_wan": 30.0, "max_wan": 10.0, "as_of": "2026-10-29"})],
    )
    with pytest.raises(PackError):
        load_pack(root)


def test_config_price_outside_band_is_rejected(tmp_path):
    root = _write(
        tmp_path,
        [_model(configs=[{"name": "超纲版", "price_wan": 99.0}])],
    )
    with pytest.raises(PackError) as exc:
        load_pack(root)
    assert "区间" in str(exc.value)


def test_blank_speech_is_rejected(tmp_path):
    """表达层三字段非空：空卖点的条目对 L3 没有价值，应在入库时发现。"""
    root = _write(tmp_path, [_model(speech="   ")])
    with pytest.raises(PackError):
        load_pack(root)


def test_dangling_tier_reference_is_rejected(tmp_path):
    """规则 8：分层引用不存在的车型。"""
    root = _write(
        tmp_path,
        [_model()],
        manifest={"tiers": [{"tier": "t", "label": "L", "models": ["不存在的车"]}]},
    )
    with pytest.raises(PackError) as exc:
        load_pack(root)
    assert "不存在的车型" in str(exc.value)


def test_uncovered_model_is_rejected_when_tiers_exist(tmp_path):
    """规则 9：有分层却没覆盖到这款车 → 它永远选不到（死知识）。"""
    root = _write(
        tmp_path,
        [_model(model_id="m-1", canonical_name="车一", aliases=[]),
         _model(model_id="m-2", canonical_name="车二", aliases=[])],
        manifest={"tiers": [{"tier": "t", "label": "L", "models": ["m-1"]}]},
    )
    with pytest.raises(PackError) as exc:
        load_pack(root)
    assert "未被任何分层覆盖" in str(exc.value)


def test_unknown_powertrain_is_rejected(tmp_path):
    root = _write(tmp_path, [_model(powertrain=["hybrid_magic"])])
    with pytest.raises(PackError):
        load_pack(root)


def test_empty_powertrain_is_legal(tmp_path):
    """**空动力形式是合法的**：它的语义是「逐档待定，别猜」。

    强制填写会逼出臆造值——M817 的逐档动力形式业务方尚未确认，这条保证那条待定
    不会阻塞入库。
    """
    root = _write(tmp_path, [_model(powertrain=[], configs=[{"name": "x", "price_wan": 10.0}])])
    pack = load_pack(root)
    assert pack.models[0].powertrain == []


def test_missing_models_file_is_rejected(tmp_path):
    root = _write(tmp_path, [_model()])
    (root / "models.json").unlink()
    with pytest.raises(PackError):
        load_pack(root)


def test_bad_json_reports_syntax_error(tmp_path):
    root = _write(tmp_path, [_model()])
    (root / "models.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(PackError) as exc:
        load_pack(root)
    assert "JSON" in str(exc.value)


# ---------------------------------------------------------------- 退化

def test_empty_pack_when_not_configured():
    """未配置知识包时返回空包而不是抛——知识是增强，不能成为新单点。"""
    from tests.conftest import make_settings

    settings = make_settings(Path("."), knowledge_pack="")
    pack = knowledge.current_pack(settings)
    assert pack.is_empty
    assert pack.models == ()


def test_broken_pack_degrades_to_empty(tmp_path):
    """包装不上也返回空包：请求路径不能因为知识文件写错而停摆。"""
    from tests.conftest import make_settings

    root = tmp_path / "packs"
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "pack.json").write_text("{}", encoding="utf-8")
    settings = make_settings(Path("."), knowledge_pack="broken",
                             knowledge_packs_dir=str(root))
    knowledge.reset_cache()
    pack = knowledge.current_pack(settings)
    assert pack.is_empty, "装载失败必须退化为空包"
    knowledge.reset_cache()


def test_emppack_constant_is_empty():
    assert EMPTY_PACK.is_empty


def test_normalize_name_folds_width_and_space():
    """归一化只做「同一个词的不同写法」，不做同义词扩展。"""
    assert normalize_name(" Ｍ８１７ ") == "m817"
    assert normalize_name("猛士 M817") == "猛士m817"
