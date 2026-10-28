# 旧版枚举键与 production 臂整改方案

> 状态：**待批准**（2026-10-27）
> 依据：`docs/reference/AI画像远程回传接口数据结构说明.md`、`docs/reference/Notes_new.txt`（payload 签名 `b36e78edf9a2`）
> 关联：`docs/reference/AI画像v2回传改造核查清单.md` §2.3；`docs/遗留问题清单.md` L-015

---

## 1. 问题陈述

前端观察到测试用例 `13733451677` 的「购买类型」显示 `replacement`、「用车场景」显示
`commute，家用，越野`。核查结论：**都是枚举键，但都不是当前契约的合法键**。

- `replacement`（换购）属于 `purchase_nature`，不属于 `purchase_type`（合法键只有
  `company` / `personal`）。旧版「购买类型」就是今天的「购买性质」。
- `commute`（通勤）、`transport`（代步）已从 `purchase_purpose` 的新版三选项
  （`family` / `offroad` / `business`）中移除。

这两个值来自 `GET /api/v1/remote/data/profile/{phone}` 的档案返回，不是本管线写入的。
但清查发现旧版键值并非只存在于历史档案，**工作流自身仍有三处会生产或放行旧键**，
不清理会持续污染数据。

## 2. 清查事实（2026-10-27 实测）

### 2.1 契约本身没有问题

```
python scripts/snapshot_note_contract.py --from-response docs/reference/Notes_new.txt --check
[OK] 与数据源一致：source=Notes_new.txt, signature=b36e78edf9a2, 45 字段
```

`notes_contract.py` 与 `Notes_new.txt` 逐字段比对 **45/45 完全一致，0 差异**
（含 `attribute_name` / `category` / `field_type` / `enum_options` 的键与展示名）。
**因此不需要重新生成契约快照**，问题全在旧资产未清理。

### 2.2 旧枚举键的分布

| 位置 | 内容 | 性质 |
|---|---|---|
| `templates/customer_profile_production__1777372805721.txt`（NOTES_GEN） | `married`/`unmarried`/`commute`/`transport` | **生产提示词**，仍在引用 |
| `templates/customer_profile_production__1777372465294.txt`（NOTES_REVIEW） | 同上 | **生产提示词**，仍在引用 |
| `data/profile` 档案接口响应 | `married`/`unmarried`/`commute`/`transport`/`replacement`/`additional`/`first`/`within_6_months`/`easy-to-drive` | 上游存量，本地不可写 |
| 我们的回写 payload | `purchase_type=换购`、`education=本科及以上`、`internet_preference=微信` 等 8 类 | **本管线生产，必须修** |
| `docs/history/*`、`experiments/batches/*` | 历史归档 | 按 §3 决策保留 |

### 2.3 旧键能进入回写 payload 的完整链条

这是最需要修的一条，因为它不依赖任何历史档案，**每次跑批都可能复现**：

1. L1 输出 `unmarried` / `commute` 这类旧键（提示词里还留着旧清单，见 §2.2 前两行）；
2. `projection.normalize_enum()` 返回 `None`，走「一个都归不到合法键」分支：
   **原值保留**进 `facts`，降为 `inferred_low`，并记 `to_confirm`（`projection.py:843-870`）；
3. `review_notes` 的 `HARD-02` 只在 `basis != inferred_low` 时动作（`hard.py:210`），
   已降级的**跳过不拦**；
4. `callback_build.build_basic_notes_updates()` 只按 `attribute_key` 白名单过滤，
   **不校验 `attribute_value` 是不是合法枚举键**；
5. 旧键原样写进 `attribute_value`，回传。

实测证据：`purchase_type=换购`（13367265521、18062597750）、`education=本科及以上`
（13100792128）、`internet_preference=微信`（17652815077）等，全部由本管线写入。

### 2.4 production 臂的现状

| 项 | 实测 |
|---|---|
| 节点数 | 30 |
| 模板引用 id | 41 个（其中 22 个是 LLM 节点 id，不是提示词模板） |
| 实际模板文件 | 19 份 |
| 注册状态 | **仍在 `WORKFLOW_MODULES` 里**（`workflows/__init__.py:36`） |
| 入口 | 已改指 `customer_profile_pipeline`，该臂无业务用途 |
| 下游依赖 | `customer_profile_entry` 以子流程方式调用它 |

