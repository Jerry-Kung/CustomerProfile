# business_opt01_v2 · v3.1 任务书

> 施工级任务书。设计依据见 `docs/business_opt01_v2/v3.1-方案设计.md`（下称「设计稿」）。
> 分支 `dev-business-opt01-v2`。**不改主干。**
> 验收一律**零 token 离线回放**（v3-fix 同批 7 案 `A-pipeline-L8-v3-20261014-fix` + 基线同号），
> 不跑 AB、不出网、不写回、不调模型。
>
> 工具：`python -I scripts/probe_v3_replay.py`（v3.1 判据读数）、
> `python -I docs/business_opt01_v2/ab对照-v3-fix/scripts/probe_field_trace.py <phone> <key>`
> （单字段逐层追踪）、`pytest tests/`（回归）。
>
> **范围**：改造一（推导链分离）+ 改造二（说话人归属）+ 改造三（锚点相关性）+ 改造四（确定性注入）。
> **L3 整改、L0/L4、对外契约不在本任务书范围**（设计稿 §1）。

---

## 1 三条硬约束（先立规矩，再改代码）

1. **对客产出必须真人可读**（设计稿 §1）。`reasoning_detail` 的 `steps` 是**推演**、
   `evidence` 是**原材料**，两者不得同集；不得出现审计与规则词、来源档代号、占位串。
2. **客户字段只装客户本人的信息**（设计稿 §3 改造二）。销售顾问的自述、自报家门
   一律不得进 `customer_name` / `occupation` / `title` / `company`。
3. **不新增事实**。`reasoning_chain` 是推演过程，不是新取值；注入字段的值只能来自
   入参与既有事实，不得造出候选集外的确定取值。

---

## 2 文件级改动清单

### S1 · 确定性字段代码注入（改造四，对应问题 4）

| 文件 | 改动 |
|---|---|
| `src/customer_profile/workflows/customer_profile_pipeline.py` | 新增 code 节点 `pipeline_l1_inject`（暂名），函数 `inject_deterministic(records_json, claims_json, phone_number)`：① `phone_number` 直取入参；② `driver_license_status` 按「有试驾录音／有旧车／置换=yes」判 `yes`。产物形状与 `claims_json` 一致，接在 `L1_MERGE` 与 `L1_LOCALIZE` 之间 |
| 同上 | 节点图接线：`L1_MERGE → pipeline_l1_inject → L1_LOCALIZE`；`CODE_SPECS` 增映射 |
| `tests/test_l1_merge.py` 或新建 `tests/test_deterministic_inject.py` | 新增：手机号直给、试驾⇒驾照、无试驾无旧车⇒不注入、注入与 LLM 同字段时取硬档 |

### S2 · 说话人归属 + 值形态门（改造二，对应问题 2）

| 文件 | 改动 |
|---|---|
| `src/customer_profile/templates/customer_profile_pipeline__pipeline_l1_facts.txt` | 第四节「主体判断」补充：销售顾问自述同样是事实但 `subject=salesperson`；加正反例（「我姓郑，这边的销售顾问」/「我姓陈」） |
| `src/customer_profile/templates/customer_profile_pipeline__pipeline_l1_fields.txt` | 硬边界增：**不得自行声明 `subject`**；输出示例去掉 `"subject": "customer"` 一行（该示例正是默认 customer 的来源） |
| `src/customer_profile/workflows/customer_profile_pipeline.py` | `merge_l1_layers`：1c 条目若在 1a 有同 `attribute`，`subject` 覆盖为 1a 的；1a 无则该条 `subject` 置 `unknown`（交归属过滤降级，不再默认 customer） |
| `src/customer_profile/projection.py` | 新增 `SALESPERSON_MARKERS` 词表；归属过滤（规则 a）之后新增**值形态门**：客户字段取值或锚点切片命中词表 → 整条拒收并留痕（新 `rule: salesperson_value`） |
| `tests/test_projection.py` | 新增：顾问自称姓名被拒收、客户自述职业不被误伤、`subject` 覆盖口径 |

### S3 · 锚点相关性门（改造三，对应问题 3）

| 文件 | 改动 |
|---|---|
| `src/customer_profile/templates/*_pipeline_l1_facts.txt` / `*_pipeline_l1_fields.txt` | 新增 `value_signal` 字段说明：信号型字段（`title` / `customer_name` / `driver_license_status` / `phone_number`）须给「锚点中支撑该取值的**最小片段**」，并给例（`title=Mr` → `"男性"`） |
| `src/customer_profile/workflows/customer_profile_pipeline.py` | `localize_claims`：① 校验 `value_signal` 是锚点切片的子串，否则降 `inferred_low` 并留痕；② 新增噪音锚点判据（问候语 / 含 `": "` 的 JSON 残片）→ 该条不进证据位 |
| `src/customer_profile/workflows/customer_profile_pipeline.py` | `CODE_SPECS` 无需改；`localize_claims` 输出增 `signal_missing` / `noisy_anchor` 计数 |
| `tests/test_pipeline_l1.py` | 新增：`value_signal` 子串校验、问候语锚点被拦、`title` 无性别信号时降级 |

