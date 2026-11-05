"""品牌与车型解析（K2）。

三个纯函数，无 IO、无缓存、可单测：

- :func:`resolve_model` —— 任意称谓 → 车型条目
- :func:`resolve_brand` —— 任意称谓 → 品牌归属（本方 / 竞品 / 未知）
- :func:`is_our_brand` —— 是否本方品牌

**匹配口径**（命中即停）：规范名精确 → 别名精确 → 转写变体精确 → 归一化后精确
→ 档位（trim）剥离后精确。

档位这一步专门解决「客户说的是在售车型的某个版本」：留资里常见「意向车型：Ultra」
「意向车系：X700」，L1 合成「<车型> Ultra」。Ultra 是该车型的在售档位，但按车型名
无论如何都对不上，闭集就会把一条完全正确的意向整条剔除（UAT 归档 792 案里 456 例如此）。
剥离只认**知识包 ``configs`` 里写明的档位名**，仍不做编辑距离、不做任意子串包含。

**明确不做模糊匹配。** 不做编辑距离、不做子串包含。``某某700`` 与 ``某某500`` 在子串上
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

    五级匹配按优先级推进，命中即停。归一化只做去空格、全半角、大小写——
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

    # 五级：档位。客户/留资写的是「车型 + 档位」或只说档位名。
    return _resolve_by_trim(key, resolved)


def trim_names(entry) -> tuple[str, ...]:
    """一款车型**全部档位称谓**：完整档位名 + 其末段。

    ``360KM 四驱 Ultra`` 产出 ``("360KM 四驱 Ultra", "Ultra")``——留资与销售记录里
    客户几乎只会说后者。末段按空白切，不猜同义词、不做子串包含。
    """
    out: list[str] = []
    for config in entry.configs:
        name = (config.name or "").strip()
        if not name:
            continue
        if name not in out:
            out.append(name)
        parts = name.split()
        tail = parts[-1] if parts else ""
        if tail and tail != name and tail not in out:
            out.append(tail)
    return tuple(out)


def model_prefix_of(text: str, pack: Pack | None = None) -> str | None:
    """``text`` 以某款在售车型的名称起头时返回该 ``model_id``，否则 ``None``。

    用途是「车型名认得出、后面那截版本写法不认识」（``<车型> PRO``、``<车型> 增程版``）：
    这仍然是我们的车，只是知识包没说这个版本叫什么。**不据此新增结论**——调用点用它
    把值留在意向位并转确认，而不是丢掉或交给模糊匹配。
    """
    if not text:
        return None
    resolved = pack or EMPTY_PACK
    if resolved.is_empty:
        return None
    key = normalize_name(str(text).strip())
    if not key:
        return None
    best: tuple[int, str] | None = None
    for entry in resolved.models:
        for name in entry.all_names():
            norm = normalize_name(name)
            if not norm or not key.startswith(norm) or len(key) <= len(norm):
                continue
            if best is None or len(norm) > best[0]:
                best = (len(norm), entry.model_id)
    return best[1] if best else None


def is_our_trim(text: str, pack: Pack | None = None) -> bool:
    """``text`` 是否为本方某款在售车型的档位名（**不要求能唯一落到一款车**）。

    ``Ultra`` 同时是 X700 与 M817 的档位：认哪一款都是猜，``resolve_model`` 因此返回
    ``None``。但它**确实是我们家的在售档位**，绝不能因为「认不出是哪一款」就被判成
    竞品或陌生称谓。本函数就是给这种歧义兜底的品牌判定入口。
    """
    if not text:
        return False
    resolved = pack or EMPTY_PACK
    if resolved.is_empty:
        return False
    key = normalize_name(str(text).strip())
    if not key:
        return False
    for entry in resolved.models:
        for name in trim_names(entry):
            if normalize_name(name) == key:
                return True
    return False


def _resolve_by_trim(key: str, pack: Pack) -> str | None:
    """档位解析：**唯一命中才认**，歧义返回 ``None``。

    两条路径，都要求「剥掉档位后剩下的那一段本身能解析成该车型」：

    1. 整串就是某个档位名（「车型 + 档位」之外，``Ultra`` 单独出现也算）——
       只有**全包唯一**的档位名才认；``Ultra`` 同时是 X700 与 M817 的档位，认哪个
       都是猜，此时返回 ``None``，由上层转确认。
    2. 以档位名结尾、前面是本方车型（``X700 Ultra`` / ``M817 MAX+``）——剥掉档位后
       用前四级重新解析，命中哪款就归哪款。

    长档位名先试：``Max+`` 必须先于 ``Max`` 尝试，否则 ``M817 Max+`` 会被剥成 ``M817 +``。
    """
    pairs: list[tuple[str, str]] = []
    for entry in pack.models:
        for name in trim_names(entry):
            norm = normalize_name(name)
            if norm:
                pairs.append((norm, entry.model_id))
    if not pairs:
        return None

    # 1) 整串命中档位名
    exact = sorted({model_id for norm, model_id in pairs if norm == key})
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None

    # 2) 尾部是档位名，前缀须是同一款车型
    for norm, model_id in sorted(pairs, key=lambda item: len(item[0]), reverse=True):
        if not key.endswith(norm) or len(key) <= len(norm):
            continue
        if resolve_model(key[: -len(norm)], pack) == model_id:
            return model_id
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
    # 歧义档位（同一档位名属于多款本方车型）仍属本方：认不出是哪一款，但绝不是竞品。
    if is_our_trim(text, resolved):
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