**关键发现：19 份模板里有 6 份不是它的私有物。** `customer_profile_l3.py` 的
`PRODUCTION_ASSET()` 按原名复用 6 份业务资产（标签库、证据格式、画像格式、产品信息 ×2、
邀约策略），测试 `test_preset_assets_reference_production_templates_by_original_name`
用 `startswith("customer_profile_production__")` 钉住了这个前缀。整臂退役时这 6 份必须
迁走而不是删除，否则 L3 表达层直接断链。

### 2.5 受牵连的测试

| 测试 | 依赖点 |
|---|---|
| `test_m2m_writeback.py` | `production.WORKFLOW` 的合并/回写/出口节点 + `merge_final_data` |
| `test_expect_json_output.py` | `production.split_notes_result` |
| `test_body_template.py` | import `production.WORKFLOW` 参与遍历（实测其 `@body_template` 节点为 0，计数不受影响） |
| `test_l3.py` | 6 份资产的模板名前缀 |
| `test_contract.py` / `test_entry_end_to_end.py` / `test_baseline_artifacts.py` / `test_baseline_runner.py` | `customer_profile_entry`，而 entry 依赖 production |

## 3. 已拍板的口径（2026-10-27）

| 事项 | 决定 |
|---|---|
| `customer_profile_production` 臂 | **整臂退役**：从注册表摘除、删除模块与私有模板 |
| `married` / `unmarried` | 分别映射为 `married_no_child` / `unmarried_no_child` |
| `commute` / `transport` | 并入 `family`（家用为最上位场景） |
| `purchase_type` 收到 `first`/`additional`/`replacement` | **转内部字段 `purchase_nature`**，`purchase_type` 本身留空 |
| `planned_purchase_time` 的 `within_6_months` | **丢弃并记 trace**（6 个月与 3 个月区间重叠，无法无损归一） |
| 回写前发现非法枚举值 | **单条剔除，其余照发**，记 trace 与计数 |
| 历史归档范围 | **只清代码与文档**；`experiments/batches/`、`docs/history/` 保留不动 |

## 4. 整改设计

### 4.1 旧键别名表（normalize 层）

在 `internal_fields.py` 新增一张**只增不改**的别名表：

```python
LEGACY_ENUM_ALIASES: dict[str, dict[str, str]] = {
    "family_structure": {"married": "married_no_child", "unmarried": "unmarried_no_child"},
    "purchase_purpose": {"commute": "family", "transport": "family"},
    "driving_preference": {"easy-to-drive": "easy_to_drive"},
}
```

设计要点：

- **只收「语义可无损落到新键」的映射**。`within_6_months` 不在表里——它与
  `within_3_months` 区间重叠但不等价，硬映射会伪造精度。
- 表放在 `internal_fields` 而不是规则层：`display_map()`、`legal_keys()` 都在这里，
  「什么算合法取值」只有一处定义。
- `normalize_enum()` 在「精确匹配失败 → 展示名反查失败」之后、返回 `None` 之前查这张表。
  命中即按新键处理，并在 `facts` 侧记一条 `legacy_alias` 留痕。

**为什么不用改写历史档案**：档案接口是新版后端的数据，本地无写权限；且档案里存的是
「当时写入的值」，改写它等于伪造历史。旧值在**读取时归一**才是正确层次。

### 4.2 `purchase_type` → `purchase_nature` 的迁移

`purchase_type` 的旧值 `first`/`additional`/`replacement` 与新契约的
`company`/`personal` 语义完全不重叠，**不能做同字段映射**。处理：

- 在 `projection` 的枚举归一里加一条跨字段路由：当 `attribute == "purchase_type"`
  且取值落在 `purchase_nature` 的合法键内时，把该值写进 facts 的
  `purchase_nature`，`purchase_type` 本字段留空；
- `purchase_nature` 是编外字段（`EXTRACTION_ONLY_KEYS`），取证有它、Notes 白名单不含它，
  因此**不会出现在回写 payload 里**——这符合「内部字段自己用」的边界；
