# 变更日志

按时间倒序记录重大功能与里程碑事件。

## 2026-10-29 — 「微信」契约缺口定论：不加键，`ENUM-02` 专项收口

`frequent_social_media` 的合法集只有微博 / 抖音 / 头条 / 小红书，而「微信」是客户口里
最高频的取值之一。此前 `ENUM-02` 把它列为「契约缺口」，就地阻断送检、写 `to_confirm`，
等业务方确认是否加键——这是该专项遗留文档唯一未解除的前置。

**业务方 2026-10-29 定论**：微信在当下日常生活中的定位是**即时通信工具**，不是社交媒体，
不能与抖音、小红书这类纯社媒并列。**不加键，契约维持现状。**

处置不变，但性质从「待定」变为「终局」：该取值仍不送检（送检必然换来一个看似相近的
`weibo`，等于凭空造一个客户没用过的平台），仍按「无法映射」走 `to_confirm`，销售侧照旧
看到「该字段的取值无法归入可选范围 / 请确认后补填」。代码未改，只更新注释与设计文档口径。

**专项文档已删除**：`docs/遗留问题-非法枚举值的门禁策略与LLM归一.md` 的两项解除条件中，
`to_confirm` 回传出口（L-017）属既有问题、独立跟踪；契约缺口现已定论。映射通道本身
（`ENUM-02`、`pipeline_l2_enum_map`、`ENUM_MAPPING_ENABLED`）已实现并有 17 条守卫用例，
开关仍缺省关闭，待真实批次上出现同类样本再评估是否开启。

## 2026-10-29 — 出口层文案改为销售口径（`REASON-02`）

`REASON-01` 修掉了「未作二次推断」这一句，但扫描回写体仍有 107 处管线内部说法，
分布在四个区块。销售的原话是「看不懂」——不是看不懂结论，是看不懂这些词本身。

**四类来源，逐一处理**：

1. **代码拼的兜底串**（填充项「已定稿事实中未见换车动因相关信息」、表达层 supplement
   「由表达层依同一份已定稿事实归纳」、末级兜底、`L3` 定级兜底）——直接改写为销售视角。
   填充项改为「客户资料与沟通记录中未提及换车原因」；末级兜底改为「客户资料中已有这条
   信息，直接采用」。
2. **投影层的三句模板语**（`由客户原话直接得出该结论` / `据现有记录归纳` /
   `综合上述线索归纳得出该结论`）——改写成「客户本人有明确表述，据此直接确定」等。
   这三句是 `_readable_steps` 的第二级退路，只在 L1 没给推导链时出现。
3. **模型写的句子**（表达层实测会写「已定稿事实显示客户处于买车中」）——改不到源头，
   出口自净：新增 `PIPELINE_VOCABULARY` 同义替换表，`_normalize_detail` 对
   `summary` / `steps` / `evidence` 统一过一遍。
4. **旧批次回放的产物**（Notes 里存的仍是旧兜底语）——同一张表一并覆盖，新旧产物在
   销售眼里是同一种说法。

- **守卫用例**：`test_no_pipeline_vocabulary_reaches_the_sales_rep` 扫四个区块的每一段
  文案，出现任一管线口径即失败。
- **实测**：同 5 案重跑，回写体内部用语 107 处 → 0 处（`readable-wording-20261029` 为 107，
  `wording-final-20261029` 与 `wording-final2-20261029` 两批均为 0，各 5 案全部成功）。
- **不动的部分**：`evidence` 为空不等于缺证据——填充项本就无来源事实，前端不渲染该区块，
  这是正确行为。契约要求 `steps` 必填（接口文档 §3），因此保留一句读得懂的说明。

## 2026-10-29 — 画像卡推导链接入断言账本（`REASON-01` 出口可读性）

销售反馈：画像卡上大量字段的「逻辑推导链」写的是「由上游已定稿的事实直接给出，未作二次推断」，
读不懂。根因不在提示词，在回写出口的兜底分支——`callback_build._FactView.detail()` 在 Notes
里找不到同字段条目时，直接吐这句内部审计话，且 `evidence` 留空、`confidence` 填固定 0.7。

**为什么必然大面积命中**：Notes 只装契约字段，`thin` 档还会把无登记档支撑的字段整批清空；
而画像卡的 `decision_psychology` 三项（`emotion` / `communication_rhythm` /
`personality_and_preferences`）、「当前用车」（`old_car_model` 等）、「换车动因」
（`purchase_nature`）主要由**编外字段**支撑，Notes 里永远不会有它们的条目。

**改动**：把 L1 的断言账本 `claims_json` 接到 `CALLBACK_BUILD` 节点（来源 `L1_INJECT`），
`_FactView` 在 Notes 无条目时退到账本，用**与 Notes 出口完全相同的渲染函数**
（`projection.detail_from_claims` → `_readable_steps` / `_readable_evidence` /
`_confidence_for`）生成推导链与证据。取料顺序：Notes 措辞 > 断言账本 > 代码中性说明。
账本为空时不编造，继续往下一级退。

- **离线重放（同一份输入，开关对照）**：uat-18 18 案，画像卡兜底 27→0、战败因素 14→0、
  心理画像 44→0；uat-all-tasks sufficient 43 案 35/14/71→0/0/0，thin 26 案 94/53/77→0/0/0。
  Notes 与 `ai_insight` 不变（它们本就 0 兜底）。
- **隔离验证**：在回放批次的同一份产物上开关 `claims_json`，除 `reasoning_detail` 外的
  全部内容零差异——本改动只改措辞，不动取值、条数与 id。
- **小范围跑批**：`claims-wire-20261029` 5 案全部成功（回放 uat-18 归档输入，模型真实调用），
  Notes / 画像卡 / 战败因素 / 心理画像共 230 条 `reasoning_detail`，兜底 0 条。
- **残留**：仍空证据的条目全是 `_filler_item` 生成的「暂无 XX」填充项，它们没有来源事实
  可供引用，属方案的第二类（代码模板话），见 `遗留问题清单.md` L-020。

## 2026-10-28 — 非法枚举值不再静默丢失（`ENUM-02` 语义映射）

L1 稳定给出契约外的中文自由文本（`已婚有子女`、`分期`、`初步了解`），三层各自都救不回来，
最终在回写出口被整条剔除且不写 `to_confirm`——六案实测丢掉 40 余条 Notes，其中包含
`basis=fact`、置信 1.0 的驾照情况。

- **新增节点 `pipeline_l2_enum_map`**（1 次受约束 LLM，`ENUM_MAPPING_ENABLED` 缺省关闭）。
  接在 `L2_APPLY` 之后：`facts` 取投影层、Notes 取兜底并入后的终判——中文原值大多是
  兜底写进 Notes 的，接在投影层之后一次也看不到（首版接错位置，六案全部报 `no_candidate`）。
  下游 5 处改指该节点，输出同名同形。
- **修 `code` 节点 LLM 注入键**：`_checker_functions` 曾用**节点 ID**做键，而节点
  `config['function']` 是**函数名**，查找落空后静默回退、`llm` 为 `None`（映射与 checker
  都受影响）。改为从函数对象取 `__name__`，并新增 `tests/test_code_injection.py` 钉住
  「注入键必须与节点引用一致」。
- **先确定性后语义**：合法键、展示名、旧键别名、跨字段路由、占位串、废弃键形态一律本地
  判定不进提示词；进程内按 `(字段, 合法键集合, 原值)` 缓存，同批同值只问一次、跨案复用。
- **代码守最后一道**：键必须属于该字段合法集，取值必须是送检过的那个，否则按未映射处理。
  映射不动置信档，仍是 `inferred_low`。
- **契约缺口不猜**：`frequent_social_media` 的「微信」在契约里没有键，就地阻断送检、
  走 `to_confirm`。（2026-10-29 补充：业务方已定论不加键，见本条下方更新。）
- **未映射一律留痕**：补 `to_confirm`（文案「该字段的取值无法归入可选范围 / 请确认后补填」，
  不含规则编号与内部键名）；`rule_trace` 记「原值 → 键」供核读。判据与送检前的过滤
  同口径——本地能翻的（`本科`→`bachelor`）、换字段能救的（`首购`）、占位串都不报，
  否则会让销售去核对一个本就正确的字段。
- **实测（六案，开/关两臂对照）**：回写出口丢弃 19 → 12 条，非法取值从静默消失改为
  可追溯的转确认；映射本身几近零命中（两轮 12 案只命中 1 条：`yes（东风风神/岚图汽车）`
  → `yes`，判断正确）。残留全是 `贷款`/`抖音`/`微信` 这类硬边界该判 null 的取值——
  通道行为正确，只是这批样本里几乎没有它该处理的对象。
  注意 `to_confirm` 目前没有回传出口（既有遗留 L-017），留痕只落在产物里。
- **顺带修三处确定性缺陷**：结构化枚举字符串（`{"key":"yes","input_value":""}`）在 gate
  与回写出口都被当成非法值——前者误降级、后者整条剔除，两处同步改为先解析 JSON 再取
  `key`；`purchase_type` 的中文展示名（首购/增购/换购）现在也能路由到 `purchase_nature`。

## 2026-10-28 — L2 兜底改由 code 节点承载（空裁决归零）

归档 148 次兜底调用里 66 次输出不足 60 token，其中 35 次候选清单本来就是空的、31 次
候选非空却回了空裁决——两种情况在 `finish_reason` 上都是 `stop`，旧逻辑一律放行。
表现为「兜底静默不干活」，`uat-all-tasks` 原始批次里 69 次调用有 46 次是这种形态。

- **候选为空不再发起调用**。零 token 直接返回 `{"verdicts": []}`。这类调用占归档两成，
  每次都稳定换来 7 token 的空结论。
- **输出量下限按候选数生效**（`L2_FALLBACK_MIN_TOKENS = 40`）。同样 7 token，候选为 0 时
  是正确答案、候选为 30 时是截断——静态阈值表达不了，所以判据是「候选数 × 输出量」。
  40 落在实测的空结论（7–8）与正常最小（65）之间，并有用例钉住这个区间。
- **节点从 `llm` 改为 `code`**。不只是为了加闸门：`llm` 节点失败会阻塞整棵下游子树，
  把「兜底没干活」升级成「整案失败」。代码承载后失败一律不抛，状态记 `failed`，
  产物是空裁决（等于「兜底没意见」），与 checker / 枚举映射同一骨架。
- **实测（六案）**：空裁决 2/6（关映射臂）、1/6（开映射臂）→ **0/6**；回传 Notes
  220 → 234。回传体非法枚举值 0 条。
- **顺带修一处留痕回归**：`code` 节点调 `llm_call` 时不传 `node_ref`，`_record_attempt`
  直接返回，整批 `request_attempts` 里一条兜底记录都没有。改为 `execute_code` 对**声明了
  `node_ref` 参数**的函数注入留痕引用（其余函数签名与行为不变）。

## 2026-10-28 — 运行台新增「参考标准表」（只读）

逐字段口径此前只散在提示词正文 `rules/texts/spec.txt`、契约与编外登记表三处，排查
「某个字段为什么填错」要来回翻。运行台在「规则清单」之后新增一页，按**字段**组织，一格
一行答完「这个字段该怎么填」，覆盖 60 个回传与编外字段（`deal_level` 暂不纳入）。

- **是视图，不是真源**：名称、业务类型、定义、输出模式由 `notes_contract` /
  `internal_fields` 现读，页面不存副本，契约一变刷新即对齐；`GET /standards` 只读。
- **新增一列需要人判的「准入模式」**：事实提取（32 个）与受控推断（28 个）。业务方
  2026-10-28 逐字段确认，含改判的家庭结构、购买类型、付款方式、牌照指标、决策人五个字段。
  标准推理路径 / 禁止项 / 参考备注首版留空。
- **规则按字段归属分页**：`SPEC-02`…`SPEC-16`、`SPEC-18` 只在标准表显示；`SPEC-17`
  （五字段一条口径）与全局规则留在规则清单。`/rules` 仍返回全量，只多一个
  `in_reference_standards` 标记——规则没有退役，过滤是视图层的事。
- 准入模式尚未接入代码校验（`ADMIT-01`）：先跑影子模式看命中率，再决定动作。

## 2026-10-28 — 小订/大定不得当作拥车（UAT 19247201510）

原文「x700小订客户，今天试驾817做对比」被读成「客户已有车 x700」：L1-1a 给出
`old_car_model=x700`（锚点「x700小订客户」），L1-1c 又推出 `current_vehicle_info`
「有车（x700，车龄未知）」、`trade_in_model=x700`、`trade_in_requirement=yes`，
CONSIST-01 顺势把购车性质改成换购。一处误读污染五个字段。

**口径（业务方 2026-10-28 定稿）**：小订/大定是购车阶段的下单动作，不是拥车。小订车
写 `intended_model`、`viewing_progress` 记「深度考虑」；大订记「已经下定」。

**两通道落地**：
- 提示词：新增 `SPEC-17`（小订/大定不是拥车，挂拥车/置换/现车信息五个字段）与
  `SPEC-18`（小订/大定对应的看车进度）；`SPEC-11` 补一句「客户已小订的车型算在考虑买」。
  为此 `Rule.scope` 放宽为可挂多字段，但**正文只渲染一次**：其余适用字段以
  「本条口径同样适用于：…」一行带出。逐字段各渲一遍会让同一段文字重复五遍
  （初版实测提示词多出 920 字），长度快照 8327 → 8688。
  同一字段被两条 active 提示词规则占用时启动期自检报错——`field_notes` 是先到先得，
  不拦就会静默丢正文（初版 `SPEC-18` 的 `intended_model` 就这样丢过一次）。
- 代码：新增 `CONSIST-04`（`check_deposit_claims`），断言的值、引文或推断依据含
  「小订/小定/大订/大定/下订/下定/定金/意向金/锁单」却落到拥车/置换类字段时整条拒收，
  留痕记 `f_deposit`。`_projection_context` 同口径——这类断言不再充当旧车旁证，
  否则 CONSIST-01 仍会把购车性质带成换购。
- 词表为封闭表，新说法出现时再补（业务方确认）。

**小范围重跑验证（2026-10-28，6 案）**：取 `19247201510`（问题案）、`13782985566`
（大定）、`17897110986`（小订+锁单）、`15527381216`、`15334131738`、`17771471913`，
用归档原始响应回放取数层、模型真实调用，6/6 成功。

- **修复生效**：`19247201510` 的「有车（x700，车龄未知）」变为「有车（车型未提及）」，
  `trade_in_model` / `trade_in_requirement` 不再输出，L1 把 `x700` 判为对比车型。
- **无泄漏**：拥车与置换类字段的取值和证据位均无下单词，也无规则编号等内部串。
- **CONSIST-04 命中 1 次**（`13782985566` 的 `purchase_nature`），与对照组相比确为该规则拒收。
- **Notes 条数下降不归咎本次改动**：6 案中 4 案下降，但对照组（同一输入、关掉新规则）
  同向下降幅度相当（如 `19247201510` 两侧同为 38、`17897110986` 两侧同为 20）。
- **已知盲区**：`RULES_DISABLED` 只作用于投影段、gate 段与 checker，**不作用于提示词渲染**
  （`field_notes` 读 `default_registry()`，不看灰度开关）。因此对照组只隔离了 CONSIST-04，
  SPEC-17/18 在两组提示词里都存在，提示词通道的净影响未能单独测出。

## 2026-10-28 — thin 档 Notes 整批清空的修复与口径放宽（P2）

UAT 两批次 87 案中 35 案（37%）的 `basic_notes_updates` 只剩 `customer_name` 一条。
核查确认**不是信息缺失门禁所致**：这 35 案在 `pipeline_l2_review` 的
`placeholder_removed` / `enum_illegal` / `issue_count` 全为 0，门禁前后条数一致。

**根因**：`_registry_only` 的判据是在 `reasoning_detail.evidence` 字符串里找档位标签
「人工/系统登记」，而证据位自 2026-10-19 可读性整改起只写「渠道：原文」。全量 1234 条
Notes 命中数为 0，thin 档被整批清空；`_registry_only_candidates` 同源失效，使 thin 案
`candidates_json` 全为 `[]`，L2 兜底无从裁决。

**修复与口径**（`customer_profile_pipeline.py`）：
- 判据改为按渠道档位（`proj.CHANNEL_TIER`）与字段置信档（`result.basis`）判定，不再读证据文本。
- thin 档保留三类字段（P2 口径）：有登记档渠道支撑（不论置信档）、第三方画像数据的高/事实档、
  无渠道但非低置信；其余不留。候选集同步收窄。
- 35 案离线回放（归档 claims 直喂 `project_claims(tier="thin")`）：每案 Notes 由 1.0 条
  升至 10.1 条，仅 3 案仍只剩姓名（其中 2 案 L1 本就未提取到任何字段）。
- 测试：`test_project_claims_thin_tier_keeps_registry_only` 重写为 P2 口径；
  新增 `test_thin_keep_predicate_covers_six_arms` 与空渠道转 `to_confirm` 的用例。

**已知边界**：空渠道断言在投影层即被判社交档转 `to_confirm`，实际够不着 thin 过滤器的
「无渠道高置信」保留臂；该路径的产出能否到销售侧取决于回传契约，已登记为 L-017。

## 2026-10-27 — experiments 定位改为功能验证与问题回归

`experiments/` 从「AB 实验专属目录」改为**功能验证与问题回归**场地：存放从 UAT 同步回
本地的真实运行记录，用于功能开发、测试验证与问题定位。本地跑批仍落 `batches/`。

- 新增 `experiments/uat_data/`：UAT 运行数据统一入口。
  - `uat-all-tasks-20261026-20261027/`：由运行台全量任务导出
    （`runtime-all-tasks-last24h-20261027-092607.zip`）整理入库，**69 根运行 / 57 号码 /
    1316 次运行 / 16324 节点 / 1365 次出网尝试**，批内 `trace.db` 完整性 ok。
    转换脚本 `scripts/ingest_uat_export.py`，可对任何同格式导出复跑。
  - `uat-18-20261026/`：原 `batches/uat-18-20261026` 整目录迁入。
  - `phones.txt`：两批次号码总清单，72 个唯一号码，仅 `18062597750` 跨批次重合。
- 过期批次清理（已另行手工归档）：`baseline-39-dsv41-noThink`、`pipeline-v3.3-39`、
  `rules-opt01-baseline-8-20261026`、`english-residue-*`、`uat-18-*-replay/rerun/legacykeys`、
  `_wirecheck`，以及失效的 `income_band/`、`smoke/` 临时分析目录；`experiments/` 由约
  1.5 GB 降到约 460 MB。
- 脚本默认批次改为 `experiments/uat_data/`：`replay_from_archive.py`（缺省源改为
  `uat-all-tasks-20261026-20261027`）、`rules_report.py`、`compare_batches.py`、
  `scan_english_residue.py`、`scan_legacy_enum_keys.py`、两个 `probe_*` 与
  `snapshot_note_contract.py` 的缺省批次、`tests/rules/test_checker.py` 的基线路径。
- 文档明确一条长期约束：**本地环境访问不到 UAT 取数接口**。使用 UAT 数据时不走 API 取数，
  直接用归档保存的客户全量原始响应（`runs/<号码>/attempts.json`）作为输入往下跑。

## 2026-10-27 — 旧版枚举键清理与生产臂退役

前端在测试用例 `13733451677` 上看到「购买类型 = `replacement`」「用车场景 = `commute，家用，
越野`」。核查结论：是枚举键，但都不是现行契约的合法键——`replacement` 属于已在 v2 迁走的
`purchase_nature`，`commute` / `transport` 已从 `purchase_purpose` 的新三选项中移除。

**旧键并非只存在于历史档案**，工作流自身仍会生产并放行它们，链条是：L1 照旧清单输出旧键
→ 归一失败但「保留原值 + 降级」→ `HARD-02` 对已降级条目跳过不拦 → 回写投影只校验字段名
不校验取值 → 旧键原样回传。

- 新增别名表 `internal_fields.LEGACY_ENUM_ALIASES`（`married`/`unmarried` →
  `*_no_child`、`commute`/`transport` → `family`、`easy-to-drive` → `easy_to_drive`），
  归一层在「精确匹配失败后、返回之前」查它。`within_6_months` **刻意不映射**：
  与 3 个月档区间重叠但不等价，硬映射等于伪造精度。
- `purchase_type` 的旧值 `first`/`additional`/`replacement` 走**跨字段路由**搬到
  `purchase_nature`——它们是换字段而非改键，该信息仍参与购车决策与成交攻防推理。
- 新增 `ENUM-01`（降级后仍是废弃内部键 → 整条剔除）与回写投影的取值校验（`ENUM-01`
  的最后一道闸，单条剔除、其余照发）。两者判据都靠「像不像枚举键」，客户原话
  （`说不清`）不受影响。
- `customer_profile_production` **整臂退役**：从注册表摘除、删除模块与 13 份私有模板；
  它被 L3 复用的 6 份业务资产改名 `customer_profile_shared__*`，正文逐字未动。
  `customer_profile_entry` 的末端子流程改指 `customer_profile_pipeline`。
