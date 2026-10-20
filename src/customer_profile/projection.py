"""L2 核对与投影层（纯代码，0 token）。

全案的枢纽：唯一同时看得见**所有字段、所有断言、所有来源**的地方，因此「字段间矛盾」
在这一层被消解，而不是靠提示词祈祷模型不写矛盾（`端到端方案.md` §3.4）。

输入 ``claims[]``，输出 ``facts`` + ``notes_updates[]`` + ``to_confirm[]``：:

    claims[]   断言账本（L1 产出，或离线回放时由既有字段重建）
      ↓ a 归属过滤 → b 来源分级 → c 冲突消解 → d 枚举归一
      ↓ e 单/多值约束 → f 跨字段规则 → g 缺失即留白
    facts          规范事实：一个字段一个值（或一组多值），有据可查
    notes_updates[] Notes 投影结果（契约全部字段），形状与基线 `basic_notes_updates` 同形
    to_confirm[]   无法坐实的字段：**空着比填错强**，交销售补充

三条设计口径（v2 起第 1、2 条的方向变了，见下）：

1. **宁降勿丢**（v2）。规则判不了的，**降级保留**比丢弃强：值留在 ``facts`` 里，
   置信档降为 ``inferred_low`` 并转 ``to_confirm``，交 LLM 兜底裁决或人工确认。
   v1 的口径是「拒绝即丢弃」，实测是掉字段的主因之一（设计稿 §2.1）。
   **唯一仍然留白的**是同档单值语义多值冲突（规则 c）——两个互斥的值都写出去，
   正是「Notes 里旧车车型有值、购买类型却仍是首购」的成因。
2. **来源分级是硬顺序**：人工/系统登记 > 一手交互 > 公开登记查询 > 社交自呈现 > 第三方预测。
   预测或社交**不得单独支撑** ``基础身份`` 类字段（16 个）的**确定性**——
   v2 起降为 ``inferred_low`` 仍写入，不再丢弃。
3. **枚举只认契约**。合法键、单/多选、结构化枚举的 ``input_value`` 全部读
   :mod:`customer_profile.notes_contract`，不在这里手抄一份。归不到合法键的取值
   **保留原值**并降级（规则 d），不再丢弃。

三条来源硬规则（设计稿 §6.2，针对明确错误类型，均为强制）：

1. 预测不得单独支撑身份类字段 → 降 ``inferred_low``，不写确定值；
2. 态度/行为类字段（``emotion`` / ``knowledge_of_mengshi`` / ``pain_points``）须有一手依据，
   社媒与预测最多作辅助 → 只有弱来源时降级；
3. 社交自呈现不得单独支撑身份类字段 → 同 1，降级。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import internal_fields as inf
from . import notes_contract as nc

# ====================================================================
# 来源分级
# ====================================================================

REGISTRY = "registry"
"""人工/系统登记：销售确认的 Notes、销售系统留痕、对历史画像的人工反馈。"""

FIRSTHAND = "firsthand"
"""一手交互：试驾录音、外呼录音、与销售的聊天记录。"""

PUBLIC_RECORD = "public_record"
"""公开登记查询：天眼查关联企业、手机号归属地。"""

SOCIAL = "social"
"""社交自呈现：微信/朋友圈/支付宝/小红书/抖音截图。"""

PREDICTION = "prediction"
"""第三方预测（极光第三方画像标签）。

v3.2 起**不再等同于「最弱」**：这批标签是采购来的商用画像数据，单独支撑某字段时
按 ``inferred_high`` 处理（见 :attr:`Claim.effective_basis`）。它仍不是 ``fact``
（不是客户原话），但在无试驾 / 无沟通记录时是唯一可靠的画像信源。"""

FACT = "fact"
"""三分类·事实：有原文锚点，或由登记系统直接给出。"""

INFERRED_HIGH = "inferred_high"
"""三分类·高置信推断：无单句锚点，但有 **≥2 条独立来源**同向支撑，或**由第三方画像
标签单独支撑**（v3.2，用户口径：采购的商用画像数据在无一手记录时是唯一可靠信源）。"""

INFERRED_LOW = "inferred_low"
"""三分类·低置信推断：仅 1 条弱事实、纯常识，或仅由**社交自呈现**单独支撑。

**不含第三方预测**：v3.2 起预测单独支撑即 ``inferred_high``（见 :data:`PREDICTION`）。"""

ANCHOR_VERBATIM = "verbatim"
"""坐标形态·锚定：值能在原文里字面定位，``quote`` 就是切出的原文（v3 接口一）。"""

ANCHOR_NORMALIZED = "normalized"
"""坐标形态·当归：值经 ASR 纠错 / 归一产生，原文里无字面匹配，``source_quote`` 记原文
（v3 接口一，如「安安I60」→「埃安」）。"""

INJECTED_PHONE_CHANNEL = "客户留资手机号"
"""``phone_number`` 的注入来源名（登记档）。取值就是入参手机号，不来自任何渠道原文。"""

INJECTED_LICENSE_CHANNEL = "试驾与置换记录"
"""``driver_license_status`` 的注入来源名（登记档）。试驾或有旧车本身即需驾照。"""

BASIS_ORDER: dict[str, int] = {FACT: 0, INFERRED_HIGH: 1, INFERRED_LOW: 2}
"""三分类由强到弱。"""

SOURCE_TIERS: tuple[str, ...] = (REGISTRY, FIRSTHAND, PUBLIC_RECORD, SOCIAL, PREDICTION)
_TIER_RANK: dict[str, int] = {tier: i for i, tier in enumerate(SOURCE_TIERS)}

TIER_LABEL: dict[str, str] = {
    REGISTRY: "人工/系统登记",
    FIRSTHAND: "一手交互",
    PUBLIC_RECORD: "公开登记查询",
    SOCIAL: "社交自呈现",
    PREDICTION: "第三方预测",
}

CHANNEL_TIER: dict[str, str] = {
    # —— 人工/系统登记
    "销售人员人工确认的客户Notes信息": REGISTRY,
    "客户留存在我方销售数据系统中的信息记录": REGISTRY,
    "销售人员对当前客户历史画像数据的反馈记录": REGISTRY,
    # —— 确定性注入（v3.1 改造四）：值由代码按入参 / 登记事实定，不经模型归纳。
    #     记人工/系统登记档，否则身份字段会因「非登记来源」被降为 inferred_low。
    "客户留资手机号": REGISTRY,
    "试驾与置换记录": REGISTRY,
    # —— 一手交互
    "试驾录音信息": FIRSTHAND,
    "外呼录音信息": FIRSTHAND,
    "客户与我方销售人员的聊天记录信息": FIRSTHAND,
    # —— 公开登记查询
    "关联企业信息": PUBLIC_RECORD,
    "手机号归属地信息": PUBLIC_RECORD,
    # —— 社交自呈现
    "朋友圈截图内容信息": SOCIAL,
    "微信手机号搜索界面信息": SOCIAL,
    "微信主页截图信息": SOCIAL,
    "支付宝主页截图信息": SOCIAL,
    "小红书主页截图信息": SOCIAL,
    "抖音主页截图信息": SOCIAL,
    # —— 第三方预测
    "第三方画像数据": PREDICTION,
}
"""渠道 → 来源分级。渠道名与 :mod:`customer_profile.workflows.customer_profile_pipeline`
的渠道表逐字一致（那里是取数口径，这里是消费口径，测试钉住两者同集合）。"""

WEAK_TIERS: frozenset[str] = frozenset({SOCIAL, PREDICTION})
"""**弱来源**两档：社交自呈现与第三方预测。

两档的处置**从 v3.2 起不同**：

- **社交自呈现**不得单独支撑身份字段的确定性（设计稿 §6.2 硬规则 1、3），也不得作为
  态度/行为类字段（见 :data:`ATTITUDE_KEYS`）的唯一依据（硬规则 2）——降级保留，不丢弃；
- **第三方预测**（极光画像标签）单独支撑某字段时改记 ``inferred_high``（用户口径：
  客户没有试驾与沟通记录时，采购来的画像标签是唯一可靠的画像信源）。

两档仍都不等于 ``fact``：它们不是客户原话，只是「够可信、可作推断结论」。
"""

ATTITUDE_KEYS: frozenset[str] = frozenset({"emotion", "knowledge_of_mengshi", "pain_points"})
"""态度/行为类字段：**须有一手依据**（登记或一手交互），社媒与预测最多作辅助。

这三项是「客户怎么想」而非「客户是什么」。只有弱来源时结论仍写入但降为
``inferred_low``——销售看得到线索，却不会把它当成客户已表态（设计稿 §6.2 硬规则 2）。
"""


IDENTITY_SUBJECT_EXEMPT: frozenset[str] = frozenset({"family_structure"})
"""身份字段里**不要求**主体为客户本人的例外。

婚否、有无孩子这类事实，其合理来源本就可能是配偶或家人（`端到端方案.md` §3.4 a：
「``subject != customer`` 的断言不得写入身份字段；**可写入家庭结构**、可进入卡片叙述」）。
因此归属过滤对这一项放行，其余 15 个身份字段一律要求主体是客户本人。"""

NAME_SOURCE_WHITELIST: frozenset[str] = frozenset({REGISTRY, FIRSTHAND})
"""``customer_name`` 的**来源白名单**（v3 接口二）。