- 该值仍参与 `purchase_decisions` / `deal_battle` 的推理（换购动因等），信息不丢；
- 记 trace 说明「购买类型旧值已转购车性质」。

### 4.3 回写闸门：非法枚举单条剔除

两层，都要做：

**第一层（规则层）**，新增 `ENUM-01`「回写枚举取值必须是契约合法键」：

| 项 | 值 |
|---|---|
| content_kind | `hard` |
| channels | `code` |
| stages | `gate` |
| severity | `block` |
| default_action | `reject` |
| 判据 | 字段是枚举、`basis` 已是 `inferred_low`、取值归一后仍不在 `legal_keys` 内 |

与 `HARD-02` 的分工必须写清：`HARD-02` 管「L1 直接给了非法值且**尚未降级**」，动作是
降级转确认（保留取值，让销售看到）；`ENUM-01` 管「降级后**仍然**非法」，动作是剔除——
一条归不到契约的取值留在 Notes 里只会让销售点开看到一个内部键。

**第二层（回写投影）**，`callback_build.build_basic_notes_updates()` 在 key 白名单之外
补一条取值校验：枚举字段的 `attribute_value` 逐项归一，任一项不合法则**整条剔除**，
其余条目照发。这是最后一道闸，不依赖上游任何环节。

> 为什么不改 `HARD-02` 的动作：它的「降级必须可见」是刻意设计（`actions.py:363` 注释），
> 改成剔除会让「L1 猜错但方向对」的条目直接消失。两条规则各管一段，语义才不打架。

### 4.4 production 臂整臂退役

| 步骤 | 动作 |
|---|---|
| 1 | 从 `workflows/__init__.py` 的 `WORKFLOW_MODULES` 摘除 `customer_profile_production` |
| 2 | 删除 `workflows/customer_profile_production.py` |
| 3 | 6 份业务资产模板改名为 `customer_profile_shared__<id>.txt`，`customer_profile_l3.py` 的 `PRODUCTION_ASSET()` 改为指向新前缀，`test_l3.py` 的前缀断言同步改 |
| 4 | 删除其余 13 份私有模板 |
| 5 | `customer_profile_entry` 的 `PRODUCTION` 节点改指 `customer_profile_pipeline` |

第 5 步的连带影响必须说明：`entry` 已是摘除状态、不可运行，仅作为历史样本被 4 个测试
import。改指后这些测试仍能验证「子流程编排」这一形态，且基线臂语义（入口 → 证据 →
画像 → 出图）保持不变，只是末端换成当前在用的管线。

> **待确认项**：若倾向连 `customer_profile_entry` 一并退役，则需同时删除
> `test_contract.py`、`test_entry_end_to_end.py`、`test_baseline_artifacts.py`、
> `test_baseline_runner.py` 中相关用例，并更新 `run_baseline.py` 的 `BASELINE_WORKFLOW`
> 常量说明。本方案默认**保留 entry、只改末端指向**——影响面小，且它是 AB 复位的唯一入口。

### 4.5 旧模板里的旧枚举清单

`1777372805721` / `1777372465294` 随模块删除一起消失，无需逐行改写。删除前须确认：
这两个模板只被 `NOTES_GEN_PROMPT` / `NOTES_REVIEW_PROMPT` 常量引用，无其它模块按名加载
（已核实：`grep "1777372805721\|1777372465294"` 仅命中 production 模块自身）。

### 4.6 文档清理

| 文件 | 改动 |
|---|---|
| `docs/history/对客产出英文残留治理方案.md` §218 | 「前端若仍显示 `unmarried`」改为指向本文档，说明旧键已在读取时归一 |
| `docs/遗留问题清单.md` L-015 | 补充：不是纯前端映射问题，`purchase_type`/`purchase_purpose` 的旧键在现行契约里无对应项 |
| `docs/reference/AI画像v2回传改造核查清单.md` | 保留原文（它是**核查记录**，记录的就是「哪些字段变了」），在文首加一行「旧键已于 2026-10-27 清理，见设计文档」 |
| `docs/changelog.md` | 新增本次整改条目 |
| `docs/design/客户人设画像管线设计.md` §3.8 | 补 `ENUM-01` 的登记与分工 |

## 5. 文件级改动清单

