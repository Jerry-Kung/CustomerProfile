"""检索守卫：保证「一处登记」与「多处落点」能对上。

两条静态检查（设计 §9.3），零 token、进测试集：

1. **孤儿标记**：代码/提示词里出现的每个 ``rule:`` 编号，必须已在登记表中且
   ``status=active``。规则退役后残留标记即红——这正是「删规则删了一半」的形态。
2. **未落地规则**：登记表里每个 ``active`` 规则，必须至少检索到一处落点。
   只登记不落地即红——挡住「文档里写了、代码里没做」。

守卫只做**编号存在性**检查，不做语义检查。语义是否一致仍靠测试与核读，但位置的
完整性可以完全机器保证，这覆盖了「改规则时漏改一处」事故的绝大部分。

本模块不含 pytest 断言：测试在 ``tests/rules/test_guard.py``，这里只给检索原语，
便于离线脚本复用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .registry import RuleRegistry, default as default_registry

_ID = r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+"

#: ``# rule: <ID> 一句话说明`` —— Python 注释落点。
COMMENT_MARKER = re.compile(rf"#+\s*rule:\s*({_ID})")

#: ``<!-- rule: <ID> begin -->`` —— 提示词成对标记。
TEXT_MARKER = re.compile(rf"<!--\s*rule:\s*({_ID})\s+(?:begin|end)\s*-->")

#: ``{"tag": "...", "rule": "<ID>"}`` —— 数据表的并行登记键。
TABLE_MARKER = re.compile(rf"""["']rule["']\s*:\s*["']({_ID})["']""")

MARKERS: tuple[re.Pattern[str], ...] = (COMMENT_MARKER, TEXT_MARKER, TABLE_MARKER)
"""三种落点形态。

**必须带形态限定，不能裸查 ``rule:``**：源码里到处是断言留痕字典的 ``"rule":
"a_attribution"``，裸查会把每一个字典键都当成落点，守卫随即淹死在误报里——一个
误报率接近 100% 的守卫等于没有守卫。同理，只有大写短横线编号（:data:`_ID`）算落点，
迁移期的旧裸串（``f_trade_in`` 这类小写下划线）**不是**标记，它们由登记表的
``legacy_codes`` 承接。
"""


@dataclass(frozen=True, slots=True)
class Mark:
    """一处落点。"""

    rule_id: str
    path: str
    line: int
    kind: str
    """``code`` / ``text``。"""

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.kind}] {self.rule_id}"


def _repo_root() -> Path:
    """仓库根：``src/customer_profile/rules/guard.py`` 上溯四层。"""
    return Path(__file__).resolve().parents[3]


def default_scan_paths(root: Path | None = None) -> list[Path]:
    """缺省检索范围：全部源码（含模板正文 ``src/customer_profile/templates``）。"""
    base = root if root is not None else _repo_root()
    return [base / "src"]


def iter_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(p for p in path.rglob("*") if p.suffix in {".py", ".txt"}))
    return files


def extract_ids(line: str) -> list[str]:
    """从一行文本里取出全部规则编号。三种标记形态任一命中即可。"""
    found: list[str] = []
    for marker in MARKERS:
        found.extend(match.group(1) for match in marker.finditer(line))
    return found


def scan_marks(
    paths: list[Path] | None = None, *, root: Path | None = None
) -> list[Mark]:
    """扫描全部落点。成对标记的**开始行与结束行都记**。

    删规则时两行都要删；落点清单里少一行，机械删除就会漏掉一半。
    """
    base = root if root is not None else _repo_root()
    files = iter_files(paths if paths is not None else default_scan_paths(base))
    marks: list[Mark] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # 模板与源码都是 UTF-8；读不了的说明是二进制残留，跳过而不是让守卫崩掉。
            continue
        kind = "text" if path.suffix == ".txt" else "code"
        try:
            display = path.relative_to(base).as_posix()
        except ValueError:
            display = path.as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            for rule_id in extract_ids(line):
                marks.append(Mark(rule_id=rule_id, path=display, line=number, kind=kind))
    return marks


def orphan_marks(marks: list[Mark], registry: RuleRegistry | None = None) -> list[Mark]:
    """登记表里没有、或状态不是 active 的落点。"""
    table = registry if registry is not None else default_registry()
    issues: list[Mark] = []
    for mark in marks:
        rule = table.get(mark.rule_id)
        if rule is None or rule.status != "active":
            issues.append(mark)
    return issues


def unlanded_rules(marks: list[Mark], registry: RuleRegistry | None = None) -> list[str]:
    """``active`` 但检索不到任何落点的规则编号。

    **挂了 prompt 通道的规则由正文标记充当落点**，不需要额外的代码标记——
    它的正文就在 ``texts/*.txt`` 的成对标记里，扫描范围已覆盖（M4 起）。
    """
    table = registry if registry is not None else default_registry()
    landed = {mark.rule_id for mark in marks}
    return [rule.id for rule in table.active() if rule.id not in landed]


def index_by_rule(marks: list[Mark]) -> dict[str, list[Mark]]:
    """``{rule_id: [落点]}``，供「删一条规则」时逐处摘除。"""
    grouped: dict[str, list[Mark]] = {}
    for mark in marks:
        grouped.setdefault(mark.rule_id, []).append(mark)
    for entries in grouped.values():
        entries.sort(key=lambda item: (item.path, item.line))
    return dict(sorted(grouped.items()))
