"""知识包：加载、校验、只读缓存。

一个**知识包** = 一个合作方的一组业务知识。包目录自带清单 ``pack.json``，声明身份、
品牌与生效期；加载器**只认清单**，不扫目录里有什么文件——往目录里丢一个文件不会
意外生效。

三条口径：

1. **加载期一次校验完，失败即抛。** 知识写错若拖到运行期，只会表现为「输出慢慢偏掉」，
   没有任何报错。因此形状、唯一性、区间自洽全部在装载时判掉。
2. **运行期不可变。** 没有写接口，不监听文件变化。内容一旦装载即冻结，与
   「写入即冻结」的运维约定一致；要改就改文件重启。
3. **装载失败不拖垮管线。** :func:`current_pack` 取不到包时返回 :data:`EMPTY_PACK`
   （零车型、零品牌），消费点据此退化为「不启用知识」。硬失败只发生在
   :func:`load_pack`——那是预检与测试的入口，不是请求路径。

格式用 JSON 不用 YAML：生产依赖里没有 PyYAML（只有 ``scripts/requirements.txt`` 有），
为读几份知识文件给镜像加一个依赖不值得；且条目由脚本生成、人只做复核。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_PACK_DIR = Path(__file__).resolve().parent / "packs"
"""包内缺省目录：``knowledge/packs/<pack_id>/``。``KNOWLEDGE_PACKS_DIR`` 可覆盖。"""

STATUSES: tuple[str, ...] = ("on_sale", "coming", "discontinued")
POWERTRAINS: tuple[str, ...] = ("bev", "phev", "erev", "ice")
"""动力形式。``powertrain`` 是**列表**且**允许为空**——空表示「逐档待定」，
不是「没有动力」。强制填写会逼出臆造值，与全项目「宁可留白」的取向一致。"""

PRICE_TOLERANCE = 0.011
"""版本价落在区间内的容差（万元）。报价常带两位小数，浮点直接比较会误判。"""


class PackError(ValueError):
    """知识包不合规。``issues`` 含全部问题，便于一次修完。"""

    def __init__(self, issues: list[str] | str) -> None:
        self.issues = [issues] if isinstance(issues, str) else list(issues)
        super().__init__("知识包校验未通过：\n" + "\n".join(f"  - {i}" for i in self.issues))


# ====================================================================
# 数据模型
# ====================================================================


class Price(BaseModel):
    """价格口径。

    ``min_wan`` / ``max_wan`` 是**全系包络**（含定制、限量版），供结构校验；
    ``core_max_wan`` 是**主力价格带上限**，供按预算筛车与对客讲述。

    两段并存的理由：猛士917 全系 63.77–109.80 万，但对客默认讲 63.77–69.90。
    只留包络会把 917 当成百万级车推给 80 万预算的客户；只留主力带则 109.8 万的版本价
    落在区间外、校验失败。两段才是真实口径。
    """

    min_wan: float
    max_wan: float
    core_max_wan: float | None = None
    as_of: str
    note: str = ""

    @model_validator(mode="after")
    def _ordered(self) -> "Price":
        core = self.max_wan if self.core_max_wan is None else self.core_max_wan
        if self.min_wan > self.max_wan:
            raise ValueError(f"价格区间倒置：{self.min_wan} > {self.max_wan}")
        if not (self.min_wan - PRICE_TOLERANCE <= core <= self.max_wan + PRICE_TOLERANCE):
            raise ValueError(
                f"core_max_wan={core} 必须落在 [{self.min_wan}, {self.max_wan}] 内"
            )
        return self

    @property
    def core_max(self) -> float:
        return self.max_wan if self.core_max_wan is None else self.core_max_wan


class Highlight(BaseModel):
    title: str
    text: str = ""

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("卖点标题不得为空")
        return value


class ConfigEntry(BaseModel):
    """一个**档位型号**。同一车型的不同档位在价格、动力、配置上都可以不同。"""

    name: str
    price_wan: float
    promo_price_wan: float | None = None
    powertrain: list[str] = Field(default_factory=list)
    drive: str = ""
    range_km: int | None = None
    note: str = ""

    @field_validator("powertrain")
    @classmethod
    def _known_powertrain(cls, value: list[str]) -> list[str]:
        bad = [item for item in value if item not in POWERTRAINS]
        if bad:
            raise ValueError(f"未知动力形式：{bad}，可选 {list(POWERTRAINS)}")
        return value


class ModelEntry(BaseModel):
    """一款在售车型（**不是**一个档位）。分标识层 / 参数层 / 表达层。"""

    model_id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    asr_variants: list[str] = Field(default_factory=list)
    """转写常见错写。与 ``aliases`` 分开：把错字当真名会污染归属，把真名当错字会漏判。"""

    series: str = ""
    status: str = "on_sale"

    price: Price
    powertrain: list[str] = Field(default_factory=list)
    body: str = ""
    seats: int = 0
    dimensions: dict[str, int] = Field(default_factory=dict)
    configs: list[ConfigEntry] = Field(default_factory=list)
    audience: list[str] = Field(default_factory=list)
    scenarios: list[str] = Field(default_factory=list)

    positioning: str
    highlights: list[Highlight] = Field(default_factory=list)
    speech: str

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        if value not in STATUSES:
            raise ValueError(f"未知在售状态：{value!r}，可选 {list(STATUSES)}")
        return value

    @field_validator("powertrain")
    @classmethod
    def _known_powertrain(cls, value: list[str]) -> list[str]:
        bad = [item for item in value if item not in POWERTRAINS]
        if bad:
            raise ValueError(f"未知动力形式：{bad}，可选 {list(POWERTRAINS)}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "ModelEntry":
        if not self.canonical_name.strip():
            raise ValueError(f"{self.model_id} 缺少规范名")
        # 表达层三字段非空：空卖点的条目对 L3 没有价值，应在入库时就发现
        if not self.positioning.strip():
            raise ValueError(f"{self.model_id} 缺定位（positioning）")
        if not self.speech.strip():
            raise ValueError(f"{self.model_id} 缺推介话术（speech）")
        if not self.highlights:
            raise ValueError(f"{self.model_id} 没有卖点（highlights）")
        low, high = self.price.min_wan, self.price.max_wan
        for item in self.configs:
            if not (low - PRICE_TOLERANCE <= item.price_wan <= high + PRICE_TOLERANCE):
                raise ValueError(
                    f"{self.model_id} 档位「{item.name}」价格 {item.price_wan} "
                    f"落在区间 [{low}, {high}] 之外"
                )
            if item.promo_price_wan is not None and item.promo_price_wan > item.price_wan:
                raise ValueError(
                    f"{self.model_id} 档位「{item.name}」促销价高于指导价"
                )
        return self

    # ---- 查询辅助
    @property
    def label(self) -> str:
        """对客展示名：规范名 + 主力价格带。"""
        return f"{self.canonical_name}（{self.price.min_wan:g}–{self.price.core_max:g} 万元）"

    def all_names(self) -> tuple[str, ...]:
        """可被客户说出口的全部称谓：规范名 + 别名 + 转写变体。"""
        return (self.canonical_name, *self.aliases, *self.asr_variants)


class BrandIdentity(BaseModel):
    canonical: str
    aliases: list[str] = Field(default_factory=list)
    asr_variants: list[str] = Field(default_factory=list)

    def all_names(self) -> tuple[str, ...]:
        return (self.canonical, *self.aliases, *self.asr_variants)


class BrandFile(BaseModel):
    our_brand: BrandIdentity
    competitors: list[BrandIdentity] = Field(default_factory=list)
    """竞品品牌。本轮为空，第 4 期竞品库接入时填。留字段是为了让归属判定从一开始就有
    三方语义（本方 / 竞品 / 未知），日后不必改签名。"""


class Tier(BaseModel):
    """产品矩阵分层。放在包清单而不是车型条目：矩阵是「几款车之间」的关系，
    写进单条会让同一句话抄三遍、改一处漏两处。"""

    tier: str
    label: str
    models: list[str] = Field(default_factory=list)
    budget_min_wan: float | None = None
    budget_max_wan: float | None = None


class Manifest(BaseModel):
    pack_id: str
    display_name: str
    vendor: str
    brands: list[str] = Field(default_factory=list)
    models_file: str = "models.json"
    brand_file: str = "brand.json"
    effective_from: str
    effective_to: str | None = None
    tiers: list[Tier] = Field(default_factory=list)
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


# ====================================================================
# 装载后的只读形态
# ====================================================================


@dataclass(frozen=True, slots=True)
class Pack:
    """装载完成的知识包。全部字段只读。"""

    manifest: Manifest
    brand: BrandFile
    models: tuple[ModelEntry, ...] = ()
    content_signature: str = ""
    source_dir: Path | None = None

    # ---- 索引（构造后惰性建立，避免每次解析都遍历）
    _by_model: Mapping[str, ModelEntry] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_by_model", {item.model_id: item for item in self.models}
        )

    @property
    def pack_id(self) -> str:
        return self.manifest.pack_id

    @property
    def is_empty(self) -> bool:
        """空包 = 未接入知识。消费点据此退化为「不启用」。

        判据只有「有没有车型」一条：品牌名可以写在清单里而车型一个都没有，
        那仍然等于没有可用的知识。
        """
        return not self.models

    def model(self, model_id: str) -> ModelEntry | None:
        return self._by_model.get(model_id)

    def tier_of(self, model_id: str) -> Tier | None:
        for tier in self.manifest.tiers:
            if model_id in tier.models:
                return tier
        return None


EMPTY_PACK = Pack(
    manifest=Manifest(
        pack_id="",
        display_name="（未配置知识库）",
        vendor="",
        effective_from="1970-01-01",
    ),
    brand=BrandFile(our_brand=BrandIdentity(canonical="")),
)
"""退化形态：管线拿不到知识包时用它，视图输出空串、分流跳过。