- 回写节点补一道形状校验：`@body_ref` 只保证「是合法 JSON」，`[]` / `"string"` 也能过关，
  回写节点现在要求请求体必须是对象，避免发一个必被 4xx 的载荷。
- **验证**：uat-18 全量 16 案离线重放（取数层回放、模型真实调用），**16/16 succeeded、
  回写 0 次**。七个废弃键字面量为 **0**，枚举字段 170 条取值 **0 非法**；商机等级
  **15/16** 与对照臂一致（唯一变化 `15327825930` 归因 L1 采样，规则层未做任何动作）；
  Notes 减少的条目全部是占位串与非法键，无一条合法取值被删。新增
  `scripts/scan_legacy_enum_keys.py` 做此验收。详见
  `docs/history/旧版枚举键与production臂整改方案.md` §10。

## 2026-10-27 — 对客产出英文残留治理：正文枚举键 13 处归零

回传体的正文（`summary` / `steps` / `label`）里混进内部枚举键，销售看到的是
「已定稿事实：power、appearance」「牌照条件：unknown」这种句子。uat-18 快照扫描出
**7 案 13 处**，全部落在代码拼装路径上，没有一处需要模型判断。

- 新增 `EXPR-01`（正文枚举键按契约展示名改写）与 `EXPR-03`（英文占位串整条去除），
  前者只动 `summary` / `steps`，不动 `attribute_value` 与 `evidence`。
- 判据是「是不是该字段的合法枚举键」，不靠英文正则——品牌名、车型名、`to B` 一类
  因此天然不误伤，也不需要维护例外词表。
- 上游同时修 `callback_build._label` 的多值与列表取值路径、`readable._enum_display`
  的占位串处理：规则是兜底，根因修复让规则大多数时候无事可做。
- 新增 `scripts/scan_english_residue.py`：把结果分成「可改枚举键 / 白名单 / 待人工」
  三类，退出码可直接用于验收。
- 离线重跑 9 案（取数层回放、模型真实）：**9/9 succeeded、写回 0 次**，
  可改枚举键 **10→0**（对照臂为同谱系旧代码重放），商机等级 9/9 与对照一致。
- 两处偏差已归因并留档：`18271918797` 的 Notes 从 21 降到 9，差异在进入审查层之前，
  归因 L1 采样；零星中文枚举值（`首购` / `单身`）在对照臂同样存在，属既有形态。
- 新增遗留问题 L-015：`attribute_value` 继续返回英文键（契约设计），若前端未按
  `display_name` 渲染，需在前端解决。

设计与实测见 `docs/history/对客产出英文残留治理方案.md`。

## 2026-10-26 — 规则层离线重跑验证：未造成劣化

在**同一份输入**上把「新代码 vs 未改代码」比出来了。此前在线重跑之所以看起来像劣化，
是因为数据接口当天已经变了（`history` 全部返回 `[]`、`profile` 换了结构），两批输入
不同、比出来的差异没有意义。

- 新增 `scripts/replay_from_archive.py`：从归档批次 `runs/<号码>/attempts.json` 重建
  取数层固定响应与转写缓存，只回放 HTTP、模型真实调用。三条口径写在模块说明里——
  AUC 的两条路径不含号码，不能走回放帧，必须用转写缓存；图片从归档 LLM 请求的
  data URI 还原原始字节。
- 结果是**取数层回放、模型层真实**的 16 案批次，外加一个跑在 `2f5eab8` 独立 worktree 上的
  未改代码对照臂。两批 `fixture.json` 逐字节相同，只剩代码一个变量。
- 结论：成功率、失败节点、占位值三批一致；Notes 总量 327（新）/ 328（对照）/ 324（归档）；
  商机等级 14/16 与对照一致，两处差异归因于 L1 采样（`temperature=0.5`）；
  唯一的硬告警（`17652815077` 掉 12 条）由该案归档里本就失败的两条录音识别引起，
  对照臂反而更少。
- 规则层第一次在真实数据上跑通：141 次命中，`checker_status` 16/16 为 `disabled`
  （开关缺省关闭），降级确实改写了档位（`driver_license_status` 从 `fact` 变
  `inferred_low`），HARD-01 的 24 次拒收把「未知」占位串挡在最终 Notes 之外。
- 报告：`docs/history/rules-opt01/离线重跑验证报告.md`。L-009 的前两条解除条件关闭，
  只剩「真实取数的新批次」这一半。

## 2026-10-26 — 运行台新增「规则清单」页

规则层落地后，规则散在登记表、`rules/texts/*.txt` 正文和代码落点三处，要回答「现在配了
多少条规则、每条具体是什么」只能翻代码。这一轮把它变成运行台上的一个只读页面。

- 后端新增 `GET /rules`：从登记表逐条导出，另补三项展示用派生值——`cluster_title`
  （簇的中文名）、`text`（prompt 通道的正文原话）、`landings`（落点文件与行号）。
  该端点**不依赖服务实例**，接 `get_service()` 之前即可访问，控制台尚未装配好时也能查规则。
- 前端新增「规则清单」标签页（`RuleCatalog.tsx`）：顶部先给规模（共几条 / 生效几条 /
  各通道几条），再是簇 / 通道 / 关键词筛选与表格，行内展开看详情。
- 详情含一句话口径、对客说法、作用字段、内容类、归属与版本、旧编码、落点、验证用例，
  以及提示词正文。**落点为空时红字提示**——登记了却没落地是配置错误，应在页面上可见，
  而不是等守卫在测试里报红。
- 筛选可选值与计数全部取自接口返回，前端不维护规则枚举；新增规则或簇时页面自动跟上。
- 新增 `tests/test_api.py` 两条用例：`/rules` 与登记表逐条对应且不重复、prompt 通道
  规则必须带正文；checker 通道规则必须报出落点。

## 2026-10-26 — 规则层收口（M7）

规则优化工作线走完 M0–M7：规则从提示词、代码、审查层三处的散句，变成有编号、有通道、
有留痕、可单条关闭的一等公民。

- 新增 `customer_profile.rules` 包：登记表（启动期自检）、动作执行器（排序 + 收敛 +
  留痕）、prompt 渲染、落点守卫、checker 通道、七个规则簇。
- 三条通道各自归位：能写成表格判据的走 code（0 token），影响「往哪找」的走 prompt，
  需要读懂意思的走 llm_checker。默认不建开放知识表。
- 投影层与审查层的现有判据全部迁入，`review_notes` 与 `validate_l3` 的重复实现合并为一份。
- 新增两个开关：`RULES_DISABLED`（单条灰度/回滚）、`RULE_CHECKER_ENABLED`（缺省关闭）。
- 新增三个脚本：`rules_index.py`（按编号查落点）、`probe_rules_diff.py`（投影对账）、
  `rules_report.py`（命中统计与批次对账）。
- 现行设计要点并入 `docs/design/客户人设画像管线设计.md` §3.8；工作区归档到
  `docs/history/rules-opt01/`。
- 未决事项登记为 `docs/遗留问题清单.md` 的 L-009 … L-014，其中首要一条是
  **规则层尚未在真实新批次上验证过命中**。

## 2026-10-26 — 规则命中统计与批次对账（M6）

规则登记了、落点了、也执行了，但「哪条规则真的触发过」此前无处回答。新增
`scripts/rules_report.py`，从批次产物出「规则 × 命中次数 × 动作分布」表。

- 两种产物形态都认：新批次读 `rule_trace_json`；旧批次读 `rejected_json` /
  `issue_json` 的旧裸串，经 `legacy_codes` 归一到新编号。归一不了的单独列出，
  不并进统计——否则命中数会被算高。
- 零命中告警只统计**含规则层产物**的批次。归档的四个批次都早于规则层，算进去会让
  每条规则都误报。prompt 通道的规则没有判据，单独标注、不参与告警。
- 同源批次按 `source_snapshot_sha256` 去重：UAT 快照与它的 8 案子集是同一次运行，
  算两批会让零命中连击虚增。
- 逐案对照出 Notes 条数与非空数、商机等级，以及占位删除 / 枚举非法 / 无锚点 fact
  三项计数（README §3 的口径）。
- 数据源是批次产物而非 `trace.db`：留痕写在节点输出里，`trace.db` 没有规则维度。

## 2026-10-26 — checker 通道上线（M5）

「职业与收入量级不自洽」这类判断写不成规则表，放进提示词又没有任何东西执行它。这一轮
给规则层加第三条通道：模型做判断，代码决定这个判断能不能用。

- `INCOME-01` 注册进 income 簇，`llm_checker` 通道、默认动作 `downgrade`。
- 管线新增 `RULE_CHECK` / `RULE_APPLY` 两个节点，插在 `L2_REVIEW` 与 `LEVEL_INPUT`
  之间；表达层与回传体改读 `RULE_APPLY` 的 Notes 与置信档，降级才能到产物。
- `RULE_CHECK` 用 **code 节点**承载而非 llm 节点：调度器会把失败节点的整棵下游子树
  标为阻塞，做成 llm 节点则一次调用失败就挡掉定级与回传。现在节点内部调模型并吞掉
  异常，失败只记 `checker_status=failed`，管线照常往下走。
- 送检清单只给中文字段名与当前取值；字段不足两个的规则不送检。代码复核四条：编号须在
  清单内、字段须在作用域内、动作须在白名单、不得携带新值——丢弃逐条留痕。
- `RULE_CHECKER_ENABLED` 缺省 `false`，关闭时在发起请求之前返回，零 token。
- 新增 `scripts/probe_checker.py`：离线组装送检清单与提示词规模，`--live` 才真调模型。

## 2026-10-26 — 取证方向进登记表（M4 prompt 通道）

L1《目标字段清单》里那 14 句「取证范围」，原先是一个字典常量（`FIELD_SOURCE_NOTES`）——
不能单独关闭、留痕里指不出出处、也没人验证它是否还生效。这一轮把它们升为 SPEC 簇规则。

- 新增簇 `spec`：`SPEC-02`…`SPEC-15`，一个字段一条，`prompt` 通道、`extract` 阶段。
  正文落在 `rules/texts/spec.txt`，成对标记包裹；`SPEC-01` 空着留给 M5 的 checker 通道。
- 新增 `rules.render.field_notes()`：按字段取正文，供《目标字段清单》贴着字段逐行渲染。
- `field_inference_guide()` 改读规则正文，`FIELD_SOURCE_NOTES` 常量已删——不留第二份。
- 迁移验收：`l1_guide()` 迁移前后**逐字一致**（8240 字符，sha256 相同）。动提示词属
  业务优化，本轮只换存放方式，一个字都没改。
- L1 模板正文里的硬性措辞本轮未动，须单独确认后再迁。

## 2026-10-26 — 审查层从「能判」到「能拦」（M3）

`review_notes` 此前判出违规只记一条 issue，条目照样进最终回传体——销售看到的仍是带占位串、
无依据确定值的画像。这一轮把它接到统一执行器上，判出即改产物。

- 四条 gate 规则（`HARD-01/02/03`、`EXPR-02`）由簇函数产出 `Action`，执行器落地：
  占位串整条删除、非法枚举与无锚点 fact 降置信到 0.5、证据位自指只摘那一条证据。
  `issue_json` 按旧编码保留（跨批次对账要用），另出 `rule_trace_json`。
- `Action` 新增 `target`（`field` / `note` / `evidence`）：同一个 `reject` 在三种载体上
  含义不同，显式声明而不是靠 payload 猜。
- `validate_l3` 的无锚点判据与登记档渠道表改为引用 `rules.clusters.hard` 的同一份实现，
  `review_notes` 与 `validate_l3` 的重复判断合并。
- `L2_REVIEW` 新增输出 `basis_json` / `rule_trace_json`，L3 的 `basis_json` 改从这里取——
  原先取 L2 投影层，审查层的降级对表达层不可见。
- 补上 `RULES_DISABLED` 的运行时读取：此前只有设置项定义、无任何读取方，关闭一条规则
  实际无效。投影段与 gate 段共用同一处读取。

## 2026-10-26 — 规则层落地：登记表、三通道骨架与投影层迁移

规则从散落在提示词、代码、审查层三处的句子，升为有编号、有落点、有留痕的一等公民
（`docs/history/rules-opt01/`，M0–M2 已完成）。

- 新增 `customer_profile.rules` 包：`Rule` 登记表（启动期自检：编号唯一、`active` 必有
  正反例、旧裸串不得共用）、`Action` 统一执行器（内容类 → 作用域 → 编号稳定排序，
  最多 3 轮收敛）、`RuleTrace` 留痕、prompt 通道正文切段、两道检索守卫。
- 六个簇、16 条 `active` 规则；现有三处代码落点全部打标（`# rule: <ID>`），
  `scripts/rules_index.py` 可按编号或簇列出落点。
- 投影层 a / b / e / f 段与等级唯一化的**判定逻辑迁入规则簇**，投影层只保留装配职责。
  迁移前后对基线 8 案做离线对账（`scripts/probe_rules_diff.py`），`facts` / `basis` /
  Notes 条数 / 确认条数**逐案零差异**。
- `project_claims` 新增输出 `rule_trace_json`；新增设置项 `RULES_DISABLED`
  （单条规则灰度）与 `RULE_CHECKER_ENABLED`（checker 通道总开关，缺省关闭）。
- 保留迁移期兼容：`rejected` / `downgraded` 的 `rule` 字段仍写旧裸串
  （`f_trade_in` 这类），新编号记在 `rule_trace`；二者的映射由登记表 `legacy_codes` 维护。

## 2026-10-22 — 收敛重复的取值与围栏工具

审查发现同一套小工具在多个模块各写一份，且**同名不同义**——改一处以为全套都改了。

- 新增 `text_utils.py`，按语义给出 `as_payload` / `as_text` / `as_display_text` /
  `as_flat_list` / `as_json_list` / `strip_code_fence`。`projection`、`callback_build`、
  `customer_profile_l3`、`executors_iteration` 改为引用，调用点不动。
  三份 `_as_text` 与三份 `_as_list` 并不等价（布尔、数字、`||`、JSON 串各认各的），
  因此没有合并成一个函数，而是按语义拆开命名。
- 六处 `_strip_code_fence`（`shared_code` / `llm` / `production` / `chat_history_data` /
  `human_corrected_info` / `user_feedback_data` 两份）收敛到一份实现，取各版并集：
  非字符串透传、语言标签通配。中段围栏仍由 `evidence_subagent._extract_fenced_block`
  处理，不在此列。
- 新增 `tests/test_text_utils.py` 固定各语义档的差异，防止以后被「顺手统一」改掉。

## 2026-10-22 — 修审查判据恒真与 resistances 双写

- `review_notes` 的无锚点 fact 判据原先写成 `any("：" in e or e for e in evidence)`，
  `or e` 让任何非空证据串都成立，且算出的 `has_quote` 从未被使用；改按证据串里的
  原文标记（`渠道：原文` / `原文「…」` / 标签引号）判定，与 `validate_l3` 同口径。
  证据位只写推断依据、又无登记渠道的 fact 现在会被记成 `unbacked_fact`。
- 删掉 `assemble_l3` 把 `resistances` 补挂进 Notes 的那段（v3.3 带入）。该键已不在
  回传契约内，回传体由 `callback_build` 从表达层直接生成 `deal_battle.loss_conditions`；
  双写让 37/39 案的 Notes 出现两条同名条目，且补挂的那条根本不经白名单。

## 2026-10-21 — 部署分支同步 dev 业务画像更新

`codex/feature-docker-compose` 合入 `dev`（`082f700`）的录音角色标注、姓名兜底、
事实可读化、收入标签修正与运行快照导出。画像生成沿用 dev 的工作流、提示词与业务规则，
保留部署分支的 Compose、M2M 回写适配、中文日志与执行器修复。
业务文档后经整理迁入 `docs/reference/`（契约材料）与 `docs/history/`（归档），部署与接口说明的相关引用已同步。

## 2026-10-20 — 收入字段只认收入标签

第三方画像里「预测车型价格偏好」「预测手机价格」与「预测收入水平」形态相似，
模型会把车价区间当成收入写进 `annual_income`（实测 13570263031 写成「中低端
（8w-15w）」，而该案 `4档收入` 应为 30–40 万）。修法两条：

- 投影层新增守卫 `e_non_income_tag`：预测档的收入断言若带标签痕迹却没有「收入」
  字样，整条拒收；全被拒时留白转人工，不拿车价区间冒充收入。无标签痕迹的推断
  金额（据职业等推出）照常放行。
- L1 模板补规则：标签只能填它自己描述的字段，收入字段只认「预测收入水平」。

## 2026-10-20 — 收入档位换算下沉到取数节点

承接同日上一条：档位→金额的换算从投影层前移到极光子流程的取数节点
`extract_jiguang_tags`（`income_band.normalize_income_tags`），第三方标签一取到就是
「20-30万」这样的区间，下游 L0/L1/L2/L3 与卡片、Notes 都不再看见「档位」；
`projection.normalize_income` 只留作离线回放旧产物与外部构造档位断言的兜底。
修的是 15818891947 的线上缺陷：L1 提示词以前只给了「1档 8-15万 … 6档 60万以上」
两个端点，模型就把标签原文 `3档收入` 写成了 8-15万，而投影层因取值已不是档位形态
原样放行。提示词同步改为「收入标签已是区间，如实写入，不得自行换算」。

## 2026-10-20 — 第三方收入档位换算成金额区间

第三方画像只给 `N档收入`（1–6 档），销售需要具体金额。业务专家确认换算口径
（1档 8-15万、2档 10-20万、3档 20-30万、4档 30-40万、5档 40-60万、6档 60万以上），
落地为 `projection.INCOME_BAND_RANGES` / `normalize_income`：档位在投影层写入
`facts` 时就换算，Notes 回写与 L2 兜底出口经 `serialize_value` 再收口一层；
客户自述或模型推断的金额原样透传。依据分析见 `docs/history/收入档位映射分析.md`。

## 2026-10-20 — AB 实验文档随批次移入 experiments/docs

`AB实验.md` 讲的是两臂跑批对照，与部署运行环境的日常操作无关，已从 `docs/runbooks/`
移到 `experiments/docs/`。`experiments/*` 仍整体忽略（批次产物约 600 MB/批），只对
`experiments/docs/` 开例外——用 `experiments/*` + `!experiments/docs/` 而不是
`experiments/`，因为父目录被排除后内部文件无法再被 `!` 纳入。

`runbooks/` 因此只剩部署、故障定位、基线实验、内网取数四类可重复执行的操作；
`experiments/README.md`、两份设计文档、`tests/test_pipeline_l1.py` 与归档文档中的引用同步改路径。

## 2026-10-20 — runbooks 只保留可执行的操作知识

`docs/runbooks/` 混进了两份记录性质的内容：《跑批可靠性》（缺陷分析 + 改进清单）与
《v3.3-可靠性改进》（实施记录 + 验证清单）。它们回答的是「过去发生了什么」，不是
「现在该怎么做」，已移入 `docs/history/`（`docs/history/跑批可靠性-分析与改进清单.md`、
`docs/history/v3.3-可靠性改进-实施记录.md`），文内相对链接同步修正。新增 `runbooks/README.md`
写明收录标准，避免同类内容再次回流。

## 2026-10-20 — 内网取数：留痕库按需快照

UAT 部署在内网，运行台不可达。新增 `scripts/export_snapshot.py`，在 UAT 侧按时间窗与
号码筛出根运行及其整棵子运行树，连同定义快照拷成自包含 `.db` 与 `manifest.json`；本地把
`DATABASE_URL` 指向快照即可用运行台完整复现，恢复侧零代码。导出在一次读事务内完成，
不停服、不阻塞写入；筛选时间窗含子运行（子运行不按时间窗单独判）。操作步骤见
`runbooks/内网取数与本地复现.md`。

## 2026-10-20 — 喂给模型的事实统一可读化（禁内部字段键与英文取值）

测试环境核读发现商机等级的证据里出现「已定稿事实显示牌照状态为needs_license、付款方式为loan」
「planned_purchase_time为within_3_months」。根因不是某条模板写错，而是 `facts` 是机器形态
（英文键 + 英文枚举键），凡原样 `json.dumps` 喂给 LLM 的装配点都会漏。

- **新增 `customer_profile/readable.py`**：把事实渲染成逐行「中文字段名：中文取值」。
  覆盖单值 / `||` 多选 / 结构化项（`key` + `input_value`）三种形态，嵌套结构（定级详情）
  递归翻键；查不到中文名的键保留原键，不静默丢内容。
- **三处装配点改接可读化事实**：商机定级输入、L2 兜底的第二个输入（新增 `facts_readable`
  输出）、L3 表达层的事实输入。给代码消费的 `facts_json` 一律不动——回传、同源核对
  仍用机器形态。
- **L1-1c 分析投影层刻意不改**：它输出的就是枚举键，喂中文名会诱发模型输出中文值。
- **提示词同步**：定级模板禁止证据与推理链出现英文字段名、英文取值与 `key=value` 写法；
  L2 兜底禁止 `reason` 写英文字段名；L3 模板里的英文字段键引用改成中文名。
