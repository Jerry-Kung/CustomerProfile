"""品牌与车型解析（K2）。

三个纯函数，无 IO、无缓存、可单测：

- :func:`resolve_model` —— 任意称谓 → 车型条目
- :func:`resolve_brand` —— 任意称谓 → 品牌归属（本方 / 竞品 / 未知）
- :func:`is_our_brand` —— 是否本方品牌

**匹配口径**（命中即停）：规范名精确 → 别名精确 → 转写变体精确 → 归一化后精确。

**明确不做模糊匹配。** 不做编辑距离、不做子串包含。``坦克700`` 与 ``坦克500`` 在子串上
互为包含，模糊匹配会把两台车归成一个；而知识库的价值在于「答得准」，不在于「答得多」。
未命中就返回 ``None``，由上层按既有口径处理（L1 照写、投影层不归一）。
"""

from __future__ import annotations

from .pack import EMPTY_PACK, Pack, normalize_name

OUR = "our"
COMPETITOR = "competitor"


def _names_of(entry) -> tuple[str, ...]:
    return entry.all_names()


def resolve_model(text: str, pack: Pack | None = None) -> str | None:
    """把任意称谓解析成 ``model_id``。未命中返回 ``None``。

    四级匹配按优先级推进，命中即停。归一化只做去空格、全半角、大小写——
    这是「同一个词的不同写法」，不是「不同的词」。
    """
    if not text:
        return None
    resolved = pack or EMPTY_PACK
    if resolved.is_empty:
        return None

    needle = str(text).strip()
    if not needle:
        return None

    # 一级：规范名精确
    for entry in resolved.models:
        if needle == entry.canonical_name:
            return entry.model_id
    # 二级：别名精确
    for entry in resolved.models:
        if needle in entry.aliases:
            return entry.model_id
    # 三级：转写变体精确
    for entry in resolved.models:
        if needle in entry.asr_variants:
            return entry.model_id

    # 四级：归一化后精确
    key = normalize_name(needle)
    if not key:
        return None
    for entry in resolved.models:
        for name in _names_of(entry):
            if normalize_name(name) == key:
                return entry.model_id
    return None


def resolve_brand(text: str, pack: Pack | None = None) -> str | None:
    """把任意称谓解析成品牌归属：``"our"`` / 竞品 ID / ``None``（未知）。

    先判车型再判品牌：客户说的多数是车型，而车型蕴含品牌。车型命中本方时直接返回
    ``our``，不必再查品牌表。
    """
    if not text:
        return None
    resolved = pack or EMPTY_PACK
    if resolved.is_empty:
        return None

    if resolve_model(text, resolved) is not None:
        return OUR

    needle = str(text).strip()
    key = normalize_name(needle)
    if not key:
        return None

    if _brand_matches(resolved.brand.our_brand, needle, key):
        return OUR
    for competitor in resolved.brand.competitors:
        if _brand_matches(competitor, needle, key):
            return competitor.canonical
    return None


def is_our_brand(text: str, pack: Pack | None = None) -> bool:
    """``text`` 是否指向本方（品牌或任一在售车型）。

    **用于「同品牌不进竞品位」这条硬约束。** 此前这条约束只写在提示词里、
    依靠模型自行认定「本方是谁」，因此是一条软约束；有知识包之后它才是代码判定。
    """
    return resolve_brand(text, pack) == OUR


def _brand_matches(identity, needle: str, key: str) -> bool:
    if needle == identity.canonical:
        return True
    for name in identity.all_names():
        if needle == name:
            return True
    for name in identity.all_names():
        if normalize_name(name) == key:
            return True
    return False


def canonical_model_name(text: str, pack: Pack | None = None) -> str | None:
    """称谓 → 规范名。用于「只改写法、不新增结论」的归一。

    未命中返回 ``None``（调用点保留原值），与 :func:`resolve_model` 同口径。
    """
    model_id = resolve_model(text, pack)
    if model_id is None:
        return None
    resolved = pack or EMPTY_PACK
    entry = resolved.model(model_id)
    return entry.canonical_name if entry is not None else None