### S4 · 推导链分离（改造一，对应问题 1）

| 文件 | 改动 |
|---|---|
| `src/customer_profile/templates/*_pipeline_l1_facts.txt` | `inferred_high` 条目新增 `reasoning_chain`（1–4 条短句，写「怎么推出来的」，不复述依据） |
| `src/customer_profile/templates/*_pipeline_l1_fields.txt` | **每条**结论新增 `reasoning_chain`；示例补一条完整推演（「因预算含税险约 9.5 万且选贷款 → 支付能力偏紧 → 收入量级取低档」） |
| `src/customer_profile/projection.py` | `Claim` 增 `reasoning_steps: tuple[str, ...]`；`_readable_steps` 分支**倒置**：`reasoning_steps` 优先，`quote` 只进 `evidence`；`_readable_evidence` **去掉** `basis_of_inference` 兜底 |
| `src/customer_profile/workflows/customer_profile_pipeline.py` | `localize_claims` / `project_claims` 透传 `reasoning_chain` |
| `tests/test_projection.py` | 新增：有 `reasoning_chain` 时 `steps` 取它、`evidence` 取原文，两者不同集；无 `reasoning_chain` 时回退且不复用锚点行 |

### 文档

| 文件 | 改动 |
|---|---|
| `docs/business_opt01_v2/v3.1-方案设计.md` | 施工中发现的口径修正回填 |
| `src/customer_profile/templates/README.md` | 若可读性要求表述需跟随，最小更新 |

---

## 3 施工顺序与验收

| 步骤 | 内容 | 验收判据（零 token） |
|---|---|---|
| **S0** | `scripts/probe_v3_replay.py` 扩 v3.1 判据 | 跑通，输出四类新读数（同集率 / 顾问词命中 / 噪音锚点 / 信号缺失） |
| **S1** | 确定性字段注入 | `phone_number` 覆盖 7/7；有试驾/旧车时 `driver_license_status=yes` |
| **S2** | 说话人归属 + 值形态门 | 客户字段命中顾问词表 = 0；1c 不得自报 `customer` |
| **S3** | 锚点相关性门 | 问候语/JSON 残片证据 = 0；信号型字段 `value_signal` 全为锚点子串 |
| **S4** | 推导链分离 | `steps` 与 `evidence` 同集率 ≤ 10%（122/234 → ≤ 24） |
| **S5** | 全量回归 | `pytest tests/` 全绿；v3 三条接口判据不回退 |

**顺序理由**：先 **S1**（纯代码、零语义、可独立回退，立刻补覆盖缺口），再 **S2/S3**
（产出内容错，比措辞雷同更严重），最后 **S4**（呈现层，依赖前几步的干净素材）。

**施工回填（三处与任务书原设计的偏差，详见设计稿 §6）**：

1. **注入落点改在 `L1_LOCALIZE` 之后**（原 §2-S1 写「接在 `L1_MERGE` 与 `L1_LOCALIZE`
   之间」）。注入来源名在记录层无对应记录，接在前面会被定位层按 `unknown_channel` 降级，
   正好废掉刚标注的登记档。实际链路 `L1_LOCALIZE → L1_INJECT → L2_PROJECT`。
2. **改造一不动 `_readable_evidence`**（原 §2-S4 写「去掉 `basis_of_inference` 兜底」）。
   v3 的验收判据本身允许推断依据充当证据，去掉它是回归而非修复；只改 `_readable_steps`。
3. **改造二不把 1c 的 `subject` 一律置 `unknown`**（原 §2-S2 写「1a 无则该条置 unknown」）。
   实测会打掉 38 个已进 Notes 的字段（16.2%），改为「1a 有则以其为准 + 值形态门兜底」。

**新增用例**：`tests/test_deterministic_inject.py`（S1）、`test_projection.py` 值形态门段
（S2）、`test_l1_merge.py` 主体继承段（S2）、`test_pipeline_l1.py` 锚点相关性 + 推导链段
（S3/S4）。

---

## 4 验收判据明细

**v3 既有判据（不回退，继续跑）**：证据位自指 = 0；无坐标事实率 = 0；身份字段来源白名单
命中 = 100%；整案归零率 = 0；审计词命中 = 0。

**v3.1 新增机器判据**：