- **防复发**：`tests/test_readable.py` 扫三处装配产物，出现 `snake_case` 形态的内部键即失败。
  新增进 `facts` 的内部键记得在 `readable.FALLBACK_FIELD_NAMES` 登记中文名。

## 2026-10-20 — 姓名兜底改用手机号后四位

`customer_name` 查不到可靠来源时不再直接输出「未知」，改为「客户」+手机号后四位
（如「客户0087」），方便销售在卡片上对上人；手机号缺失时才退回「未知」。

- **兜底点不变**：仍在投影层一次性落定，L2 兜底与审查层不经手姓名字段；置信度与
  Notes 说明（「客户信息中未查找到真实姓名」）对兜底值同口径，恒 1.0。
- 提示词姓名规则同步措辞（「下游会统一写未知」→「客户+手机号后四位」）。
- 口径见 `docs/design/客户人设画像管线设计.md` §3.5。

## 2026-10-20 — 录音说话方语义标注（销售误识别为客户）

实测发现外呼录音里销售的一句「我们是4S店的」被归到客户方，导致整条画像把客户职业判成
汽车销售。根因是 AUC 的 `speaker` 编号与逐句 `gender` 在本批录音中不可信（55 条有效录音中
41 条存在同一编号下性别矛盾，说话人连续段 78% 长度为 1）。

- **新增角色标注变体** `audio_content_extract_roled`：在纯识别链路之上多一次 LLM 调用，
  按语义把转写切成连续角色分段（`销售方` / `客户方` / `未知来源`），代码展开成逐句角色。
  管线录音抽取改指向该变体，回退只需改一个常量。
- **错误不落到客户方**：分段缺口、非法角色、解析失败一律记 `未知来源`；判成客户方却命中
  销售自述句式（「我们店」「给您报」等）的句子单向改判 `未知来源`。
- **证据位标注说话方**：录音证据在 Notes 里写出「销售原话 / 客户原话 / 说话者身份存疑，
  信息可靠度低」，非录音证据不加前缀。三种角色都进分析层，`未知来源` 不作强证据。
- **勘误与原文分离**：正文用勘误后写法，原识别文本单独留存在 `content_raw`，分节头列出
  说话方分区与勘误句号。
- 口径见 `docs/design/客户人设画像管线设计.md` §3.1 / §3.2；离线验证脚本
  `scripts/probe_role_labelling.py`。
- **验证**：40 条真实转写离线跑批（21 试驾 / 18 外呼，覆盖短中长三层）零校验问题；
  18 条外呼全部产出销售/客户切分，客户说「埃安那个4S店」未被误判为销售。
  试驾中的店员闲聊、私人电话、语音助手类录音整篇判 `未知来源`，符合预期。
  已知残余：多段对话混录的长试驾文件（262–431 句）里的零星销售洽谈会随整篇一起判成
  `未知来源`——代价是少用一条证据，不会把销售话记成客户话。

## 2026-10-20 — 商机等级可读性整改（证据可读化 + 推理链精简）

测试环境逐案核读（手机号 `15308826588`）发现：等级结论本身正确，但「信源原文匹配」
全是 ASR 半句对白（如「家用啊，没有，真够啊。」），推理链 16 条且末尾混入跟进建议。

- **证据改为可读化陈述**：提示词第五节由「逐字原文」改为「出处 + 信号 + 指向」，
  三个来源三类写法——原始记录讲客户说了什么、说明什么；L1 已定稿事实与推断按置信度
  标注「经分析该客户大概率…」或「该客户有可能…」；Notes 标签与画像卡结论写明出处。
  三处证据（三个维度 + `homework_evidence`）一起改；明确不得把坐席的话当客户证据。
- **撤除原文定位过滤**：`apply_level` 不再逐字核对，改由 `_level_evidence_readable`
  剔除不足 12 字的碎片，留痕字段改为 `evidence_fragments_removed`；置信度不再因
  定位失败扣分。
- **推理链精简到 5–8 条**：提示词要求只写「等级怎么定出来的」，删除 `obstacles` 与
  `next_action` 两个输出项，代码不再拼「关键障碍」「下一步建议」两条；超过 8 条截断
  （`LEVEL_STEP_LIMIT`）。
- 设计口径见 `docs/design/客户人设画像管线设计.md` §3.6。全量测试通过。

## 2026-10-20 — 商机等级原样回传（L-008 关闭）

前端确认 `deal_level` 是文本字段而非 ABCD 纯枚举，可直接承接定级规范的五个取值。

- **撤除过渡映射**：`callback_build.contract_level()` 删除，`build_ai_insight` 按归一后的
  `H` / `A` / `B` / `C` / 待识别 原样回传（此前 `H`→`A`、「待识别」不回传、旧 `D`→`C`）。
- **删除配置项** `PURCHASE_INTENT_LEVEL_PASSTHROUGH`（`settings.py` 与 `.env.example`）。
- **推理链不再补展示口径说明**：等级与依据不再存在「值 A、依据写 H」的矛盾，补丁句随之撤除。
- `遗留问题清单.md` L-008 改为**已解决待验证**：待真实批次确认前端筛选与后台落库值。

## 2026-10-19 — 业务优化与 Compose 部署整合

将业务优化工作线合入部署工作线：主入口改为 `customer_profile_pipeline`，采用 v2 回传体，
新增鉴权接单进程，同时保留统一中文日志、独立 worker 与优雅停机。

- 部署统一为 `compose.yaml` 的 API / worker / inbound 三个服务，移除重复配置。
- 沿用 `customer-profile` 项目名、`profile-data` 数据卷与 UID/GID；运行台缺省端口 18173，
  兼容 `CP_API_PORT` 宿主机映射，对外接口缺省 8099。
- 旧入口不再接收任务，历史记录与定义快照保留；`outputs.result1` 仍为 JSON 字符串，
  内部结构改为 v2 回传契约。旧调用方升级与部署验收见 `runbooks/部署.md`。

验证：后端 736 passed / 81 既有 skipped，前端构建与源码 lint 通过（5 条既有警告）；
镜像构建与独立 Compose 项目验收通过，覆盖旧库升级、独立 worker、鉴权接单、重启状态
与三进程日志。验收使用临时数据卷、固定响应或模拟回写，未触发真实业务回写。
## 2026-10-19 — 姓名与置信度口径修正（v3.5）

分支 `dev-business-opt01`。测试环境实测发现：手机号 `15308826588` 的客户姓名被写成
「先生」；大量推理链里读起来是事实的 Notes 字段置信度只有 40–50%，分数无法排序。

- **姓名口径**：`customer_name` 查不到一手自述或登记姓名时固定输出「未知」，Notes
  写明「客户信息中未查找到真实姓名」（置信度 1.0）。称呼（先生/女士/大哥）、昵称、
  企业名一律不得当姓名；L2 兜底既不补姓名，也不在候选集中出现。thin/empty 档同样保留
  这条姓名交代。
- **置信度口径**：出口取值改为透传 L1 自报分并按三档校准——铁的事实（手机号、截图中
  可读的微信号、登记档来源）1.0；事实与高可信结论 0.7–0.9，多来源同向 0.95；
  低置信推断 0.4–0.6，纯猜测 0.2–0.3；L2 软补全 0.25。
- **豁免**：`wechat_id` 不再因「仅社交自呈现」被强制降级（主页截图是其唯一现实来源）。
- **提示词**：L1 两层补充 `confidence` 三档打分规则与「姓名查不到不写」；
  L2 兜底声明姓名字段不在其职责内。
- 设计口径见 `docs/design/客户人设画像管线设计.md` §3.5。全量测试通过。

## 2026-10-19 — 证据链与推理链可读性整改（v3.4）

分支 `dev-business-opt01`。测试环境逐案核读发现：部分字段「信源原文匹配」
只写渠道名、「逻辑推导链」写「来自某渠道」；「行业」的信源与结论看起来毫不相干。

- **渠道拆分**：`手机号归属地信息` 改接 `phonenumber_analysis_result`，
  `关联企业信息` 继续接 `company_analysis_result`。此前两条渠道接的是同一份企业注册
  信息，「珠宝首饰零售」因此挂在归属地名下。
- **定位容错**：`_locate` / `_occurrences` 增加忽略 Markdown 标记的匹配（`**国标行业**`
  与 `国标行业`），切片仍按原文下标还原。锚点失败时用字段取值在**同一渠道**回查补证据，
  置信档仍按原规则降级。
- **对客渲染**：证据位不再输出裸渠道名或「来自{渠道}」；第三方画像标签输出
  「第三方画像标签“预测年龄段”给出“50岁及以上”」；注入字段输出具体取值
  （「客户留资手机号：138****0000」）。
- **L2 兜底**：补出来的字段把理由里的事实部分填进 `evidence`，不再固定为空。
- **提示词**：L1 两层要求锚点为原文连续片段（禁字段名、记录编号、Markdown 标记）；
  第三方标签的 `basis_of_inference` 必须带标签取值；推导链「能一句说清就一句」，
  复杂推理才分步。
- 方案与实测数据见 `docs/history/证据链与推理链可读性优化方案.md`。全量测试通过。

## 2026-10-19 — 回传输出侧三项修正（意向等级 / 空分类填充 / result1 接线）

分支 `dev-business-opt01`。测试环境实测发现：画像「购车意向等级」恒为空；
「换车动因」「对比车型」经常整块缺失；运行台「结果」页展示的仍是九板块旧结构。

- **意向等级**：`deal_level` 此前不在 L1《目标字段清单》里（既非契约字段，也不在编外
  登记表），facts 恒无此键，`ai_insight.purchase_intent_level` 因此从未产出。现登记为
  **画像卡字段**（`internal_fields.CARD_ONLY_KEYS`，A/B/C/D 枚举），取证照常、仍不进
  Notes 白名单。
- **空分类填充**：「换车动因」「对比车型」缺料时各给一条「暂无换车动因」/「暂无对比
  车型」填充项（置信度 0.3），不再整块缺席；其余四类缺料仍按原语义整个不出现。
- **旧车证据**：`old_car_model="无"` 这类占位取值不再被当作旧车证据，首购客户不会被
  改写为换购。
- **`result1` 接线**：业务测试臂 `END` 改挂 `callback_build`，运行台展示 v2 回传体；
  L3 九板块旧结构仍留在 `pipeline_production` 出参里备查。

## 2026-10-19 — 主入口未传 batch_id 时补 batch_001

分支 `dev-business-opt01`。测试环境实跑发现：照 README 的 curl 示例只传 `phone_number`
提交，运行在 `pipeline_production` 节点以
`KeyError: 节点 pipeline_start 未产出字段 batch_id` 失败（40 节点中 36 成功、3 阻断），
而**提交本身返回 200**——调用方看不出问题。

- **根因**：`start` 节点把 `batch_id` 声明为可选（`required_batch_id=False`），但
  `execute_start` 只输出「声明了且调用方传了」的字段，不传时该键根本不出现；下游
  `pipeline_production` 取 `(START, "batch_id")` 直接 KeyError。定义里的缺省值
  （`default_inputs()` 返回空串）在 `/runs` 提交路径上从未被套用。
- **修法（最小）**：提交入口 `POST /runs` 对主入口补缺省值——未传或传空白时写入
  `batch_id = "batch_001"`（`api.DEFAULT_BATCH_ID`）。显式传值原样保留。
  不在 `execute_start` 里改：那里语义是「原样暴露调用方入参」，给所有工作流的可选入参
  凭空补值会改变其它工作流的既有行为。
- **影响面**：前端提交页本就总是发 `batch_id`（空也发空串），行为不变，只是空串现在会被
  补成 `batch_001` 而不是空串；对外任务接口（8099）要求 `batch_id` 必填，不受影响。
- **测试**：`tests/test_api.py` 新增三条——不传补缺省、空白串补缺省、显式值保留。
- 验证：测试环境带 `batch_id` 提交实测 `succeeded`（42 节点全成功，442 s）。

## 2026-10-16 — 运行台端口由写死 8000 改为可配，缺省 18173

分支 `dev-business-opt01`。测试环境打包时报
`Bind for 0.0.0.0:8000 failed: port is already allocated`：部署机上 8000 已被别的服务占用。

- **根因不在前端。** 前端 `api/client.ts` 走相对路径（同源托管），构建产物里 `8000`
  出现 0 次；审计页面没有端口可配项，也不需要——它不选后端地址。真正写死的是
  `main.py` 的 `uvicorn.run(..., port=8000)`，而 compose 的端口映射又照抄了一遍，
  两处各写一份，撞端口只能改代码重打镜像。
- **新增配置项 `API_HOST` / `API_PORT`**（新增 §7.1），缺省 `0.0.0.0:18173`。
  `main.py` 与 `docker-compose.yml` 都取该值（compose 用 `${API_PORT:-18173}` 插值），
  健康检查跟着走。**改 .env 的 `API_PORT` 即换端口**，不必碰代码。
  缺省不再用 8000：那是各框架的开发端口，测试机上极易被占用。
- **`API_PORT=` 留空回落缺省值**，不复用 `_blank_means_unset`——那个把空串归一成 `None`，
  而本字段是纯 `int`，会让 `Settings()` 构造失败、服务起不来。空配置行不该成为开机失败的原因。
- 同步位置：`.env.example` §7.1、`frontend/vite.config.ts` 的 dev proxy、
  `docs/runbooks/部署.md`、`docs/design/对外任务接口.md`、两份 README。
- **前端产物无需重建**：本次只改 `vite.config.ts` 的 dev proxy（构建时被替换，不进产物），
  `frontend/dist` 保持原样。

## 2026-10-16 — 部署改容器化：Dockerfile + docker compose

分支 `dev-business-opt01`。上线测试环境的部署方式由「手工装依赖起三个进程」改为容器编排，
新文件 `Dockerfile`、`.dockerignore`、`docker-compose.yml`。运维要点见 `docs/runbooks/部署.md` §1.1。

- **一个镜像承载三个进程**，compose 服务 `api` / `worker` / `inbound` 只覆盖 `command`。
  三份几乎相同的镜像各自构建会互相漂移，因此不拆多个 Dockerfile。
- **依赖源全部换清华镜像**（apt 与 pip），替换方式取自仓库里的 `Dockerfile.example`。
  apt 侧同时处理 deb822 的 `debian.sources` 与旧式 `sources.list`：只改前者的话，将来换
  基础镜像标签会让替换静默失效、退回官方源（慢但不出声）。
- **未照抄示例里的 ffmpeg**：本项目音频走云端 URL，仓库内无 ffmpeg / ffprobe / pydub 调用，
  装它只增体积。只装 `curl`（健康检查）与 `ca-certificates`（对外 HTTPS）。
- **镜像内不做 npm 构建**。`frontend/dist` 本就随仓库入库（`.gitignore` 的例外），
  直接 `COPY`；前端改动需先 `npm run build` 再 `docker build`。
- **配置仍以 `.env` 为唯一入口**，compose 用 `env_file` 注入，与 `settings.py` 自己读 `.env`
  取值一致。compose 只覆盖两项随形态变化的：`SERVE_UI=true`，以及 API 服务的
  `INLINE_WORKER=false`——否则 API 与 worker 两个容器会抢同一个队列。
- **命名卷 `cp-data` 挂 `/app/data`**，落在本机磁盘，满足「SQLite 不可放网络挂载」这条
  硬约束（WAL 依赖 `-shm`，网络文件系统上会锁失效并可能损坏库）。
- **停止宽限期按进程分别设**：api 90s、inbound 30s、**worker 30 分钟**。一次画像约 21 分钟，
  缺省的 10 秒宽限会在停机时把在飞运行砍成 `interrupted`。
- **`init: true`**：uvicorn 与 worker 都靠信号优雅停机，让 shell 当 PID 1 会让信号转发不可靠。

## 2026-10-16 — 上线测试环境准备：新增对外任务接口（8099）

分支 `dev-business-opt01`。上线业务测试前补对外面：运行台（8000）是**内部只读面**，
提交任务只是它顺带提供的入口、无鉴权，而任务此后由前端任务队列批量推送。
新增独立进程 `python -m customer_profile.inbound`，监听 **8099**，只做接单。
契约见 `docs/design/对外任务接口.md`，运维与验收步骤见 `docs/runbooks/部署.md` §9。

- **接单接口** `POST /api/v1/tasks`，入参 `phone_number` + `batch_id`（**均必填**），
  请求头 `X-API-Key` 鉴权。收到即 `Service.submit` 持久化入队并返回 200
  （`{"status":"accepted","run_id":…}`），**不执行、不查询、不轮询**——执行由 worker 做，
  结果由回写接口送到业务系统。
- **`batch_id` 在接口层必填**，尽管工作流定义里它可选。它会被原样填进回写体顶层
  `batch_id`，前端据此判断任务来源；空值会让来源丢失。定义层保持可选是刻意的——
  跑批脚本传空串（它透传进 `result1`，填批次号会污染与基线的可比性），那条路径不经本接口。
  新增用例 `tests/test_inbound.py` 同时钉住「入参进库」与「定义层绑到回写体」两段，
  缺任一段都会漏掉一类静默失效。
- **密钥独立新增 `INBOUND_API_KEY`，不复用 `PROFILE_API_KEY`。** 后者是我们主动调用生产回写
  服务的**出站**凭据；同一个值身兼两用会把「谁能调我们的接口」与「我们能写生产库」并成
  同一个泄露面。**未配置时接口整体关闭（全部请求 503），而不是关闭校验**——对外写面的空
  密钥若被当作「不校验」，端口就是任何人可写的入口。比较走 `secrets.compare_digest`。
- **接单进程不执行工作流，也不动别人的运行状态。** `inline_worker` 与 `interrupt_on_start`
  由 `inbound_settings()` 强制关掉（不经 `.env`，不是可调参数）：前者会让 21 分钟的工作流
  拖住接单能力；后者会与运行台、worker 共用的同一个 SQLite 上，把**别的进程**正在跑的
  运行标成 `interrupted`。`scripts/run_baseline.py` 为同样的理由显式关掉它。
- **错误码**：401 未带密钥 / 403 密钥不符（与既有回写服务的语义一致）/ 422 入参非法 /
  429 队列已满（带 `Retry-After`）/ 503 接口未启用。
- **配置**：`.env` 与 `.env.example` 新增 `INBOUND_API_KEY` / `INBOUND_HOST` / `INBOUND_PORT`
  （缺省 `0.0.0.0:8099`）。
- **测试**：`tests/test_inbound.py` 14 项（鉴权三态、接口关闭不放行、接单落库、
  `batch_id` 必填与进库、手机号形态与既有契约同形、进程定位两条、定义层接线、回传体三键）。
- **对接文档** `docs/对外任务接口对接文档.md`：写给前端任务队列的**联调说明**——怎么调、
  响应码各自该怎么做、多久出结果、批量推送的排队与 429 退避、一份自检清单。与
  `docs/design/对外任务接口.md` 分工：那份记设计取舍（为什么 401/403 分开、为什么未配密钥
  是 503），这份讲**怎么用**。前端团队已读的 `AI画像远程回传接口数据结构说明.md` 是同体裁先例，
  两份放在同一层。
- **密钥生成脚本** `scripts/gen_inbound_key.py`：一条命令产出 `INBOUND_API_KEY` 并可直接落盘。
  **默认只打印、不动文件**；写 `.env` 需显式 `--write`；**已有非空值则拒绝覆盖**（换密钥要两端
  同时动作，对端还持旧值时它的请求会全部 403），`--force` 才覆盖。只改该键那一行并按原行尾
  回写，不产出无关 diff；拒绝信息与日志**不含旧值**。用 hex 而非 base64：这个值要在 `.env`、
  `curl -H`、前端配置三处手工出现，base64 的大小写与 `+`/`/` 容易被改写。新增
  `tests/test_gen_inbound_key.py` 24 项钉住上述每条（含 CRLF 保持、末尾无换行时不粘连、
  拒绝时不回显旧值、以及「跑一次生成脚本，仓库 `.env` 必须原封不动」）。

**上线前验证（2026-10-16，单号真回写）**：四类响应码实测正确（401 无头 / 403 错密钥 /
422 手机号非法 / 422 `batch_id` 空白），被拒的请求在库里零运行记录；合法请求返回 200 且
落库为 `queued`；worker 领走后跑完，父运行 `succeeded`（129.4 s）。

- **取数侧**：`GET /api/v1/remote/data/history/13415100087` → 200。任务数据确实来自正式接口。
- **回写侧**：`POST /api/v1/remote/callback/update-profile` → **200**，线上返回
  `{"msg":"Profile updated successfully via remote webhook."}`。回写体顶层三键
  `phone` / `batch_id` / `analysis_result` 齐全，`batch_id = verify-single-20261016`
  与推送值逐字一致，`analysis_result` 六键齐全（`basic_notes_updates` 44 项 /
  `purchase_decisions` 4 类 / `deal_battle` 两数组 / `decision_psychology` 4 项）。