姓名只认一手自述（「我姓陈」）与登记姓名。营业执照主体名、微信昵称、店铺名**一律不进**
——v2.1 实测 ``customer_name`` 落成「…新泽水果店」（营业执照主体名），那是企业名不是人名。
白名单之外的来源（``公开登记查询`` / ``社交自呈现`` / ``第三方预测``）**整条拒收**，
不是降级：一个错误的人名比没有人名更糟。
"""

GENDER_WEAK_TIERS: frozenset[str] = frozenset({FIRSTHAND, SOCIAL, PREDICTION})
"""``title``（性别/称呼）里**只能算弱证据**的来源档（v3 接口二）。

优先级：登记称呼 > 一手自述 > 头像性别标签 > 录音性别标签（用户口径）。
一手交互整体入弱档，是因为录音里的性别标签**天生不可靠**、且说话人未必是客户本人；
非登记来源的性别一律降为 ``inferred_low``（``_confidence_for`` 随之给 0.5，满足「≤0.6」）。
"""

SPEAKER_SALES = "销售方"
SPEAKER_CUSTOMER = "客户方"
SPEAKER_UNKNOWN = "未知来源"
"""录音证据的说话方（与 :mod:`customer_profile.workflows.role_labelling` 同一套词表）。

只在**录音渠道**的证据上有值；其余渠道为空串，空值不加任何前缀——「不是录音」与
「身份不明」是两种情况，混用会让销售误以为截图、登记数据也不可信。
"""

SALESPERSON_MARKERS: tuple[str, ...] = (
    "销售顾问",
    "顾问自称",
    "非客户",
    "我方人员",
    "我方销售",
    "销售员",
)
"""**销售顾问指向词**（v3.1 改造二）：客户字段里出现的这些词说明该值说的大概率

不是客户本人，而是我方销售顾问。

只收**精指向**词，不收泛职业词：``销售`` 单独出现可能是客户自己的职业
（「我是做销售的」），``顾问`` 单独出现可能是客户的职业（咨询顾问）。命中即**整条拒收**
（不是降级）——把顾问的信息当客户信息写出去，比这个字段空着更糟，与
:data:`NAME_SOURCE_WHITELIST` 同口径。

实测命中：``13435844484`` 的 ``customer_name`` 取值为「郑小姐（顾问自称，非客户姓名）」。
"""

AGGREGATE_TEXT_KEYS: frozenset[str] = frozenset(
    {
        "hobbies",
        "pain_points",
        "special_requirements",
        "intended_brand",
        "intended_model",
        # v3.2：竞品类四字段。客户的意向与竞品本就可以并列多项——「在 A、B、C 之间比」
        # 是常态，判冲突会把整字段留白，正好丢掉销售最需要的那份对比清单。
        "competing_brand",
        "competing_model",
        "test_driven_models",
        "brand_concerns",
    }
)
"""**聚合语义**的自由文本字段：同来源档给出多个不同值时应当**汇总**，而不是判冲突留白。

契约里这些文本字段全是 `field_type: "text"`、`enum_options: null`，因此
:func:`notes_contract.is_multi_value` 对它们一律为假。但「多个值算不算冲突」是**字段语义**
问题，契约没给这个标志位：

- **聚合语义**（本集合）：`hobbies`「钓鱼」「露营」并存不矛盾，是同一客户的多面；
  判冲突会把整字段留白（R0 实测 `16761543238` 因此丢掉全部三个爱好）。v3.2 增补的
  竞品类四字段（`competing_brand` / `competing_model` / `test_driven_models` /
  `brand_concerns`）同属此列：客户在多个品牌/车型之间对比是常态，`test_driven_models`
  的契约描述本就写着「提取车型名，**可多个**」。
- **单值语义**（其余文本字段）：`occupation`「教师」与「医生」互斥，
  同档并列时确应留白转确认（宁空勿错）。

