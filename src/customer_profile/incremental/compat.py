"""管线兼容键：底座能不能直接沿用，由这一处说了算。

键 = 契约签名（Notes 字段键集合）+ 规则登记表 ID 集合 + L3 板块键集合 + 键格式版本。
**不同即 T3 全量重建**（``01`` §4.1）。

只收「会改变 claims 形状或字段语义」的部分：提示词措辞调整不进键。否则每次微调都让
全部客户落 T3，增量形同虚设；反过来，规则簇增删必须进键——旧 claim 是按旧规则定档的，
拿它当「上一版已定稿事实」会带着旧口径往下走。
"""

from __future__ import annotations

import hashlib
import json

COMPAT_VERSION = "p1-1"
"""键格式版本。改键的构成方式时递增——只改内容不改格式不算。"""


def compat_material() -> dict:
    """参与哈希的全部内容，单独取出来便于排障时逐项比对。"""
    from .. import notes_contract as nc
    from ..rules import registry as rule_registry
    from ..workflows import customer_profile_l3 as l3

    notes_keys = sorted({str(item["attribute_key"]) for item in nc.RAW_CONFIGS})
    card_keys = sorted(_card_and_internal_keys())
    rules = rule_registry.default()
    rule_ids = sorted(rule.id for rule in rules.all() if str(rule.status) == "active")
    return {
        "version": COMPAT_VERSION,
        "notes_keys": notes_keys,
        "card_keys": card_keys,
        "rule_ids": rule_ids,
        "sections": list(l3.SECTION_KEYS),
    }


def _card_and_internal_keys() -> set[str]:
    """画像卡字段与编外字段的键。它们同样改变 L1 的输出形状，必须进键。"""
    from .. import internal_fields as inf

    return set(inf.CARD_ONLY_KEYS) | set(inf.EXTRACTION_ONLY_KEYS)


def compat_key() -> str:
    """当前管线兼容键。同一进程内内容不变，但每次照算——调用点极稀疏。"""
    material = json.dumps(compat_material(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
