"""品牌名残留守卫（T3-1）：提示词与代码里不得再出现具体品牌/车型名。

## 它拦的是什么

第 0–2 期把「广汽埃安」的资产从表达层摘掉、把提示词里的品牌举例中性化。这些改动
**不会自我维持**：下一次有人复制一段旧提示词、或从别处粘一个示例进模板，品牌名就回来了，
而产物只是「慢慢偏掉」，不会有任何报错。

守卫把这件事从「靠人记得」变成「跑一次就知道」。判据只有一条：
**出现在扫描范围内的品牌名，必须在豁免清单里有对应条目并写明理由。**

## 品牌清单从哪来

- **本方品牌**从当前知识包的 ``brand.json`` / ``pack.json`` 现读——换合作伙伴后守卫
  自动跟着换，不需要改代码。这正是知识库存在的意义在治理侧的体现。
- **外部品牌**是一份固定的历史清单（见 :data:`FOREIGN_BRANDS`）。它们不在任何知识包里，
  却能证明「曾经被误写进提示词」，因此永久列入。

## 三层豁免

1. **知识包目录**：``knowledge/packs/`` 下就是品牌与车型知识本身，整目录豁免。
2. **显式豁免条目**：按「路径 + 品牌」成对声明，并附理由。成对而不是按路径整体豁免——
   豁免整个文件会让该文件里任何新品牌名一并放行。
3. **归档目录**：``docs/history/`` 不在扫描范围（本守卫只扫 ``src/``）。

## 不判什么

不判「这个品牌名用得对不对」，只判「它出现在不该出现的位置」。客户原话、竞品名在
知识包里、契约字段名（``knowledge_of_mengshi``）都属合法用途，靠豁免清单表达。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[3]
"""仓库根：``src/customer_profile/knowledge/brand_guard.py`` 上溯四层
（knowledge → customer_profile → src → 仓库根）。

命中里的路径一律给**仓库相对**形态（``src/customer_profile/...``），
这样报告里的位置可直接点开，与豁免清单的写法也一致。
"""

DEFAULT_SCAN_ROOT = Path(__file__).resolve().parents[1]
"""缺省扫描范围：``src/customer_profile/``。

只扫包内目录而不是整个 ``src/``：``src/`` 下只有 ``customer_profile`` 一个包，两者等价，
但写死包名能让「扫描范围」这件事在代码里显式可见。
"""

EXEMPTIONS_FILE = Path(__file__).resolve().parent / "brand_exemptions.json"

SCAN_SUFFIXES: tuple[str, ...] = (".py", ".txt", ".md", ".json")

FOREIGN_BRANDS: tuple[str, ...] = (
    # 旧合作方（第 0 期止血的对象）——永久列入，防止旧资产回流
    "埃安",
    "AION",
    "昊铂",
    "广汽",
    "ADiGO",
    "星灵架构",
    # 常见竞品。它们只应出现在知识包里，不应当出现在提示词正文
    "比亚迪",
    "特斯拉",
    "小鹏",
    "蔚来",
    "理想汽车",
    "问界",
    "享界",
    "坦克",
    "方程豹",
    "豹8",
    "极氪",
    "领克",
    "吉利",
    "长安汽车",
    "奇瑞",
    "红旗",
    "传祺",
    "丰田",
    "大众汽车",
    "本田",
    "日产",
    "别克",
    "奥迪",
    "宝马",
    "奔驰",
    "雷克萨斯",
    "零跑",
    "Model Y",
    "BMW",
)
"""外部品牌清单。

