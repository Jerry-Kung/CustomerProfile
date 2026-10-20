# AI 画像 v2 回传改造方案

> 前端（业务架构师侧）指定了新的回传契约：`analysis_result` 六键 + 一份 45 字段的 Notes 清单。
> 本文是落地计划：目标、验收标准、阶段划分、每阶段的改动面与验证方式。
>
> 差异逐项核对见 `docs/AI画像v2回传改造核查清单.md`；本轮不解决的问题见 `docs/遗留问题清单.md`。
> 契约原文：`docs/AI画像远程回传接口数据结构说明.md`；线上实测清单：`docs/Notes_new.txt`。
>
> 立项：2026-10-15。承接臂：`customer_profile_pipeline`（对外口径改称**业务测试臂**）。
>
> **状态（2026-10-16）**：P0–P5 编码与调测已完成，P6 待单独下令。
> 落地结果：契约快照 45 字段 / 7 分类、签名 `b36e78edf9a2`（`--check` 通过）；
> 回传体六键由 `callback_build.py` 纯代码投影；回写旁支在业务测试臂上，缺省不发请求。
> 测试 655 passed / 80 skipped。
>
> **两处与本文原文不同的落地口径**（以实际实现为准）：
> 1. `SECTION_KEYS` **未改**（本文 P3 写的「9 个板块改为 6 键」未执行）。原因：新契约六键里
>    没有五节的位置，一动 `result1` 形状即变、既有批次可比性失效。改登记为 L-006 待业务方确认。
> 2. 回传节点指向 `callback_build:main` 而非 `build_analysis_result`：后者返回 JSON **字符串**，
>    而 `execute_code` 要求 code 函数返回字段字典。见 P4 节的旁注。

---

## 1. 目标与验收标准

**目标**：业务测试臂产出 v2 契约可接受的 `analysis_result` 六键结构，回写代码就位。
**调测期不做真实回传**——回写节点存在、可被断言、但恒不发请求。

验收标准四条，逐条可证：

| # | 标准 | 怎么证 |
|---|---|---|
| A1 | 契约快照为 45 字段 / 7 分类，且可由线上 payload 复现 | `python scripts/snapshot_note_contract.py --from-response docs/Notes_new.txt --check` 输出 `[OK] … 45 字段` |
| A2 | 单号端到端跑完，`basic_notes_updates` 的每个 `attribute_key` 都在新契约 45 键内 | 批次产物里逐条比对，零未知键 |
| A3 | 三个条目化区块的 id 在同客户内全局唯一 | 断言 `purchase_decisions` / `deal_battle` / `decision_psychology` 取出的 id 集合无重复 |
| A4 | 调测期零真实回传 | 批次 `attempts.json` 中零条 `update-profile`；回写节点产出 `writeback_skipped: true` |

---

## 2. 总体数据流

改造后业务测试臂的尾段：

```
L2_PROJECT ──► L2_REVIEW ──► PRODUCTION (L3 子流程)
                                   │
                                   ├─► END          （result1 不变，对外契约冻结）
                                   │
                                   └─► callback_build ──► callback_writeback
                                        （code，0 token）      （http-request，@writeback）
```

要点：

- **END 挂在 `callback_build` 上**（2026-10-19 调整）：运行台「结果」页展示的 `result1` 改取 v2 回传体 `callback_json`，与平台实际画像同源。此前 END 挂 `PRODUCTION`，展示的是 L3 九板块旧结构，核对时容易拿错产物；L3 旧结构仍留在 `PRODUCTION` 节点出参里备查。
- `callback_build` 只读 facts 与 notes，不产生新事实。
- `callback_writeback` 带 `@writeback: True`，受既有的 `WRITEBACK_ENABLED` 闸门管辖（`executors_http.py:71`）。

---

## 3. 阶段划分

### P0 · 契约快照重建（前置，其余全部依赖）

**改什么**

`scripts/snapshot_note_contract.py` 三处：