- **L-003 就此销案**（已从遗留问题清单删除）：线上收的就是**顶层三键**，契约文档正确；
  现役基线臂的 `data[]` 信封是待下线的旧实现，不是线上真实契约。
- **留痕不含凭据**：`request_attempts` 里 `X-API-Key` 记为 `***REDACTED*`（既有脱敏逻辑）。
- **验证用独立库** `data/verify_inbound.db`：运行台主进程（PID 11800）仍持共享库
  `data/customer_profile.db`，故验证不共用库——否则任务会被它以内联 worker、回写关闭领走，
  验收即失效。**验证期间该号的真实画像已写入生产库**，这是一次有意的、单号的真实副作用。

## 2026-10-16 — 提交入口改指业务测试臂；修复「节点永久停在运行中」

分支 `dev-business-opt01`。两件事：提交页跑的仍是已无用途的旧臂；业务测试臂的运行里
「极光数据」节点永远显示「运行中」。方案见
`docs/history/提交臂切换与极光节点卡死修复方案.md`。

- **入口臂切换。** 「提交任务」写死的 `customer_profile_entry`（显示名「客户初始画像」，
  基线臂）从 `WORKFLOW_MODULES` 摘除：不再加载、不再出现在 `/workflows`、不可提交与跑批。
  前端提交页与后端入参校验必须同指一个 ID——只改前端会让手机号校验**静默失效**，
  因此 `api.py` 的 `ENTRY_WORKFLOW_ID` 一并改指 `customer_profile_pipeline`（业务测试臂）。
  该臂历史运行连同子运行树共 **248 条运行 / 3199 条节点行 / 293 条尝试记录**已从留痕库清除
  （3 份不再被引用的定义快照一并删除）；
  `experiments/batches/baseline-39-dsv41-noThink/` 自带 `trace.db`，**不受影响**。
  `run_baseline.py` 的 `--workflow-id` 缺省值同步改指业务测试臂——原缺省是基线臂，
  正是《跑批可靠性》A5 记录的「漏传参数导致整批白烧」那个坑。
  模块文件保留：`tests/test_contract.py` 等仍以它为样本图，且它是基线臂**可复位**的前提。
- **节点永久停在运行中（真实缺陷，非脏数据）。** `_RunRecord.call_path` 被当成跨节点的
  可变字段：`scheduler.py` 为循环体临时改写它，`tracker.py` 的 `node_finished` 又以
  `execution.call_path or run.call_path` 回落取值，而顶层节点的 `execution.call_path` 是空串。
  运行内节点是**并发**的，于是循环体覆盖期间结束的**兄弟节点**把终态写进了另一条调用路径，
  自己那条 `running` 行再无人更新——界面因此永久显示「运行中」。
  **修法**：调用路径在本次执行上定死（`NodeExecution.call_path`），并显式传给
  `node_started`；循环体改用 `ContextVar` 取基路径，嵌套迭代的路径重复段一并消除。
- **回归用例**：`tests/test_iteration_call_path.py`。**修复前失败**（实测复现
  `[('', 'running'), ('/iter:n_iter/0', 'succeeded')]`），修复后通过。库里 4 处孤儿节点行
  按「终态回填 + 删错位行」修复（动手前整库备份 `data/customer_profile.db.bak-20261016`）。

## 2026-10-16 — 运行台对齐 v2 回传：双产物并列、链路分层、回写状态可读

分支 `dev-business-opt01`。回传旁支落地后，运行台（`frontend/`）的展示口径没跟上——
它只认 `result1` 一个产物、只按节点类型分类、把「回写已跳过」渲染成一段生英文 JSON。
本轮把这三处对齐到设计稿的分段与双产物口径。**只改前端**，不动执行逻辑、提示词、
存储与接口。方案与验收见 `docs/history/运行台对齐v2回传方案.md`。

- **聚合结果改为双产物并列。** 设计稿 §5 明确本轮对外产物有**两份**：`result1`（画像卡九板块）
  与回传体（六键 `analysis_result`）。此前浮层只读**运行级** `outputs`，而它只有 `result1`；
  回传体只存在于构造节点的产出里。现从**定义快照**定位回写节点（`config['@writeback']`）
  → 取上游构造节点的 `callback_json` → 解析后与 `result1` 并列呈现。
  **判据不写死工作流名或节点 ID**，换臂或改名仍成立。
- **节点行新增阶段与成本芯片。** 阶段取自标题前缀（`L0`/`L1`/`L2`/`L3`，`L1-1a` 归 `L1`），
  成本标签取自节点类型（`code`/`start`/`end` 等 → 「纯代码」，`llm` → 「LLM」）。
  取不到阶段时不显示该芯片，**不用节点 ID 硬编码去补**——那会把展示层绑死在某一条工作流上。
  `tool` 与 `http-request` 不标成本：前者是子流程、后者是外部接口，硬归一类会是错的。
- **「回写已跳过」改为中文可读。** 判据是产出里存在 `writeback_skipped` 键，不绑定工作流。
  摘要一行「回写已跳过（WRITEBACK_ENABLED=false）」；详情渲染成中性说明并明写
  **运行显示成功不等于画像已写进生产库**；原始 JSON 仍留在下方可查。
- **后端零改动。** 现有 `/runs/{id}/graph` 与 `/runs/{id}` 已足够：定义快照里带节点 `config`，
  回写节点因此可在前端识别。`result1` 的对外契约未动。
- **构建产物随源码同批入库**（本项目的既有决策：未装 node 的机器要能直接起）。
  `npm run build` 通过（27 modules），产物内已含新逻辑。

**验证**：`npm run build` 与 `oxlint src/` 通过（退出码 0；现存告警均为改动前既有的
`react(set-state-in-effect)`，落在未改动的组件上）；构建产物中 `chip-stage` / `chip-cost` /
`result-block` / `rv-notice` 及中文文案均已出现。前端无测试框架，故以构建与 lint 为静态门。

## 2026-10-16 — business_opt01：AI 画像 v2 回传改造（P0–P5 编码完成）

分支 `dev-business-opt01`。前端（业务架构师侧）指定了新的回传契约：`analysis_result`
六键 + 一份 45 字段的 Notes 清单。本轮把业务测试臂（原「实验臂」）的产物对齐到该契约，
**调测期不做真实回传**——回写节点存在、可被断言、但恒不发请求。
方案与验收标准见 `docs/design/AI画像v2回传改造方案.md`。

- **P0 契约快照重建。** `scripts/snapshot_note_contract.py` 重写为
  `--from-response <线上 payload>` 模式：签名由契约内容摘要得出（不再是逐进程随机的
  `hash()`），分类名未登记即报错而不是静默回落。`notes_contract.py` 随之重生成：
  **45 字段 / 7 分类**，签名 `b36e78edf9a2`，来源 `docs/Notes_new.txt`，
  `--check` 可复现。`notes_contract_extension.json` 清空——线上 payload 本身就是完整契约。
- **P1 编外字段机制（`internal_fields.py`）。** 新增「取证照旧全量、回传只走契约」的白名单层。
  17 个下线字段分三类处置：编外 11 个（`old_car_model` / `company` / `seating_requirement` …）、
  迁移 4 个（`emotion` / `communication_rhythm` / `personality_and_preferences` → `decision_psychology`；
  `resistances` → `deal_battle.loss_conditions`）、舍弃 2 个（抖音/小红书账号）。
  另新增编外字段 `purchase_nature`。
  **顺带修掉一处静默退化**：编外身份字段（`company` 等）离开契约后，
  归属过滤与销售顾问值形态门不再认它们，实测有 `subject=unknown` 的「我们单位也要买一个」
  被当作客户企业写出——现由 `IDENTITY_GUARD_KEYS` 取并集补回。
- **P2 跨字段规则随枚举换血改写。** 新契约的 `purchase_type` 只剩个户/公户，
  换购判定的落点改到编外字段 `purchase_nature`，否则会写进一个只认两个值的单选字段并被静默丢弃。
  编外枚举（`emotion` 等）在 L2 归一、L1 附录、L3 枚举门三处一并接上。
- **P3 四区块投影（`callback_build.py`，0 token）。** `ai_insight` / `purchase_decisions` /
  `deal_battle` / `decision_psychology` 由**代码**从 facts 投影，不由表达层 LLM 归类
  （L-002 的定论）。id 由内容摘要生成（同输入恒同 id），并跑一次同客户全局唯一性断言——
  重复即抛错而非静默去重。每条的 `reasoning_detail` 复用 Notes 那一条的措辞，同一事实
  在卡片与 Notes 上不会有两种说法。契约语义一并守住：**缺料的分类整个不出现**，
  而不是给 `items: []`（后者会清空客户已有数据）。
- **P4 回写节点接入业务测试臂。** 新增 `@body_ref`：请求体**整段取自绑定值**，
  绕过文本替换——`@body_template` 恒把引用渲染成字符串，产不出契约要求的
  `analysis_result` **对象**。与 `@body_template` 互斥，解析失败抛错而不发坏载荷。
  管线新增 `pipeline_callback_build` → `pipeline_callback_writeback` 旁支，
  `END` 仍挂 L3，`result1` 的对外契约一个字不改。显示名改「业务测试臂」，
  `workflow_id` 不动（跑批入口与既有批次的可比性判据以它为准）。
- **P5 调测（零真实回传）。** 回归用例钉住：回写节点存在、带 `@writeback` 与 `@body_ref`、
  缺省设置下一个请求都不发且产出 `writeback_skipped: true`；开关打开时请求体是对象而非字符串。
  `@body_ref` 另附 5 条解析语义用例（含互斥与无绑定两处报错）。

**验收（四条全部取证）**：

- **A1** 契约快照 `--check` 通过（45 字段 / 7 分类 / 签名 `b36e78edf9a2`，来源 `docs/Notes_new.txt`）；
- **A2** 单号产出的 `basic_notes_updates` 为 45 条、45 唯一键，零越界——每个 `attribute_key`
  都落在契约 45 键内，编外字段一个都没漏进回传体；
- **A3** 同客户 20 个 id / 键全局唯一（用例覆盖 + 真实载荷复核双证）；
- **A4** 调测期零真实回传：全库零条 `update-profile` 请求，回写节点产出
  `{"writeback_skipped": true, "reason": "WRITEBACK_ENABLED=false"}` / `status: 0`。

单号（`customer_profile_pipeline`，号码 `13415100087`）端到端 `succeeded`：40 个节点、
16 次子运行、106.7 秒，`result1` 仍为字符串。四区块结构复核：`purchase_decisions` 只产出
非空的三类（到店动因 / 核心关注 / 决策阻力），`decision_psychology` 四键与契约
`PSYCHOLOGY_KEYS` 逐字一致，`reasoning_detail` 全为 `{summary, steps, evidence, confidence}` 四键。
测试 **655 passed / 80 skipped**。

**顺带修一处脚本缺口（非本次改造引入）**：`scripts/smoke_run.py` 原先只为
`customer_profile_entry` 补 `batch_id` 默认值，业务测试臂漏掉时 `pipeline_start` 不产出该字段，
而 `pipeline_production` 的 `(START, "batch_id")` 绑定早已存在，解析即 `KeyError`。
改为无条件补齐，与 `run_baseline.py`（对任何 `--workflow-id` 都显式传 `batch_id`）同口径。

**一处未决**：画像卡原有的 `inferred_tags` / `usage_scenarios` / `communication_strategies` /
`purchase_motivations` / `product_preferences` 五节在新契约六键里没有落点，本轮刻意不动
`SECTION_KEYS`（一动 `result1` 形状即变）。登记为 `docs/遗留问题清单.md` L-006。
L-005（契约快照生成器缺失）已随 P0 解除并删除。

## 2026-10-15 — business_opt01_v2 第三轮（v3.3）：跑批可靠性四项修复 + 全量 39 案验收

分支 `dev-business-opt01-v2`。本轮是「事实账本管线」（实验臂 `customer_profile_pipeline`）
的**收口批次**：跑通全量 39 案并完成第三方审核，余下优化点转入下一轮。

**可靠性四项（`docs/runbooks/v3.3-可靠性改进.md`）**

- **A4 异常落库**：`request_attempts` 表新增 `anomaly` / `finish_reason` 两列（migration
  自动补列）。此前 `LlmAttempt.anomaly` 只在内存里用，重试成功后前几次的异常原因永久丢失。
- **A3 输出量下限闸门**：`detect_output_anomaly()` 新增 `output_too_short` 与
  `suspicious_finish_reason`。起因是 `171****0673` 案输出从 12k 字符塌陷到 575 字符
  （22 倍），而 `finish_reason` 仍是 `stop`、无语法错误，被判成功——静默降级。
  本轮把 `min_completion_tokens` 从 `llm_call()` 一路接到 **executor 层**
  （`src/customer_profile/execution/{llm,executors_llm}.py`）：节点在 `config` 里声明阈值，
  可写非法值**报错而非忽略**（静默降级正是要修的那类「看着在守、实际没守」）。
  **缺省不启用**——短输出不总是故障，信息稀薄的案子合法返回 `{"fields": []}`
  （`167****3251` 只有 5 token）。故阈值由**知道自身输入规模**的节点按节点给，
  不在公共层设全局值。
- **A2 失败案自动重跑**：`run_baseline.py --retry-failed N`（默认 0，退避 60 秒，
  `manifest.json` 记 `retry_rounds`）。v3.2 首跑 4/8 案因上游瞬断失败，靠人工 `--resume` 救回。
- **A5 参数错配防护**：批次 ID 以 `A-` 开头但 `workflow_id` 不是实验臂时告警；转写缓存
  目录为空且有输入待跑时提示可能配错路径。两个坑都实际浪费过整批产物。

**验收工具修正**

- `scripts/validate_v3.3_results.py`：原先读 `result["customer_profile_card"]`——该路径
  **不存在**，真实层级是 `result.json` → `result1`（JSON **字符串**）→ `analysis_result`。
  错路径让它对每个成功用例都报「字段缺失」，39 案报出 25 条假问题。改为按 10 个段落
  **全量**核查（不抽样），并把「段落留空」与「结构缺失」分开报告：前者在事实簿稀疏时
  属正常，交人工确认；后者才算缺陷。

**全量跑批与审核**

- 全量 39 案两臂对照。读数：Notes 字段 1005 → **1564**，契约字段覆盖 914 → **1524** 字段次；
  LLM 调用 897 → **317**，token 14.72M → **7.09M**，单案耗时中位 281.4s → **117.3s**；
  异常留痕 0 条，重试救回 4 案。
- 产出两份第三方审核报告（`docs/history/审核报告-{业务,销售}视角.md`，
  **含客户原话，不入库**），逐案核读 26/39 案。已确认的最高优先级缺陷：**L2 审查层
  「能判但不拦」**——审查已能报出枚举违规（如 `payment_method=公司采购`），
  判定结果却未落到装配动作上，违规值照样进最终 Notes。判据现成，只差执行。
- 其余已确认缺陷（销售的话被当成客户的话、沟通策略同模板复读、客户明说的地域/语言被丢弃、
  产品参数由模型内部知识补全、L3 同源守卫误报率 114/115）逐条记在两份报告里，
  连同各自**证据底稿**（`docs/history/` 下的审核报告与逐案对照材料）留作下一轮输入。

**工作区整改**

- 管线设计写成**给人看的**文档：`docs/design/客户人设画像管线设计.md`。
- `experiments/` 从 2152 MB 精简到 **857 MB**：删除旧 AB 场地 `abexperiments/`（906 MB）、
  失败批次 `FAILED-template-miss-*`（254 MB）、`batches/batches/`（60 MB）、
  `A-pipeline-L8-v3.2-*`（71 MB）、`_smoke/`。保留**基线臂**、**v3.3 实验臂**与
  **转写归档** `transcript_cache/`（跨批次共用，删掉等于毁掉复用设计）。
  批次目录规范化：`batches/baseline-39-dsv41-noThink/`（基线臂）与
  `batches/pipeline-v3.3-39/`（实验臂）。批次数据**不在 `docs/` 下留存**，只此一份。
- 删除已收口的中间文档：v3/v3.1/v3.2/v3.3 方案设计与交付说明、`docs/specs/` 四份任务书、
  `old_templates/`（84 份 = `src/templates/` 现有版本 + 5 份仅属**已删节点**的孤儿模板，
  `REMOVED_NODES` 可查）、一次性脚本与探针共 9 个。
- **`old_templates/` 的判定依据**：模板完整性守卫
  `tests/test_llm_template_bindings.py` 扫描 `llm` + `template-transform` 两类节点并要求
  每个引用都能解析，全量测试通过即证明 `src/templates/` 自足。该守卫本身正是为此拓宽过的
  ——一次清理把 83 个模板删到 5 个时，被删的 29 个只被 `template-transform` 引用，
  守卫因只扫 `llm` 而全程绿灯，直到 39/39 跑批失败才暴露。


## 2026-10-14 — business_opt01_v2：画像卡四节大面积缺失（表达层嵌套 + 装配层静默补空）

分支 `dev-business-opt01-v2`。用户审查 v3.1 批（`A-pipeline-L8-v3.1-20261014`，8 案）时发现
画像卡 `purchase_motivations` / `product_preferences` / `resistances` / `emotion_state`
四节大面积留白。排查结论：**不是数据丢失，是产物位置错 + 装配层静默抹平**。

- **根因**：`l3_express` 提示词把第 6/7/8 节的标题挤在同一行（`### 6. …（数组）7. …（数组）
  8. …（数组）`），紧跟在第 5 节 `customer_overview`（对象型）之后。模型据此把这三节连同
  第 9 节当成第 5 节的**子键**，写进了 `customer_overview` 内部。
- **静默点**：`assemble_l3` 只按顶层键抄 9 个 section，嵌套的值不认，随后 `setdefault`
  补成空数组/空对象。全程无异常、无留痕。`section_count` 又是**补空之后**统计的，恒等于 9，
  所以回归绿着、卡片空着。
- **实测规模**：8 案中 **5 案**触发（`13415100087` / `13435844484` / `13527911093` /
  `13530559591` / `19820803068`），非空节合计 52/72。基线 39 案无此问题——基线那 4 节由
  `customer_profile_production` 的独立 LLM 节点各自产出顶层 JSON，没有嵌套余地。
- **已排除**：不是事实表稀疏导致的合并。八案 facts 键数 24/35/32/17/29/39/38/36，与成败
  无单调关系（17 键的失败、29 键的成功，而 36 键者照样失败）。
- **修复（三处，零 token）**：
  1. 模板拆行——第 6/7/8 节各自独立成 `### N.` 标题，并补一条「9 个键**平级**，一律平铺在
     `analysis_result` 之下，不得嵌进别的板块」的硬规则；
  2. 装配层**上提归位**——`assemble_l3` 把被嵌进其它 section 里的节搬回顶层（顶层已有该节
     时就地清除嵌套副本，不覆盖）；
  3. **报警**——`section_count` 改为统计**实到节数**（补空之前），新增
     `sections_missing` / `sections_hoisted` 两个输出；缺节从此是可见事件。
- **归属**：该挤压行由 `ab68a1d`（v2.1 优化代码落地）引入，v3 / v3.1 / v3.2 三批提交均未
  碰过这份模板——是 v2.1 起就存在的老缺陷，只是 v2.1 那轮跑的是 production 图，未暴露。

验证：对 v3.1 八案**已有产物回放**真实 `assemble_l3`，非空节 **52 → 72/72**，八案全部
`count=9`、`missing=[]`——**无需重跑批次即可救回**。全量 `pytest` **600 passed / 37 skipped**。


分支 `dev-business-opt01-v2`。上游是 v3.1 四项整改（批 `A-pipeline-L8-v3.1-20261014`，8 案齐）。
用户审查 v3.1 输出后发现**两处信息丢失**，v3.2 据此修：

- **变更一 · 第三方画像标签准入**：L1-1a 由「只收两类」改为**三类**，把第三方画像标签
  （极光）收作第三类；某字段仅由它单独支撑时记 `inferred_high`（不再降 `inferred_low`）。
  口径依据：这批标签是**采购的商用画像数据**，在客户无试驾、无沟通记录时是唯一可靠的画像
  信源。投影层规则 b、`_basis_of`、`Claim.effective_basis` 同步改档——预测渠道**封顶
  `inferred_high`**，仍**不等于** `fact`。实测案例：`13530559591` 的 `occupation` 此前
  全程为空，根因是孤立标签既不算 fact 也不算「≥2 条同向」，不是数据缺失。
- **变更二 · 汽车品牌归属路由 + 契约补挂**：L1 提示词显式区分**意向车型**与**竞品**
  （对比品牌/车型、试驾过的车型、本品牌顾虑），并明确品牌辨识**先以 LLM 原生汽车知识
  为准**（知识库待建）。决定性的修正在契约层——`_to_notes_updates` 只遍历契约字段，而
  原 42 字段契约**没有竞品字段**，竞品信息无处可放。
