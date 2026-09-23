"""模板资源仓库。

提示词正文受 Git 管理、外置为文本资源（规划 §6.5）。**模板语法可改，模板内容不可改**——
任何提示词文本改动都属业务优化，须单独提出并获得确认。

因此本模块只做两件事：按名字取回逐字符原文、按变量表渲染；不做任何「顺手修正」
（不 trim、不补换行、不规范化空白）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .execution.context import MissingVariable, coerce_to_text

DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@dataclass(frozen=True, slots=True)
class Template:
    """一份外置模板。``text`` 是逐字符原文。"""

    name: str
    text: str
    path: Path

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def chars(self) -> int:
        return len(self.text)


class TemplateRepository:
    """按名字加载模板文件。名字即文件名（不含扩展名）。"""

    def __init__(self, root: Path | str = DEFAULT_TEMPLATE_DIR) -> None:
        self.root = Path(root)
        self._cache: dict[str, Template] = {}

    def load(self, name: str) -> Template:
        if name in self._cache:
            return self._cache[name]

        path = self._resolve(name)
        # 以 UTF-8 读，且 newline="" 保留原始换行，不做 CRLF 归一
        with path.open("r", encoding="utf-8", newline="") as handle:
            text = handle.read()
        template = Template(name=name, text=text, path=path)
        self._cache[name] = template
        return template

    def _resolve(self, name: str) -> Path:
        candidate = self.root / name
        if candidate.suffix == "":
            candidate = candidate.with_suffix(".txt")
        if not candidate.is_file():
            raise FileNotFoundError(
                f"模板 {name!r} 不存在：{candidate}。"
                "提示词正文属资源文件，须随代码一起提交"
            )
        return candidate

    def render(
        self,
        name: str,
        variables: Mapping[str, Any] | None = None,
        *,
        strict: bool = True,
    ) -> str:
        """渲染模板。

        ``strict=True`` 时缺失变量报错。缺省严格，因为静默渲染空串会把错误推到很远
        的下游（LLM 收到残缺提示词，结果不可解释）。
        """
        template = self.load(name)
        return render_text(template.text, variables or {}, strict=strict)

    def all_names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.txt"))

    def clear_cache(self) -> None:
        self._cache.clear()


DIFY_REF_PATTERN = re.compile(r"\{\{#([^#{}]+)#\}\}")
"""Dify 的节点引用：``{{#node_id.field#}}``。

外置的正文里**原样保留**这种写法（正文不可改，见模块说明）。渲染时把它映射成本次
调用已提供的变量值，因此这里先做一遍引用翻译，再做普通的 ``{{ 名字 }}`` 替换。
"""

BARE_NAME_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")

LITERAL_PATTERN = re.compile(r'\{\{\s*"([^"]*)"\s*\}\}')
"""Dify 里 ``{{ "字面量" }}`` 表示直接输出该字符串（如无数据时的固定提示语）。"""

JOIN_PATTERN = re.compile(
    r"""\{\{\s*([A-Za-z_][\w.]*)\s*\|\s*join\(\s*"""
    r"""(['"])((?:\\.|(?!\2).)*)\2\s*\)\s*\}\}""",
    re.S,
)
"""``{{ arr | join('\n') }}``：把列表按给定分隔符拼成文本。

全项目只用到 ``join`` 这一个过滤器（8 处，均在各工作流的「多图/多文件内容聚合」节点），
因此这里只实现它，不引入通用模板引擎：多一个依赖就多一处行为差异面。
"""


def render_text(
    text: str, variables: Mapping[str, Any], *, strict: bool = True
) -> str:
    """渲染模板正文。

    按顺序处理三种占位符：

    1. ``{{#node_id.field#}}``：Dify 引用。按 ``node_id.field`` 在 ``variables`` 里
       查同名键，或按 ``node_id`` 取对象再取字段；
    2. ``{{ "字面量" }}``：直接输出引号内的内容（Dify 的常量写法）；
    3. ``{{ 名字 }}``：普通变量。

    不使用通用 ``str.replace`` 粗暴替换全部内容（`Dify迁移任务说明.md` §5.1）：
    只识别上述形态，且逐个占位符替换。
    """

    def resolve(key: str) -> Any:
        if key in variables:
            return variables[key]
        root, _, rest = key.partition(".")
        if root in variables and rest:
            return _walk(variables[root], rest.split("."))
        if strict:
            raise MissingVariable(
                f"模板变量 {key!r} 未提供；已提供：{sorted(variables)}"
            )
        return ""

    def replace_ref(match: "re.Match[str]") -> str:
        return coerce_to_text(resolve(match.group(1).strip()))

    def replace_literal(match: "re.Match[str]") -> str:
        return match.group(1)

    def replace_join(match: "re.Match[str]") -> str:
        value = resolve(match.group(1))
        separator = match.group(3).encode("utf-8").decode("unicode_escape")
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return separator.join(coerce_to_text(item) for item in value)
        return coerce_to_text(value)

    def replace_bare(match: "re.Match[str]") -> str:
        return coerce_to_text(resolve(match.group(1)))

    text = DIFY_REF_PATTERN.sub(replace_ref, text)
    text = LITERAL_PATTERN.sub(replace_literal, text)
    text = JOIN_PATTERN.sub(replace_join, text)
    return BARE_NAME_PATTERN.sub(replace_bare, text)


def find_placeholders(text: str) -> list[str]:
    """列出模板引用的全部来源（去重、保持出现顺序）。

    包含 Dify 引用（``{{#node.field#}}`` → ``node.field``）与普通变量名，用于校验
    节点定义的绑定是否齐全。
    """
    seen: dict[str, None] = {}
    for match in DIFY_REF_PATTERN.finditer(text):
        seen.setdefault(match.group(1).strip(), None)
    for match in JOIN_PATTERN.finditer(text):
        seen.setdefault(match.group(1), None)
    for match in BARE_NAME_PATTERN.finditer(text):
        seen.setdefault(match.group(1), None)
    return list(seen)


def _walk(value: Any, path: list[str]) -> Any:
    for step in path:
        if isinstance(value, Mapping):
            value = value.get(step)
        else:
            return None
    return value