| # | 现状 | 改为 |
|---|---|---|
| 1 | `load_payload()` 只从批次产物读（`experiments/batches/baseline-39-dsv41-noThink`，冻结在 42 字段版） | 新增 `--from-response <file>`：读 HTTP 信封文件，剥出 `body` 字段（它本身是 JSON **字符串**），再 `json.loads` 得 `{"configs": …}`。旧路径保留为 `--batch-dir`，供回归对照 |
| 2 | `apply_extension()` 同键以基线为准、不覆盖 | 走 `--from-response` 时**整段跳过合并**。新 payload 已是完整现行契约，再叠加任何旧快照都会产出新老混杂的定义 |
| 3 | `_category_slug()` 的表只有 6 个分类，新分类落到 `hash()` 回退分支（逐进程随机） | 补入「地域与生活圈 → REGION_LIFE」；**回退分支从哈希改成抛错**，新增分类必须显式登记 |

`src/customer_profile/notes_contract_extension.json` 的 5 条补挂字段（`competing_brand` / `competing_model` / `test_driven_models` / `brand_concerns` / `resistances`）在新契约里已全部包含，`configs` 清空。文件保留（生成器仍认这个参数），`_comment` 改写为「本文件本轮清空，原因见 `docs/design/AI画像v2回传改造方案.md` P0」。

**为什么回退分支要抛错而不是换个哈希**：哈希回退会让「忘了登记新分类」这件事表现为「快照每次 `--check` 都失败」，排查方向指向缓存、编码、换行符，而真因是少一行映射。抛错把真因摆在报错信息里。

**同步改的测试**（这些是「钉住旧契约」的用例，属预期失效）：

| 文件:行 | 现值 | 新值 |
|---|---|---|
| `tests/test_projection.py:32` | `RAW_CONFIGS == 47` | `45` |
| `tests/test_projection.py:33` | `CATEGORIES == 6` | `7` |
| `tests/test_projection.py:34` | `IDENTITY_KEYS == 16` | `15` |
| `tests/test_projection.py:35-36` | 16 单选 / 5 多选 | 17 单选 / 7 多选 |
| `tests/test_projection.py:43` | `seating_requirement` 的结构化枚举 | 该字段已下线，改用 `family_structure` / `driver_license_status` / `trade_in_requirement` 三项 |
| `tests/test_pipeline_l1.py:289` | `== 47` | `45` |
| `tests/test_pipeline_l1.py:340` | 枚举字段 `== 21` | `24` |

**验收**：A1。此阶段单独提交，提交后除依赖旧字段的用例（P1/P2 处理）外应全绿。

---

### P1 · 内部字段与回传字段解耦

现状是「facts 的键集合 == 契约键集合」：`_to_notes_updates`（`projection.py:1123`）直接遍历 `nc.ALL_KEYS` 产出回传项。保留编外字段后二者必须分开。

**新增** `src/customer_profile/internal_fields.py`：

```
INTERNAL_ONLY_KEYS   编外字段：只在内部 facts 保留，回传前裁掉
CALLBACK_KEYS        回传白名单：nc.ALL_KEYS 本身
EXTRACTION_KEYS      L1 取证目标 = CALLBACK_KEYS ∪ INTERNAL_ONLY_KEYS
```

成员的定夺见第 4 节。

**改什么**

- `projection.py:_to_notes_updates` 语义不变（仍遍历 `nc.ALL_KEYS`），但注释里点明它的角色已从「facts 的映射」变成「回传体的白名单投影」。
- `pipeline.py:field_inference_guide()`（1050 行）的遍历源改为 `EXTRACTION_KEYS`，并给编外字段渲染一行「**内部字段，不回传**」的标注。这张清单是 L1 的取证目标，编外字段必须照旧全量取证，否则 P3 的四个区块会缺料。
- `pipeline.py:enum_key_guide()` 同样扩到 `EXTRACTION_KEYS` 里的枚举字段。
- `tests/test_pipeline_l1.py:280` 的断言改为 `listed == set(EXTRACTION_KEYS)`。

**验收**：A2 的前半段——单号跑完后，`basic_notes_updates` 的键是 `EXTRACTION_KEYS` 与契约的交集，编外字段一个都不出现在回传项里。

---

### P2 · 跨字段规则随枚举换血改写