刻意**不**收「华为」「鸿蒙」「乾崑」：它们是技术生态名，本方车型的卖点里就写着
「华为乾崑 ADS」「鸿蒙座舱」，属产品事实而非品牌泄漏。把它们列入会制造大量豁免噪声，
噪声一多守卫就会被无视。
"""


@dataclass(frozen=True, slots=True)
class Hit:
    """一处命中。相对路径以便在报告里直接点开。"""

    path: str
    line: int
    brand: str
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.brand} → {self.text}"


def our_brand_names(pack: Any = None) -> tuple[str, ...]:
    """本方品牌全部称谓。包不可用时返回空——守卫只靠外部清单仍能工作。"""
    if pack is None:
        try:
            from . import current_pack

            pack = current_pack()
        except Exception:  # noqa: BLE001 —— 守卫本身不能因为装载失败而崩
            return ()
    names: list[str] = []
    brand = getattr(pack, "brand", None)
    identity = getattr(brand, "our_brand", None)
    if identity is not None:
        names.extend(identity.all_names())
    manifest = getattr(pack, "manifest", None)
    if manifest is not None:
        names.extend(getattr(manifest, "brands", ()) or ())
    seen: dict[str, None] = {}
    for name in names:
        text = str(name).strip()
        if text:
            seen.setdefault(text, None)
    return tuple(seen)


def forbidden_names(pack: Any = None) -> tuple[str, ...]:
    """本方称谓 + 外部品牌，去重后按长度降序（长名先匹配，避免「猛士」吃掉「东风猛士」）。"""
    merged = list(our_brand_names(pack)) + list(FOREIGN_BRANDS)
    unique = {name: None for name in merged if name}
    return tuple(sorted(unique, key=len, reverse=True))


@dataclass(frozen=True, slots=True)
class Exemption:
    """一条豁免：某个路径下的某个品牌名，附理由。"""

    path: str
    brand: str
    reason: str

    def covers(self, relative: str, brand: str) -> bool:
        if brand != self.brand:
            return False
        # 以 / 结尾表示整目录
        if self.path.endswith("/"):
            return relative.startswith(self.path)
        return relative == self.path


def load_exemptions(path: Path | None = None) -> tuple[Exemption, ...]:
    """读豁免清单。文件缺失视为空清单（那会让守卫更严，而不是更松）。"""
    target = path or EXEMPTIONS_FILE
    if not target.is_file():
        return ()
    data = json.loads(target.read_text(encoding="utf-8"))
    items = data.get("exemptions") if isinstance(data, dict) else data
    out: list[Exemption] = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        brand = str(item.get("brand") or "").strip()
        where = str(item.get("path") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if brand and where and reason:
            out.append(Exemption(path=where, brand=brand, reason=reason))
    return tuple(out)


SELF_FILES: tuple[str, ...] = (
    "src/customer_profile/knowledge/brand_guard.py",
    "src/customer_profile/knowledge/brand_exemptions.json",
)
"""结构性跳过：这些文件里的品牌名是**定义**，不是泄漏。"""


def _structurally_skipped(relative: str) -> bool:
    # 知识包与竞品库都是**知识本身**：那里的品牌名是数据，不是泄漏。
    slashed = f"/{relative}"
    if "/knowledge/packs/" in slashed or "/knowledge/competitors/" in slashed:
        return True
    return relative in SELF_FILES


def _relative(path: Path, base: Path) -> str:
    """报告与豁免比对用的相对路径。

    两级回退，因为扫描根可能不在仓库内（测试用临时目录）：

    1. 相对**仓库根**——正式扫描时给出的形态，可点开、与豁免清单一致；
    2. 相对**扫描根**——临时目录上的扫描用这个，否则绝对路径永远匹配不上豁免条目。

    不做「取文件名」这种第三级回退：两个不同目录下的同名文件会被混成一谈。
    """
    for anchor in (REPO_ROOT, base):
        try:
            return path.relative_to(anchor).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def iter_scan_files(root: Path | None = None) -> list[Path]:
    base = root or DEFAULT_SCAN_ROOT
    return sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in SCAN_SUFFIXES)


def scan(
    root: Path | None = None,
    *,
    exemptions: Iterable[Exemption] | None = None,
    pack: Any = None,
) -> list[Hit]:
    """扫描全部文件，返回**未被豁免**的命中。"""
    base = root or DEFAULT_SCAN_ROOT
    allowed = tuple(exemptions) if exemptions is not None else load_exemptions()
    names = forbidden_names(pack)
    hits: list[Hit] = []
    for path in iter_scan_files(base):
        relative = _relative(path, base)
        # 三处结构性跳过，不写进豁免清单——它们**必然**含有品牌名，
        # 写成豁免条目只会让清单充满噪声，而噪声会让守卫失去约束力：
        #   1. 知识包目录：那里就是知识本身；
        #   2. 本守卫自身：品牌词表必须写出这些名字；
        #   3. 豁免清单本身：条目里要写品牌名。
        if _structurally_skipped(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            # 最长名优先并占位：``东风猛士`` 命中后，其内部的 ``猛士`` 不再重复记一条。
            # 不这样做，一个含「东风猛士」的句子会同时产生 4 条命中（四个称谓全中），
            # 报告被噪声淹没，豁免清单也要为同一句话写四遍。
            taken: list[tuple[int, int]] = []
            for name in names:
                start = line.find(name)
                if start < 0:
                    continue
                end = start + len(name)
                if any(start >= a and end <= b for a, b in taken):
                    continue
                taken.append((start, end))
                if any(item.covers(relative, name) for item in allowed):
                    continue
                hits.append(
                    Hit(path=relative, line=number, brand=name, text=line.strip()[:120])
                )
    return hits


def uncovered_exemptions(
    root: Path | None = None, *, exemptions: Iterable[Exemption] | None = None
) -> list[Exemption]:
    """已不再命中的豁免条目。

    过期豁免会让守卫悄悄变松：文件里的品牌名早被删了，豁免却留着，日后同一位置再写回来
    就没人拦。与规则层的「孤儿标记」同一性质，因此也检查。
    """
    base = root or DEFAULT_SCAN_ROOT
    allowed = tuple(exemptions) if exemptions is not None else load_exemptions()
    names = forbidden_names()
    seen: set[tuple[str, str]] = set()
    for path in iter_scan_files(base):
        relative = _relative(path, base)
        if _structurally_skipped(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for item in allowed:
            if not item.covers(relative, item.brand):
                continue
            if item.brand in text:
                seen.add((item.path, item.brand))
    return [item for item in allowed if (item.path, item.brand) not in seen]


def report(hits: list[Hit], stale: list[Exemption]) -> dict[str, Any]:
    return {
        "ok": not hits and not stale,
        "hits": [
            {"path": h.path, "line": h.line, "brand": h.brand, "text": h.text} for h in hits
        ],
        "stale_exemptions": [
            {"path": s.path, "brand": s.brand, "reason": s.reason} for s in stale
        ],
        "brand_count": len(forbidden_names()),
    }
