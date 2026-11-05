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


# ---------------------------------------------------------------- 档位（版本）

def test_model_with_trim_suffix_resolves(pack):
    """「车型 + 档位」是客户与留资最常见的写法，必须落回车系。

    UAT 归档里 792 案有 456 例的意向车型是这么被闭集剔除的——值本身完全正确。
    """
    assert resolve.resolve_model("猛士X700 Ultra", pack) == "dfms-x700"
    assert resolve.resolve_model("X700 Ultra", pack) == "dfms-x700"
    assert resolve.resolve_model("M817 MAX+", pack) == "dfms-m817"
    assert resolve.resolve_model("猛士917 增程基础版", pack) == "dfms-917"


def test_trim_suffix_is_case_and_space_insensitive(pack):
    assert resolve.resolve_model(" x700   ultra ", pack) == "dfms-x700"
    assert resolve.resolve_model("X700 ULTRA", pack) == "dfms-x700"


def test_ambiguous_trim_alone_does_not_pick_a_model(pack):
    """``Ultra`` 同时是 X700 与 M817 的档位：认哪一款都是猜。"""
    assert resolve.resolve_model("Ultra", pack) is None
    assert resolve.resolve_model("Max+", pack) is None


def test_unambiguous_trim_alone_resolves(pack):
    assert resolve.resolve_model("Mate", pack) == "dfms-m817"
    assert resolve.resolve_model("增程基础版", pack) == "dfms-917"


def test_ambiguous_trim_is_still_our_brand(pack):
    """认不出是哪一款，但它确实是我们的在售档位——绝不能判成竞品或陌生称谓。"""
    assert resolve.is_our_trim("Ultra", pack) is True
    assert resolve.resolve_brand("Ultra", pack) == "our"
    assert resolve.is_our_brand("Ultra", pack) is True


def test_trim_does_not_open_the_door_to_fuzzy_matching(pack):
    """档位只认知识包里写明的名字：近似写法仍然不命中。"""
    assert resolve.resolve_model("X700 Super", pack) is None
    assert resolve.resolve_model("猛士老款917特别版", pack) is None
    assert resolve.resolve_model("坦克700", pack) is None