新契约的 `purchase_type` 只剩 `company` / `personal`（个户 / 公户），首购 / 增购 / 换购的区分没有位置了。现有规则把 `purchase_type` 写成 `replacement`（`projection.py:839`），在新契约下会写出非法枚举。

**处置**：把「购车性质」移到编外字段 `purchase_nature`（取值 `first` / `additional` / `replacement` / `company`），`purchase_type` 只由 L1 的个户 / 公户证据填充。这是本轮**唯一**新增的编外字段。

| 位置 | 改动 |
|---|---|
| `projection.py:830` | 换购旁证改读 `current_vehicle_info` 与编外 `old_car_model` / `owned_brand_model` |
| `projection.py:839-841` | 写 `purchase_nature = "replacement"`，不再写 `purchase_type` |
| `projection.py:862` | 注销企业规则键不变（`company` / `company_type` 已转为编外），去掉「必须是契约键」的隐含假设 |
| `projection.py:144` `ATTITUDE_KEYS` | `emotion` 已迁往卡片，换成新契约的态度类字段：`pain_points` / `knowledge_of_mengshi` / `knowledge_of_erev` / `knowledge_of_adas` |
| `projection.py:196` `AGGREGATE_TEXT_KEYS` | 去掉 `special_requirements` / `intended_brand`（转编外），加入 `optional_config` / `pain_points` |
| `tests/test_projection.py:287`、`311-337` | 换购相关用例改断言 `purchase_nature`；`additional` 透传用例改到 `purchase_nature` 上 |

**验收**：`purchase_type` 的取值恒在 `{company, personal}` 内；同一客户的 `purchase_nature=replacement` 与 `trade_in_requirement=yes` 同时成立。

---

### P3 · 四区块投影（0 token，纯代码）

四个区块当前完全没有产物结构。按 L-002 的定论**由代码从 facts 投影**，不由表达层 LLM 归类。

**新增** `src/customer_profile/callback_build.py`，导出：

```
build_analysis_result(phone, batch_id, facts_json, notes_updates_json, express_json) -> dict
```

内部四个构建函数与取值来源：

| 区块 | 取值来源 | 备注 |
|---|---|---|
| `ai_insight.purchase_intent_level` | facts `deal_level`（画像卡字段，由商机定级环节产出） | `value` 为 `H`/`A`/`B`/`C`/待识别，原样回传，**不含「级」字** |
| `ai_insight.customer_segment` | L3 表达层的自由生成值 | 本轮不约束，见 L-001 |
| `purchase_decisions` · 到店动因 | `purchase_purpose` + `core_concerns` | |
| `purchase_decisions` · 当前用车 | `current_vehicle_info` + 编外 `old_car_model` / `owned_brand_model` / `owned_car_age` | |
| `purchase_decisions` · 换车动因 | 编外 `purchase_nature`=replacement + `pain_points` | 缺料时填「暂无换车动因」 |
| `purchase_decisions` · 对比车型 | `competing_brand` / `competing_model` / `test_driven_models` | 缺料时填「暂无对比车型」 |
| `purchase_decisions` · 核心关注 | `core_concerns` | |
| `purchase_decisions` · 决策阻力 | `brand_concerns` + L3 `resistances` | `resistances` 由 Notes 迁至此 |
| `deal_battle.win_conditions` | `core_concerns` + `knowledge_of_mengshi`≥basic + `planned_purchase_time` 档位 + `license_plate_status` | |
| `deal_battle.loss_conditions` | `brand_concerns` + L3 `resistances` + 竞品 + `credit_status` | |
| `decision_psychology.decision_style` | 编外 `personality_and_preferences` | |
| `decision_psychology.current_attitude` | `viewing_progress` + `knowledge_of_mengshi` | |
| `decision_psychology.communication_preference` | `contact_preference` + 编外 `communication_rhythm` | |
| `decision_psychology.emotion_state` | 编外 `emotion` + L3 `emotion_state` | |

**id 生成规则**（唯一约束是 `(phone, item_id)` 与 `(phone, psychology_key)`，冲突会让整次回传 500）：

