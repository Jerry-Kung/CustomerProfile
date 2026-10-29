"""品牌与车型解析（K2）。

要点不是「能匹配上」，而是**匹配的边界**：

- 命中即停、四级精确匹配；
- **不做模糊匹配**——``坦克700`` 与 ``坦克500`` 在子串上互为包含，模糊匹配会把两台
  不同的车归成一个，这种错不会报错，只会让归属莫名其妙地错。
"""

from __future__ import annotations

import pytest

from customer_profile import knowledge
from customer_profile.knowledge import resolve
from customer_profile.knowledge.pack import DEFAULT_PACK_DIR, load_pack


@pytest.fixture(scope="module")
def pack():
    return load_pack(DEFAULT_PACK_DIR / "dfms-mengshi")


# ---------------------------------------------------------------- 命中

def test_canonical_name_resolves(pack):
    assert resolve.resolve_model("猛士M817", pack) == "dfms-m817"


def test_alias_resolves(pack):
    assert resolve.resolve_model("M817", pack) == "dfms-m817"
    assert resolve.resolve_model("917", pack) == "dfms-917"
    assert resolve.resolve_model("M917", pack) == "dfms-917"


def test_normalized_form_resolves(pack):
    """全半角、大小写、空格差异不该影响匹配。"""
    assert resolve.resolve_model("猛士m817", pack) == "dfms-m817"
    assert resolve.resolve_model(" 猛士 M817 ", pack) == "dfms-m817"
    assert resolve.resolve_model("ｍ817", pack) == "dfms-m817"


def test_brand_resolves_for_model_names(pack):
    """车型名蕴含品牌：客户说的多数是车型，不必再查品牌表。"""
    assert resolve.resolve_brand("猛士M817", pack) == "our"
    assert resolve.is_our_brand("M817", pack) is True


def test_brand_resolves_for_plain_brand_name(pack):
    assert resolve.resolve_brand("东风猛士", pack) == "our"
    assert resolve.resolve_brand("猛士", pack) == "our"


# ---------------------------------------------------------------- 不命中

def test_competitor_is_not_ours(pack):
    """竞品不是本方——这条保证「同品牌不进竞品位」不会反向误伤。"""
    assert resolve.resolve_model("坦克700", pack) is None
    assert resolve.is_our_brand("坦克700", pack) is False


def test_no_fuzzy_match_on_similar_names(pack):
    """**不做模糊匹配**：``M81`` 不是 ``M817``。

    这条反例的价值高于所有正例：模糊匹配会把「客户随口提到的型号」归到最近的车上，
    而归属错误会一路影响竞品位与意向位。
    """
    assert resolve.resolve_model("M81", pack) is None
    assert resolve.resolve_model("猛士M81", pack) is None
    assert resolve.resolve_model("M8170", pack) is None


def test_blank_input_returns_none(pack):
    assert resolve.resolve_model("", pack) is None
    assert resolve.resolve_model("   ", pack) is None
    assert resolve.resolve_brand(None, pack) is None


def test_empty_pack_never_matches():
    """空包下一切皆不命中——退化时不能凭空认出车型。"""
    assert resolve.resolve_model("猛士M817", knowledge.EMPTY_PACK) is None
    assert resolve.is_our_brand("猛士", knowledge.EMPTY_PACK) is False


# ---------------------------------------------------------------- 归一辅助

def test_canonical_model_name_normalizes_alias(pack):
    assert resolve.canonical_model_name("m817", pack) == "猛士M817"
    assert resolve.canonical_model_name("不存在的车", pack) is None