**不抛异常**是刻意的：知识是增强，不能成为新的单点（见 docs/feature-knowledge-base/07 R1）。
"""


# ====================================================================
# 装载与校验
# ====================================================================


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise PackError(f"知识包缺少文件：{path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise PackError(f"JSON 语法错误（{path}）：{exc}") from exc


def load_pack(path: Path | str) -> Pack:
    """装载并校验一个知识包目录。**失败即抛**——供预检与测试使用。

    校验项（任一不过即抛）：
    1. 清单必填项齐全、日期可解析；
    2. ``model_id`` 包内唯一；
    3. **别名全局唯一**——``aliases`` 与 ``asr_variants`` 合起来，任一字符串不得被
       两条条目认领，也不得与另一条目的 ``canonical_name`` 相同；
    4. 清单声明的文件都存在；
    5. 价格区间自洽、版本价落在区间内（由模型校验器承担）；
    6. 表达层三字段非空。

    第 3 条是这套校验里最有价值的一条：别名表是污染高发点，两条目声称同一别名时
    查表结果取决于遍历顺序，而这种错**没有任何报错**，只会表现为「客户明明说了某款车
    却没进 Notes」。
    """
    root = Path(path)
    if not root.is_dir():
        raise PackError(f"知识包目录不存在：{root}")

    manifest_path = root / "pack.json"
    raw_manifest = _read_json(manifest_path)
    try:
        manifest = Manifest(**raw_manifest)
    except Exception as exc:  # pydantic 的 ValidationError
        raise PackError(f"清单不合规：{exc}") from exc

    try:
        brand = BrandFile(**_read_json(root / manifest.brand_file))
    except PackError:
        raise
    except Exception as exc:
        raise PackError(f"品牌文件不合规：{exc}") from exc

    raw_models = _read_json(root / manifest.models_file)
    if not isinstance(raw_models, list):
        raise PackError(f"{manifest.models_file} 应为车型数组")
    models: list[ModelEntry] = []
    issues: list[str] = []
    for index, raw in enumerate(raw_models):
        try:
            models.append(ModelEntry(**raw))
        except Exception as exc:
            issues.append(f"第 {index + 1} 个车型条目不合规：{exc}")

    if issues:
        raise PackError(issues)

    issues = _check_uniqueness(models, manifest)
    if issues:
        raise PackError(issues)

    signature = hashlib.sha256(
        (root / manifest.models_file).read_bytes()
    ).hexdigest()
    return Pack(
        manifest=manifest,
        brand=brand,
        models=tuple(models),
        content_signature=signature,
        source_dir=root,
    )


def _check_uniqueness(models: list[ModelEntry], manifest: Manifest) -> list[str]:
    """编号、称谓、矩阵引用三重唯一性。"""
    issues: list[str] = []

    seen_id: dict[str, str] = {}
    for item in models:
        if item.model_id in seen_id:
            issues.append(f"model_id 重复：{item.model_id}")
        seen_id[item.model_id] = item.model_id

    # 规范名与全部称谓放进同一张表：客户说出口的词，不能同时指向两台车
    owner: dict[str, str] = {}
    canonical = {item.canonical_name: item.model_id for item in models}
    for item in models:
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

    # 矩阵引用必须存在（悬空引用），且每款车至少被一个分层覆盖（死知识）
    known = {item.model_id for item in models}
    covered: set[str] = set()
    for tier in manifest.tiers:
        for model_id in tier.models:
            if model_id not in known:
                issues.append(f"分层 {tier.tier} 引用了不存在的车型 {model_id}")
            covered.add(model_id)
    if manifest.tiers:
        for model_id in known - covered:
            issues.append(f"车型 {model_id} 未被任何分层覆盖——它不会被 K4 选中")

    return issues


def normalize_name(text: str) -> str:
    """称谓归一：去空白、全角转半角、统一小写。

    **不做**同义词扩展、不做编辑距离、不做子串包含。``坦克700`` 与 ``坦克500``
    在子串上互为包含，模糊匹配会把两台车归成一个；知识库的价值在答得准，不在答得多。
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


def empty_pack_reason() -> str:
    """空包的可读说明，供视图与日志使用。"""
    return "未配置知识库（知识包加载失败或未指定）"