- 前缀固定：`visit-` / `change-` / `compare-` / `focus-` / `barrier-` / `win-` / `loss-`；
- 后缀取来源事实的**稳定摘要**（枚举键或归一后的文本），同客户内去重；
- `decision_psychology` 用契约给定的固定 `key`，不另生成 id；
- 生成后跑一次**同客户全局唯一性断言**，重复即抛错而不是静默去重——静默去重会把两条不同的关注点合成一条，卡片上少一项而无人发现。

**改什么**

- `customer_profile_l3.py:SECTION_KEYS`（81-91 行）从 9 个板块改为 6 键；
- 删掉 `assemble_l3` 里 `resistances` 补挂进 Notes 的那段（371-401 行），其内容改走 `deal_battle.loss_conditions`；
- `LIST_SECTIONS`（421-426 行）按新结构重划；
- `callback_build` 从 `PRODUCTION.result1` 解出 facts 与 notes（`result1` 是字符串，需二次 `json.loads`）。

**验收**：A3 的 id 唯一性；四区块的 `reasoning_detail` 均为 `{summary, steps, evidence, confidence}` 四键，`evidence` 为空时不渲染该区块。

---

### P4 · 回写节点接入业务测试臂

**前置：一处执行器能力必须先补**（本次核对新发现）

`@body_template` 产不出文档要求的请求体。`_resolve_body`（`executors_http.py:99-159`）把每个 `{{#节点.字段#}}` 一律当**字符串**：引用不在引号内时 `_json_quote` 补引号（139 行），在引号内时只转义。文档要求 `analysis_result` 是对象，经这条路径恒为字符串。基线臂现役的 `data[]` 信封正是这个机制的产物。

**补法**：给 http 执行器加 `@body_ref`——声明「请求体整段取自某个绑定值」，绕过文本替换，值直接作为 `json_body`。要点：

- 值可以是 dict，也可以是 JSON 字符串（解析后作 dict）；
- 与 `@body_template` 互斥，同时声明即报错；
- 解析失败抛 `NodeExecutionError`，不发坏载荷；
- 现有 `tests/test_body_template.py:184` 断言全仓恰有 **3** 个 `@body_template` 节点——本节点走 `@body_ref`，该用例不受影响。

**改什么**

`customer_profile_pipeline.py`：

- 新增节点常量 `CALLBACK_BUILD` / `CALLBACK_WRITE`；
- `callback_build`：`code` 节点，`after=(PRODUCTION,)`，`function="customer_profile.callback_build:build_analysis_result"`，出参 `callback_json`；
- `callback_writeback`：`http-request`，`after=(CALLBACK_BUILD,)`，`@service: "profile"`、`@method: "post"`、`@path: f"{PROFILE_API_PREFIX}callback/update-profile"`、`@auth: True`、`@body_ref`、`@writeback: True`、`@outputs: {"body": "$text", "status": "$status"}`；
- `END` 的 `after` 改挂 `(CALLBACK_BUILD,)`，`result1` 取 `callback_json`（运行台展示与平台画像同源）；
- `display_name` 改「业务测试臂」，`workflow_id = "customer_profile_pipeline"` **不动**。

`PROFILE_API_PREFIX` 需在 pipeline.py 内自建（production.py 有一份，两文件不互相 import）。

**为什么不改 `workflow_id`**：模板资源名是 `<slug>__<node_id>`，`node_id` 与模板正文都不得改动；`workflow_id` 一动，`scripts/run_baseline.py --workflow-id` 与既有批次的可比性判据一并断掉。改名限显示名层。

**验收**：单号跑完，`attempts.json` 中零条 `update-profile`，回写节点 meta 为 `{"writeback": "skipped", "reason": "WRITEBACK_ENABLED=false"}`。

---

### P5 · 调测（零真实回传）

**护栏**：只用既有的 `WRITEBACK_ENABLED`（缺省 `false`，`run_baseline.build_settings` 硬编码 false 并在为 true 时 `SystemExit`，`settings.py:149`）。不新增开关——多一个开关就多一处「哪个才是真的关着」的疑问。

**做三件事**：

1. **单号跑通**，逐条核 A2 / A3 / A4；
2. **抽 3 案人工核读**四区块，作为 P6 放行的判据（这四块没有历史产出可比，质量只能人读）；
3. **补一条回归用例**：钉住业务测试臂的图里回写节点存在、带 `@writeback`，且在缺省设置下产出 `writeback_skipped: true`。这是「仅预留代码」这条要求在测试层面的落地。