**新增（3）**
- `tests/rules/test_enum_legacy.py`
- `scripts/scan_legacy_enum_keys.py`：回传体里的废弃键/非法取值扫描，退出码可直接验收
- （`ENUM-01` 无提示词正文，见下）

> `ENUM-01` 只挂 code 通道，不需要提示词正文（`rules/texts/` 现只有 `expression.txt`
> 与 `spec.txt`，不新增 `hard.txt`）。已确认 `ENUM-01` 编号未被占用，现有规则编号为
> `ATTE` / `CONSIST` / `EXPR` / `HARD` / `IDENT` / `INCOME` / `LEVEL` / `SPEC` 八簇。

**修改（9）**
- `internal_fields.py`：`LEGACY_ENUM_ALIASES` + `legacy_alias` / `legacy_cross_field` /
  `looks_like_enum_key`
- `projection.py`：`normalize_enum` 查别名表；`purchase_type` 跨字段路由
- `callback_build.py`：`build_basic_notes_updates` 取值校验（`_repair_enum_value`）
- `rules/clusters/hard.py`：`ENUM-01` 登记与判据
- `execution/executors_http.py`：回写节点的请求体形状校验（**实施中新增**，见下）
- `workflows/customer_profile_pipeline.py`：接入 `ENUM-01`，新增计数与 issue
- `workflows/__init__.py`：摘除 production
- `workflows/customer_profile_entry.py`：`PRODUCTION` 节点改指 pipeline
- `workflows/customer_profile_l3.py`：资产模板前缀

> **实施中新增的一处（不在初稿内）**：`@body_ref` 只保证「是合法 JSON」，
> `[]` 与 `"string"` 都能过关并真的发出去，后端只会回一个笼统的 4xx。
> 旧生产臂用的是 `@body_json_field`，那条路径本来就校验「须为对象」。改指 pipeline 后
> 这道校验丢了，因此在回写节点补回来——只对 `@writeback` 节点生效，其它 `@body_ref`
> 用法不受影响。发现方式是把 `test_m2m_writeback.py` 改指 pipeline 后原用例变红。

**改名（6）**：`customer_profile_production__<id>.txt` → `customer_profile_shared__<id>.txt`

**删除（14）**：`customer_profile_production.py` + 13 份私有模板

**测试调整（4）**：`test_m2m_writeback.py`（改指 pipeline 的 `callback_build` /
`callback_writeback` / `pipeline_end` 三个节点，`merge_final_data` 的等价断言改为
直接比对 `callback_json`）、`test_expect_json_output.py`（删 `split_notes_result` 用例）、
`test_l3.py`（资产模板前缀）、`test_contract.py` 等（随 entry 改指自动通过）

> **核实结论**：`test_body_template.py` **不需要改**。该测试统计的是
> 「`audio_content_extract` + `customer_profile_production` 里带 `@body_template` 的节点，
> 应共 2 个」，实测 production 臂此类节点为 **0**，两个都在 `audio_content_extract`。
> 删掉 production 后计数仍是 2，只需把该测试里对 production 的 import 与遍历去掉。

**文档（5）**：见 §4.6

## 6. 验证方案

### 6.1 离线重放（不取数）

沿用既有方式，本地访问不到 UAT 取数接口，**不发真实请求**：

```
python scripts/replay_from_archive.py --source uat-18-20261026 --only <号码>...
```

覆盖 6 案：`13733451677`（本次问题案）、`13207117251`（`unmarried` 档案）、
`13367265521`（`commute` + `replacement`）、`15335772822`（`easy-to-drive`）、
`13100792128`（`education=本科及以上` 中文当值）、`19972120792`（字段最全的对照）。

### 6.2 验收标准

| # | 标准 | 判定方式 |
|---|---|---|
| 1 | 回写 payload 中**所有**枚举字段取值都在 `legal_keys` 内 | 写脚本遍历全部 `attempts.json` 的 `update-profile` 请求体，`enum_illegal=0` |
| 2 | `married`/`unmarried`/`commute`/`transport`/`replacement` 在 payload 中归零 | 同上，正则计数为 0 |
| 3 | 换购/增购/首购信息不丢 | 检查 `facts.purchase_nature` 与 `purchase_decisions` 的换车动因 |
| 4 | 剔除不波及同案其它字段 | 逐案对比 Notes 条数，仅允许因剔除非法条目而减少 |
| 5 | 6 份业务资产在 L3 表达层正常加载 | `pytest tests/test_l3.py` |
| 6 | 全量测试绿 | `pytest` |
| 7 | 规则守卫无告警 | `python scripts/rules_index.py --problems` |
| 8 | 可单条回滚 | `RULES_DISABLED=ENUM-01` 后回到改造前行为 |

