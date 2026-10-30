"""竞品库：对外部世界的认识，与「我们在卖什么」分开管理。

## 为什么不放进合作方知识包

``knowledge/packs/<pack_id>/`` 装的是**本方**资料：品牌、在售车型、价格、话术。
换合作伙伴时它整包替换。竞品库不一样——它描述「别人家有什么」：

- **变化快**：竞品改款、降价、上新，频率高于本方资料；
- **换伙伴不变**：换一家合作方，客户拿来比较的仍是那几台车，竞品库不该跟着换；
- **生效期独立**：本方资料按上市节奏更新，竞品资料按市场观察更新，两条时间线不同。

塞进合作方包，会让「换伙伴」与「换竞品情报」两件事绑死，改一个必须动另一个。

## 与知识包的三处结构对应

| | 知识包 | 竞品库 |
|---|---|---|
| 目录 | `knowledge/packs/<id>/` | `knowledge/competitors/<id>/` |
| 清单 | `pack.json` | `manifest.json` |
| 内容 | `models.json` + `brand.json` | `competitors.json` |
| 配置 | `KNOWLEDGE_PACK` | `KNOWLEDGE_COMPETITORS` |
| 留痕 | `content_signature` | `content_signature` |

两套结构刻意同形：读代码的人只需理解一次「清单 + 内容 + 指纹」，就能读懂两边。

## 三个口径

1. **默认不启用**。``KNOWLEDGE_COMPETITORS`` 缺省空串——业务方尚未提供竞品资料
   （见 ``docs/feature-knowledge-base/07`` Q8）。空库的产物与没有这个模块时逐字节一致。
2. **装载失败退化，不抛**。与知识包同一取向：硬失败留给预检，请求路径上只退化。
   竞品资料缺了，卡片少一段对比，不该让整张卡作废。
3. **装载期严格校验**。编号唯一、称谓全局唯一、价格区间自洽、定位非空。别名表是
   污染高发点，与车型条目同一个理由。

## 明确不做的事

- **不判「谁的更好」**。条目里的 ``gaps`` 写的是「客户会在意、我们可以谈」的客观差异，
  不是贬损话术；``talking_points`` 也只能基于条目里已有的事实，不得扩展成攻击性表述。
- **不用它做归属判定**。``resolve_brand`` 仍是唯一的三方归属入口（本方 / 竞品 / 未知）；
  竞品库只提供**资料**，供 K4 在客户提到某台竞品时报出可比之处。两者共用词表会让
  「这个品牌算不算竞品」出现第二个判据，而那正是要避免的两处真源。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from loguru import logger
from pydantic import BaseModel, Field, field_validator, model_validator

from ..settings import get_settings

DEFAULT_COMPETITORS_DIR = Path(__file__).resolve().parent / "competitors"
"""库内缺省目录：``knowledge/competitors/<library_id>/``。"""

POWERTRAINS: tuple[str, ...] = ("bev", "phev", "erev", "ice", "unknown")
"""动力形式。比车型条目多一个 ``unknown``：竞品参数常有一两项查不到，
而「查不到」与「没有」是两件事，不能混。"""

PRICE_TOLERANCE = 0.011


class CompetitorError(ValueError):
    """竞品库不合规。``issues`` 含全部问题，便于一次修完。"""

    def __init__(self, issues: list[str] | str) -> None:
        self.issues = [issues] if isinstance(issues, str) else list(issues)
        super().__init__(
            "竞品库校验未通过：\n" + "\n".join(f"  - {item}" for item in self.issues)
        )


# ====================================================================
# 数据模型
# ====================================================================


class CompetitorPrice(BaseModel):
    """竞品价格带。**只有一段**——不看定制版，因为竞品的定制版销售谈不到。"""

    min_wan: float
    max_wan: float
    as_of: str
    note: str = ""

    @model_validator(mode="after")
    def _ordered(self) -> "CompetitorPrice":
        if self.min_wan > self.max_wan:
            raise ValueError(f"竞品价格区间倒置：{self.min_wan} > {self.max_wan}")
        try:
            date.fromisoformat(self.as_of)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"价格口径日期 {self.as_of!r} 不是 YYYY-MM-DD") from exc
        return self


class Point(BaseModel):
    """一条对比要点。``title`` 是短标签，``text`` 是给销售看的一句话。"""

    title: str
    text: str = ""

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("对比要点的标题不得为空")
        return value


class CompetitorEntry(BaseModel):
    """一款竞品车型。分三层：标识 / 参数 / 对比。"""

    # ---- 标识层
    model_id: str
    canonical_name: str
    brand: str
    aliases: list[str] = Field(default_factory=list)
    asr_variants: list[str] = Field(default_factory=list)

    # ---- 参数层
    price: CompetitorPrice
    powertrain: list[str] = Field(default_factory=list)
    body: str = ""
    seats: int = 0

    # ---- 对比层
    positioning: str
    """一句话定位：这台车对外讲的是什么。"""
    strengths: list[Point] = Field(default_factory=list)
    """客户会认可的强项。**写事实，不写结论**——「轴距 3 米」而不是「空间完胜」。"""
    gaps: list[Point] = Field(default_factory=list)
    """客户可能在意的短板。同样写事实，由销售判断要不要提。"""
    talking_points: list[Point] = Field(default_factory=list)
    """应对要点：客户问到这台车时怎么接。"""

    # ---- 溯源
    source: str = ""
    """出处。**空的可以，写错的不行**——留空表示「暂未登记」。"""
    updated_at: str = ""

    @field_validator("powertrain")
    @classmethod
    def _known_powertrain(cls, value: list[str]) -> list[str]:
        bad = [item for item in value if item not in POWERTRAINS]
        if bad:
            raise ValueError(f"未知动力形式：{bad}，可选 {list(POWERTRAINS)}")
        return value

    @field_validator("updated_at")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        if not value:
            return ""
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"更新日期 {value!r} 不是 YYYY-MM-DD") from exc
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "CompetitorEntry":
        if not self.canonical_name.strip():
            raise ValueError(f"{self.model_id} 缺少规范名")
        if not self.brand.strip():
            raise ValueError(f"{self.model_id} 缺少品牌名")
        if not self.positioning.strip():
            raise ValueError(f"{self.model_id} 缺定位（positioning）")
        return self

    # ---- 查询辅助
    def all_names(self) -> tuple[str, ...]:
        return (self.canonical_name, *self.aliases, *self.asr_variants)


class CompetitorManifest(BaseModel):
    library_id: str
    display_name: str
    file: str = "competitors.json"
    effective_from: str
    effective_to: str | None = None
    note: str = ""

    @field_validator("effective_from", "effective_to")
    @classmethod
    def _iso_date(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"日期 {value!r} 不是 YYYY-MM-DD") from exc
        return value


@dataclass(frozen=True, slots=True)
class CompetitorLibrary:
    """装载完成的竞品库。全部字段只读。"""

    manifest: CompetitorManifest
    entries: tuple[CompetitorEntry, ...] = ()
    content_signature: str = ""
    source_dir: Path | None = None

    _by_id: dict[str, CompetitorEntry] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_by_id", {item.model_id: item for item in self.entries}
        )

    @property
    def library_id(self) -> str:
        return self.manifest.library_id

    @property
    def is_empty(self) -> bool:
        """空库 = 未启用竞品。消费点据此输出空串。"""
        return not self.entries

    def entry(self, model_id: str) -> CompetitorEntry | None:
        return self._by_id.get(model_id)


EMPTY_LIBRARY = CompetitorLibrary(
    manifest=CompetitorManifest(
        library_id="",
        display_name="（未启用竞品库）",
        effective_from="1970-01-01",
    )
)
"""退化形态：未配置或装载失败时用它。所有消费点输出空串。"""


# ====================================================================
# 装载与校验
# ====================================================================


def normalize_name(text: str) -> str:
    """称谓归一。与 :func:`customer_profile.knowledge.pack.normalize_name` 同口径。

    不直接 import 那边的实现：两个模块描述的是两套数据（本方 / 外部），
    一方改口径不该悄悄影响另一方。两处一致性由用例钉住。
    """
    if not text:
        return ""
    out: list[str] = []
    for char in text:
        code = ord(char)
        if 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII
            char = chr(code - 0xFEE0)
        elif code == 0x3000:  # 全角空格
            char = " "
        if char.isspace():
            continue
        out.append(char)
    return "".join(out).lower()


def load_library(path: Path | str) -> CompetitorLibrary:
    """装载并校验一个竞品库目录。**失败即抛**——供预检与测试使用。"""
    root = Path(path)
    if not root.is_dir():
        raise CompetitorError(f"竞品库目录不存在：{root}")

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise CompetitorError(f"竞品库缺少清单：{manifest_path}")
    try:
        manifest = CompetitorManifest(
            **_read_json(manifest_path, "清单")
        )
    except CompetitorError:
        raise
    except Exception as exc:
        raise CompetitorError(f"清单不合规：{exc}") from exc

    raw_path = root / manifest.file
    raw = _read_json(raw_path, "竞品条目")
    if not isinstance(raw, list):
        raise CompetitorError(f"{manifest.file} 应为竞品数组")

    entries: list[CompetitorEntry] = []
    issues: list[str] = []
    for index, item in enumerate(raw):
        try:
            entries.append(CompetitorEntry(**item))
        except Exception as exc:
            issues.append(f"第 {index + 1} 个竞品条目不合规：{exc}")
    if issues:
        raise CompetitorError(issues)

    issues = _check_uniqueness(entries)
    if issues:
        raise CompetitorError(issues)

    signature = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    return CompetitorLibrary(
        manifest=manifest,
        entries=tuple(entries),
        content_signature=signature,
        source_dir=root,
    )


def _read_json(path: Path, what: str) -> Any:
    if not path.is_file():
        raise CompetitorError(f"竞品库缺少{what}文件：{path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise CompetitorError(f"{what} JSON 语法错误（{path}）：{exc}") from exc


def _check_uniqueness(entries: list[CompetitorEntry]) -> list[str]:
    """编号与称谓双重唯一。与车型条目同一纪律。"""
    issues: list[str] = []

    seen_id: dict[str, str] = {}
    for item in entries:
        if item.model_id in seen_id:
            issues.append(f"model_id 重复：{item.model_id}")
        seen_id[item.model_id] = item.model_id

    owner: dict[str, str] = {}
    canonical = {item.canonical_name: item.model_id for item in entries}
    for item in entries:
        for name in item.all_names():
            key = normalize_name(name)
            if not key:
                continue
            holder = owner.get(key)
            if holder is not None and holder != item.model_id:
                issues.append(
                    f"称谓「{name}」同时被 {holder} 与 {item.model_id} 认领"
                    "——查表结果将取决于遍历顺序"
                )
            owner[key] = item.model_id
            if key in canonical and canonical[key] != item.model_id:
                issues.append(
                    f"{item.model_id} 的称谓「{name}」与 {canonical[key]} 的规范名相同"
                )
    return issues


# ====================================================================
# 解析
# ====================================================================


def resolve_competitor(
    text: str, library: CompetitorLibrary | None = None
) -> str | None:
    """任意称谓 → 竞品 ``model_id``。未命中返回 ``None``。

    四级精确匹配，与车型解析同口径：规范名 → 别名 → 转写变体 → 归一化。
    **不做模糊匹配**——理由与车型侧完全相同，``某某700`` 与 ``某某500`` 在子串上互为包含。
    """
    if not text:
        return None
    resolved = library or EMPTY_LIBRARY
    if resolved.is_empty:
        return None

    needle = str(text).strip()
    if not needle:
        return None

    for item in resolved.entries:
        if needle == item.canonical_name:
            return item.model_id
    for item in resolved.entries:
        if needle in item.aliases:
            return item.model_id
    for item in resolved.entries:
        if needle in item.asr_variants:
            return item.model_id

    key = normalize_name(needle)
    if not key:
        return None
    for item in resolved.entries:
        for name in item.all_names():
            if normalize_name(name) == key:
                return item.model_id
    return None


def mentioned_competitors(
    texts: Iterable[str], library: CompetitorLibrary | None = None
) -> list[CompetitorEntry]:
    """从一批称谓里筛出命中的竞品条目（去重、保持出现顺序）。"""
    resolved = library or EMPTY_LIBRARY
    if resolved.is_empty:
        return []
    out: list[CompetitorEntry] = []
    seen: set[str] = set()
    for text in texts:
        model_id = resolve_competitor(text, resolved)
        if model_id is None or model_id in seen:
            continue
        seen.add(model_id)
        entry = resolved.entry(model_id)
        if entry is not None:
            out.append(entry)
    return out


# ====================================================================
# 入口
# ====================================================================

_CACHE: dict[str, CompetitorLibrary] = {}


def competitors_root(settings=None) -> Path:
    resolved = settings or get_settings()
    raw = str(getattr(resolved, "knowledge_competitors_dir", "") or "").strip()
    return Path(raw) if raw else DEFAULT_COMPETITORS_DIR


def _configured_library_id(settings=None) -> str:
    resolved = settings or get_settings()
    return str(getattr(resolved, "knowledge_competitors", "") or "").strip()


def current_competitors(settings=None) -> CompetitorLibrary:
    """当前生效的竞品库。**永不抛异常**——取不到返回 :data:`EMPTY_LIBRARY`。

    与 :func:`customer_profile.knowledge.current_pack` 同一取向：装载期严格，
    请求路径上只退化。默认配置为空串（业务资料未到位），此时恒返回空库。
    """
    name = _configured_library_id(settings)
    if not name:
        return EMPTY_LIBRARY

    directory = competitors_root(settings) / name
    key = str(directory.resolve())
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    try:
        library = load_library(directory)
    except Exception as exc:  # noqa: BLE001 —— 任何装载失败都退化为空库
        logger.warning(
            "竞品库 {!r} 装载失败，本次不注入竞品资料：{}", name, exc
        )
        library = EMPTY_LIBRARY

    _CACHE[key] = library
    return library


def reset_cache() -> None:
    """清空缓存。供测试与本地调试。"""
    _CACHE.clear()