**同步更新的文档**（此时代码已改，文档该跟上）：

- `docs/design/客户人设画像管线设计.md` §5「输出契约」：47 字段 / 6 分类 → 45 字段 / 7 分类；9 个板块 + `basic_notes_updates` → 六键结构；
- `docs/design/客户人设画像管线设计.md` §6 决策表：补一行「四区块由代码投影，不用 LLM 归类」；
- `docs/遗留问题清单.md`：L-002 标为已定（代码投影，落在 `callback_build.py`）；L-005 在 P0 完成时删除。

---

### P6 · 真实回传联调（本轮不做，待单独下令）

翻 `WRITEBACK_ENABLED=true` 后**先跑单号**，不要直接全量跑批。**这是 L-003 的兑现点**：文档写顶层三键，线上现役实现发的是 `data[]` 信封，两者只能有一种对。若线上实际收信封，回写体要改回，文档需更正。

---

## 4. 编外字段的定夺

17 个下线字段的处置分三类。**三类都留在内部 facts 里**（facts 本就不受契约约束），区别只在回传时有没有落点。

| 处置 | 字段 | 说明 |
|---|---|---|
| **编外**（无回传位） | `old_car_model`、`owned_brand_model`、`owned_car_age`、`company`、`company_type`、`seating_requirement`、`primary_driver`、`usage_frequency`、`driving_preference`、`special_requirements`、`intended_brand` | 换购旁证链、企业交叉验证、家庭用车决策的核心，新清单无替代项 |
| **迁移**（换位置回传） | `emotion`、`communication_rhythm`、`personality_and_preferences` → `decision_psychology`；`resistances` → `deal_battle.loss_conditions` | 值离开 Notes，落进卡片 |
| **舍弃** | `douyin_id`、`xiaohongshu_id` | 分析价值低，平台偏好已由 `frequent_social_media` 覆盖 |

另**新增一个编外字段** `purchase_nature`（首购 / 增购 / 换购），承接被新 `purchase_type` 枚举挤掉的购车性质。这是销售判断逼单策略的第一顺位信息，整块删掉不可接受。

**长期归属未决**，登记为 L-004：编外字段只在本项目内有意义，业务系统看不到。若业务方后续仍需要这些维度，需重新协商契约，届时这轮保留的值可直接复用。

---

## 5. 风险与已知不确定

| # | 风险 | 处置 |
|---|---|---|
| R1 | **`definition_hash` 会变**（新增两个节点），业务测试臂与既有 `pipeline-v3.3-39` 批次不再可比 | 在批次 manifest 里显式标注；这是必要代价，不掩盖 |
| R2 | **L-003 的请求体形状差异是真的**：线上若收 `data[]` 信封，首次真实联调会 403 / 500 | 真实回传单列为 P6，不混进调测；先跑单号 |
| R3 | **四区块无历史产出可比**，质量只能人读 | P5 抽 3 案人工核读，作为 P6 放行判据 |
| R4 | **分类边界重叠**（如「核心关注」与「到店动因」） | 属规则参数问题，改规则远比改架构便宜；实测后调 |
| R5 | **L1 提示词正文改动**（新增 6 个字段的取证口径、编外字段标注）属业务优化 | 已获口径确认（「所有的新字段本轮都要上」）；改动逐条列在 P1，落地前单独提审 |

---

## 6. 不做什么

- **不做灰度**：六个新字段本轮全部上，一次做完所有 API 更新。
- **不做通道矩阵**：逐字段 × 逐通道的硬绑定矩阵仍是研究稿，本轮不动。
- **不动基线臂**：`customer_profile_production` 维持原状不再演进，它的 `data[]` 回写节点保留为对照。
- **不动 `entry` 臂**：`customer_profile_entry` 的 5 节点 / 4 边结构、入参口径、`result1` 字符串契约全部冻结。
- **不追求四区块的措辞质量**：本轮目标是结构正确、可回传、零真实副作用；文案优化留给后续。