- **对外契约变更（需评审）**：42 字段 / 5 分类 → **46 字段 / 6 分类**，新增「竞品对比」
  一类共 4 个文本字段（`competing_brand` / `competing_model` / `test_driven_models` /
  `brand_concerns`）。落法是**增量补挂**：基线快照不动，新增走
  `src/customer_profile/notes_contract_extension.json`，由 `scripts/snapshot_note_contract.py`
  合并生成（生成器与 `--check` 已扩展）。**未**整表换用线上 45 字段——线上同时下线了
  16 个现役字段（`old_car_model` 被确定性注入依赖等），整表替换会静默打掉既有功能。

验证：全量 `pytest` **597 passed / 37 skipped**（零 token）；契约快照 `--check` 与批次 +
补充文件一致。**新批次的 LLM 效果验证须另跑真实批次（消耗 token），不在本次收口内。**

设计与契约见 `docs/business_opt01_v2/v3.2-方案设计.md`。

## 2026-10-12 — business_opt01_v2：事实账本管线（P1–P5 编码完成）

分支 `dev-business-opt01-v2`。把「先取证成文、再事后校对」的旧链路，改为
**L0 记录 → L1 字段级取证 → L2 代码投影 + 受约束兜底 → L3 校验与表达** 的四层结构。

- **L0/L1（P1/P2）**：取数侧三个「产物无人消费」的 LLM 节点以**纯识别变体**关闭
  （新增 `audio_content_extract_pure`、`moments_info_pure`、`test_drive_audio_pure`、
  `outbound_call_audio_pure`，原定义逐字保留供基线臂使用）。L1 由「抽取断言」改为
  「字段级直出 + 三分类 `fact` / `inferred_high` / `inferred_low`」，并把 42 字段的
  契约清单渲染进提示词输入。
- **L2（P3）**：投影规则由「拒绝即丢弃」改为「**降级保留**」——枚举归一失败、身份字段
  仅由预测或社媒支撑，都不再丢字段，改为写入 + 降档 + 转人工确认。新增一个**受约束的
  LLM 兜底节点**（`pipeline_l2_fallback`），只能裁决候选集、**不得新增取值**，越界一律
  拒收留痕。
- **L3（P4）**：新建 `customer_profile_l3`（11 节点 / 1 个 LLM）替代 production 的成文
  职责。**9 个画像 section 与 Notes 读同一份 `facts`**（旧链里两者各自生成、无人对齐，
  是本项目「卡片与 Notes 不一致」的根源）；6 个业务资产按**原名**引用，正文一字未改。
- **P5**：删除管线的旧证据整理链与兼容覆写节点。管线终态 **35 节点 / 2 个 LLM 节点**。

验证（**零 token 离线**，AB 按用户要求待完工后单独开展）：13 案批次回放 + 全量单元测试。
9 节齐全 13/13、无依据确定值率 0、图校验 0 错误。回放测得的 Notes 字段数与基线仍有差，
经归因**卡在 L1 覆盖率**（回放无法体现新提示词的效果），该判据留待 AB 闭合。
施工记录见 `docs/specs/business_opt01_v2-施工计划.md` §6。

## 2026-10-05 — 系统优化：运行台改「概览 + 双栏」版式

分支 `dev-system-opt`。依据第三方设计交付件（`docs/history/Workflow_UI_Implementation_Guide.md`
与设计效果图）重做运行详情页的版式与视觉。**这一轮只动展示层**：未改工作流执行逻辑、
提示词、存储模型与通信协议，后端零改动。

- **版式**：由「纵向展开列表」改为「顶部运行概览 + 左侧节点清单 + 右侧节点详情」。
  概览给业务标题、执行状态、业务标识、总耗时、节点执行数与本次使用的定义版本；
  右侧详情可关闭，关闭后清单独占整行。
- **节点清单**：三行结构（业务名 + 状态 + 耗时 / 结果摘要 / 类别标签），
  带节点搜索与「全部 / 有结果 / 异常」筛选。**排序口径改为按完成时刻**
  （`started_at_ms + duration_ms`），并把口径写在列表头上。
- **节点详情**：三标签「业务结果 / 原始数据 / 执行信息」。业务结果按真实数据类型渲染
  （Markdown、字段表、记录表、通用 JSON 降级），Markdown 由自建渲染器输出 React 元素，
  不经 `dangerouslySetInnerHTML`，也不引解析依赖。
- **新增「查看聚合结果 / 复制结果」**：数据来自运行级 `outputs`（`GET /runs/{id}`，
  前端此前未调用该端点）。
- **展示层清洗开发期标记**：「（新）」「SubAgent - 」「 - 工具」等前缀后缀只影响显示，
  原始标题与工作流标识保持不变。

实施中发现并修正三处展示层缺陷（均与真实数据形态相关，见方案文档 §7.1）：

1. 未执行节点（`blocked` / `skipped`）在库里记的是 `started_at_ms = 0` 而非 null，
   只判 null 会把「输出（被阻断）」顶到时间线第 01 位。
2. 多字段输出（如 `{phone_number, batch_id}`）无已知正文键时被误报「本次输出为空」。
3. 数组里的对象元素直接字符串化，渲染成 `[object Object]`。

方案、口径决策与对设计图的偏离见 `docs/history/系统优化-运行台UI优化（方案）.md`。
验证：`npm run build` 与 `npm run lint` 通过；以本机库 219 条真实运行在
1366 / 1680 / 1920 三种宽度下逐场景核对。

## 2026-10-05 — 系统优化：提示词模板节点并入 LLM 节点

分支 `dev-system-opt`。承接上一节的命名整改：上一节只改名字，本节动结构。

- **17 对「提示词节点 + LLM 节点」折叠为单节点**。这 17 对是迁移期「`template-transform`
  拼提示词 → `llm` 接住」的形态，前者只为拼提示词而存在，前端流程列表里因此每处 LLM 调用
  占两行。现删掉提示词节点，把它的 `template=` 与全部 `bindings` 搬到 `llm` 节点，
  节点 ID 与标题不变。`template-transform` 从 46 降到 29，`llm` 从 27 升到 44。
  涉及 production 11 对、profile_features 3 对、chat_history / evidence_subagent /
  user_feedback 各 1 对。
- **提示词正文零改动**。模板资源（`<slug>__<原节点ID>.txt`）名字与内容都不变——
  资源名是不可见键，改名留待模板命名专项。
- **丢弃三处悬空分支门**。`profile_features_analysis` 的 `on_branch` 指向两个早已删除的
  `if-else` 节点，随提示词节点一并清掉；`user_feedback_data` 指向存活 `BRANCH` 的门保留。
- **`prompt_field="input_prompt"` 退场**。提示词改由 `config['template']` 渲染，
  `execute_llm` 的「先入参、后模板」判定顺序不变。

方案与取舍见 `docs/history/系统优化-模板节点与LLM节点合并（方案）.md`。
验证：374 passed / 30 skipped；结构校验零悬空引用、零占位符缺绑定。

## 2026-10-04 — 系统优化：运行台改流程视图、脱离画布依赖、迁移台账收口

分支 `dev-system-opt`。五个批次（A 前端 / B 后端 / C 命名 / D 台账 / E 术语），按「前端 → 后端 → 术语 → 命名 → 台账」推进。

- **运行台不再用连线图**。运行详情改为**按执行时间排列的节点清单**：每行一个节点执行，
  可展开看运行时间、输入输出、每次尝试的模型与 token 消耗，以及子运行下钻。
  并行关系不做分组，由每行的「前置依赖项」对照看出。新增 `FlowList.tsx`，删除
  `TopologyGraph.tsx` 与 `RunTimeline.tsx`，`@xyflow/react` 依赖一并移除。
  顶部的「工作流结构」标签页随之移除——它强依赖画布模块，而画布这一表达方式已被放弃。
- **后端删除布局层**。`layout.py`（最长路径分层）与 `GET /workflows/{id}/topology`、
  `GET /definition-versions/{id}` 两个端点删除；`NodeDef` 去掉 `coords` 字段，
  全部工作流定义去掉 `original_node_id` 与 `coords`。定义导出的 `is_synthetic` 标记让
  人工验证图不进运行台的工作流列表。
- **术语清理**。`src/` 注释与 docstring 中残留的迁移期措辞（原平台、原始定义、
  「Gemini 异常输出重试版」、差异编号、台账文件引用）改为对当前行为的直接描述。
  三份无任何节点引用的孤儿模板 `gemini_retry__*.txt` 删除。
- **命名整改**。19 个工作流的显示名去掉迁移期的「（新）SubAgent - 」前缀（`画像内容生成&回写`
  还带着一处重复的「（新）」后缀），还原为业务名；`source_dsl` 字段按决定**保留**（值取显示名，
  不再指向已废弃的 `.yml`），快照形状不变。节点标题去掉 Dify 味与数字后缀：`Gemini（异常输出
  重试版）` → `客户画像反馈总结报告生成`，`模板转换 (1)`、`输出 2`、`代码执行 2/3`、`变量聚合器 2`、
  `HTTP 请求 2`、裸 `LLM` 等一律改成描述该节点在做什么。**`node_id` 一律不动**——模板正文硬引用
  其中 9 个，改了会静默渲染失败。
  收口后复查又清掉一轮同类残留：5 个工作流显示名、4 处子流程引用标题去掉「（生产环境）」；
  17 个模板节点标题去掉 `Prompt` 后缀，改为「`X 提示词`」——直接去后缀会与其下游的 `llm`
  节点同名，流程列表里两行同名读不清，故保留「提示词」这一区分词。
- **迁移台账收口**（本次的主要工作）。`docs/history/ledger/` 8 份文件删除。删除前把其中的
  业务价值内容迁入**现行文档**，而不是留在归档里：
  - 业务方修订与缺陷修复的取舍 → `docs/adr/0004-照搬定义文本还是按实测修正.md`。
    核心是：有三处**照搬迁移前定义文本会错**（企业信息取值键、JSON 围栏识别、模板绑定），
    照搬的后果都是「数据静默丢失而运行仍显示成功」，故改动正文并显式登记为偏离。
  - 执行器的行为契约（迭代串行与失败即终止、分组聚合的 `{"output": 值}` 形态、
    聚合判据是「执行过」而非「非空」、HTTP ≥400 失败、最多 3 次尝试、多模态硬约束）
    → `docs/design/执行语义口径.md`。这些在代码里只有一行实现，台账删掉就再无文字依据。
  - 对外协议结论（回写 2xx 即成功、幂等键粒度为客户）与「缺省采用」项一并记入上文。
- **文档同步**：`README.md` 与 `frontend/README.md` 去掉「拓扑图 / React Flow / 坐标由后端给」
  等已失效描述；`docs/README.md` 的目录分工不变（`adr/`、`design/` 本就在列）。
- **测试**：全量 **374 passed / 13 skipped**。

## 2026-10-03 — 系统优化分支开工前的清理（与原平台彻底脱钩）

- **分支**：新建 `dev-system-opt`，后续系统优化工作在此进行，不直接影响 `dev` 主干。
- **移除外来 DSL 数据目录**：原定义文件目录连同其 `.gitignore` 条目一并移除；内容已由业务方在仓库外另行备份。项目不再依赖任何外部定义文件。
- **清理仅服务于逐字符比对的脚本与用例**：导出、抽取、台账生成/复核等脚本，以及三条只在这些文件存在时才有意义的比对用例（缺文件时长期 `skip`）全部删除；相关断言中依赖它们的部分一并去掉。
- **归档迁移期文档**：迁移任务说明、各版本规划与交付报告、台账、评审副本移入 `docs/history/`（仅作归档，不更新、非必要不阅读）；`docs/README.md` 的目录分工相应补上 `history/`。
- **措辞清理**：`src/`、`tests/`、`docs/`、`frontend/`、`.env.example` 中的原平台措辞改为中性表述；定义来源字段（`source_dsl` 等）按决定**保留**，只去除其措辞。
- **前端**：重新构建并提交 `frontend/dist`（注释级改动，产物哈希不变）。
- **测试**：全量 **391 passed / 13 skipped**（干净检出、无需任何本地外部文件）。删除的三条逐字符比对用例原本只在外部定义文件存在时才有意义，缺文件时长期 `skip`，故一并移除。

## 2026-09-29（基线收口） — 数据基线跑批完成与工作区清理

- **批次**：`baseline-39-dsv41-noThink`，**39/39 succeeded**（首轮 38/39，`16761543238` 因模型在 JSON 字符串里输出裸换行而解析失败，补跑通过）。归档 39 个目录 / 189 MiB / 每个恰好 4 文件。
- **数据核对**：聚合包 15 个字段全部 39/39 非空。第三方画像数据 32 真实 / 7 兜底，关联企业信息 8 真实 / 31 兜底——兜底是子流程对「查无结果」的正常说明，不是解析失败。核对中发现并修复了 W52。
- **工作区清理**：删除 旧定义目录（3 个文件与 旧定义目录 逐字节相同、全仓零引用）与旧批次 `baseline-39-w50`（178 MB，旧模型且含 W47/W50 两处缺陷，结果不可作对照——已移至仓库外备份，工作树内只保留最新批次）。清掉 `.pytest_cache/`、`__pycache__/` 与临时日志。
- **刻意保留**：`experiments/transcript_cache/`（58 条目 / 33 MB）**不随批次清理删除**——键与工作流逻辑无关，是跨批次复用的转写归档，删掉等于毁掉复用设计；`test_phone_numbers.txt`（39 个虚拟号，入库的输入锚点）与 `phone_numbers.txt`（11 字节，1 个真实号，已忽略，被 `smoke_run.py` / `record_fixture.py` 读取）同理保留。
- **台账**：台账提取脚本 重跑无差异，台账复核脚本 通过——20 工作流 / 298 节点 / 328 边 / 81 模板。
- **测试**：546 passed / 14 skipped。

## 2026-09-29（续） — 修复围栏在正文中段时的静默解析失败（W52）

- **背景**：核对基线批次 `baseline-39-dsv41-noThink` 时发现，39 个号码里 2 个的`wechat_profile_info` 字段为空，但运行状态全是 `succeeded`。
- **根因**：`证据线索汇总` 的「客户标准信息数据聚合」（节点 `1778232215760`）中，辅助函数 `_parse_json` **并非没有剥围栏**——它有 `text.startswith("```")` 分支，但只认「整段以围栏开头」。这两例的模型输出是「一句说明 + 换行 + ```json 块」，既不满足该条件，又无法被 `json.loads` 直接解析，于是整体落入 `except` 返回空 dict。
- **真正的问题是静默性**：该函数的注释明写「解析失败、空字符串、None…统一返回空 dict」，因此下游无法区分「这个客户没有微信信息」与「我们解析砸了」。3/39 的比例下这次没炸，但它是典型的事后才发现型接缝。第三例 `13535737221` 模型完全没输出 JSON（纯散文），无围栏可提，属模型侧问题，不在本次修复范围。
- **修复**：新增模块级辅助函数 `_extract_fenced_block`，围栏不在开头时按**第一个**围栏块提取；无成对围栏时返回 `None`、沿用原文，不猜。原「整段围栏」分支一字未动。
- **新增一条登记通道**：`_parse_json` 是**辅助函数**，而既有的 `CODE_BODY_DEVIATIONS` 只管 `main`，辅助函数的逐字符比对原本**没有登记通道**。故在 逐字符比对用例 新增 `CODE_HELPER_DEVIATIONS`，并配一条反向校验用例（登记过期即红）——与 W50 的机制同构。
- **实测验证**：用批次真实留痕回放，`wechat_profile_info` 命中数 **36/39 → 38/39**；新命中的 `15012891982` 取到 `nickname`/`wechat_id`，`18003012053` 取到正常空值。全量 **546 passed / 14 skipped**（修复前 531；+1 条反向校验用例、+14 条解析用例）。

## 2026-09-29 — 换模型并关闭思维链，新增两个可配置模型参数（W51）

- **背景**：`baseline-39-w50` 批次卡在 `1773135369428`（LLM 识别结果校对）：三次尝试全为 `empty_text`，实测停在 590.6–592 s。
- **根因不是我们的超时**：基线脚本以 `http_read_timeout=1800.0` 显式传参，覆盖 `.env` 的 600，实测最长尝试 591.6 s 从未触及。上限在模型侧：全部 ≥590 s 的尝试中 `completion_tokens` 齐齐落在 49.6k–53.7k，而提示词长度差 3 倍也不变。
- **换模型验证**：`deepseek-v4.1-flash`（用户 2026-09-29 指定），同一条真实失败提示词在开启思维链时 `finish_reason=stop`、437 s 出 55213 字符；可用性由此验证。
- **关闭思维链有一个硬前提**：实测表明，不指定上限时服务端把输出**卡在 8192 token**（`completion_tokens=8192`、`finish_reason=length`），而该节点需要约 2.9 万 token 的 JSON 正文。截断后的半截 JSON 会静默通过异常检测（`detect_output_anomaly` **不检查 `finish_reason`**），到下游解析时才炸掉。
- **新增两个参数**：`llm_enable_thinking` / `llm_max_tokens`（`Settings`，对应 `LLM_ENABLE_THINKING` / `LLM_MAX_TOKENS`）。缺省均为 `None`，**即不发该键**、沿用服务端默认，因此 §6.4.4「不设其他参数」的原文在缺省下仍成立。当前 `.env` 显式设为 `false` / `128000`（上限取 128k 而非 64k：业务流中观察到过 80k 左右的输出；两款常用模型的上下文窗口均为 1M）。
- **修复了一处测试隔离缺陷**：`tests/conftest.py` 的 `make_settings` 未钉住新字段，于是**从开发者本地 `.env` 取值**——该表现非常危险，测试结果不应随开发机而变。已在配侧显式补 `None`。
- **全量测试**：531 passed / 14 skipped（改动前后一致，即未引入新失败，也未因新增字段破坏既有断言）。

## 2026-09-28（续） — 修复企业/归属地数据静默丢失（W50）

- **背景**：用 `13415100087` 排查「新版原始定义 集成的两个渠道取不到数据」时定位到两处**各自独立**的缺陷，本次一并收口。该号码的真实响应含 4 个渠道，极光与企业两路都有数据、都拿到了，但都没被正确处理。
- **缺陷一（我们的集成遗漏）**：「证据线索汇总」图中极光数据的调用点传的渠道名是**旧值 `jiguang`**（`evidence_subagent.py`）。子流程自身的 `DATA_SOURCE` 已按新版原始定义 改为 `DatametInterestPoints`，但父图显式传参把它覆盖掉了，因此取数永远命中不了。真实载荷实测：传 `jiguang` 得空串，传 `DatametInterestPoints` 得 5617 字符 tags。已改为显式传新值。
- **缺陷二（原 原始定义 自身就存在，非本轮引入）**：`手机号&企业信息分析` 的 `结果解析` 节点按 `operator_location_json` / `affiliated_company_json` 取值，而该渠道真实载荷从不含这两个键，导致两个字段恒为 `{}`、企业数据被静默丢弃。三条证据表明不是迁移引入：该键全仓库只出现在这段代码自身、全部留痕里出现 0 次、旧版 `split_operator_and_company` 与新版**逐字符相同**。经用户确认后按真实载荷的两种实测形态改写取值，并保留对原键名的兼容读取。见差异 W50。
- **该改写登记为 code 正文的有意差异**（逐字符比对用例 的 `CODE_BODY_DEVIATIONS`）：逐字符断言默认要求正文与原始定义 一致，偏离必须显式登记理由，且登记表会**反向校验「确实不一致」**——原始定义 日后修好了这条会红，提示登记已过期。这是全项目第一处 code 正文偏离。
- **回归用例**：新增 `tests/test_phone_company_analysis.py`，用**留痕里的真实载荷形态**（含企业数据的形态 A / 无企业数据的形态 B）而非构造的理想载荷，覆盖归属地取键、企业块收集与剔除、空载荷不误判、原键名兼容、空串与 `None` 兜底等。
- **全渠道对表**：按业务方给出的渠道清单逐条核对 14 个子流程的「清单 ↔ 新版原始定义 实参 ↔ 代码字面量 ↔ 子流程默认值」四方，修复后唯一不一致项已消除；另对全部 21 个 `tool` 节点做了「父图实参 → 子流程 start 变量」覆盖审计，无缺传。

## 2026-09-28 — 集成业务方修订版 原始定义（证据线索汇总 / 手机号&企业信息分析 / 极光数据）

- **背景**：V0.6 基线跑批途中发现工作流有问题，真因不在代码——**当初提供的 原始定义 是错误版本，
  含一个业务 BUG**（原有平台已修复）。业务方随后提供修订版，只含 3 份改动文件，
  本次把它们集成进 `src/customer_profile/workflows/`。
- **新增子流程「手机号&企业信息分析」**（`phone_company_analysis`，8 节点 / 8 边）：原先散在
  「证据线索汇总」里的四节点（取关联企业 → 拆运营商/企业 → 两个 LLM）整体迁入，成为独立子流程。
  该图用分组聚合器（P3 口径，每组包 `{"output": 值}`），下游「聚合结果拆壳」节点取 `["output"]`；
  两个 LLM 的输出字段是 `text` 而非 `result`——与其它 llm 节点形态不同，照抄会静默取空。