集合的选取依据是契约 `description` 的措辞：「提取…兴趣爱好或生活话题」「提取非常规个性化
需求」「提取客户购车正在考虑的**品牌**/**车型**」——这些描述本身就在要求**列举多项**；
而 `occupation`「提取客户行业+职位」、`company`「提取客户具体供职的公司名称」等，
描述指向**唯一取值**。

**边界**：`usage_frequency`（描述输出「高频/中频/低频」）与 `budget_range`（上下限）
虽可有多段表述，但描述指向单一结论，故不纳入本集合，仍走「同档多值 → 留白转确认」。
"""

# 跨产物字段：同一事实在卡片与 Notes 各有一处，L2 在这里**唯一化**。
LEVEL_KEYS: tuple[str, ...] = ("deal_level", "business_opp_level")
"""等级在卡片里有两个载体（``profile_summary.deal_level`` 与
``customer_overview.business_opp_level.value``）。基线 39 案里 **4 案**两者不一致，
销售因此看到两个等级。L2 只保留一个。"""

LEVEL_ORDER: tuple[str, ...] = ("H", "A", "B", "C", "D", "待识别")
"""商机等级由高到低。冲突时取**证据更强者**，无法判定则取较高档并转 ``to_confirm``。

H/A/B/C/待识别 取自业务专家的《客户商机等级定级规范》；``D`` 仅保留给历史批次
（旧口径「线索质量低」），新流程不再产出。
"""

AGE_PATTERN = re.compile(r"(\d{1,3})\s*[~—\-－至]\s*(\d{1,3})")
AGE_OPEN_PATTERN = re.compile(r"(\d{1,3})\s*岁?\s*以上")

# ====================================================================
# 断言
# ====================================================================


@dataclass(frozen=True, slots=True)
class Claim:
    """一条断言账本条目。

    L1 产出它，离线回放时由既有字段重建。字段与 `端到端方案.md` §3.3 的 ``claims[]``
    对齐，去掉 ``span`` / ``quote``（那是「能否回溯到原文」的凭据，只影响 L1 的取舍，
    投影层按 ``record_id`` / ``channel`` 判来源即可）。
    """

    attribute: str
    """字段键（Notes 契约键，或 ``deal_level`` 这类跨产物字段）。"""

    value: Any
    channel: str = ""
    """来源渠道名（决定来源分级）。"""

    subject: str = "customer"
    """事实主体：``customer`` / ``spouse`` / ``family`` / ``colleague`` / ``salesperson`` /
    ``other`` / ``unknown``。"""

    claim_type: str = "fact"
    """v1 口径的三分类：``fact`` / ``observation`` / ``prediction``。

    v2 起由 :attr:`basis` 取代；本字段保留供离线回放既有批次（那批产物里只有它）。
    """

    created_at: str = ""
    claim_id: str = ""

    basis: str = "fact"
    """v2 的三分类置信档：``fact`` / ``inferred_high`` / ``inferred_low``。

    与 :attr:`claim_type` 的关系：``fact`` 对应 ``fact``；``observation`` 与
    ``prediction`` 都对应 ``inferred_*``（它们本就是推断或统计判断，不是客户陈述）。
    L2 的冲突消解按这一档排序：``fact`` > ``inferred_high`` > ``inferred_low``。
    """

    basis_of_inference: tuple[str, ...] = ()
    """推断依据（``basis`` 为 ``inferred_*`` 时的必填项）。

    ``fact`` 靠 ``anchor`` 回溯原文；推断类没有单句锚点，靠这一串「从哪几条推出来的」
    交代来源。为空且非 ``fact`` 的断言的 ``basis`` 会被 L1 定位层降为 ``inferred_low``。
    """

    reasoning_steps: tuple[str, ...] = ()
    """**推导链**：由依据推到取值的思维路径，1–4 条短句（v3.1 改造一）。

    与 :attr:`basis_of_inference` 的分工是本字段存在的全部理由：
    ``basis_of_inference`` 是**论据**（原文里有什么），本字段是**论证**（怎么从论据
    落到这个取值）。v3 把两者混在一起渲染，导致 ``steps`` 与 ``evidence`` 从同一份原料
    分流、两栏字面雷同（实测 234 字段中 122 个完全同集，占 52%）。

    L1 的事实层对 ``inferred_high`` 条目、分析投影层对**每条**结论给出本字段；离线回放
    的旧产物没有它，届时由 :func:`_readable_steps` 退到一句非复述的说明。
    """

    confidence: float | None = None
    """L1 模型对这条断言自报的置信度（0–1，v3.5）。

    出口置信度以它为基准做**档位校准**（见 :func:`_confidence_for`），不再按档位
    给固定值——此前 ``fact`` 恒 0.7、``inferred_low`` 恒 0.5，模型实际给出的
    0.3–0.95 打分被整条丢弃，字段级分数因此看起来像乱填。离线回放的旧产物没有它，
    届时由函数按档位给默认值。
    """

    quote: str = ""
    """L1 定位层切出的**原文切片**（原文里的连续一段）。

    v2.1 起随断言一路带到 Notes 出口：``reasoning_detail.evidence`` 的首选内容就是它
    （「证据锚点要精确到客户原始数据中的某段内容」）。离线回放既有批次时通常为空。
    """

    anchor_kind: str = ""
    """这条断言的来源坐标形态（v3 接口一）：

    - ``verbatim``：值能在原文里**字面定位**，``quote`` 就是切出来的原文（模型不许誊抄）；
    - ``normalized``：值经 ASR 纠错 / 品牌归一产生，原文里**无字面匹配**（安安 → 埃安），
      此时 ``source_quote`` 记原文、理由记在 ``basis_of_inference``。

    空的按 ``verbatim`` 处理（离线回放的旧产物没有这个字段）。
    """

    source_quote: str = ""
    """当归型事实（``anchor_kind=normalized``）对应的**原文原样串**。

    值不是原文给的（是模型改过的），但改动前的原文必须留下——否则销售点开这条
    分不清「客户说的」与「模型改的」（v3 接口一）。``verbatim`` 时为空。
    """

    speaker_role: str = ""
    """录音证据的说话方：``销售方`` / ``客户方`` / ``未知来源``。

    由 L1 定位层按引文所在的句子反查得到（非录音渠道为空）。出口证据位据此加前缀：
    销售原话、客户原话、说话者身份存疑（信息可靠度低）。空值不加前缀——那不是
    「身份不明」，而是「这条证据不是录音」。
    """

    @property
    def tier(self) -> str:
        return CHANNEL_TIER.get(self.channel, SOCIAL)

    @property
    def rank(self) -> int:
        return _TIER_RANK.get(self.tier, len(SOURCE_TIERS))

    @property
    def is_prediction(self) -> bool:
        return self.claim_type == "prediction" or self.tier == PREDICTION

    @property
    def is_social(self) -> bool:
        return self.tier == SOCIAL

    @property
    def effective_basis(self) -> str:
        """自述档位与来源档冲突时，**取更弱的那个**。

        典型：一条渠道是「第三方画像数据」却自称 ``basis=fact``。自述档位是模型给的，
        来源档是代码按渠道定的——来源档更硬，因此这类断言不能算 ``fact``。

        v3.2：第三方画像标签是**采购来的商用画像数据**，单独支撑某字段时按
        ``inferred_high`` 处理（用户口径：客户没有试驾/沟通记录时，它是唯一可靠的
        画像信源）。此前一并压到 ``inferred_low``，使这类字段在字段级塌陷。
        """
        if self.is_prediction and self.basis == FACT:
            return INFERRED_HIGH
        return self.basis or FACT

    @property
    def basis_rank(self) -> int:
        """三分类的强弱序号（越小越强）。``fact`` 0、``inferred_high`` 1、
        ``inferred_low`` 2，未知档按**最弱**处理——宁可低估也不要让一个叫不出名字的档
        冒充事实。"""
        return BASIS_ORDER.get(self.effective_basis, len(BASIS_ORDER))


@dataclass(slots=True)
class Projection:
    """L2 的产物。"""

    facts: dict[str, Any] = field(default_factory=dict)
    notes_updates: list[dict[str, Any]] = field(default_factory=list)
    to_confirm: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)

    basis: dict[str, str] = field(default_factory=dict)
    """字段级置信档。缺省视为 ``fact``；被规则 b/d 降级的字段在此记 ``inferred_low``。

    L2 的产物是**字段值**，而三分类是**断言**的属性。降级一条断言后这个信息若不落在
    字段上就会丢，下游（L3 校验、表达层「无依据确定值率」）就无从判断哪些字段
    是「已写入但不硬」。因此在此显式留存。
    """

    downgraded: list[dict[str, Any]] = field(default_factory=list)
    """被**降级保留**的断言（v2 新增）。

    与 :attr:`rejected` 的分工：``rejected`` 是**没写进 facts** 的；
    ``downgraded`` 是**写进去了但置信档被拉低**的。两者都留痕——静默降级会让人
    误以为模型很有把握，而静默丢弃会让人误以为模型没提取到。
    """

    def asdict(self) -> dict[str, Any]:
        return {
            "facts": self.facts,
            "notes_updates": self.notes_updates,
            "to_confirm": self.to_confirm,
            "rejected": self.rejected,
            "basis": self.basis,
            "downgraded": self.downgraded,
        }


# ====================================================================
# 取值归一
# ====================================================================


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, default=str)


def split_multi(value: Any) -> list[Any]:
    """把 ``"a||b"`` / ``["a","b"]`` 这类多值表示摊平成列表。

    ``||`` 是基线里模型自造的连接符（`核心关注点` 等 5 个多选字段大量出现），
    不是契约的一部分，因此只在**多选字段**上按它切分。
    """
    if isinstance(value, (list, tuple)):
        out: list[Any] = []
        for item in value:
            out.extend(split_multi(item))
        return out
    if isinstance(value, str) and "||" in value:
        return [part.strip() for part in value.split("||") if part.strip()]
    return [value]


def _parse_structured(value: Any) -> dict | None:
    """把 ``'{"key":"yes", "input_value":"驾龄年限"}'`` 这类字符串解成字典。"""
    if isinstance(value, Mapping):
        return dict(value)
    text = _as_text(value)
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def normalize_enum(attribute: str, value: Any) -> Any:
    """把一个取值归一到契约枚举键。

    接受四种形态，全部实测出现过：

    1. **已是合法键**（``"fuel_efficiency"``）→ 原样；
    2. **中文展示名**（``"油耗"``）→ 反查得键；
    3. **结构化对象**（``'{"key":"yes","input_value":"..."}'``）→ 解出并补 ``input_value``；
    4. **不合法** → ``None``（由调用方决定拒绝还是留白）。

    多选字段由调用方先 :func:`split_multi`，本函数只处理单个值。
    """
    if not inf.is_enum(attribute):
        return None
    legal = inf.legal_keys(attribute)

    structured = _parse_structured(value)
    if structured is not None:
        key = _as_text(structured.get("key"))
        if key not in legal:
            key = inf.display_to_key(attribute, key) or ""
        if not key:
            return None
        if inf.is_structured(attribute, key):
            raw_input = structured.get("input_value")
            return {"key": key, "input_value": _as_text(raw_input)}
        return key

    text = _as_text(value)
    if not text:
        return None
    if text in legal:
        return text
    by_display = inf.display_to_key(attribute, text)
    if by_display:
        if inf.is_structured(attribute, by_display):
            # 只给了展示名、没给 input_value：按契约补空串，由 L4 校验提示补充。
            return {"key": by_display, "input_value": ""}
        return by_display
    return None


IDENTITY_GUARD_KEYS: frozenset[str] = nc.IDENTITY_KEYS | inf.INTERNAL_IDENTITY_KEYS
"""归属过滤与值形态门的判定集合：契约的「基础身份」**并上**编外身份字段。

**为什么必须取并集**：这两道门原来只按 ``nc.IDENTITY_KEYS`` 判定，而 v2 契约把
``company`` / ``company_type`` / ``old_car_model`` / ``owned_brand_model`` /
``owned_car_age`` 移出了「基础身份」。若不并回来，保护会**静默消失**——
不报错、测试也大多仍绿，只是某些本该被拦下的断言开始写进 facts。
实测：``company`` 下线后，一条 ``subject=unknown`` 的「我们单位也要买一个」
被当作客户的企业写了出去。
"""


def normalize_age(value: Any) -> str | None:
    """把年龄归一到统一格式。

    两套格式实测并存（卡片用 ``"40 ~ 49"``、Notes 用 ``"40-49岁"``，还有 ``"25~30岁"``、
    ``"50岁以上"``），同一客户的年龄因此在两处长得不一样。归一后统一为
    ``"40 ~ 49 岁"`` / ``"50 岁以上"``，两处取同一个值。
    """
    text = _as_text(value)
    if not text:
        return None
    matched = AGE_PATTERN.search(text)
    if matched:
        low, high = int(matched.group(1)), int(matched.group(2))
        if low > high:
            low, high = high, low
        return f"{low} ~ {high} 岁"
    opened = AGE_OPEN_PATTERN.search(text)
    if opened:
        return f"{int(opened.group(1))} 岁以上"
    return None


INCOME_BAND_RANGES: dict[str, str] = {
    "1": "8-15万",
    "2": "10-20万",
    "3": "20-30万",
    "4": "30-40万",
    "5": "40-60万",
    "6": "60万以上",
}
"""第三方「预测收入水平」档位 → 年收入区间（业务专家口径，2026-10-20）。

第三方标签只给 ``1档收入`` … ``6档收入`` 这样的档位，销售要的是具体金额。换算口径
与档位的升降序均由业务方确认：1 档最低、6 档最高，切点如上表。映射只认档位原文，
客户自己说的金额（``30-50万``）原样透传，不得被改写。
"""

_INCOME_BAND_PATTERN = re.compile(r"(?:第\s*)?([1-6])\s*档")


def normalize_income(value: Any) -> str | None:
    """把第三方收入档位换成区间数值；不是档位形态的取值返回 ``None``。

    ``None`` 表示「这条取值本来就不是档位」，调用方应保留原值——推断出来的金额、
    客户自述的金额都不是档位，不能被这张表吞掉。
    """
    text = _as_text(value)
    if not text:
        return None
    matched = _INCOME_BAND_PATTERN.search(text)
    if not matched:
        return None
    return INCOME_BAND_RANGES[matched.group(1)]


def normalize_level(value: Any) -> str | None:
    """把商机等级归一到 ``H`` / ``A`` / ``B`` / ``C`` / ``D`` / ``待识别``。

    「待识别」是规范里的独立结论（信息不足），**不是 C 级**，因此必须先于单字母
    匹配判断——否则任何含「待」或「识」的串都会落到字母分支里被误读。
    """
    text = _as_text(value).upper()
    if not text:
        return None
    if "待识别" in text or "待定" in text or "未知" in text:
        return "待识别"
    for level in LEVEL_ORDER:
        if level == "待识别":
            continue
        if text == level or text.startswith(level):
            return level
    return None


# ====================================================================
# 冲突消解
# ====================================================================


def _pick_best(group: Sequence[Claim]) -> tuple[Claim, list[Claim]]:
    """挑出最强的一条，返回 ``(最佳, 同档并列)``。

    排序键是 ``(basis 强弱, 来源档强弱)``——**``basis`` 优先于来源档**（设计稿 §6.2
    「冲突消解取『更强的一条』：``fact`` > ``inferred_high`` > ``inferred_low``」）。
    一条来源档较低但标了 ``fact``（有原文锚点）的断言，强于一条来源档高却只是推断的断言：
    前者能回溯到客户原话，后者不能。

    「同档并列」按**同一个 (basis, 档位) 组合**取，冲突检测据此判定「是否同档多值」。
    """
    best_key = min((claim.basis_rank, claim.rank) for claim in group)
    same_tier = [claim for claim in group if (claim.basis_rank, claim.rank) == best_key]
    same_tier.sort(key=lambda c: (c.created_at or ""), reverse=True)
    return same_tier[0], same_tier


def _diverse(values: Iterable[Any]) -> list[Any]:
    """去重且保序（值可能是 dict，不能直接进 set）。"""
    out: list[Any] = []
    seen: set[str] = set()
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


# ====================================================================
# 主流程
# ====================================================================


def project(
    claims: Sequence[Claim],
    *,
    context: Mapping[str, Any] | None = None,
) -> Projection:
    """把断言账本投影成规范事实与 Notes 更新项。

    ``context`` 承载投影所需、但不在字段值里的旁证：

    - ``company_deregistered``：关联企业的登记状态是否为「注销」。
      已注销主体**不得**当作在职企业填入（基线 ``13926073386`` 即此缺陷）。
    - ``has_old_car_evidence``：原文里是否出现旧车证据（用于补 ``old_car_model``
      与纠正 ``purchase_nature``）。
    """
    context = dict(context or {})
    result = Projection()

    # ---- a 归属过滤：非客户本人的断言不得写入身份字段
    kept: list[Claim] = []
    for claim in claims:
        if not claim.attribute:
            continue
        if (
            claim.attribute in IDENTITY_GUARD_KEYS
            and claim.attribute not in IDENTITY_SUBJECT_EXEMPT
            and claim.subject != "customer"
        ):
            result.rejected.append(
                {
                    "attribute": claim.attribute,
                    "value": claim.value,
                    "rule": "a_attribution",
                    "reason": f"事实主体是 {claim.subject}，不是客户本人",
                }
            )
            continue
        # ---- a2 值形态门（v3.1 改造二）：取值或锚点明写「销售顾问 / 我方人员」的，
        #      不得进客户字段。归属过滤只看 `subject`（模型自报，默认 customer），
        #      这一门看**取值本身说了什么**，两者是与关系。
        #      实测 `13435844484` 的姓名取值为「郑小姐（顾问自称，非客户姓名）」，
        #      subject 仍是 customer，只靠归属过滤拦不住。
        #      **豁免登记档**：人工/系统登记的取值是销售本人确认过的，客户职业确实可能
        #      就是销售顾问（「我是做销售的」），据此拒收会误杀真实事实。
        if (
            claim.attribute in IDENTITY_GUARD_KEYS
            and claim.tier != REGISTRY
            and _salesperson_marked(claim)
        ):
            result.rejected.append(
                {
                    "attribute": claim.attribute,
                    "value": claim.value,
                    "rule": "a_salesperson_value",
                    "reason": "取值/锚点指向我方销售顾问，不是客户本人信息",
                }
            )
            continue
        kept.append(claim)

    # ---- b 来源分级：身份字段的弱来源处置
    #      社交自呈现仍降级并转人工确认；**第三方画像标签单独支撑改记 inferred_high**
    #      （v3.2，用户口径：采购的画像数据在无一手记录时是唯一可靠画像信源）。
    grouped: dict[str, list[Claim]] = {}
    for claim in kept:
        grouped.setdefault(claim.attribute, []).append(claim)

    surviving: dict[str, list[Claim]] = {}
    for attribute, group in grouped.items():
        # ---- v3 接口二：身份字段来源白名单（在来源分级之前）
        # ``customer_name``：只认一手自述与登记姓名。营业执照主体名 / 昵称 / 店铺名
        # （公开登记查询 / 社交自呈现 / 预测档）**整条拒收**——错的人名比没有人名更糟。
        # v2.1 实测 ``customer_name`` 落成「…新泽水果店」（营业执照主体名）。
        if attribute == "customer_name":
            allowed = [c for c in group if c.tier in NAME_SOURCE_WHITELIST]
            if not allowed:
                result.rejected.append(
                    {
                        "attribute": attribute,
                        "value": [c.value for c in group],
                        "rule": "name_source_whitelist",
                        "reason": "姓名仅认一手自述或登记姓名，不采信企业/社交/预测来源",
                    }
                )
                result.to_confirm.append(
                    {
                        "attribute_key": attribute,
                        "candidates": [c.value for c in group],
                        "reason": "姓名来源不属于白名单（一手自述 / 登记）",
                        "suggested_action": "向客户或销售确认后补充",
                    }
                )
                continue
            # **只让白名单来源参与后续裁决**：否则一条社交档却自称 ``fact`` 的断言会在
            # ``_pick_best`` 里压过一手来源（排序先看 ``basis`` 再看来源档），
            # 白名单就成了摆设。过滤而非放行，是把白名单真正做成硬约束。
            group = allowed
            # 白名单来源给出的值若只是称呼（先生/女士），同样不是姓名。
            if all(
                str(c.value).strip().lower() in NAME_SALUTATION_VALUES for c in group
            ):
                result.rejected.append(
                    {
                        "attribute": attribute,
                        "value": [c.value for c in group],
                        "rule": "name_salutation_value",
                        "reason": "取值是社交称呼，不是客户姓名",
                    }
                )
                continue

        # ``title``（性别 / 称呼）：登记称呼 > 一手自述 > 头像性别标签 > 录音性别标签。
        # 非登记来源（含一手交互）一律降为 inferred_low —— 录音性别标签天生不可靠、
        # 说话人未必是客户本人；头像标签同属账号自呈现（接口二）。
        if attribute == "title" and all(c.tier in GENDER_WEAK_TIERS for c in group):
            result.basis[attribute] = INFERRED_LOW
            result.downgraded.append(
                {
                    "attribute": attribute,
                    "value": [c.value for c in group],
                    "rule": "title_weak_source",
                    "reason": "性别/称呼仅有非登记来源（录音/头像标签），不作确定值",
                    "action": "降为 inferred_low 写入（不丢字段）",
                }
            )

        weak_only = all(c.tier in WEAK_TIERS for c in group)
        # ``wechat_id`` 豁免社交降级（v3.5）：它的唯一现实来源就是主页截图，
        # 且「截图里能明确读出微信号」正是用户口径认定的铁的事实（1.0）。
        if attribute in IDENTITY_GUARD_KEYS and weak_only and attribute != "wechat_id":
            only_prediction = all(c.is_prediction for c in group)
            if only_prediction:
                # v3.2：**第三方画像标签单独撑身份字段 → 记为可信的高置信推断**。
                # 它不再是「低置信线索」：这批标签是采购来的商用画像数据，在客户既无
                # 试驾也无沟通记录时，往往是唯一可靠的画像信源。既不降级、也不转人工
                # 确认——它已被当作可用的推断结论（用户口径）。
                result.basis[attribute] = INFERRED_HIGH
            else:
                # 社交自呈现（昵称 / 头像 / 主页截图）仍不作数：那是客户**对外表演**的
                # 身份，与本人实际情况可以不一致。降级保留并转确认。
                result.basis[attribute] = INFERRED_LOW
                result.downgraded.append(
                    {
                        "attribute": attribute,
                        "value": [c.value for c in group],
                        "rule": "b_social_only",
                        "reason": "身份字段仅由社交自呈现支撑",
                        "action": "降为 inferred_low 写入（不丢字段）",
                    }
                )
                result.to_confirm.append(
                    {
                        "attribute_key": attribute,
                        "candidates": [c.value for c in group],
                        "reason": "身份字段仅由社交自呈现支撑，不构成客户事实",
                        "suggested_action": "向客户或销售确认后补充",
                    }
                )
            # 注意：**没有 continue** —— 该组继续进入 c/d/e，仍会被写入 facts。
        if attribute in ATTITUDE_KEYS and all(c.tier in WEAK_TIERS for c in group):
            # 硬规则 2：态度/行为类字段须有一手依据。只有弱来源时降级保留。
            result.basis[attribute] = INFERRED_LOW
            result.downgraded.append(
                {
                    "attribute": attribute,
                    "value": [c.value for c in group],
                    "rule": "b_attitude_weak_source",
                    "reason": "态度类字段仅有社交/预测依据，无一手或登记来源",
                    "action": "降为 inferred_low 写入（不丢字段）",
                }
            )
        surviving[attribute] = group

    # ---- c/d/e 冲突消解 + 枚举归一 + 单多值约束
    for attribute, group in surviving.items():
        best, same_tier = _pick_best(group)
        values = _diverse(claim.value for claim in same_tier)

        # 字段级置信档 = **胜出那条断言的档**（v2.1 补：此前只有「被降级」的分支才写
        # ``result.basis``，于是未被降级的字段在档案里**没有档位**——下游把「缺失」
        # 默认当成 ``fact``（L3 校验会拦），探针则误报成「全是 inferred_low」，
        # 两边都读错。档位必须在所有路径上都落定，而不是只在降级时落定）。
        # 注意：不得覆盖 b 硬规则刚写下的 ``inferred_low``（弱来源撑不起身份/态度字段），
        # 因此只在尚未落档时写入。
        result.basis.setdefault(attribute, best.effective_basis)

        if (
            len(values) > 1
            and not inf.is_multi_value(attribute)
            and attribute not in AGGREGATE_TEXT_KEYS
        ):
            # 同来源档给出多个不同值 → 不可判，留白转确认，绝不两个都写。
            # 例外：聚合语义字段（见 AGGREGATE_TEXT_KEYS）本就该汇总，落到下方的 else 分支。
            result.to_confirm.append(
                {
                    "attribute_key": attribute,
                    "candidates": values,
                    "reason": f"同来源档（{TIER_LABEL[best.tier]}）给出 {len(values)} 个不同值",
                    "suggested_action": "人工确认以哪一个为准",
                }
            )
            result.rejected.append(
                {
                    "attribute": attribute,
                    "value": values,
                    "rule": "c_conflict",
                    "reason": "同档多值冲突，未写入",
                }
            )
            continue

        if inf.is_enum(attribute):
            normalized: list[Any] = []
            illegal: list[Any] = []
            pool = values if inf.is_multi_value(attribute) else [best.value]
            for raw in pool:
                for item in split_multi(raw):
                    key = normalize_enum(attribute, item)
                    if key is None:
                        illegal.append(item)
                        continue
                    normalized.append(key)
            normalized = _diverse(normalized)
            if not normalized:
                # v2：一个都归不到合法键 → **保留原值** + 降为低置信 + 转人工确认，
                # 不再丢弃（设计稿 §6.2 规则 d）。原值往往就是客户的原话，丢掉它等于
                # 「因为格式对不上就把事实扔了」——R0 实测枚举归一失败是掉字段主因之一。
                original = list(values) if inf.is_multi_value(attribute) else values[0]
                result.facts[attribute] = original
                result.basis[attribute] = INFERRED_LOW
                result.downgraded.append(
                    {
                        "attribute": attribute,
                        "value": illegal or list(values),
                        "rule": "d_enum",
                        "reason": "取值无法归一到契约枚举",
                        "action": "保留原值 + 降为 inferred_low + 转人工确认",
                    }
                )
                result.to_confirm.append(
                    {
                        "attribute_key": attribute,
                        "candidates": illegal or list(values),
                        "reason": "取值无法归一到契约枚举",
                        "suggested_action": "人工确认后补填合法枚举键",
                    }
                )
                continue
            if illegal:
                # 部分归一成功：可归一的照写，归不了的那几项留痕（不因一项不合法丢掉整字段）。
                result.downgraded.append(
                    {
                        "attribute": attribute,
                        "value": illegal,
                        "rule": "d_enum_partial",
                        "reason": "部分取值无法归一到契约枚举，该项未写入",
                        "action": "保留可归一的部分",
                    }
                )
            result.facts[attribute] = normalized[0] if inf.is_single_value(attribute) else normalized
        else:
            chosen = values[0] if len(values) == 1 else values
            # 第三方收入档位（`N档收入`）在**事实层**就换成金额区间：下游分析、
            # 卡片、Notes 全读 facts，晚换一处就有一处会漏出「5档收入」。
            # 非档位形态（客户自述金额、推断金额）原样保留。
            if attribute == "annual_income" and not isinstance(chosen, (list, tuple)):
                chosen = normalize_income(chosen) or chosen
            result.facts[attribute] = chosen

    # ---- f 跨字段规则
    _apply_cross_field_rules(result, context)

    # ---- 等级唯一化（跨产物字段）
    _unify_level(result)

    # ---- 姓名兜底（v3.5 / 2026-10-20 用户口径）：没有任何可靠来源指向真实姓名时，
    #      输出「客户」+手机号后四位（如「客户0087」），没有手机号才写「未知」。
    #      留白会让下游误读成「没提取到」，并被 L2 兜底补一个假姓名（线上实测补出过
    #      ``customer_name="先生"``）。这里一次性落定，兜底层不再经手姓名字段。
    if "customer_name" not in result.facts:
        result.facts["customer_name"] = unresolved_customer_name(
            result.facts.get("phone_number")
        )
        result.basis["customer_name"] = INFERRED_LOW

    # ---- g 缺失即留白 + 产出 notes_updates
    result.notes_updates = _to_notes_updates(result, group_by_attribute(claims))
    return result


def group_by_attribute(claims: Sequence[Claim]) -> dict[str, list[Claim]]:
    grouped: dict[str, list[Claim]] = {}
    for claim in claims:
        grouped.setdefault(claim.attribute, []).append(claim)
    return grouped


def _apply_cross_field_rules(result: Projection, context: Mapping[str, Any]) -> None:
    """跨字段一致性：一处有证据，另一处必须跟上；一处被否，另一处不得留。"""
    facts = result.facts

    # —— 旧车 ⇒ 换购。基线多案正文写了旧车、购买性质却仍填 ``first``。
    #
    # 注意落点：v2 契约的 ``purchase_type`` 只剩「个户/公户」，购车性质（首购/增购/换购）
    # 已由编外字段 ``purchase_nature`` 承接。这条规则原本写 ``purchase_type``，
    # 不改落点的话它会写进一个只认 ``company``/``personal`` 的单选字段——
    # ``replacement`` 既非法、又会在回传时被静默丢弃。
    old_car = _as_text(facts.get("old_car_model"))
    has_old_car = bool(old_car) and old_car not in {"无", "没有", "无旧车"}
    has_old_car = has_old_car or bool(context.get("has_old_car_evidence"))
    trade_in = facts.get("trade_in_requirement")
    trade_in_yes = trade_in == "yes" or (isinstance(trade_in, dict) and trade_in.get("key") == "yes")

    # ``trade_in_requirement=yes`` **本身就是**「有旧车」的凭据，因此它与旧车证据同权：
    # 任一条成立即把购买性质定为换购。这里**不**清除 ``trade_in_requirement``——
    # 清除它会抹掉置换的唯一凭据，反而制造新的自相矛盾。
    if (has_old_car or trade_in_yes) and facts.get("purchase_nature") != "replacement":
        facts["purchase_nature"] = "replacement"
    if not (has_old_car or trade_in_yes) and facts.get("purchase_nature") == "replacement":
        # 声称换购却没有任何旧车凭据 → 撤销该断言，而不是留一个无证的「换购」。
        facts.pop("purchase_nature", None)
        result.rejected.append(
            {
                "attribute": "purchase_nature",
                "value": "replacement",
                "rule": "f_trade_in",
                "reason": "声称换购但无旧车证据支撑",
            }
        )
        result.to_confirm.append(
            {
                "attribute_key": "purchase_nature",
                "reason": "换购与其证据不一致",
                "suggested_action": "确认客户是否有旧车置换",
            }
        )

    # —— 已注销企业 ⇒ 不填在职属性
    if context.get("company_deregistered"):
        for key in ("company", "company_type"):
            if key in facts:
                result.rejected.append(
                    {
                        "attribute": key,
                        "value": facts.pop(key),
                        "rule": "f_deregistered",
                        "reason": "关联企业登记状态为注销，不得当作在职企业",
                    }
                )

    # —— 年龄/姓名等跨产物字段在卡片侧的唯一化由 _to_notes_updates 与 L3 共用同一 facts。


def _unify_level(result: Projection) -> None:
    """把 ``deal_level`` 与 ``business_opp_level`` 收敛成一个 ``deal_level``。"""
    values: list[tuple[str, str]] = []
    for key in LEVEL_KEYS:
        if key in result.facts:
            level = normalize_level(result.facts[key])
            if level:
                values.append((key, level))
            if key != "deal_level":
                result.facts.pop(key, None)

    if not values:
        return
    distinct = {level for _, level in values}
    chosen = min(distinct, key=lambda lv: LEVEL_ORDER.index(lv))
    result.facts["deal_level"] = chosen
    if len(distinct) > 1:
        result.rejected.append(
            {
                "attribute": "deal_level",
                "value": dict(values),
                "rule": "f_level",
                "reason": "两处等级不一致，取较高档并转人工确认",
            }
        )
        result.to_confirm.append(
            {
                "attribute_key": "deal_level",
                "candidates": sorted(distinct, key=lambda lv: LEVEL_ORDER.index(lv)),
                "reason": "卡片与商机等级不一致",
                "suggested_action": "人工确认最终等级",
            }
        )


MULTI_JOIN = "||"
"""多选**枚举**的落库连接符。契约提示词原文：多个选项值之间使用双竖线「||」连接。"""

TEXT_JOIN = "、"
"""自由文本**聚合**字段的落库连接符。与基线同形（`hobbies="美食、按摩/推拿"`）。"""


def serialize_value(attribute: str, value: Any) -> str:
    """把 ``facts`` 里的取值序列化成契约要求的**字符串**形态。

    契约（`GET /config/note-attributes` 字段说明与 production 提示词）只认字符串：
    `attribute_value` 一律是 ``str``，三种子形态各有约定——

    - 结构化枚举项 → **JSON 字符串**，形如 ``{"key":"yes","input_value":"5年"}``；
      基线 39 案实测 69 处全部如此（`family_structure` 16 / `driver_license_status` 23 /
      `trade_in_requirement` 14 / `seating_requirement` 16）。
    - 多选枚举 → ``a||b``。基线实测 18 个字段用此形态。
    - 自由文本聚合 → 中文顿号连接。

    阶段二实测违约：实验臂曾把 ``purchase_purpose`` 写成 ``["family"]``、
    ``driver_license_status`` 写成字典，而基线同字段是字符串。这是**下游契约违约**，
    与「枚举键填中文」同一性质，故在 L2 出口统一收口，不指望模型自觉。
    """
    if attribute == "annual_income" and not isinstance(value, (Mapping, list, tuple)):
        # 兜一层：事实层已换算过，但 L2 兜底的候选值与终判可能仍是档位原文，
        # 它们不经过 ``project()`` 的落值分支，只在这里收口。
        value = normalize_income(value) or value
    if isinstance(value, Mapping) or (
        isinstance(value, (list, tuple))
        and any(isinstance(item, Mapping) for item in value)
    ):
        if isinstance(value, (list, tuple)):
            items = list(value)
        else:
            items = [value]
        return MULTI_JOIN.join(
            json.dumps(item, ensure_ascii=False, default=str) for item in items
        ) if len(items) > 1 else json.dumps(value, ensure_ascii=False, default=str)

    if isinstance(value, (list, tuple)):
        parts = [_as_text(item) for item in value]
        parts = [part for part in parts if part]
        if not parts:
            return ""
        if inf.is_enum(attribute) and inf.is_multi_value(attribute):
            return MULTI_JOIN.join(parts)
        return TEXT_JOIN.join(parts)

    return _as_text(value)


CONFIDENCE_UNKNOWN_NAME = "未知"
"""姓名查不到可靠来源、且**没有手机号可拼兜底**时的取值（用户口径 v3.5）。

姓名与其它字段不同：**没有可靠来源时必须显式给一个占位值**，而不是留白，更不是拿
称呼（先生/女士）、昵称、企业名去凑。留白会让下游（卡片、L3 校验）读成「没提取到」，
进而被兜底层再补一个假姓名——线上实测补出过 ``customer_name="先生"``。
"""

NAME_PLACEHOLDER_PREFIX = "客户"
"""姓名兜底前缀（用户口径，2026-10-20）：优先输出「客户」+手机号后四位。"""

_NAME_PLACEHOLDER_PATTERN = re.compile(r"^客户\d{4}$")
"""姓名兜底值「客户0087」的形态。"""


def unresolved_customer_name(phone_number: Any) -> str:
    """姓名查不到可靠来源时的兜底取值。

    有手机号 → 「客户」+后四位（如「客户0087」），销售靠它把卡片对上人；手机号缺失或
    不足 4 位 → 退回「未知」。
    """
    digits = re.sub(r"\D", "", _as_text(phone_number))
    if len(digits) >= 4:
        return f"{NAME_PLACEHOLDER_PREFIX}{digits[-4:]}"
    return CONFIDENCE_UNKNOWN_NAME


def name_is_unresolved(value: Any) -> bool:
    """取值是否为「查不到真实姓名」的兜底形态（固定「未知」或「客户+后四位」）。"""
    text = _as_text(value)
    return text == CONFIDENCE_UNKNOWN_NAME or bool(_NAME_PLACEHOLDER_PATTERN.match(text))

NAME_SALUTATION_VALUES: frozenset[str] = frozenset(
    {
        "先生", "女士", "小姐", "大哥", "大姐", "帅哥", "美女",
        "mr", "ms", "mrs", "miss",
    }
)
"""只能当称呼、不能当姓名的取值。

兜底与投影层的值形态门共用：姓名取值命中即拒收。线上实测 L2 兜底把
「微信资料显示称呼为先生」写成 ``customer_name="先生"``。
"""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _confidence_for(result: Projection, key: str, sources: Sequence[Claim]) -> float:
    """按「事实 / 高可信 / 低置信」三档给字段级置信度（用户口径 v3.5）。

    基准是 **L1 模型自报的 ``confidence``**，按档位校准到可信区间，而不是按档位给
    固定值——旧实现 ``fact`` 恒 0.7、``inferred_low`` 恒 0.5，把模型 0.3–0.95 的
    真实打分整条丢弃，字段级分数因此失去排序意义。

    - **铁的事实**：手机号（直接取自原始数据）恒 1.0；微信号在能从截图中明确识别
      （有原文切片或登记档）时 1.0；其余登记档来源 1.0。其它 ``fact`` 落在 0.85–0.95。
    - **高可信结论**：多条不同渠道同向 → 0.95；否则按自报分落在 0.70–0.90。
    - **低置信推断**：按自报分落在 0.40–0.60；自报分低于 0.4 的纯猜测落在 0.20–0.30。
    - **姓名兜底值**（「未知」或「客户+后四位」）恒 1.0——「查遍了也没有」本身是
      确定的事实，不是猜测。
    """
    if key == "customer_name" and name_is_unresolved(result.facts.get(key)):
        return 1.0

    raw = next((c.confidence for c in sources if c.confidence is not None), None)
    tier = sources[0].tier if sources else ""
    basis = result.basis.get(key)

    if basis == INFERRED_LOW:
        if raw is None:
            return 0.5
        if raw < 0.4:
            return round(_clamp(raw, 0.2, 0.3), 2)
        return round(_clamp(raw, 0.4, 0.6), 2)

    if basis == INFERRED_HIGH:
        channels = {c.channel for c in sources if c.channel}
        if len(channels) >= 2:
            return 0.95
        return round(_clamp(raw if raw is not None else 0.8, 0.7, 0.9), 2)

    # 事实档
    if key == "phone_number":
        return 1.0
    if key == "wechat_id" and (
        tier == REGISTRY or any(c.quote or c.source_quote for c in sources)
    ):
        return 1.0
    if tier == REGISTRY:
        return 1.0
    return round(_clamp(raw if raw is not None else 0.9, 0.85, 0.95), 2)


_AUDIT_TOKENS = tuple(
    sorted(
        set(TIER_LABEL.values())
        | {
            "归属过滤", "来源分级", "枚举归一", "单多值约束", "契约校验",
            "未知渠道", "按来源分级投影得到", "断言",
        },
        key=len,
        reverse=True,
    )
)
"""对客文案里**必须**剥除的内部用语：来源档代号与投影规则名（v2.1 硬约束，设计稿 §1）。"""


def _plain_text(text: Any) -> str:
    """把内部用语从对客文案里剥掉。

    L1 用 ``basis_of_inference`` 写推断依据时常把来源档代号与置信度一并写进去
    （实测「第三方预测性别为男（0.94）」）。这些是审计串，销售看不懂，**必须**在出口
    剥除——提示词已要求模型别写，但出口的自净不能指望模型自觉。
    """
    out = str(text or "")
    for token in _AUDIT_TOKENS:
        out = out.replace(token, "")
    # 「置信度0.9377」「（0.94）」这类内部置信度一并去掉：它不面向销售，
    # 且会暴露证据分级的存在（v2.1 实测 ``title`` 的证据里残留「置信度0.9377」）。
    out = re.sub(r"置信度\s*0?\.\d+", "", out)
    out = re.sub(r"[（(]\s*0?\.\d+\s*[)）]", "", out)
    return re.sub(r"\s+", " ", out).strip(" ：:、,，")


_CONCLUSION_MARKER = re.compile(r"(推断|推测|判断)[为成是]")
"""判据：一段文字里的**结论型从句**起点。

``evidence`` 必须指向客户原始数据的一段内容，不得把「…，推断为实用型」这类**结论**
当成依据写进去（v2.1 实测：11 条证据位自指）。结论从句可能在字符串**任意位置**
（实测形态是「客户主动询问…，关注实际购车成本，推断为实用型」），因此不能只判开头。
"""


def _strip_conclusion(text: str) -> str:
    """裁掉从结论型标志词起的整段从句，返回其前的**事实部分**。

    「客户主动询问车型优惠价格及置换补贴，关注实际购车成本，推断为实用型」
    → 「客户主动询问车型优惠价格及置换补贴，关注实际购车成本」。
    若整段都是结论（标志词在开头），返回空串——调用方据此丢弃该条依据。
    """
    cleaned = _plain_text(text)
    matched = _CONCLUSION_MARKER.search(cleaned)
    if not matched:
        return cleaned
    head = cleaned[: matched.start()].rstrip("，,。;；、 ")
    return head


def _speaker_prefix(role: str) -> str:
    """证据位的说话方前缀（用户口径，2026-10-20）。

    - ``销售方`` → 「销售原话：」——这条证据出自我方销售，**不是客户说的**；
    - ``客户方`` → 「客户原话：」；
    - ``未知来源`` → 「说话者身份存疑，信息可靠度低：」——身份不明，销售据此自行打折；
    - 空值 → 不加前缀。空不是「身份不明」，而是「这条证据不是录音」（截图、登记、
      第三方画像等），给它们加前缀会凭空造出一个不存在的可信度信号。
    """
    if role == SPEAKER_SALES:
        return "销售原话："
    if role == SPEAKER_CUSTOMER:
        return "客户原话："
    if role == SPEAKER_UNKNOWN:
        return "说话者身份存疑，信息可靠度低："
    return ""


def _is_conclusion(text: str) -> bool:
    """整段皆为结论（无任何事实部分）时为真。"""
    return not _strip_conclusion(text)


def _salesperson_marked(claim: Claim) -> bool:
    """取值、引文或当归原文里出现销售顾问指向词（v3.1 改造二）。

    同时看三处：``value``（取值本身）、``quote``（原文切片）、``source_quote``（当归前原文）。
    **不看** ``basis_of_inference``——那是推断依据，本就常提到「销售说…」，把它算进来会
    误杀大量正确的推断（实测 ``title`` 的依据多为「销售以『大哥』称呼对方」）。
    """
    parts = (_as_text(claim.value), claim.quote or "", claim.source_quote or "")
    text = " ".join(parts)
    return any(marker in text for marker in SALESPERSON_MARKERS)


def _readable_evidence(sources: Sequence[Claim]) -> list[str]:
    """证据串：**原文切片优先**，其次当归型事实的原文，再次推断依据。

    v2.1 硬约束（设计稿 §1）：证据锚点要精确到客户原始数据中的某段内容。v3 接口一
    在此基础上再分两类：

    - ``verbatim``：``quote`` 是切出的原文，直接呈现；
    - ``normalized``：值经纠错/归一（安安 → 埃安），呈「原文『安安I60』」（标明是原文，
      值本身是字段取值）——销售一眼能分清「客户说的」与「模型改的」。

    **渠道名不再作为证据**（2026-10-19 可读性整改）：裸渠道名只回答了「取自哪里」，
    没回答「原文是什么」，销售看不懂。没有原文与依据时证据位留空，由前端不渲染该区块。

    第三方画像标签另有专门形态：标签名（``quote``）+ 取值，呈
    「第三方画像标签“预测年龄段”给出“50岁及以上”」。

    结论型依据（``_is_conclusion``）一律剔除，不留任何占位符。
    """
    evidence: list[str] = []
    for claim in sources:
        tag = _prediction_tag_text(claim)
        if tag:
            if tag not in evidence:
                evidence.append(tag)
            continue
        if claim.quote:
            prefix = _speaker_prefix(claim.speaker_role)
            body = f"{claim.channel}：{claim.quote}" if claim.channel else claim.quote
            text = f"{prefix}{body}"
            if text not in evidence:
                evidence.append(text)
            continue
        if claim.source_quote:
            prefix = _speaker_prefix(claim.speaker_role)
            text = f"{prefix}原文「{claim.source_quote}」（经纠错归一）"
            if text not in evidence:
                evidence.append(text)
            continue
        for hint in claim.basis_of_inference:
            text = _strip_conclusion(hint)
            if text and text not in evidence:
                evidence.append(text)
        injected = _injected_evidence(claim)
        if injected and injected not in evidence:
            evidence.append(injected)
    return evidence


def _injected_evidence(claim: Claim) -> str:
    """注入类字段的证据句：注入值本身就是事实，直接写出来。

    ``phone_number`` / ``driver_license_status`` 的取值由代码按入参或试驾记录定，
    不来自任何渠道原文，因此此前证据位只有一个渠道名（「客户留资手机号」）。
    这里把取值与依据一并写出，销售能直接核对。
    """
    if claim.channel == INJECTED_PHONE_CHANNEL:
        value = _as_text(claim.value).strip()
        return f"客户留资手机号：{value}" if value else ""
    if claim.channel == INJECTED_LICENSE_CHANNEL:
        return "到店试驾或有旧车，驾驶资格成立"
    return ""


def _prediction_tag_text(claim: Claim) -> str:
    """第三方画像标签的证据句：标签名 + 取值。

    提示词已要求 ``basis_of_inference`` 写全「标签名 + 取值」，但旧产物与模型抖动都可能
    只给标签名（实测 163/163 条证据只到标签名）。这里用断言自身的 ``quote``（标签名）
    与 ``value`` 兜底拼出完整句，保证销售看得到标签给出了什么。
    """
    if claim.tier != PREDICTION:
        return ""
    tag = _as_text(claim.quote).strip()
    value = _as_text(claim.value).strip()
    display = inf.display_map(claim.attribute).get(value)
    if display:
        value = display
    if not tag or not value:
        return ""
    if value in tag:
        return f"第三方画像标签“{tag}”"
    return f"第三方画像标签“{tag}”给出“{value}”"


def _readable_steps(sources: Sequence[Claim]) -> list[str]:
    """推理步骤：由依据推到结论的**思维路径**（v3.1 改造一，1–4 条中文短句）。

    **与 :func:`_readable_evidence` 的分工**：``evidence`` 落**原材料**（客户原话、
    当归前原文），本函数落**论证**（怎么从材料推到取值）。v3 的病灶正是两者从同一份
    原料分流渲染——``steps`` 有锚点时吐「原始依据：<锚点>」、无锚点时吐「推断依据：<依据>」，
    两栏字面雷同（实测 234 字段中 122 个完全同集，占 52%）。

    取料顺序：L1 显式产出的 ``reasoning_steps`` > 一句**非复述**的来源说明。缺
    ``reasoning_steps`` 时**不再抄锚点原文**——抄锚点正与 ``evidence`` 撞车。

    不得出现投影规则名（归属过滤 / 来源分级 / 枚举归一）与来源档代号——那些是
    内部审计串，销售看不懂（设计稿 §1）。
    """
    for claim in sources:
        if not claim.reasoning_steps:
            continue
        lines: list[str] = []
        for step in claim.reasoning_steps:
            text = _plain_text(step)
            if text and text not in lines:
                lines.append(text)
        if lines:
            return lines[:4]

    # —— 退路：一句**不复述材料**的说明。旧的「原始依据：<锚点>」正是雷同的来源。
    if any(claim.quote for claim in sources):
        return ["由客户原话直接得出该结论"]
    if any(claim.source_quote for claim in sources):
        return ["经语义纠错归一并核对后得出该结论"]
    if any(claim.basis_of_inference for claim in sources):
        return ["综合上述线索归纳得出该结论"]
    # **不再输出「来自{渠道}」**（2026-10-19 可读性整改）：它只复述了出处，没给出推导。
    return ["据现有记录归纳"]


def _readable_summary(sources: Sequence[Claim]) -> str:
    """一句话说清结论怎么来的（真人可读）。"""
    quote = next((c.quote for c in sources if c.quote), "")
    if quote:
        return f"有原文依据：{quote}"
    hint = next((_strip_conclusion(h) for c in sources for h in c.basis_of_inference if _strip_conclusion(h)), "")
    if hint:
        return f"由多条线索推断：{hint}"
    return "据现有记录归纳得出"


def _to_notes_updates(
    result: Projection, grouped: Mapping[str, list[Claim]]
) -> list[dict[str, Any]]:
    """把 ``facts`` 投影成契约字段形态的 Notes 更新项（字段集见 :mod:`customer_profile.notes_contract`）。

    只输出**有事实支撑**的字段（缺失即留白，规则 g）。

    ``reasoning_detail`` 面向**销售**，一律真人可读（v2.1 硬约束，设计稿 §1）：
    ``evidence`` 首选 L1 定位层切出的原文切片（``quote``），其次是推断依据
    （``basis_of_inference``），都不给才退到渠道名；``steps`` 写推导链。**不得**再出现
    投影规则名（归属过滤 / 来源分级 / 枚举归一）或来源档代号，也不留「未知渠道」占位。
    """
    updates: list[dict[str, Any]] = []
    for key in nc.ALL_KEYS:
        if key not in result.facts:
            continue
        value = result.facts[key]
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and not value:
            continue
        if key == "age":
            value = normalize_age(value) or value
        value = serialize_value(key, value)
        if not value:
            continue

        sources = sorted(grouped.get(key) or [], key=lambda c: c.rank)
        if key == "customer_name" and name_is_unresolved(result.facts.get(key)):
            # 姓名查不到可靠来源：固定兜底值（「客户+手机号后四位」，无手机号则「未知」），
            # 并说清为什么没有——不写「据现有记录归纳」这种像是从记录里提取到了什么的措辞。
            detail = {
                "summary": "客户信息中未查找到真实姓名",
                "steps": ["现有记录中没有客户本人的姓名信息", "社交资料中的称呼与昵称不作为姓名"],
                "evidence": [],
                "confidence": 1.0,
            }
        else:
            detail = {
                "summary": _readable_summary(sources),
                "steps": _readable_steps(sources),
                "evidence": _readable_evidence(sources),
                "confidence": _confidence_for(result, key, sources),
            }
        updates.append(
            {
                "attribute_key": key,
                "attribute_value": value,
                "reasoning_detail": detail,
            }
        )
    return updates


# ====================================================================
# LLM 兜底：候选集与终判校验（设计稿 §6.3）
# ====================================================================


def build_l2_candidates(
    claims: Sequence[Claim], result: Projection, covered: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """整理「规则判不了」的部分，作为 LLM 兜底的输入。

    **能写成规则的，不交给 LLM**（设计稿 §6.1）：只有进了 ``to_confirm`` 的字段才在这里出现，
    且每条都带**带来源档与 basis 的候选值**——兜底只能在这个集合里裁决。

    v2.1 增补：``covered`` 传入「L1 已给结论的字段」，其余契约字段作为**缺字段**候选
    一并交给兜底（``missing=True``）。它们没有候选值，兜底只能基于已有 ``facts`` 归纳，
    且产出恒为 ``inferred_low``（见 :func:`apply_l2_verdict`）——这是问题 2「没有任何一层
    负责字段级补全」的落地，同时不松开「不得编造候选集外取值」的红线（缺字段本就没有
    候选集，因此只能给软值）。
    """
    grouped = group_by_attribute(claims)
    candidates: list[dict[str, Any]] = []
    for entry in result.to_confirm:
        attribute = str(entry.get("attribute_key") or "")
        if not attribute:
            continue
        if attribute == "customer_name":
            # 姓名字段**不进兜底**（v3.5）：投影层已把它落定为「未知」或一个白名单来源的
            # 真名，让兜底再裁决一次，等于把刚拒收的营业执照主体名从候选集里放回来
            # （线上实测发生）。
            continue
        options: list[dict[str, Any]] = []
        for claim in grouped.get(attribute) or []:
            options.append(
                {
                    "value": claim.value,
                    "channel": claim.channel,
                    "tier": TIER_LABEL[claim.tier],
                    "basis": claim.effective_basis,
                }
            )
        candidates.append(
            {
                "attribute_key": attribute,
                "current_value": result.facts.get(attribute),
                "current_basis": result.basis.get(attribute, FACT),
                "candidates": entry.get("candidates") or [],
                "options": options,
                "reason": entry.get("reason") or "",
            }
        )

    seen = {c["attribute_key"] for c in candidates}
    for key in nc.ALL_KEYS:
        if key in seen or key in covered or key in result.facts:
            continue
        candidates.append(
            {
                "attribute_key": key,
                "current_value": None,
                "current_basis": FACT,
                "candidates": [],
                "options": [],
                "missing": True,
                "reason": "L1 未给出该字段结论，可按已有事实归纳补一个软值",
            }
        )
    return candidates


def apply_l2_verdict(
    candidates: Sequence[Mapping[str, Any]], verdict_json: Any
) -> dict[str, Any]:
    """校验 LLM 兜底的终判，返回 ``{accepted, rejected, out_of_bounds}``。

    **硬边界：不得新增候选集之外的字段值**（设计稿 §6.3）。终判给的值必须能在该字段的
    候选集里找到出处——比对的形态是 ``serialize_value`` 之后的字符串，因为模型看到的
    是候选值本身，而它回的可能已经是落库形态。

    越界项一律拒收并留痕，**不修改 facts**：兜底是裁决者，不是作者。宁可字段停在
    ``to_confirm``，也不能让一个凭空生成的取值进产物——那正是 production 那 11 路
    自由生成的老毛病。
    """
    data = verdict_json
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return {"accepted": [], "rejected": [], "out_of_bounds": 0, "parse_ok": False}
    if isinstance(data, Mapping):
        data = data.get("verdicts", data.get("fields", []))
    if not isinstance(data, (list, tuple)):
        return {"accepted": [], "rejected": [], "out_of_bounds": 0, "parse_ok": False}

    allowed: dict[str, set[str]] = {}
    missing: set[str] = set()
    for candidate in candidates:
        attribute = str(candidate.get("attribute_key") or "")
        pool: list[Any] = list(candidate.get("candidates") or [])
        pool += [option.get("value") for option in candidate.get("options") or []]
        allowed[attribute] = {serialize_value(attribute, value) for value in pool}
        if candidate.get("missing"):
            missing.add(attribute)

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    out_of_bounds = 0
    for item in data:
        if not isinstance(item, Mapping):
            continue
        attribute = str(item.get("attribute_key") or "")
        value = item.get("value", item.get("attribute_value"))
        if not attribute or value is None:
            continue
        as_text = serialize_value(attribute, value)
        if attribute == "customer_name":
            # 姓名不归兜底管（v3.5）：投影层已落定为「未知」或白名单来源的真名。
            # 线上实测兜底凭「微信资料显示称呼为先生」补出过 ``customer_name="先生"``。
            out_of_bounds += 1
            rejected.append(
                {
                    "attribute": attribute,
                    "value": value,
                    "rule": "g_name_not_adjudicated",
                    "reason": "姓名由投影层定稿，不交给兜底补全",
                }
            )
            continue
        if attribute in missing:
            # 缺字段补全：本就没有候选集，因此**不判越界**，但恒为软值 inferred_low
            # （兜底是「补一个可核对的猜测」，不是「给一个事实」）。
            if not as_text.strip():
                continue
            accepted.append(
                {
                    "attribute_key": attribute,
                    "attribute_value": as_text,
                    "basis": INFERRED_LOW,
                    "reason": str(item.get("reason") or "据已有事实归纳"),
                    "missing": True,
                }
            )
            continue
        if as_text not in allowed.get(attribute, set()):
            out_of_bounds += 1
            rejected.append(
                {
                    "attribute": attribute,
                    "value": value,
                    "rule": "g_out_of_candidates",
                    "reason": "兜底给出的取值不在候选集内，已拒收",
                }
            )
            continue
        accepted.append(
            {
                "attribute_key": attribute,
                "attribute_value": as_text,
                "basis": str(item.get("basis") or INFERRED_HIGH),
                "reason": str(item.get("reason") or ""),
            }
        )
    return {
        "accepted": accepted,
        "rejected": rejected,
        "out_of_bounds": out_of_bounds,
        "parse_ok": True,
    }


# ====================================================================
# 回放适配：由既有 Notes 条目重建断言
# ====================================================================

EVIDENCE_PREFIX = re.compile(r"^(?P<channel>[^：:]{2,40})[：:]")


def claims_from_notes_entry(entry: Mapping[str, Any]) -> list[Claim]:
    """把一条既有 ``basic_notes_updates`` 重建为断言。

    ``reasoning_detail.evidence`` 的每一条形如「渠道名：原文片段」，渠道名可与
    :data:`CHANNEL_TIER` 对齐；据此判定来源档与是否「仅第三方预测撑腰」。

    识别不出渠道的条目按 ``social`` 处理（保守：不会把它当登记事实）。
    """
    attribute = str(entry.get("attribute_key") or "")
    if not attribute:
        return []
    detail = entry.get("reasoning_detail") or {}
    snippets = [str(x) for x in (detail.get("evidence") or [])]
    if not snippets:
        return [Claim(attribute=attribute, value=entry.get("attribute_value"))]

    claims: list[Claim] = []
    for snippet in snippets:
        matched = EVIDENCE_PREFIX.match(snippet.strip())
        channel = ""
        if matched:
            name = matched.group("channel").strip()
            channel = name if name in CHANNEL_TIER else _match_channel(name)
        tier = CHANNEL_TIER.get(channel, SOCIAL)
        claims.append(
            Claim(
                attribute=attribute,
                value=entry.get("attribute_value"),
                channel=channel,
                claim_type="prediction" if tier == PREDICTION else "fact",
                created_at=str(detail.get("summary") or ""),
            )
        )
    return claims


def _match_channel(name: str) -> str:
    """证据前缀与渠道名不完全一致时按包含关系就近匹配。"""
    for channel in CHANNEL_TIER:
        if channel in name or name in channel:
            return channel
    aliases = {
        "试驾录音": "试驾录音信息",
        "外呼录音信息": "外呼录音信息",
        "外呼录音": "外呼录音信息",
        "微信手机号搜索界面": "微信手机号搜索界面信息",
        "朋友圈": "朋友圈截图内容信息",
        "聊天记录": "客户与我方销售人员的聊天记录信息",
        "第三方画像数据": "第三方画像数据",
        "第三方画像": "第三方画像数据",
        "极光": "第三方画像数据",
        "关联企业信息": "关联企业信息",
        "手机号归属地": "手机号归属地信息",
    }
    for prefix, channel in aliases.items():
        if prefix in name:
            return channel
    return ""