- **推演同集率**：同一字段 `set(steps)` 与 `set(evidence)`（去前缀、按 `；` 拆分后）相等 → 命中；
- **顾问词命中**：客户字段取值或锚点切片命中 `SALESPERSON_MARKERS` → 命中即失败；
- **噪音锚点**：证据位出现问候语或含 `": "` 的 JSON 残片 → 命中即失败；
- **信号缺失**：信号型字段的 `value_signal` 非锚点子串 → 命中即失败；
- **确定性覆盖**：`phone_number` 7/7；有试驾/旧车证据时 `driver_license_status=yes`。

**回放口径**：v3-fix 同批 7 案（`A-pipeline-L8-v3-20261014-fix/runs`）+ 基线同号
（`baseline-39-dsv41-noThink/runs`）。逐案与合计都看。

---

## 5 风险与回退

| 风险 | 处置 |
|---|---|
| `reasoning_chain` 又被写成依据复述 | 验收用**同集率**机器判定，不靠人看；S4 单独提交便于二分 |
| 顾问词表误伤客户自述（客户自称「我做销售的」） | 词表只收**顾问/我方**指向词，不含泛职业词；命中仅拒收，可人工确认补 |
| `value_signal` 拖累召回 | 先在 4 个信号型字段强制，其余记录不强拦（先宽后可收） |
| 1c 的 `subject` 一律覆盖，盖掉合理的 `spouse` | 仅在 1a 有该 `attribute` 时取其 `subject`；1a 无则置 `unknown` |
| 注入值与 LLM 结论冲突 | 按 `basis` 取更硬；同档**以注入为准**（注入是定义级蕴含，非猜测） |
| 回退 | S1–S4 各自单独提交，便于二分 |

---

## 6 阶段验收记录

> 施工中逐条填入实测读数与结论。

| 步骤 | 判据 | 实测 | 结论 |
|---|---|---|---|
| S0 | 探针跑通、四类新读数 | `probe_v3_replay.py` 输出改造一~四四类读数 | ✅ |
| S1 | 确定性字段注入 | `phone_number` 覆盖 **7/7**；`driver_license_status` 注入 **5 案**（余 2 案无试驾无旧车，规则正确地不注入） | ✅ |
| S2 | 顾问词命中 = 0 | 值形态门拒收 **1** 条（`13435844484` 姓名「郑小姐（顾问自称，非客户姓名）」）；客户自述「做销售的」未被误伤 | ✅ |
| S3 | 噪音锚点 = 0 | 收回引文 **7** 条（4 处 `wechat_id": "…` 残片、2 处 `gender/nickname` 残片、1 处「喂，你好」寒暄语），逐条核对全为真阳性 | ✅ |
| S4 | 推演同集率 ≤ 10% | 旧产物 216 字段同集 0（弱证据）；**新批次 244 字段同集 0（0.0%），真推导链 202** | ✅ |
| S5 | 全量回归 | `pytest tests/` 全绿（含既有 skip）；11 个改动文件 UTF-8 无 BOM 无乱码 | ✅ |

**S4 口径说明（重要）**：0.0% 同集率**在首次回放里是弱证据**。冻结产物出自 v3/L1 三层
之前，**没有 `reasoning_chain` 字段**，`steps` 走的是「不复述材料」的退路句
（「由客户原话直接得出该结论」等 22 个字段）或渠道名——同集自然为 0。真正的推导链质量
要等**带 `reasoning_chain` 的新批次**才能验证：提示词已要求 L1 各层产出，探针已能区分
「真推导链」与「退路占位」（本次实测真链 0 / 退路 216）。**该判据未在本轮闭环。**

**S4 闭环（v3.1 批次 `A-pipeline-L8-v3.1-20261014`，8 案齐）**：带 `reasoning_chain` 的
新批次已跑（含补跑成功的第 8 案 `13829156876`）。读数：有 `steps`/`evidence` 的字段
**244** 个，两者同集 **0（0.0%）**，其中**真推导链 202**、退路占位 42——同集率低是**强证据**
（真链占 82.8%，不是靠退路句把同集率做没）。判据 **≤ 10% 达标**。

**覆盖不回退（完整链路回放，v3-fix → v3.1）**：逐案 Notes 条数 `30/27/31/42/37/42/25`
→ `30/27/31/42/36/42/25`，合计 **254 → 253**。差额**全部**来自 `13435844484` 的
`customer_name` 被值形态门拒收（意图内）；其余 6 案零丢失。`driver_license_status` 在 6 案
被确定性注入改写为 `fact` 档。

**v3 三条接口判据（不回退）**：证据位自指 **0**、无坐标事实 **0**（46/46）、身份字段来源
越界 **0**、审计词命中 **0**、整案归零 **0 案**——全部保持。

**v3-fix 基线（改动前）**：推导链同集 122/234（52.1%）；顾问信息串入 ≥1 例
（`13435844484` 姓名）；噪音锚点 2 例（`13530559591` 问候语、`13435844484` JSON 残片）；
`phone_number` 靠截图反捞、`driver_license_status` 无一条走规则。