- **「证据线索汇总」结构变化**：**24 节点 / 39 边 → 21 节点 / 35 边**，主图改用一个 `tool` 调用点
  （`1789983285382`）承接上述子流程。
- **两处功能性修复**（不只是结构搬移）：
  - 「客户数据聚合」模板新增 `第三方画像数据` 字段，绑定极光数据——此前**极光结果根本没有
    进聚合包**；
  - 极光数据子流程的 `data_source` 默认值由 `jiguang` 改为 `DatametInterestPoints`，上游同时
    改为显式传参同名值，旧值**取不到该渠道数据**。
- **台账基线随之更新**：`docs/history/ledger/` 的 5 份文件与 `docs/history/prompts/` 的 81 份评审副本
  均按新版原始定义 重新生成，计数 **19 / 293 / 324 → 20 / 298 / 328**。
  定义交叉核对用例、逐字符比对用例、`test_layout.py` 里的计数断言同步更新；
  台账提取脚本 的 `ASSERTED` 同步更新。
  差异逐项记于 `docs/history/ledger/differences.md` §1.7（W44–W48）。
- **修正一处绑定错误**（用户 2026-09-28 确认）：「客户数据聚合」模板里 `关联企业信息` 渲染的是
  `{{ phone_result }}`，而绑定表为它提供了 `company_result`——**企业分析结果被丢弃、该字段
  实际装的是手机号归属地分析**。旧版 原始定义 同样如此，非本轮引入。现改为 `{{ company_result }}`。
  提示词正文改动按规划 §6.5 须单独提出并获确认，本轮已取得确认。
  - **该修订登记为模板资源的有意差异**（模板导出工具 的 `TEMPLATE_OVERRIDES`）：
    原始定义侧未同步改，而 `--check` 断言包内正文与 原始定义逐字符一致，因此偏离必须显式登记，
    未登记的差异一律不放行。这是全项目唯一的模板正文有意差异，见 `docs/history/ledger/differences.md` W49。
- **测试**：522 passed / 13 skipped（较 V0.6 基线的 518 / 13 增加 4 项——新子流程模块与
  台账交叉核对用例）。

## 2026-09-24 — V0.6 数据基线与实验归档

- **需求**：为后续的工作流优化任务建立**数据基线**。此前工作流改了一处提示词或一个节点，
  「输出质量有没有崩」**无法回答**——没有任何历史结果可对照，只能靠人读一两份新结果拍脑袋。
  本版拿测试方给的 39 个虚拟手机号各跑一遍真实工作流，把最终结果与全部中间节点输出归档，
  供日后抽 2-3 个号码对比。
- **与既有「基线」区分**：项目里此前的「基线」一律指 原始定义 迁移台账（`docs/history/ledger/`）。
  本版是**运行结果**的基线，两回事。
- **测试号清单**（`test_phone_numbers.txt`，入库 39 行）：`scripts/collect_test_phones.py` 从
  `json_by_phone/` 递归提取。目录是解压产物，混有 macOS 的 `__MACOSX/` 与 `._` 前缀残留
  （21 个），且两批的层级不同（A 批多套一层），因此递归 + 前缀过滤是必需的。**异常（是
  `.json` 但文件名不是合法号码）单独报出而非静默跳过**——静默跳过会让清单悄悄少号码，
  而「少号码」与「号码本身不需要跑」在结果上无法区分。`--check` 随时校验清单与目录是否漂移。
- **基线脚本**（`scripts/run_baseline.py`）：把冒烟脚本的取向推广到批量。
  - **进程内执行，不走 HTTP API**：`Service.run` 直连调度器，绕过队列，因此不受
    `MAX_QUEUED_RUNS` 与 `RunSlotGate` 约束，也不会撞 429。
  - **4 路并发**（上限即 4）：39 个号码串行约 13.6 小时，4 并发约 4–5 小时。并发共享同一
    进程的出网闸门，故脚本内把 `MAX_CONCURRENT_REQUESTS` 与 `max_active_nodes` 一起提到 16
    （每股平均 4 个出网位）。**这两个值只写在脚本里，不落 `.env`**，生产 worker 不受影响。
  - **断点续跑默认开启**，判定依据是「产物存在且状态 succeeded」而不是「库里有行」——产物
    是价值，库是过程。每号码结束即原子落盘，中途中断不丢已完成的结果。
  - **超时/失败也导出**：一次运行被中断时已完成的那半棵树比没有强，且能看到卡在哪。不做
    脚本层自动重试——LLM 的 3 次尝试与 HTTP 的 3 次重试已在执行器内部，外层再加只会在同一
    错误上重复烧掉一整个号码的时间。
- **三处硬编码的配置**（都是「看起来正常、实际改坏状态」的类型）：
  - `writeback_enabled=False`：仅跑数据、不调回写接口。
  - `replay_mode="off"`：基线必须是真实链路，回放模式下工作流会静默「跑通」。
  - `interrupt_on_start=False`：该开关为真时 `build_service` 会把库里残留的 `running` 全标成
    中断。指向共享库而恰有 worker 在跑时，脚本一启动就会改坏对方**正在执行**的运行。
- **批次独立 SQLite**（`experiments/batches/<id>/trace.db`）：不复用
  `data/customer_profile.db`。后者被定义为「可重建」，与基线须长期留存冲突；且长时间大量
  写入会与 worker 抢写者锁，而留痕层是 `fail_soft`——超时不会让运行失败，只会**静默丢留痕**。
  独立库还让归档退化成一条复制命令。决策与代价见 `docs/adr/0003`。
- **入参恒为 `batch_id=""`**：该字段会一路透传进 `result1`，模板要求它是固定值。把实验批次号
  填进去会让结果因实验元数据而与基线不同，直接污染对比。批次身份只存在于目录名与
  `manifest.json`；给运行打标记用 `business_ref`（纯元数据，不进结果）。
- **导出必须递归**（`scripts/baseline_artifacts.py`）：实测主入口一次运行是一棵 18 个运行、
  250 条节点执行、深度 3 的树，只导根运行会丢掉绝大部分中间结果。定位键统一用
  `(node_id, call_path)`——迭代类工作流的同一个静态节点每轮重跑，只按 `node_id` 会混轮次。
  单条节点输出超过 512 KB 替换为摘要（类型/字节数/sha256/前 200 字符），需要原文按定位键回
  库取。落盘是原子的（临时文件 + `os.replace`），全程显式 UTF-8。
- **新文件**：`test_phone_numbers.txt`、`scripts/collect_test_phones.py`、
  `scripts/baseline_artifacts.py`、`scripts/run_baseline.py`、`tests/test_phone_collection.py`、
  `tests/test_baseline_artifacts.py`、`tests/test_baseline_runner.py`、
  `docs/runbooks/基线实验.md`、`docs/adr/0003-基线实验用批次独立-sqlite-与冻结规则.md`。
  `.gitignore` 新增 `experiments/` 与 `json_by_phone/`（后者正文含真实客户标签，此前处于
  「未跟踪也未忽略」的随时可误提交状态）。
- **测试**：484 passed / 13 skipped（基线 438，增量 46）。新用例覆盖：跨两层目录递归提取、
  macOS 残留排除、异常不静默、清单读写与 UTF-8 无 BOM、`--check` 漂移与异常判定；导出管线的
  三方**递归到孙运行**（自造三层嵌套，只导一层会被这条断言抓住）、`call_path` 保留、截断后
  sha256 对应原文、中文往返一致、原子落盘无临时文件残留、深度上限被截断时记进
  `missing_runs` 而非静默丢弃；批次 ID 形状、续跑判定（含损坏产物按未完成处理）、配置白名单
  不含凭据、批次库为绝对路径、超时仍导出、导出失败如实记录、汇总表给出补跑命令。
- **测试写法上踩到一处**：测试里构造 `NodeRuntime` 时漏了 `recorder`。`SubWorkflowRunner` 在
  没有 `scheduler_factory` 时按 `runtime.recorder` 新建子调度器——漏了这一步，子流程**照样
  跑通**（父图 succeeded、输出也透传），但子运行**一次都不落库**。正是导出管线最该抓的那类
  静默故障，被「递归到孙运行」的断言逮住了。
- **本次不做**：自动对比工具。用户确认「工具以后再说」；当前是人工比对两批的 `result1` 与
  节点状态。产物格式（每号码一份 JSON + 批次级 `summary.json`）已按便于机器对比设计。
- **真实冒烟中抓到并修掉一个缺陷**（单号码真跑之后暴露，不在设计阶段能看出来）：
  「无待跑号码」这条分支此前会**无条件重写 `manifest.json`**，把 `created_at` 重置为本次
  调用时刻，并丢掉 `finished_at` / `bytes_written` / `leftover_temp_files`。触发条件是
  「对一个已完成的批次重跑同一个 `--only`」，也就是手滑重跑——一次空转就能把批次的
  manifest 改坏，而 `summary.json` 当时是对的，只有 manifest 被悄悄改掉，症状隐蔽。
  - 修法：把 manifest 的首次写入移到空队列判定**之后**，该分支改为**零写入**（追加式日志
    是唯一例外）；补跑时从既有 manifest 继承开批时刻与历次入队记录
    （`load_manifest` + `runs_this_invocation` 累计），`bytes_written` 改按整目录累计。
  - 同批修掉两处相关问题：汇总从**磁盘上的产物**重建（`load_manifest` / `merge_results` /
    `load_batch_results`），因此补跑 2 个号码时，先前跑完的 37 个不会从汇总表里消失；
    `phones_count` / `phones_sha256` 改取自 `phones.txt` 快照，清单日后漂移不会让两者对不上。
  - 测试：新增 no-op 路径下「批次文件字节级不变」的回归用例（subprocess 走真实脚本），
    覆盖此前完全没被覆盖的那条分支。
  - 已用 `smoke-1` 直接验证：重跑已完成的号码后，`summary.json` / `summary.md` / `manifest.json`
    三者字节不变。
- **实测数字修正**：单号码真实运行 **29.2 分钟**（1,754,415 ms），比规划里引用的 21 分钟
  （1282 s）长约 40%。全量 39 个号码的时间估算需按此上调。
- **双号码并发冒烟暴露并修复一个执行引擎缺陷**（真实数据才走到，合成用例此前恰好绕开）：
  - **现象**：`test_drive_audio` 子流程的 iteration 节点抛
    `RuntimeError: run_iteration_body_node 必须在一次运行内调用`，进而使
    `evidence_subagent` 与主入口整条失败。`13430349943` 因此失败（10.4 分钟即终止，
    198 节点 / 11 次出网，远少于成功时的 252 / 29）。
  - **机制**（两处共享状态叠加）：① `Scheduler.__init__` **无条件**把
    `self.run_iteration_body_node` 写到**共享的** `runtime.iteration_node_runner` 槽位上，
    而一个进程内所有调度器共用同一个 `NodeRuntime`，后构造的直接覆盖先前的；
    ② `SubWorkflowRunner.__call__` 在未传 `scheduler_factory` 时**每次调用都 new 一个
    Scheduler**（为的是「子运行沿用父运行的留痕器」）。于是子运行一构造就抢走槽位，
    而 `run_iteration_body_node` 读的是**每实例单槽**的 `_current_record`——父图随后执行
    iteration 节点时，取到的 runner 属于早已结束的子调度器，其记录句柄已清空。
  - **真实触发路径是并发争抢，不是执行顺序**（此处修正过一版判断）：`evidence_subagent`
    的 **13 个 tool 节点共享同一前置节点**，即 13 路并发 fan-out，每个子运行各构造一个
    `Scheduler`、各自覆盖共享槽位。`test_drive_audio` 的 iteration 节点在执行开始时读一次
    槽位，取到的可能是**另一个仍在运行的**子调度器；第 0 项因此能跑完——但它把执行记录
    追加到了**别人的** `_RunRecord` 上（留痕错位，不报错）；那个外来子运行随后结束、把
    自己的记录句柄清空，第 1 项才抛错。这解释了报错里「已完成 1 项」这个反常细节。
  - **根本缺陷是共享可变状态**，两种形状都能命中：`tool` 与 `iteration` 同图且 `tool` 先执行
    （合成用例的确定性复现，修复前必红），以及上述并发争抢（真实链路）。触发点是「运行期间
    又构造了一个 Scheduler」，而不在于两个节点是否互为兄弟——`test_drive_audio` 里二者
    实为同一个 `if-else` 的**互斥分支**（true / false），不会互相竞态。
  - **`smoke-1` 未暴露它**，是因为那次 `test_drive_audio` 的 iteration 节点全部 `skipped`
    （上游 if-else 分支未走），**从未真正执行过循环体**——一次「成功」的基线其实没有覆盖
    这条路径。
  - **爆炸半径**：`test_drive_audio` 与 `outbound_call_audio` 两个工作流（形状相同，
    同图有 tool + iteration）。二者都挂在 `evidence_subagent` 的取证分支下，因此凡是走
    试驾 / 外呼取证分支的手机号都会失败。
  - **潜在的第二处**：`_current_record` 单槽在并发下不崩但**会记错**——同一 Scheduler 上
    并发运行的循环体执行记录会被追加到别的运行的 `executions` 上（留痕错位，无报错）。
    V0.6 基线脚本用 4 并发，这条路必须成立。
  - **修法**（`src/customer_profile/execution/scheduler.py`）：`_current_record` 单槽改为
    按 `run_id` 索引的 `_records`；新增 `_end_run`（取消与正常结束两条路径共用，清理
    context / record / route）；新增挂在共享 `runtime.extra` 上的 `iteration_routes`
    （`run_id → 属主调度器`）；`run_iteration_body_node` 改为**自路由**——按
    `item_ctx.run_id` 查出属主并由属主执行。这样「谁被挂在共享槽位上」不再重要。
  - **测试**：新增 `tests/test_iteration_nesting.py` 三条用例——同图 tool 先于 iteration
    （**修复前为红**，报错与真实冒烟逐字一致）、循环体内含 tool（钉住「触发点在读取时机，
    而非循环体内部」这个结论）、4 路并发不互相偷换入口。全部不出网。
  - **测试**：493 passed / 13 skipped（上一轮 490，增量 3）。
- **同一次真实验证又暴露并修复第二个缺陷：取消运行后节点任务被抛下**（`src/customer_profile/execution/scheduler.py`）：
  - **现象**：带单号码超时的批量脚本用 `asyncio.wait_for` 包住 `Scheduler.run`，超时取消后
    关库，日志刷出上百行 `[留痕] node_finished 写入失败：RuntimeError: Store 尚未 connect()
    （或已关闭）`。取消没有真正停住工作。
  - **机制**：`_execute_graph` 里的节点是 `asyncio.create_task` 出来的，而
    **`asyncio.wait` 被取消时不会连带取消它等待的任务**。于是那批节点继续跑完：既产生
    无谓的外部调用（**真实计费**），又把留痕写进已关闭的连接而**静默**失败——留痕层是
    `fail_soft`，只打 stderr 不报错，这正是它没被更早发现的原因。
  - **修法**：`_execute_graph` 的推进循环包进 `try/finally`，新增 `_reap_running_nodes`
    ——取消所有未完成的节点任务并 `gather` 回收；外层若在取消过程中再收到取消信号则吞掉，
    让原有的取消继续传播，不顶替调用方真正关心的异常。取消一次运行，它名下的节点就该一起停。
  - **测试**：新增 `tests/test_scheduler_reap.py` 两条用例——取消后不得有存活节点任务；
    关库后不得再出现「已关闭」写入告警（后者是前者的可观测症状）。**撤掉修复时该用例转红**
    （实测抓到 `node:syn_timer_slow`、`node:syn_timer_fast` 两个游离任务），复原后转绿——
    避免写出一条「碰巧通过」的用例。
- **同时修正脚本侧一处取值**：`--per-phone-timeout` 默认从 2700 s（45 分钟）提到 5400 s
  （90 分钟）。实测依据：不走录音取证分支的号码约 26–29 分钟，而走该分支的号码仅 iteration
  循环体两个 tool 就 298 s + 1388 s（28.1 分钟），循环体之后的节点照跑。45 分钟会把本来
  能成功的号码误判成失败——实测有一次恰好在 2700 s 被取消，当时仍在跑后续 LLM 节点。
- **录音转写归档与复用**：录音识别是链路里最慢的一段（单条可达 20–30 分钟），且与被优化的
  工作流逻辑无关——同一段录音、同一组参数，结果必然相同。因此把识别结果归档，重跑时直接复用。
  - 落点在 AUC 适配层（`execution/auc.py`）：命中归档时提交与查询**都不出网**，直接合成两个
    节点的内网契约，下游两个 code 节点与提示词看到的形状与真实路径**完全一致**。
  - 归档的是云服务**响应体原文**（完整 utterances）而非下游的六字段数组，因此复用路径与真实
    路径走**同一个解析函数**，解析规则变化时复用路径自动跟随。此前原文在 `_do_query` 里被
    就地解析后丢弃，实测四个批次的 7 次 `query_analyze` 留痕里都没有留存。
  - **键含识别参数**（`sha256(录音URL + 识别参数)`）：改 `model_name` / `model_version` /
    `enable_ddc` 等参数后旧条目自动未命中，不会拿旧参数的转写冒充新配置的结果。录音直链是
    稳定无签名的 TOS 链接，因此按 URL 做键在多次运行之间有效；URL 形态若变，命中数会掉到 0，
    这正是判据。
  - 归档目录 `experiments/transcript_cache/`，**跨批次共用**且与批次同级，因此 runbook §7 的
    「整目录复制到仓库外」一次带走两者。`manifest.json` 与日志记命中/未命中/写入计数。
  - **读与写是两个独立开关**（`--no-transcript-cache` / `--no-cache-write`）：不复用与不归档
    是两件事。复用时 LLM 节点照常真实调用——优化工作流改的通常正是这些节点。
  - 「静音音频是业务结论不是失败」这条判定在复用路径上保持不变；坏条目只当未命中，退回真实
    识别，不让整批号码失败。
- **测试**：495 passed / 13 skipped（最终，含 `test_scheduler_reap.py` 的 2 条）。
- **同一次真实验证第三次暴露的问题是模型侧的既有病理，未改动代码**：`fix-verify-2` 的真实
  重跑没有成功，但**两个已修缺陷都通过**——迭代循环体 `/0/0/iter:.../0` 成功、`/0/0/iter:.../1`
  执行到内部 LLM 节点才失败；全程 0 次 `RuntimeError: run_iteration_body_node`、0 行
  `Store 尚未 connect()` 告警、`update-profile` 尝试数为 0。真正的失败点是
  `1773135369428`（「LLM 识别结果校验」，`qwen3.8-flash`）在 `/0/0/1` 上 **3 次尝试全部
  `empty_text`**（设计行为：空响应属可重试异常，三次耗尽才失败）。**判据是耗时**：六个空响应
  全部停在 590.3–592.1 s，而提示词从 266 token 到 32408 token 差两个数量级、耗时只差 1.8 s
  —— 属时间闸门截断而非生成结束；`http_read_timeout` 为 1800 s 且响应 HTTP 200，故不是我们的
  超时。`reasoning_content` 未落库，「思维链吃满、`content` 为空」仍是未证实的推测。该病理
  **此前就存在**（`smoke-1`、`fix-verify` 留痕中均有），只是因为更早那一步崩溃而未暴露。
  处置：未改代码，写进 `docs/runbooks/基线实验.md` §9。

## 2026-09-24 — V0.5.4 中断处理、人工出口与运维文档

- **需求**：补齐 V0.5 原定「生产运行保障」的第四项——重启中断处理（交人工）与运维说明。
  本版是「做前三版」这一范围（用户 2026-09-24 决定）的收口版。
- **新增人工出口 `POST /runs/{run_id}/abandon`**：把 `interrupted` 运行显式终结为
  `cancelled`。此前 v0.5.2 已把「残留 running 标为 interrupted 并保留已完成结果」做对，
  但**没有任何出口**——这些运行会永远留在列表里，看不出是「待处理」还是「已处理」。
- **用条件 UPDATE 而不是「先查后写」**：先查后写之间的窗口里，该运行可能已被另一个
  worker 重新领取并开始执行，此时把它改成 cancelled 会与一个正在跑的运行冲突——界面上
  看不到任何在跑的东西，但它确实在写外部接口。**只接受 `interrupted`**，其它状态返回 409。
- **与 `/cancel` 的语义区分**（写进了端点 docstring 与 runbook）：`/cancel` 是「停止一条
  正在跑的运行」，`/abandon` 是「结掉一条已经停掉的运行」。两者混用会出现「我点了取消但
  任务还在跑」这种最难查的状态。
- **终结时保留原中断原因**（`error` 追加而非覆盖）并记 `finished_at_ms`：中断原因才是
  「为什么停下」的答案，被覆盖掉的话人工回看时无从判断。
