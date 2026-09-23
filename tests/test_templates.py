"""提示词资源：包内正文必须与 DSL 逐字符一致。

有意差异 W11 定下「运行期唯一来源是包内 ``src/customer_profile/templates/``，
``docs/specs/prompts/`` 只是评审副本」，并声称**由测试断言两边逐字符一致**。
V0.3 收口时实测：**并没有这个测试**，而 81 份评审副本里大量内容已经与包内资源不同
（包内资源经 ``export_templates.py --check`` 与 DSL 比对通过，是可信的一侧）。

本文件补上前半句——「包内资源 == DSL」；评审副本的一致性见文末的用例，它把
「副本已失真」这件事变成一条**会失败的**断言，而不是一句没人验证的声明。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys

import pytest

from .conftest import REPO_ROOT, requires_dsl

TEMPLATE_DIR = REPO_ROOT / "src" / "customer_profile" / "templates"
PROMPTS_DIR = REPO_ROOT / "docs" / "specs" / "prompts"
EXPORT_SCRIPT = REPO_ROOT / "scripts" / "export_templates.py"

EXPECTED_TEMPLATES = 81
"""DSL 里提示词/模板节点总数：46 个 template-transform + 35 个 llm。"""


def _run_check() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT), "--check"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        # Windows 控制台的默认代码页不是 UTF-8，导出脚本的中文输出会解码失败；
        # 这里只关心退出码与比对结论，坏字符不改变判定，但不能让它变成线程异常。
        errors="replace",
    )


@requires_dsl
def test_package_templates_match_dsl_verbatim():
    """包内 81 份正文与 DSL 逐字符一致（借助导出脚本的 --check 模式）。

    这条断言挡的是「手工改了一处提示词但没同步」——提示词正文的差异会直接改变模型
    输出，而两边都「看起来像正常文本」，靠人眼比对不现实。
    """
    result = _run_check()
    assert result.returncode == 0, (
        "包内提示词资源与 DSL 不一致：\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@requires_dsl
def test_template_count_matches_ledger():
    """资源文件数 = 台账里的提示词节点数（81）。少一个就是静默丢了一份提示词。"""
    files = [p for p in TEMPLATE_DIR.glob("*.txt")]
    assert len(files) == EXPECTED_TEMPLATES, (
        f"应有 {EXPECTED_TEMPLATES} 份资源，实际 {len(files)}；"
        f"缺失或多出的会在这里暴露"
    )


def test_review_copies_match_package_templates():
    """评审副本 ``docs/specs/prompts/`` 与包内资源**逐字节一致**（W11 的前半句）。

    背景：V0.1 生成的副本用了另一套命名（显示名转写 + 内容哈希 + 节点类型后缀）与另一套
    抽取口径（保留 ``system`` / ``user`` 角色行、CRLF 换行），且取自更早的 DSL 修订，
    因此 81 份里 43 份与今天的 DSL 正文不同——是一份**过期且口径不同**的副本。

    V0.3 收口时按包内资源（经 ``export_templates.py --check`` 与 DSL 逐字符一致）重新导出，
    命名也统一成 ``<slug>__<node_id>.txt``。这里断言两边逐字节相同，把 W11 从「声称」
    变成被测的事实——副本一旦再次失真，这条用例会红。
    """
    if not PROMPTS_DIR.is_dir():
        pytest.skip(f"缺少评审副本目录 {PROMPTS_DIR}")

    package = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in TEMPLATE_DIR.glob("*.txt")
    }
    review = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in PROMPTS_DIR.rglob("*.txt")
    }

    assert package, "包内没有任何资源文件，测试本身失去意义"
    assert set(package) == set(review), (
        "评审副本与包内资源的文件名集合不同："
        f"只在包内 {sorted(set(package) - set(review))[:5]}；"
        f"只在副本 {sorted(set(review) - set(package))[:5]}"
    )
    differing = sorted(n for n in package if package[n] != review[n])
    assert not differing, (
        f"{len(differing)} 份副本与包内资源内容不同：{differing[:5]}；"
        "重新导出：python scripts/export_templates.py（包内）后同步副本"
    )
