"""知识版本进批次留痕（T3-3）。

## 为什么必须有这一项

理由与 ``definition_hash`` 完全相同：**换了知识，两批结果就不可直接对比。**
没有留痕时，两批差异只能靠「记得当时改过知识包」来解释，而记忆不可靠。

三项都要在，才够定位到具体内容：``pack_id``（哪个合作方）、``content_signature``
（``models.json`` 的 sha256，内容一动就变）、``effective_from``（这批知识自哪天生效）。

留痕**不该让跑批失败**：知识是增强项，取不到就记 ``available: False``，
批次照跑，只退回无知识形态。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from .conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import run_baseline as rb  # noqa: E402

from customer_profile.settings import Settings  # noqa: E402


def _settings(**overrides) -> Settings:
    """最小配置：不读 .env，避免用例受本机环境影响。"""
    base = {"knowledge_pack": "dfms-mengshi", "knowledge_packs_dir": ""}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 正式包

def test_snapshot_records_pack_id_and_signature():
    """正式包必须给出三项标识，缺一项就定位不到「当时用的是哪份知识」。"""
    snapshot = rb.knowledge_snapshot(_settings())
    assert snapshot["available"] is True
    assert snapshot["pack_id"] == "dfms-mengshi"
    assert snapshot["content_signature"], "内容指纹缺失，两批差异将无法归因"
    assert snapshot["effective_from"] == "2026-10-29"
    assert snapshot["model_count"] == 3


def test_signature_is_a_sha256_of_models_file():
    """指纹必须真是 models.json 的摘要——改了内容它就该变。"""
    import hashlib

    path = (
        REPO_ROOT
        / "src"
        / "customer_profile"
        / "knowledge"
        / "packs"
        / "dfms-mengshi"
        / "models.json"
    )
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert rb.knowledge_snapshot(_settings())["content_signature"] == expected


# ---------------------------------------------------------------- 退化

def test_unconfigured_pack_records_unavailable_not_crash():
    """置空 ``KNOWLEDGE_PACK`` 是刻意的回滚口，留痕要如实记「没启用」，不能抛。"""
    snapshot = rb.knowledge_snapshot(_settings(knowledge_pack=""))
    assert snapshot["available"] is False
    assert snapshot["pack_id"] == ""
    assert "reason" in snapshot


def test_broken_pack_records_unavailable_not_crash(tmp_path):
    """包装不起来时同样只记录：知识缺失不该让整个批次跑不动。"""
    broken = tmp_path / "packs" / "broken-pack"
    broken.mkdir(parents=True)
    (broken / "pack.json").write_text("{not json", encoding="utf-8")

    snapshot = rb.knowledge_snapshot(
        _settings(knowledge_pack="broken-pack", knowledge_packs_dir=str(tmp_path / "packs"))
    )
    assert snapshot["available"] is False
    assert snapshot["pack_id"] == "broken-pack", "要留下「配了哪个包」，否则查不出配错了"


# ---------------------------------------------------------------- 配置白名单

def test_knowledge_settings_are_in_the_config_allowlist():
    """两项知识配置要进白名单快照。

    白名单是「禁止 dump 整个 Settings」的替代品：漏登记会让 manifest 里查不到
    「当时配的是哪个包」，而那正是排障第一问。
    """
    assert "knowledge_pack" in rb.CONFIG_ALLOWLIST
    assert "knowledge_packs_dir" in rb.CONFIG_ALLOWLIST


CREDENTIAL_WORDS: tuple[str, ...] = ("api_key", "apikey", "secret", "password", "credential")
"""凭据词根。**按词根而不是任意子串判**：``llm_max_tokens`` 里含 ``token``，
但那是「补齐上限」参数，不是凭据——用子串判会把它误拦成违规，
而一条会误报的守卫最终会被无视。"""


def test_allowlist_still_excludes_credentials():
    """回归：白名单不得混入凭据字段——这是它存在的全部理由。"""
    for name in rb.CONFIG_ALLOWLIST:
        lowered = name.lower()
        assert not any(word in lowered for word in CREDENTIAL_WORDS), (
            f"{name} 看起来是凭据，不该进归档"
        )
