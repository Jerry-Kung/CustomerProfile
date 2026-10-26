"""仓库级守卫：对真实代码树跑一遍两道检查。

与 ``test_guard.py`` 的分工：那边用临时目录钉住**检索原语**的行为，这边钉住
**当前仓库的实际状态**——守卫真正要拦的事故（删规则删一半、登记了没落地）都发生在
真实树上，用夹具模拟不出来。

三条断言，任何一条红了都表示「规则层的完整性已经破了」：

1. 没有孤儿标记；
2. 没有未落地规则；
3. 每条 ``active`` 规则引用的测试都真实存在——登记表自检只查「有没有写测试引用」，
   查不出「引用的用例已被删除/改名」，那正是最容易被忽略的失效方式。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from customer_profile.rules import default as default_registry
from customer_profile.rules import guard as gd

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def registry():
    return default_registry()


@pytest.fixture(scope="module")
def marks():
    return gd.scan_marks()


def test_no_orphan_marks(marks, registry):
    orphans = gd.orphan_marks(marks, registry)
    assert not orphans, "孤儿标记（登记表里没有或已退役）：\n" + "\n".join(
        str(mark) for mark in orphans
    )


def test_no_unlanded_rules(marks, registry):
    unlanded = gd.unlanded_rules(marks, registry)
    assert not unlanded, (
        "登记的 active 规则检索不到任何落点（文档里写了、代码里没做）："
        + ", ".join(unlanded)
    )


def _resolve_test_ref(ref: str) -> tuple[Path, str]:
    path_part, _, name = ref.partition("::")
    return REPO_ROOT / path_part, name


def test_every_active_rule_references_existing_tests(registry):
    missing: list[str] = []
    for rule in registry.active():
        for ref in rule.tests:
            path, name = _resolve_test_ref(ref)
            if not path.is_file():
                missing.append(f"{rule.id}: 测试文件不存在 {ref}")
                continue
            if name and f"def {name}(" not in path.read_text(encoding="utf-8"):
                missing.append(f"{rule.id}: 测试用例不存在 {ref}")
    assert not missing, "active 规则引用了不存在的测试：\n" + "\n".join(missing)


def test_registry_is_consistent(registry):
    assert registry.verify() == []


def test_every_landed_rule_is_registered_active(registry, marks):
    """反向确认：真实树上的落点集合与 active 规则集合完全相等。"""
    landed = {mark.rule_id for mark in marks}
    active = {rule.id for rule in registry.active()}
    assert landed == active, (
        f"多出的落点：{sorted(landed - active)}；缺落点的规则：{sorted(active - landed)}"
    )