- **运维文档首次落地**（此前 `docs/architecture.md` 是 0 字节，`runbooks/`、`adr/` 三目录
  全空，而 `docs/README.md` 早已为它们定义了收录标准）：
  - `docs/architecture.md`：系统边界、双进程形态与队列/租约、四层留痕、并发与限额的四层
    闸门、外部依赖、SQLite 的三条硬约束、明确的「不做」清单。
  - `docs/runbooks/部署.md`：安装、两种部署形态（单机内联 / API 与 worker 分离）、
    数据库约束（**库不可放网络挂载**，WAL 依赖 `-shm`）、升级即补列、停机语义、提交与运行台。
  - `docs/runbooks/故障定位.md`：按现象组织 9 条——任务停在 queued、卡在 running、
    interrupted 堆积、`database is locked`、限流与慢的区分、回写「成功」却没写、
    `duplicate column name`、限额不可整除、前端 404。
  - `docs/adr/0001`、`0002`：队列为什么用 SQLite 表而非 Redis/Celery（含被否方案与代价）、
    配额为什么显式分配而非共享。
- **新文件**：`docs/adr/0001-队列用-sqlite-表而非-redis.md`、
  `docs/adr/0002-配额用显式分配而非共享.md`、`docs/runbooks/部署.md`、
  `docs/runbooks/故障定位.md`；`docs/architecture.md` 由 0 字节首次填充。
- **测试**：438 passed / 13 skipped（基线 430，增量 8，即 `tests/test_interrupt.py`）。
  新用例覆盖：abandon 只接受 interrupted（running/succeeded 均不动）、重复终结不二次生效、
  终结留时刻与原因、**不覆盖原中断原因**、中断运行保留已完成节点结果、三个 API 层用例
  （409 / 200 / 404）。
- **测试写法上踩到一处**：API 层用例起初拿一个已在别处的 `Store` 塞进服务，而服务 lifespan
  退出时会关掉它自己持有的那个——症状是运行期报「Store 尚未 connect()」，且看不出是谁关的。
  改为让服务按同一文件路径自建连接后成立。
- **本版未交付（显式记录，不伪装完成）**：
  1. **「回写失败复用已保存结果重试」**——这是原规划 §3 V0.5 验收标准之一，依赖幂等键与
     回写协议，随幂等回写一并**顺延至 V0.5.5**。
  2. **中断运行的自动续跑**——刻意不做（差异 W28），重领保留为显式动作。
  3. 真实模型冒烟未在本版复跑（用户 2026-09-24 明确「按实际需求做，不再必须」）。
  4. `MAX_QUEUED_RUNS=50` 与 `MAX_ACTIVE_RUNS=4` 仍是保守值，未压测。

## 2026-09-24 — V0.5.3 全局配额与限额

- **需求**：补齐 V0.5 原定「生产运行保障」的第三项——全局请求限额 + 模型侧 RPM/TPM 约束。
  本轮按用户决定只做前三版，幂等回写与影子运行仍顺延。
- **`LLM_RPM_LIMIT` / `LLM_TPM_LIMIT` 从死配置改为真正生效**（差异 W29）。两者自 V0.2
  起就声明在 `Settings` 里、`.env.example` 写着「V0.5 起生效」，但**全仓无任何读取方**
  ——与 `MAX_ACTIVE_RUNS` 在 v0.5.1 之前是同一形态。本机 `.env` 已配
  `LLM_RPM_LIMIT=10000` / `LLM_TPM_LIMIT=500000`，因此**下次真实运行就会开始受限**。
- **用令牌桶而非固定窗口**（差异 W30）：固定窗口在边界会瞬时放行 2× 配额，而本项目一次
  运行 32 次 LLM 调用、历时二十余分钟，恰好会撞在边界上。容量由 `LLM_RPM_BURST` /
  `LLM_TPM_BURST` 配置（置空取对应 limit）。
- **接线点在「每次真实出网」那一层**（差异 W31）。这是本版最容易做错的地方：放在
  `llm_call` 的循环外层会让 3 次尝试只记 1 个请求，配额被**系统性低估 3 倍**。
- **TPM 是「预扣 + 校正」**（差异 W32）：出网前按 prompt 长度保守估算并预扣（不预扣就
  无从在出网前限制），拿到响应后按真实 `usage` 追扣或退还（不校正则偏差在长会话下持续
  累积）。`usage` 缺失时**不校正**——「未知」不能当成 0 退还。
- **回放不消耗额度**（差异 W33）：回放请求没有真的发出去，让它排队只会让全量测试凭空
  变慢，还会掩盖真实的限额行为。
- **多 worker 是显式分配**（差异 W35）：`LLM_RPM_LIMIT` 的语义是全局额度，按
  `WORKER_REPLICAS` 等分给每进程；**不可整除时启动即报错**，避免「RPM=60 配 4 个
  worker 实际打到 240」这类静默超发。不做跨进程共享配额（差异 W36，需中间件，与
  约束 7 冲突）。
- **并发上限与速率限额并存**（差异 W37）。顺序刻意是「先速率、后并发」：速率等待可达
  数十秒，若先占住并发名额等待，会把其它本可以出网的请求一起堵住。
- **新文件**：`src/customer_profile/execution/quota.py`（`TokenBucket` / `LlmQuota` /
  `estimate_prompt_tokens`）。
- **测试**：430 passed / 13 skipped（基线 412，增量 18，即 `tests/test_quota.py`）。用
  **假时钟 + 假 sleeper** 推进令牌桶，不真 sleep——RPM 的等待动辄几十秒。新用例覆盖：
  容量内不等待、等待时长精确、空闲不超容、单次超容报错、未配置完全不干扰、只配一个
  维度不激活另一个、TPM 预扣与双向校正、usage 缺失不退还、图片计入估算、按 worker 数
  等分、不可整除启动报错、回放不耗额度。
- **测试写法上踩到一处**：`Settings` 会自动读仓库根 `.env`，而本机已配 RPM/TPM，导致
  「未配置时不生效」的断言被真实配置污染。测试 helper 改为**显式置空**限额后才成立
  ——这一条对后续写「未配置」类断言同样适用。

## 2026-09-24 — V0.5.2 持久化队列与独立 worker

- **需求**：补齐 V0.5 原定「生产运行保障」的前三项——持久化任务队列 + 独立 worker、
  重启中断处理、全局配额。**范围决定（用户）**：本轮只做前三版，幂等回写与影子运行
  顺延（`docs/history/V0.5工作台任务触发入口.md` 的 W19 之后续作）。
- **队列复用 `workflow_runs` 表，不新建队列表**（用户决定，差异 W26）。入队即写一行
  `status='queued'`——这个状态从 V0.2 就声明了，**此前全仓无写入方**，只作缺省值。
  现在它终于成为「先持久化入队再返回 run_id」这条验收标准的载体。
- **原子领取用单条 `UPDATE ... WHERE status='queued'` + `RETURNING`**（SQLite 3.50）。
  比「先 SELECT 再 UPDATE」安全：后者之间的窗口会让两个 worker 领到同一个 run_id
  并把它跑两遍。实测本机 SQLite 3.50.4 支持 `RETURNING`。
- **修正一处与验收标准直接冲突的既有行为**（差异 W24）：`mark_running_as_interrupted`
  此前把 `running` 与 `queued` 一并标为中断——worker 一重启，所有还没开始的任务都变成
  需要人工处理的中断态，「重启不丢任务」随之不成立。现在只标 `running`。
  `tests/test_persistence.py` 的那条用例相应改为断言 `queued` **存活**。
- **租约过期不自动重领**（差异 W28）。租约与心跳已实现（崩溃后可回收），但启动路径
  刻意不自动重跑：重领会把跑到一半的运行**整条重跑**，而根目录文档明确「不做任意节点
  断点自动续跑」，且 worker 停机不证明外部副作用未发生。`requeue_expired_leases`
  保留为显式动作，留给后续版本的人工处理入口。
- **`POST /runs` 的 429 触发条件改为队列深度**（差异 W23，新增 `MAX_QUEUED_RUNS`）。
  `RunSlotGate` 保留但只作用于进程内路径（`Service.submit_local`）——CLI 与单测仍走它。
- **保留 `INLINE_WORKER`（缺省 true）**（差异 W27）：API 进程同时充当一个消费者。
  五个既有测试文件依赖「POST /runs 后立刻能查到并跑完」的契约，单机部署也不应被迫
  另起进程；生产按 §4 分离部署时置 false 即可。
- **新增最小补列机制**（差异 W25）：`CREATE TABLE IF NOT EXISTS` 对已存在的表不会补列，
  而 `data/customer_profile.db` 已在磁盘上。读 `PRAGMA table_info` 后按需 `ALTER TABLE
  ADD COLUMN`，**不引入 alembic**（规划 §5 约束 7）。另补 `PRAGMA busy_timeout`——
  API 与 worker 双进程共库后，不设它时写相遇会直接报 `database is locked`。
- **新文件**：`src/customer_profile/worker.py`（`python -m customer_profile.worker`）。
- **测试**：412 passed / 13 skipped（基线 398，增量 14，即 `tests/test_worker_queue.py`）。
  新增用例覆盖：入队即落库（无消费者参与）、两 Store 并发领取只成功一次、子运行不参与
  排队、租约过期回收、队深上限拒绝且不留半条记录、`QueueFull`→429、旧库补列后旧数据
  完好、双连接并发写不抛锁错、worker 单轮执行、`INLINE_WORKER=false` 时不自动执行。

## 2026-09-24 — V0.5.1 工作台任务触发入口（V0.5 开工）

- **需求**：在 V0.4 只读运行台上增加任务触发入口——输入 `phone_number` 提交任务，
  就地观察完整运行过程与结果。**范围决定（用户）**：V0.5 只做触发入口，迁移规划里
  原定的「生产运行保障」（持久化队列、独立 worker、全局配额、幂等回写、影子运行）
  整体后移（差异 W19）。
- **盘点后有意外收获：缺口比预想的小。** `POST /runs` 早在 V0.2 就存在，V0.4 明确
  「不改动」它（不是删掉），且已满足「先落库再返回 run_id」。过程展示也齐备
  （`/runs/{id}/graph` + 节点尝试 + 子运行下钻 + 时间线），`RunDetail` 已有运行中 2s 轮询。
  **真缺口只有两处**：前端没有任何写方法，以及 `MAX_ACTIVE_RUNS` 是死配置。
  因此本次**不新增提交端点**。
- **`MAX_ACTIVE_RUNS` 由死配置改为生效**（差异 W20）。它此前只在 `settings.py` 声明、
  `.env.example` 写了语义、`.env` 里配了值，而**全仓无任何读取方**——唯一真正生效的并发
  限制是 `Scheduler._active_slot`（`MAX_ACTIVE_NODES`，限制一次运行**内部**的节点数）。
  新增 `runner.RunSlotGate`，装在**服务层**（对 CLI 与 API 同样生效），满则抛
  `RunSlotUnavailable` → API 翻成 **HTTP 429**，**不排队**：排队会让调用方拿到 run_id
  却迟迟不开始，看上去像提交失败。
  - 用**计数器而非 `Semaphore`**：申请严格非阻塞（满了立刻抛），而 `Semaphore` 没有
    非阻塞 `acquire`，用它就得去动 `_value` 私有属性。
  - **子运行不占名额**：它们由 `SubWorkflowRunner` 直接调 `Scheduler.run`，不走
    `Service.submit`——主入口一次扇出十几个子运行，若子运行也申请名额，上限一低就自锁。
  - 名额在 `add_done_callback` 里释放；`_active` 对多余释放**直接忽略**，掉到负数会让
    闸门永久多放行若干个运行，比少放行危险。
- **手机号提交前校验**（差异 W21）：此前**无任何前置校验**，`start` 节点的必填性在执行期
  才检查，于是少传或传错会变成一个**失败的运行**而非可解释的 4xx。现在主入口的
  `phone_number` 按 `^1[3-9]\d{9}$` 校验，失败返回 422。**只对主入口生效**——其它工作流
  （录音类要 `customer_data` / `data_source`）入参契约不同，一并约束会挡掉合法调用。
  手机号还会进 `business_ref` 与下游 HTTP 路径（`/data/history/{phone}`），
  空串或带斜杠的值会在外部服务上变成难以归因的 404。
- **新增只读端点 `GET /runtime`**（差异 W22）：报出回放模式、**回写开关**、模型名与并发上限，
  **不含任何凭据**（由测试断言）。存在的理由很具体：`WRITEBACK_ENABLED=false` 时回写节点
  只产出「已跳过」的结果，**而运行仍显示 succeeded**。不在界面上显式标出，用户会把「成功」
  读成「画像已写进生产库」。这正是 `smoke_run.py` 在报告里记 `writeback_enabled` 的理由，
  只是那份记录在 CLI 里，运行台上看不到。
- **前端**：`api/client.ts` 新增 `postJson` / `submitRun` / `runtime`；新增
  `components/SubmitRun.tsx`（手机号 + 批次号，提交中禁用按钮，错误就地显示，并显示运行态
  面板、回写关闭时明确标注）；`App.tsx` 新增「提交任务」标签页，提交成功后直接落到该运行的
  详情页开始轮询。`business_ref` 取手机号——子运行会自动派生它，父运行不设会让列表里
  父运行显示 `—` 而子运行带着号码。
- **一处实现失误，记录备查**：改写 `runner.py` 时用「从 `class RunSlotGate:` 到
  `async def build_service(`」的范围做替换，而 `Service` 类定义在 `RunSlotGate` **之前**，
  于是整个 `Service` 被删掉（首次表现为 5 个测试文件 collection error）。从 `git show HEAD`
  取回时又漏了它上面的 `@dataclass` 装饰器（表现为 `TypeError: Service() takes no arguments`
  与 16 failed / 48 errors）。两处均已修正，最终 diff 只剩意图内的增补。
- 测试 **398 项**（25 个文件）全绿，13 skipped。新增 `tests/test_submit_gate.py`（12 项）。
  基线 386 项，增量 12 项，对得上。
- 版本号 `0.4.4` → `0.5.1`（`pyproject.toml` 与 FastAPI `version`）。
- 文档：新增 `docs/history/V0.5工作台任务触发入口.md`；`differences.md` 新增 W19–W22；
  `.env.example` §6 补记 `MAX_ACTIVE_RUNS` 的真实语义与 429 行为；
  `frontend/README.md` 不再称前端为「只读」。

## 2026-09-23 — V0.4.4 执行顺序时间线 + 静态资源托管（V0.4 收口）

- **修正一个更早的缺陷：节点 `started_at_ms` 从未落库，且语义是错的。**
  `scheduler._execute_node` 把它与 `duration_ms` 写成同一个表达式
  （`int((self._clock() - started) * 1000)`），即「距节点开始的耗时」而非时间戳；
  更隐蔽的是 `upsert_node_execution` 的 `ON CONFLICT ... DO UPDATE SET` 列表里**没有这一列**，
  `node_started` 插入时留空、收尾时也不覆盖，于是永远是 NULL。
  后果：没有墙钟起点就摆不到同一条时间轴上，也无法判断两个节点是否并发——
  而这正是 V0.4 的验收要求。修复：`started_ms = now_ms()` 取墙钟，
  `duration_ms` 继续用单调钟（墙钟会被系统时间调整影响、算出负时长），
  并把 `started_at_ms` / `queued_at_ms` 补进冲突更新列表。
- **新增 `frontend/src/components/RunTimeline.tsx`**：横向条形按真实起止时间绘制，
  与依赖图**并排切换**而非互相替代——图表达依赖，时间线表达并发，缺一就看不出
  「并行跑的」与「有依赖所以先后跑」的区别（迁移说明 §8.4）。标题如实报出重叠对数。
- **不加新端点**：`/runs/{id}/graph` 已带节点的 `started_at_ms` / `duration_ms` 与运行的
  起点，时间线直接渲染，省掉一次往返。
- **SERVE_UI 收口**：实测 `GET /` 返回页面、哈希资源 200、API 路由仍可达。
- 版本号 `0.4.1` → `0.4.4`。
- 测试 **385 项**（24 个文件）全绿，13 skipped。新增 `tests/test_node_timing.py`（5 项），
  含「起点是墙钟而非耗时」「起点不等于耗时」「时间戳落在运行窗口内」
  「重叠可被算出」四类判别断言。

## 2026-09-23 — V0.4.3 运行详情：节点状态叠加、尝试与子运行下钻

- **修正一个 V0.4 期间引入的缺陷：`attempt_count` 恒为 0。** 节点行上的这一列是
  详情页判断「要不要去拉尝试」的依据，而后端**从无生产代码写它**——`bump_attempt_count`
  只有自己的单测在调（`test_persistence.py:177`）。后果是数据在库里、界面永远空白，
  且不报任何错。修复采用**派生**而非自增：`node_finished` 收尾时用节点行自己的
  ``call_path`` 去数 `request_attempts`（`store.count_attempts` 新增按节点/调用路径过滤）。
  不用自增的原因很具体：`fork_iteration` 复制父上下文的 `call_path`，而迭代循环体节点
  在库里是按 `<父路径>/iter:<id>/<n>` 存的，按 `node_ref.call_path` 自增会更新到**零行**，
  静默失效。派生与来源表天然一致，键必然对得上。
- **新增 `GET /runs/{run_id}/nodes/{node_id}/attempts`**：节点级尝试，供详情页按需拉取。
  **不并进 `/graph`**：`response_text` 单条可达上万字符，主入口跑一次有 17 处 LLM 调用，
  全部内联会让首屏为了一张图加载好几 MB。`call_path` 可选传入以区分迭代各轮。
- **新增 `frontend/src/components/RunDetail.tsx`**：节点状态叠加到**当次定义快照**的图上
  （详情页从不请求当前定义的拓扑）；点节点看输入输出、错误、实际 requests 与
  每次尝试的响应/用量/错误；带 `sub_run_id` 的节点可进入子运行。
  - 长文本折叠显示（默认 1200 字符，可展开），避免一条 LLM 响应把面板撑成几屏。
  - 运行仍在跑时 2s 轮询，进入终态即停。
  - App 用**导航栈**而非单个 runId：主入口 → 画像生成 → 其子流程可逐层下钻，
    单值会让「返回」只能退回列表。
- **测试自身的缺陷一并修正**：新用例原先用 `next(...)` 扫生成器取带尝试的节点，
  没有匹配时抛 `StopIteration`，在协程里变成
  `RuntimeError: coroutine raised StopIteration`——报错信息完全指不到「计数没被写」
  这个真实原因。改为显式断言并打印各节点计数，且**先断言计数非零再验证内容**。
- 测试 **379 项**（23 个文件）全绿，13 skipped。新增 `tests/test_node_attempts.py`（7 项）。

## 2026-09-23 — V0.4.2 任务列表：筛选、排序与分页

- **`GET /runs` 扩展**：新增 `created_after_ms` / `created_before_ms` / `order_by`。
  返回值仍是**裸数组**——现有调用方与测试按数组取，改成 `{items,total}` 信封是破坏性
  变更，而分页需要的只是多一个数字。
- **新增 `GET /runs/count`**：与 `GET /runs` **共用同一组筛选条件**的总数。存储层把
  条件构造抽成 `_run_filter_clause`，列表与计数共用。两者条件一旦分叉，页数就会比实际
  数据多，翻到后半段是空页——那看起来像后端丢了数据，而不是页码越界。测试逐套筛选断言
  「列表条数 == 计数」。
  - 路由顺序是个真陷阱：`/runs/count` 必须注册在 `/runs/{run_id}` **之前**，否则会被
    当成 run_id 吃掉、返回 404。已由 `test_count_route_is_not_shadowed_by_run_detail` 钉住。
- **排序键走白名单**（`_RUN_ORDERINGS`），不拼接进 SQL；未知取值**回落到缺省**而不是
  报错——排序参数写错不该让整个列表打不开。注入形状的取值由
  `test_order_by_cannot_inject_sql` 验证只回落、不执行。
- **新增 `frontend/src/components/RunList.tsx`**：状态徽标、工作流、业务标识、开始时间、
  耗时、错误摘要（截断并带 title 悬浮全文）；筛选按工作流/状态/业务标识、含子运行开关、
  三种排序、分页器。筛选条件变化时**回到第 0 页**：留在第 5 页而新条件下只有 1 页会看到
  空表，那看起来像「查不到数据」而不是「页码越界」。
- 前端工作流下拉**取 `/workflows`**，不写死清单——写死会在新增工作流后静默漏掉它。
- 前端加标签页切换（工作流结构 / 任务列表），刻意不引入路由库：三个视图、无深链接需求。
- 测试 **372 项**（22 个文件）全绿，13 skipped。新增 `tests/test_run_filters.py`（11 项）。
  时间范围参数经独立探针验证：两侧计数与列表均自洽、同值区间返回空。

## 2026-09-23 — V0.4.1 只读运行台：布局与拓扑渲染

- **新增 `src/customer_profile/layout.py`**：按 `predecessors` 求最长路径分层的**纯函数**
  `layout()`，导出时按需计算、不落库。前端因此不需要自己的布局逻辑，「只有一份拓扑」
  这条约束落到了代码上。有 `NodeDef.coords` 的节点优先用其值（保留手工钉位置的能力）。