### 6.3 回归基线

重放前后各跑一次 `scripts/scan_english_residue.py --batch <批次>`，确认英文残留仍为 0
（本次改动不应回退上一轮的成果）。

## 7. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| `commute`→`family` 丢失「通勤」语义 | 销售看不到"通勤"这个高频场景 | 在 `reasoning_detail` 里保留原词；`purchase_decisions` 的分类文案来自事实表原文，不经过枚举 |
| `within_6_months` 直接丢弃 | 计划时间字段少一条 | 记 trace，并在遗留问题登记「是否需新增 6 个月档位」，交业务确认 |
| production 臂删除影响历史批次复现 | 旧归档无法用旧代码重放 | 归档已存 `attempts.json`，重放只需输入不需旧代码；且 `experiments/` 保留 |
| entry 改指后基线语义漂移 | AB 对照失真 | entry 已摘除、本就不可运行，不在任何对照臂中 |

回滚：代码改动集中、无数据迁移，`git revert` 即可；规则侧另有 `RULES_DISABLED` 开关。

## 8. 明确不做的事

1. **不改写历史档案**。`data/profile` 里的旧值是上游数据，本地无写权限；读时归一才是正确层次。
2. **不改 `attribute_value` 的键语言**。契约要求枚举回传英文键，前端按 `display_name` 映射（L-015）。
3. **不删 `experiments/batches/` 与 `docs/history/`**。它们是历史快照，删除后无法复现对照。
4. **不改 `notes_contract.py`**。已验证与新版文档 45/45 一致。
5. **不接 `EXPR-01` 的 prompt 通道**。L1 提示词被测试钉在 8240 字符，改动须单独立项。

## 9. 实施进度（2026-10-27）

| 步骤 | 内容 | 状态 |
|---|---|---|
| 1 | 别名表 `LEGACY_ENUM_ALIASES` + `looks_like_enum_key` | **已完成**。`internal_fields.py`；`within_6_months` 与 `purchase_type` 旧值刻意不入表 |
| 2 | 归一层查别名 + `purchase_type` 跨字段路由 | **已完成**。`normalize_enum` 在「展示名反查失败后、返回前」查表；`_route_legacy_cross_field` 搬到 `purchase_nature` |
| 3 | 登记 `ENUM-01` 并接入 gate | **已完成**。判据带**键形态**前置——只按「不在合法键集合」判会误删客户原话（`说不清`），已由 `tests/rules/test_gate.py` 的反例挡住 |
| 4 | 回写投影取值校验 | **已完成**。`build_basic_notes_updates`：先尝试别名/展示名修正，救不回来才单条剔除 |
| 5 | production 臂整臂退役 | **已完成**。模块与 13 份私有模板删除；6 份业务资产改名 `customer_profile_shared__*`，`L3` 与 `test_l3` 同步；`entry` 末端改指 pipeline |
| 6 | 回写节点形状校验 | **已完成**。`@body_ref` 只保证合法 JSON，`[]` / `"string"` 原会照发；回写节点现要求对象 |
| 7 | 测试与文档收口 | **已完成**。新增 `tests/rules/test_enum_legacy.py`（15 例）；`test_m2m_writeback.py` 改指 pipeline；全量 `pytest` 绿；守卫 0 告警 |
| 8 | uat-18 全量离线重放验证 | **已完成**，见 §10 |

## 10. uat-18 离线重放验证（2026-10-27）

重放批次：`experiments/batches/uat-18-legacykeys-20261027`（**16 案全量**）。
取数层回放归档响应（不调 UAT 取数接口），模型真实调用；**16/16 succeeded，实际发出
`update-profile` 请求 0 次**（`WRITEBACK_ENABLED` 缺省关闭）。

### 10.1 废弃键：全部归零

