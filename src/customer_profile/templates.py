"""模板资源仓库。

提示词正文受 Git 管理、外置为文本资源（规划 §6.5）。**模板语法可改，模板内容不可改**——
任何提示词文本改动都属业务优化，须单独提出并获得确认。

因此本模块只做两件事：按名字取回逐字符原文、按变量表渲染；不做任何「顺手修正」
（不 trim、不补换行、不规范化空白）。
"""

from __future__ import annotations

import hashlib
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


def render_text(
    text: str, variables: Mapping[str, Any], *, strict: bool = True
) -> str:
    """用 ``{{ variable }}`` 语法做纯文本替换。

    不使用通用 ``str.replace`` 粗暴替换全部内容（`Dify迁移任务说明.md` §5.1）；
    只识别 ``{{ 名字 }}`` 形态，且**只替换一次**每处占位符。
    """
    import re

    pattern = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")

    def replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in variables:
            return coerce_to_text(variables[key])
        root = key.split(".", 1)[0]
        if root in variables:
            return coerce_to_text(_walk(variables[root], key.split(".")[1:]))
        if strict:
            raise MissingVariable(
                f"模板变量 {key!r} 未提供；已提供：{sorted(variables)}"
            )
        return ""

    return pattern.sub(replace, text)


def find_placeholders(text: str) -> list[str]:
    """列出模板中的变量名（去重、保持出现顺序）。用于校验绑定是否齐全。"""
    import re

    pattern = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")
    seen: dict[str, None] = {}
    for match in pattern.finditer(text):
        seen.setdefault(match.group(1), None)
    return list(seen)


def _walk(value: Any, path: list[str]) -> Any:
    for step in path:
        if isinstance(value, Mapping):
            value = value.get(step)
        else:
            return None
    return value