- **新增两个只读端点**：`GET /runs/{run_id}/graph`（当次版本定义快照 + 节点状态 + 子运行
  映射，一次返回，避免前端三次往返）与 `GET /definition-versions/{version_id}`。
  两者返回**同一种嵌套形状**（图在 `definition` 键下）——同一类东西两种形状会让前端
  出现两套取图代码。
- **`GET /workflows/{id}/topology` 增加 `layout` 字段**：`asdict()` 的 `coords` 保持原样
  （声明），算出来的展示坐标单独放在 `layout`（展示）。混在一起就分不清哪个是人定的。
- **新增 `frontend/`**（Vite + React + TypeScript + `@xyflow/react` 12）：结构视图带缩放、
  平移、点选、缩略图；节点按执行状态着色，**分支边按 `source_handle` 用不同颜色并在边上
  标注分支名**——看不出哪条边是哪一支时，分支图等于没画。构建产物 401.72 kB（gzip 127.10 kB）。
- **`frontend/dist/` 有意入库**（差异 W16）：运行台要能在未安装 node 的机器上直接启动。
  仓库根与 `frontend/` 两处 `.gitignore` 各加例外，缺一处产物就会被忽略。代价是改前端必须
  重新构建并提交产物，已写入 `frontend/README.md`。
- **`SERVE_UI` 开关**（差异 W17）：缺省 `false`；置 true 时把 `dist` 挂在根路径。置 true 但
  未构建时打印告警并跳过，**不**让服务启动失败——可选功能不该变成硬依赖。已实测挂载成功：
  `GET /` 返回页面，哈希资源 200，API 路由仍可达。
- **三处实现弯路，均由实测纠正**（差异 W18）：
  1. 设计初稿称「迭代工作流带环」，**实测 20 个定义全部无环**（`structural_order()` 逐个成功）。
     迭代节点引用其循环体，循环体不反向引用。
  2. 据此实现的第一版按 DFS 回边断环，在自造环图上把链**中间**切断，环上节点错位。
  3. 第二版改用有界松弛并声称「带环时退化为忽略回边」，实测环上节点被推到第 10 层——
     松弛在环上不收敛到有意义的层号。最终口径：**按 DAG 设计，不为环做特殊语义**，
     环普查由 `test_no_real_workflow_is_cyclic` 断言，引入环时直接失败。
  另修正一处**测试自身的缺陷**：环用例只断言相对顺序，退化实现（节点被推到第 10 层）
  照样通过；改为断言精确层号后才暴露。
- 测试 **361 项**（21 个文件）全绿，13 skipped。新增 `tests/test_layout.py`（12 项，含
  「最长路径而非最短路径」的判别用例，用最短路径会把汇合点排到扇出点左边、连线反向）与
  `tests/test_run_graph.py`（7 项）。
- 版本号 `0.2.0` → `0.4.1`（`pyproject.toml` 与 FastAPI `version`，此前落后于已交付的 V0.3）。

## 2026-09-23 — AUC 改接火山引擎云服务 + 录音链路打通 + 两个新缺陷修复

- **AUC 语音识别由内网自建服务改为火山引擎云服务**（用户 2026-09-23 决定，有意差异 W14）。
  原 `192.168.0.5:5005` 长期不可达，导致两个录音类工作流的真实链路一直无法验证。
  云服务的协议与 原始定义契约不同：异步提交 + **由调用方轮询**、处理状态在**响应头**
  `X-Api-Status-Code`、`task_id` 由调用方生成、鉴权需 `x-api-key` 与 `X-Api-Resource-Id`。
  新增 `src/customer_profile/execution/auc.py` 做协议适配，**把云形状翻译回内网契约**：
  `/submit_analyze` 产出 `{task_id, x_tt_logid}`、`/query_analyze` 产出
  `{auc_result, poll_count}`。因此**工作流的图、两个 HTTP 节点与两个逐字符取自 原始定义的
  code 节点一字未改**——迁移基线保持不动。`HttpClient.request` 在 `service="auc"` 时转交该客户端。
  凭据落 `.env`（`AUC_API_KEY` / `AUC_RESOURCE_ID`），定义与代码中不出现；
  原明文硬编码密钥的那份 handler 文件按用户决定**已在整合后删除**。
- **录音链路端到端打通（V0.3 收口证据）。** 先用真实外呼录音（TOS 直链，2.53 MB）单跑
  `outbound_call_audio` 验证链路：12 节点全成功、耗时 249 s，AUC 提交 1.4 s、轮询 9.1 s
  （识别本身很快，耗时主要在 LLM 校对 149 s），有效性判断走**「有效」分支**——
  这是该分支首次被真实执行。
- **主入口端到端跑通（V0.3 最终验收）。** `customer_profile_entry` 全链路 **succeeded**，
  总耗时 **1282 s**；3 个一级子流程全部成功（证据汇总 393 s、人设特征 239 s、
  画像生成&回写 650 s），其下 **13 路扇出全部成功**，`result1` 为 **27950 字字符串**
  （对外契约不变）。本次扇出中的 `outbound_call_audio` 耗时 236 s，产出 **7375 字**
  真实通话分析（含说话人身份推断表）。至此 V0.3「端到端打通」目标达成。
- **第二波真实冒烟又发现两个迁移缺陷**（测试全绿时全部漏过，均为「路径没被走过」而非「代码写错」）：
  1. **模板变量未绑定（10 个节点）**。`llm` 节点的模板正文引用 `{{#节点.字段#}}`，
     但节点未声明对应 `inputs`，渲染期变量表为空，报「模板变量未提供」。
     同一处遗漏经 3 个声明点影响 **10 个节点**（7 个截图类「多图片内容总结」+ 录音类两个节点）；
     截图类因视觉路径未被走到而一直未暴露。修复：3 个声明点补上绑定；新增
     `tests/test_llm_template_bindings.py`，**静态**断言「模板里的每个引用都被绑定覆盖」。
  2. **`expect_json` 把 dict 当文本**。执行器把解析后的 **dict** 放进 `text`，而两个消费方
     （`判断结果提取`、`Notes结果拆分`）都对 `text` 做 `json.loads`——传 dict 抛异常，
     被各自 `except` 吞掉并按缺省值返回，**静默给出错误业务结论**。实测同一份录音里
     模型正确判出 `is_valid: true`，code 节点却输出 `false`。此前「单方录音」样本恰好也应判
     无效，错误与正确无从区分，因此一直没被发现。修复：`text` 改为 `json.dumps(...)` 字符串
     （原平台 契约：llm 输出一律是文本），解析后的对象保留在 `parsed_json`；补 `import json`；
     新增 `tests/test_expect_json_output.py` 钉住该契约。**该缺陷亦影响主入口链路的
     `Notes结果拆分`**，不只是录音流程。
- **`scripts/probe_auc.py` 改为探云服务**：TCP → 空查询体（`45000000 cannot find task`
  即凭据与协议全对）→ 三档结论。实测退出码 0「可用」。不打印凭据值，只报「已配置 / 未配置」。
- 测试 **342 项**（19 个文件）全绿，13 skipped（需被 gitignore 的 原始定义 或数据条件的用例）。
- 文档：`docs/history/ledger/differences.md` 新增 W14；`v0.3_smoke_report.md` §2/§3/§6.1/§7 按实测更新
  （缺陷表扩到六项，AUC 缺口标记收口）；`V0.3全量迁移与端到端打通.md` §4.1 扩为五个缺陷、§6.2 收口；
  `V0迁移规划.md` §7.1 与三条 AUC 事实按实测刷新。

## 2026-09-23 — V0.3 收口：提示词评审副本重新导出 + AUC 可达性探测脚本

- **`docs/history/prompts/` 重新导出并补上一致性断言。** V0.1 生成的 81 份评审副本用了另一套命名
  （显示名转写 + 内容哈希 + `__llm` / `__template-transform` 类型后缀）与另一套抽取口径
  （保留 `system` / `user` 角色行、CRLF 换行），且取自更早的 原始定义修订；实测 **43/81 份**与
  包内资源内容不同。已按包内资源（经 模板一致性检查 与 原始定义逐字符一致）重新导出，
  命名统一为 `<slug>__<node_id>.txt`，两边**逐字节相同**；新增
  `test_review_copies_match_package_templates` 断言这一点，删掉了原先那条「把副本已失真写成
  xfail」的用例，并新增 `docs/history/prompts/README.md` 说明出处、再生顺序与失真史。
  测试 **312 项**（16 个文件）全绿，1 skipped（缺被 gitignore 的 原始定义），xfail 归零。
- **新增 `scripts/probe_auc.py`**（临时运维脚本）。AUC 服务 `192.168.0.5:5005` 在本机 TCP 超时，
  该脚本分四步给出可执行的裁决：TCP 连接（翻译 errno）→ HTTP 应答 → 对 `/submit_analyze` 与
  `/query_analyze` 发**故意无效**的请求体（不触发真实任务）→ 三档结论。当前实测：TCP 超时，
  退出码 2「不可达」。服务就绪后由业务方在本机复核，再放开读超时重跑录音类冒烟。

## 2026-09-23 — V0.3 全量工作流迁移与端到端打通完成

- **19 个 原有工作流全部迁移**（原始定义计 293 节点 / 324 边 → 18 个独立图 / 272 节点 / 300 边；差额逐项对应有意差异 W1/W2/W4）。新增 `executors_iteration.py`、`executors_aggregator.py`，补上迭代、聚合（含分组）、vision 三类节点语义。测试增至 **300 项**（15 个文件）。
- **迁移后的图与台账逐项一致**：定义交叉核对用例 覆盖全部 18 个工作流，比对节点集合、类型、边集合，并含一条**反向**检查（差异表里声明的每一项都必须真的发生）；逐字符比对用例 覆盖 47 个随工作流迁移的 code 节点正文。
- **三个只有真实运行才会暴露的缺陷**——它们在 300 项测试下全部通过，是真实模型冒烟才发现的：
  1. **分支门槛丢失（最严重）**。原始定义的边用 `sourceHandle` 区分 `if-else` 的两支，而迁移只记了「有这条边」，于是**两条分支同时执行**。截图类流程里表现为「文件不存在」与「文件存在」两条路一起跑，后者对空串做 JSON 解析直接抛 `JSONDecodeError`。修复：`NodeDef` 新增 `branch_gates`/`branch_from`，调度层按「前置实际走的分支」判定边是否失效，未走到的子树标为 `SKIPPED`（与故障性的 `BLOCKED` 区分开，不影响运行成败）；`edge_list()` 保留 `source_handle`。共 21 个节点补上注解。
  2. **`input_prompt` 提示词入参被忽略**。`gemini_retry_2_times` 子流程的入参名是 `input_prompt`，17 处调用点原样保留，而 llm 执行器只认 `prompt`，导致这 17 个节点全部以「没有提示词入参」失败。修复：执行器同时接受两者。
  3. **`@body_template` 调了不存在的 `ctx.get`**。请求体模板的引用替换写成 `ctx.get(*parse_selector(...))`，而 `get` 是 `NodeResult` 的方法、`RunContext` 上没有，AUC 提交节点抛 `AttributeError`。该路径此前**零测试覆盖**。改用 `ctx.resolve`，新增 `tests/test_body_template.py` 按「引用在字符串外/内」两种语义钉住。
  4. **图片 data URI 用了文本而非原始字节**。`_files_of` 从 `response.text` 取内容再编码 base64，而 httpx 会把二进制按文本解码——实测 PNG 头 `iVBORw0KGgo` 被改写成 `77+9UE5HDQoaCg`，视觉节点把坏 base64 发给模型，模型侧只回一个语焉不详的下载失败。修复：`HttpAttempt` 增加 `response_bytes`，`_files_of` 优先用字节编码；回放 fixture 用 `bytes_base64` 承载二进制；`record_fixture.py` 增加 `--record-binary`（图片可能含客户信息，默认不录）。
  5. **`required_<name>` 空值校验器**（V0.2 遗留）。`.env` 里 `MAX_ACTIVE_RUNS=` 为空串时 pydantic 解析 `int` 直接抛错，服务无法装配。
- **主入口端到端**：`tests/test_entry_end_to_end.py`（真实 tool 执行器 + 真实嵌套调度器，子流程换成同形状微缩图）与 `tests/test_contract.py`（入参口径与 `result1` 为字符串的对外契约）。`scripts/record_fixture.py` 支持按真实链路录制回放 fixture（含手机号与密钥脱敏，fixture 目录已加入 `.gitignore`）；`scripts/smoke_run.py` 强制 `WRITEBACK_ENABLED=false`，不产生生产副作用。
- **提示词外置**：81 份正文的运行期唯一来源是 `src/customer_profile/templates/`，模板一致性检查 逐字符比对 原始定义 通过。`docs/history/prompts/` 的评审副本当时已失真（43/81 份与包内资源不同，W11 声称的「两边一致」无测试支撑），**已于同日收口**，见上一条。
- 文档：新增 `docs/history/V0.3全量迁移与端到端打通.md`；`docs/history/ledger/differences.md` 升 v2（P1–P5 由「待确认」改为实测复核结论，其中 **P3 与 P4 的复核改变了实现口径**）；`docs/history/V0迁移规划.md` §7 基线按实测刷新。

## 2026-10-19 — 商机等级改为独立定级环节（H/A/B/C/待识别）

- **新增独立定级链路**：`商机定级输入装配（代码）→ 商机等级评定（1 次 LLM）→ 商机定级收口（代码）`。
  这是业务专家《客户商机等级定级规范》的落地：等级衡量「截至当前时点客户可实现的购车/成交潜力」，
  对销售的重要程度断档领先其它字段，因此单开一次调用、独占全部原始记录与 8–15 条推理链的输出篇幅。
- **阶段由代码判定**：有实质内容的试驾录音 → 转商机阶段，否则线索邀约阶段。客户口头说
  「准备试驾」不改变阶段（规范 §2.2）。
- **规则在代码层强制**：线索阶段最高 A（R04）；H/A 必须同时具备需求、时间、行动三类**可定位**
  证据，缺一类降为 B（R05）；每条证据回原文核对，定位不到的剔除并扣置信度；待识别与 C 级严格区分（R08）。
- **等级同源**：`ai_insight.purchase_intent_level` 与 `customer_overview.business_opp_level`
  共用同一份定级结论与推理链；L3 表达层不再自行推断等级。
- **回传契约过渡口径**：契约当前只认 A/B/C/D，故 `H` 回传为 `A`、「待识别」不回传。
  由 `PURCHASE_INTENT_LEVEL_PASSTHROUGH` 控制，后端/前端支持五值后置 `true` 即可切回。
  契约缺口记入 `遗留问题清单.md` L-008。
- 内部字段 `deal_level` 的中文名改为「商机等级」，枚举扩为 H/A/B/C/D/待识别（D 仅兼容历史批次）。

## 2026-09-23 — V0.2 最小执行器与留痕骨架完成

- 新增 `src/customer_profile/`（执行器主体）与 `tests/`（145 项测试，全绿）。跑通「定义 → 调度 → 执行 → 落库 → 查询」，载体为规划 §3 V0.2 指定的两个真实工作流 + 一个人造验证图。
- **执行层**：`execution/scheduler.py` 按依赖就绪启动，不做逐层屏障；**两级独立信号量**——`active_slot` 限制同时执行的节点数，`request_limiter` 限制真实出网请求数（LLM + HTTP 共用）。子运行不申请 `active_slot`，因此父节点等待子流程期间不会与子流程互等；`tests/test_scheduler.py::test_no_parent_child_deadlock_with_single_slot` 在名额为 1 的条件下证明这一点。
- **留痕四层落 SQLite**（`persistence/`）：工作流运行 / 节点执行 / 请求尝试 / 版本快照。子运行与父运行共表，用 `parent_run_id` + `call_path`（形如 `/0/2/1`）区分同一静态节点 ID 的多次动态执行。历史运行绑定当次定义版本，新版本不覆盖旧展示。
- **统一 LLM 调用层**（`execution/llm.py`）：17 处 `gemini_retry_2_times` 调用点归一为一次 `llm_call()`；重试下沉到该层，最多 3 次尝试（差异清单 M1 缺省口径）；异常判定沿用原 原始定义 判断节点的四条规则。因重试不再由图结构自然分开，**每次尝试独立写入 attempt 表**（规划 §6.6 点名的要求）。
- **固定响应回放**（`replay.py`）：LLM 按 `SHA256(model + messages)` 匹配，HTTP 按 `METHOD + 服务相对路径` 匹配；同一请求可排队多个响应以验证重试。另挂 `httpx.MockTransport` 兜底返回 599，确保回放模式下不可能出网。严格模式缺省，未命中即失败。
- **最小 API**（`api.py` + `main.py`）：提交返回 run_id、查询运行详情（含节点明细与每次尝试）、运行列表、拓扑导出、取消。提交等运行记录真正落库后才返回。
- **迁移两个真实工作流**：`（新）SubAgent - 猛士IT系统数据信息`（纯 code）与 `（新）SubAgent - 人工确认信息提取（生产环境）`（1 个 GET）。两者的 code 节点函数体与 原始定义原文**逐字符一致**，由 逐字符比对用例 用 AST 定位函数体后机械比对（只忽略函数名与 docstring）。
- 与 V0.1 台账的交叉核对落在 定义交叉核对用例：节点集合、节点类型、直接前置、边集合、code 节点入参绑定来源逐项比对 `node_ledger.json`。**只对已迁移的两个工作流断言**，不假装覆盖其余 16 个。
- 有意差异在测试中显式断言，避免日后被误当成缺陷：W4（`llm_model` 入参删除）、原始定义 明文密钥不进入任何定义。
- 一处实现修正需记录：`Store` 的连接**由专属工作线程独占**。先前用 `check_same_thread=False` 让连接被线程池任意线程共用，在 Windows + Python 3.14 上直接导致解释器段错误（不是可捕获异常）。改为单线程独占 + 命令队列后消失。

## 2026-09-23 — V0.1.1 遗留决策落地

- **提示词正文改为入库**（D8 决策反转）：由 gitignored 的 `data/prompts/` 迁至 `docs/history/prompts/`，共 81 份逐字符原文。理由是正文属后续版本的重要优化对象，需评审与追溯。入库前已扫描确认不含密钥形状字符串与手机号，台账复核脚本 新增对正文的持续校验。
- 删除仓库根目录的受控空壳文件 `Untitled`（内容仅一行 `旧定义目录`，误提交）。
- 密钥轮换时机由「V0.3 前」改为「V0 版本完成后手工执行」——V0 开发阶段不触达生产外网。
- `docs/architecture.md` 留待代码架构设计开发阶段编写；`docs/history/` 定位为各类设计文档的存放目录。
- `differences.md` 中 Q1/Q2/Q5–Q8 按用户指示改为「缺省采用」，右列缺省处理即为实现依据。**这不等于等价验证通过**；P1–P5、M1 仍待以 原有定义的实际行为复核。

## 2026-09-22 — V0.1.1 迁移盘点与基线固化完成

- 新增盘点脚本 `scripts/inventory/`（台账提取脚本 生成台账、台账复核脚本 复核），纳入 Git 管理，仅依赖 PyYAML。
- 台账落于 `docs/history/ledger/`：节点/边台账、变量引用台账、资源指纹清单、`tool_name` 映射表、工作流摘要。原始定义实测 **19 工作流 / 293 节点 / 324 边**，与规划文档的 16 项断言逐项一致。
- 新增 `docs/history/ledger/differences.md`：24 条差异逐项标注「已确认 / 待确认 / 缺省采用」，无空白项。
- 新增 `docs/history/ledger/secrets_report.md`：修正规划文档中「4 处明文密钥」的口径错误——实测 **5 个节点、2 个密钥值**；确认密钥从未进入 Git 历史。轮换时机已改为 V0 版本完成后手工执行（原「V0.3 前」建议作废）。
- 修正规划文档中 `phone_numbers.txt` 为「0 字节空文件」的过期描述（实测 20 个真实手机号，258 字节）。
- 提示词正文导出至 `data/prompts/`（81 份），台账只存 SHA256 与长度，不含正文。**【次日修订】** 该落盘位置已于 2026-09-23 按 D8 决策改为入库的 `docs/history/prompts/`，见下一条。

## 2026-09-22 — V0 规划确立工程优化规则

- 规划文档 `docs/history/V0迁移规划.md` 新增 §6「工程优化规则」，明确迁移产出的 Python 应是人写的普通代码，而非 节点图的逐节点转写。
- 关键收敛项（均属**有意差异**，须记入差异清单）：
  - 17 处 `Gemini（异常输出重试版）` 调用点统一简化为单次 `llm_call()`，重试由 `llm_call()` 内部承担。
  - 全项目 LLM 统一为 `.env` 中的单一模型，`temperature=0.5`、thinking 开启、不配置其他参数。
  - `llm_model` 工作流入参废弃，相关条件分支归一。
- 关闭原待决问题 Q4（`llm_model` 取值），新增 1 项待确认：重试次数口径（3 次尝试 vs 首次+3 次重试）。
- 记录一项新硬约束：统一模型**必须具备多模态能力**，因 12 个截图解析节点依赖图片输入。
- `.env.example` 的三模型配置收敛为单一 `LLM_MODEL`。