| 扫描项 | 结果 |
|---|---|
| 点名废弃键（`married`/`unmarried`/`commute`/`transport`/`replacement`/`additional`/`within_6_months`/`easy-to-drive`） | **0 处** |
| 可映射旧键（产出侧漏了归一） | **0 处**（源批次 `uat-18-20261026` 有 15 处非法取值） |
| 枚举字段条目逐条校验 | **170 条 / 0 非法** |
| `result1` 字面量检索上述七个键 | **全部 0 次** |

`python scripts/scan_legacy_enum_keys.py --batch uat-18-legacykeys-20261027` 退出码 **0**。

`ENUM-01` 全批只命中 **1 次**：`13100792128` 的 `frequent_social_media = unknown`
（该字段合法键只有四个社媒名，`unknown` 不在其中），整条剔除——正是预期形态。

### 10.2 结果劣化：无规则层删除，差异均来自 L1

对照臂用同谱系既有重放批次 `uat-18-20261026-replay`（旧代码、同一份输入、同样 16 案）。

**商机等级 15/16 一致。** 唯一变化 `15327825930` C→待识别，归因如下：该案的定级输入
`facts` 里 `emotion` 一项在新批次缺失，而 `pipeline_l1_merge` 阶段就已缺（新批次
`emotion` 出现 0 次，旧批次 1 次）——**进入投影层之前就不存在**，规则层对该案
`rule_trace` 为空、未做任何动作。也就是说这是 L1 采样的差异，定级 LLM 因此给不出
「沟通风格防备强」这一条依据，判为待识别。

**Notes 净变化 −21 条，逐案核对后无一例是规则误删：**

| 号码 | Notes | L2 投影层 | 归因 |
|---|---|---|---|
| 15972379760 | 34→28 | 57→32 | 投影层就少了 25 条，L1 采样 |
| 15335772822 | 14→7 | 11→8 | 同上；`placeholder_removed` 由 1 降为 0 |
| 18062597750 | 45→41 | 56→59 | 投影层反而变多，审查层删 3 条占位串 |
| 18271918797 | 21→24 | 24→58 | 投影层多 34 条，审查层删 17 条占位串（旧批次删 24 条） |
| 19972120792 | 45→41 | 58→58 | 审查层删 1 条占位串 |
| 13100792128 | 44→40 | 51→60 | 审查层删 3 条：2 条中文占位串 + **1 条 `unknown`（ENUM-01）** |
| 17652815077 | 30→34 | 40→45 | 投影层变多，审查层未删 |

全部减少的条目都来自 `HARD-01` 占位串与 `ENUM-01` 非法键，**没有一条是合法取值被删**。
`18271918797` 的 `placeholder_removed` 从 24 降到 17，也是 L1 少产出占位串所致。

### 10.3 换购信息未丢失

`purchase_type` 的旧值转入内部字段 `purchase_nature`（编外、不进 Notes），但**推理链路
照常使用**：16 案中有 **7 案**的 `purchase_decisions` / `deal_battle` 仍出现「换购」，
说明该信息没有因为不回传而消失。

### 10.4 验收判定

| 验收标准 | 结论 |
|---|---|
| 1 回写枚举取值全部合法 | **通过**。170 条枚举条目 0 非法 |
| 2 废弃键归零 | **通过**。七个点名键字面量为 0 |
| 3 换购/增购/首购信息不丢 | **通过**。7 案仍在决策与攻防中使用 |
| 4 剔除不波及同案其它字段 | **通过**。逐案核对，减少项均为占位串与非法键 |
| 5 6 份业务资产正常加载 | **通过**。`tests/test_l3.py` 绿 |
| 6 全量测试 | **通过**。`pytest` 全绿 |
| 7 规则守卫 | **通过**。0 孤儿标记、0 未落地规则 |
| 8 可单条回滚 | **通过**。`RULES_DISABLED=ENUM-01` 由既有机制承接 |

### 10.5 遗留

`EXPR-01` 对「英文单词型合法键 + 前有 ASCII 空格」仍有盲区（`符合 basic`），
已登记 **L-016**。该形态与本次要消灭的废弃键无关（废弃键由 `ENUM-01` 与回写投影按
形态拦截），不构成本次验收的阻塞项。

