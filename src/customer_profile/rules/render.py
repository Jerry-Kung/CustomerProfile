"""prompt 通道：把规则正文渲染成提示词片段。

正文不放在 Python 字符串里，放在 ``rules/texts/<簇>.txt``，用成对标记包裹整段：

```
<!-- rule: <ID> begin -->
从沟通记录里的时间表述推断（「这个月想定」「过完年再看」），**不得**用手机号归属地猜。
<!-- rule: <ID> end -->
```

三条约束（设计 §9.1）：

1. **成对标记包裹整段，不做行内标注。** 改规则时要能整段删掉。
2. **一段只挂一个编号。** 一处被两条规则共用，说明这两条该拆。
3. **渲染结果不得含标记本身。** 标记是落点凭据，不是提示词内容。

本模块在 M0 只提供「切段 / 渲染」的原语；接入 L1 输入是 M4 的事。在此之前
:func:`render` 不被任何生产路径调用，由测试单独覆盖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .registry import ID_PATTERN, Rule, RuleRegistry, default as default_registry

TEXT_DIR = Path(__file__).resolve().parent / "texts"
"""规则正文目录。按簇一个文件，文件名即簇名。"""

_BEGIN = re.compile(r"<!--\s*rule:\s*(\S+)\s+begin\s*-->")
_END = re.compile(r"<!--\s*rule:\s*(\S+)\s+end\s*-->")


class RuleTextError(ValueError):
    """规则正文的标记结构不合法。"""


@dataclass(frozen=True, slots=True)
class RuleBlock:
    """一段被标记包裹的规则正文。"""

    rule_id: str
    text: str
    cluster: str = ""

    def strip_markers(self) -> str:
        return self.text


def parse_blocks(text: str, *, cluster: str = "") -> list[RuleBlock]:
    """把一份正文切成规则段。未标记的正文（角色、任务、输出格式）原样跳过。

    结构错误一律抛：开始标记没有配对、配对顺序颠倒、编号不合形态。这些都是
    「标记被打断」的形态，静默续读会让半段规则混进提示词。
    """
    blocks: list[RuleBlock] = []
    lines = text.splitlines(keepends=True)
    open_id: str | None = None
    buffer: list[str] = []
    for number, line in enumerate(lines, start=1):
        begin = _BEGIN.search(line)
        end = _END.search(line)
        if begin and end:
            raise RuleTextError(f"第 {number} 行开始与结束标记在同一行，无法整段摘除")
        if begin:
            if open_id is not None:
                raise RuleTextError(
                    f"第 {number} 行出现 {begin.group(1)} 的开始标记，但 {open_id} 尚未结束"
                )
            rule_id = begin.group(1)
            if not ID_PATTERN.match(rule_id):
                raise RuleTextError(f"第 {number} 行的规则编号 {rule_id!r} 不合形态")
            open_id = rule_id
            buffer = []
            continue
        if end:
            rule_id = end.group(1)
            if open_id is None:
                raise RuleTextError(f"第 {number} 行出现 {rule_id} 的结束标记，但没有开始标记")
            if rule_id != open_id:
                raise RuleTextError(
                    f"第 {number} 行结束标记是 {rule_id}，但当前段是 {open_id}"
                )
            blocks.append(RuleBlock(rule_id=open_id, text="".join(buffer), cluster=cluster))
            open_id = None
            buffer = []
            continue
        if open_id is not None:
            buffer.append(line)
    if open_id is not None:
        raise RuleTextError(f"规则 {open_id} 的开始标记没有对应的结束标记")
    return blocks


def load_blocks(directory: Path | str | None = None) -> dict[str, RuleBlock]:
    """装载全部规则正文，返回 ``{rule_id: block}``。同一编号出现在两个文件里即报错。"""
    root = Path(directory) if directory is not None else TEXT_DIR
    if not root.is_dir():
        return {}
    blocks: dict[str, RuleBlock] = {}
    for path in sorted(root.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        for block in parse_blocks(text, cluster=path.stem):
            if block.rule_id in blocks:
                raise RuleTextError(
                    f"规则 {block.rule_id} 的正文同时出现在 "
                    f"{blocks[block.rule_id].cluster}.txt 与 {path.stem}.txt"
                )
            blocks[block.rule_id] = block
    return blocks


def render(
    *,
    scope: str | Iterable[str] | None = None,
    stage: str | None = None,
    registry: RuleRegistry | None = None,
    blocks: Mapping[str, RuleBlock] | None = None,
    separator: str = "\n\n",
) -> str:
    """按作用域与阶段取出规则正文，拼成一段提示词。

    - ``scope``：字段名、字段簇名或 ``"*"``；``None`` 表示不限。
    - ``stage``：``extract`` / ``express`` 等；``None`` 表示不限。
    - 只渲染 ``status=active`` 且挂了 ``prompt`` 通道的规则；
    - 只渲染有正文的规则——登记了 prompt 通道却没有正文，由守卫在测试里报红，
      这里跳过而不是抛错，避免一处缺文挡掉整段提示词。
    """
    table = registry if registry is not None else default_registry()
    pools = blocks if blocks is not None else load_blocks()
    wanted = None
    if scope is not None:
        wanted = {scope} if isinstance(scope, str) else set(scope)

    pieces: list[str] = []
    for rule in table.active():
        if not rule.enables_channel("prompt"):
            continue
        if stage is not None and not rule.enables_stage(stage):
            continue
        if wanted is not None and "*" not in wanted and not (set(rule.scope) & wanted):
            continue
        block = pools.get(rule.id)
        if block is None:
            continue
        text = block.text.strip()
        if text:
            pieces.append(text)
    return separator.join(pieces)


def field_notes(
    *,
    stage: str = "extract",
    registry: RuleRegistry | None = None,
    blocks: Mapping[str, RuleBlock] | None = None,
) -> dict[str, str]:
    """``{字段: 正文}``：挂在 prompt 通道上的**逐字段**规则正文。

    与 :func:`render` 的分工：那个把整段提示词拼成一节，用在「规则自成一段」的场合；
    这个给《目标字段清单》这类**按字段逐行**渲染的场景——正文要贴着字段出现，拼成
    一整节模型就读不到归属了。

    **多字段规则只在它的第一个字段下渲染一次**（2026-10-28）：一条规则声明五个字段
    就把同一段正文抄五遍，是最贵的写法——SPEC-17 曾因此让提示词多出 920 字，全部是
    重复。其余适用字段由 :func:`field_note_extra_scopes` 给出，调用方渲染成一行
    「本条口径同样适用于：…」。

    ``scope=("*",)`` 的规则不参与：全局规则没有「贴在哪个字段下面」这回事，调用方
    要用它就该走 :func:`render`。同一字段被两条 active 规则占用在启动期自检即报错
    （:meth:`RuleRegistry.verify`），不会静默丢正文。
    """
    table = registry if registry is not None else default_registry()
    pools = blocks if blocks is not None else load_blocks()
    notes: dict[str, str] = {}
    for rule in table.active():
        if not rule.enables_channel("prompt"):
            continue
        if not rule.enables_stage(stage):
            continue
        block = pools.get(rule.id)
        if block is None:
            continue
        text = block.text.strip()
        if not text:
            continue
        for field in rule.scope:
            if field == "*":
                continue
            notes.setdefault(field, text)
            break
    return notes


def global_notes(
    *,
    stage: str = "extract",
    registry: RuleRegistry | None = None,
    blocks: Mapping[str, RuleBlock] | None = None,
    separator: str = "\n\n",
) -> str:
    """**全局规则**的正文（``scope=("*",)`` 且挂 prompt 通道），拼成一段。

    **不能复用** :func:`render`：那里 ``scope="*"`` 表示「不限制」——把这个值当作用域
    过滤器传进去，结果是**全部**规则都被放行，而不是只取全局那几条，语义正好相反。
    全局规则也没有「贴在哪个字段下面」这回事，因此同样不进 :func:`field_notes`。

    段落按登记顺序拼接；``stage`` 与 :func:`field_notes` 同口径过滤。
    """
    table = registry if registry is not None else default_registry()
    pools = blocks if blocks is not None else load_blocks()
    pieces: list[str] = []
    for rule in table.active():
        if not rule.enables_channel("prompt") or not rule.enables_stage(stage):
            continue
        if "*" not in rule.scope:
            continue
        block = pools.get(rule.id)
        if block is None:
            continue
        text = block.text.strip()
        if text:
            pieces.append(text)
    return separator.join(pieces)


def field_note_extra_scopes(
    *,
    stage: str = "extract",
    registry: RuleRegistry | None = None,
    blocks: Mapping[str, RuleBlock] | None = None,
) -> dict[str, tuple[str, ...]]:
    """``{主字段: 其余适用字段}``：多字段规则在正文之外还要标注的字段名。

    与 :func:`field_notes` 配对使用——正文只抄一次，其余字段以一行列名带过。
    只列**真的会被渲染到字段清单里**的字段（交集 ``EXTRACTION_KEYS`` 等由调用方
    保证），这里照规则声明原样返回。
    """
    table = registry if registry is not None else default_registry()
    pools = blocks if blocks is not None else load_blocks()
    extra: dict[str, tuple[str, ...]] = {}
    for rule in table.active():
        if not rule.enables_channel("prompt") or not rule.enables_stage(stage):
            continue
        block = pools.get(rule.id)
        if block is None or not block.text.strip():
            continue
        fields = tuple(field for field in rule.scope if field != "*")
        if len(fields) > 1:
            extra[fields[0]] = fields[1:]
    return extra


def render_rule(rule: Rule, *, blocks: Mapping[str, RuleBlock] | None = None) -> str:
    """取出单条规则的正文（去掉标记）。缺失即抛——调用方明确要这一条，不该静默给空。"""
    pools = blocks if blocks is not None else load_blocks()
    block = pools.get(rule.id)
    if block is None:
        raise RuleTextError(f"规则 {rule.id} 挂了 prompt 通道，但没有正文（texts/ 下找不到标记段）")
    return block.text.strip()
