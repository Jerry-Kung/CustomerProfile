# business_opt01_v2 · 施工计划

> 定位：`docs/business_opt01_v2/端到端方案设计.md`（下称「设计稿」）的施工级执行计划。
> 设计稿讲「做成什么样」，本文讲「怎么落地、验收怎么看」。
>
> 分支 `dev-business-opt01-v2`，基线提交 `bea6ea2`。**不改主干**。
> 验收一律**零 token 离线**：用批次产物回放 + 单元测试，不跑 AB（用户明确「AB 实验待完工后再开展」）。

---

## 1. 一条硬约束：共用子流程不得就地改动

设计稿 §4.4 与 §12 要求「关闭 3 个白跑的 LLM 节点」并「删除 production」。但实测三处被
**基线臂**直接消费：

| 共用件 | 基线臂消费点 | 就地删除的后果 |
|---|---|---|
| `audio_content_extract`（2 个 LLM 节点） | `audio_flow` 的 `AUDIO_EXTRACT_SINGLE` / `AUDIO_EXTRACT_IN_ITERATION` 读它的 `file_content` | 基线臂图结构改变，`definition_hash` 漂移，AB 可比性判据二失效 |
| `moments_info`（1 个多图总结 LLM） | `evidence_subagent` 的 `MOMENTS` 路由读它的 `result` | 同上 |
| `customer_profile_production`（12 个 LLM 节点） | 基线主入口 `customer_profile_entry` 的 `PRODUCTION` 节点 | 基线臂产物直接变样 |

**因此一律走「v2 专属变体」，原定义逐字不动**——这是设计稿 §4.4 自己推荐的方案 1
（「在子流程内加纯识别出口」）的等价实现，且回退只需改一个常量。

`SUBWORKFLOWS` 导出机制已具备（`workflows/__init__.py::load_all`），变体作为**新
workflow_id** 注册，与原件并存。

---

## 2. 阶段分解与验收

| 阶段 | 内容 | 验收判据（零 token） |
|---|---|---|
| **P1** | L0 关闭 3 个白跑 LLM 节点：新增 `audio_content_extract_pure` 与 `moments_info_pure`，管线切到变体 | 原 `audio_content_extract` / `moments_info` 定义**逐字未变**（节点数与图结构断言）；变体图内**零 llm 节点**；管线产出的录音文本与关闭前逐字一致 |
| **P2** | L1 改为字段级直出 + 三分类；重写提示词 | `fact` 无锚点率 = 0；字段数不低于基线（**受 P3 制约，见 §6.3**）；`inferred_low` 可识别 |
| **P3** | L2 把「拒绝丢弃」改为「降级保留」，并加 1 次受约束 LLM 兜底 | 枚举归一失败不再丢字段；b 规则由丢弃改为降级；兜底不得新增候选集外的值 |
| **P4** | L3 重写：代码校验 + 1 次 LLM 复审 + 表达层，复用 6 个预置模板 | 9 个 section 与 Notes 同源；无新增事实 |
| **P5** | 删旧：11 路 production 生成、`pipeline_evidence_llm`、兼容覆写节点 | 全链路跑通，产物形状与基线同形 |

**顺序理由**：P1 独立且零质量风险，先做以锁定「成本下降」这一确定收益；P2 是召回瓶颈的
主因，必须先于 P3（否则 P3 的降级规则没有输入）；P4 依赖 P2/P3 定稿的 `facts`；P5 最后，
因为覆写节点是 P2–P4 期间的度量手段。

---

## 3. 文件级改动清单

### P1
| 文件 | 改动 |
|---|---|
| `workflows/audio_content_extract.py` | 抽出 `build_workflow(pure_recognition=False)`；`WORKFLOW` 保持 12 节点；新增 `PURE_WORKFLOW`（6 节点、零 LLM）与 `SUBWORKFLOWS` |
| `workflows/screenshot_flow.py` | `ScreenshotFlow` 加 `summary_llm: bool = True`；为 False 时不建 `SUMMARY_LLM`，`AGGREGATE_2` 只接 `VISION_SINGLE` |
| `workflows/moments_info.py` | 新增 `PURE_FLOW` / `PURE_WORKFLOW` 与 `SUBWORKFLOWS` |
| `workflows/customer_profile_pipeline.py` | `AUDIO_EXTRACT_WORKFLOW` 与 `MOMENTS` 的 `@workflow` 指向变体 |
| `tests/test_pipeline_l0.py` | 取数声明比对加变体别名映射（防日后单边漂移的口径不变） |

### P2
| 文件 | 改动 |
|---|---|
| `templates/customer_profile_pipeline__pipeline_l1_llm.txt` | 按设计稿 §5.6 四处要点重写（**提示词改动，业务优化，已随设计稿一并获批**） |
| `workflows/customer_profile_pipeline.py` | `localize_claims` 改为「定位失败降级而非丢弃」；`build_l1_input` 微调 |
| `projection.py` | `Claim` 加 `basis` / `basis_of_inference` 字段 |

### P3
| 文件 | 改动 |
|---|---|
| `projection.py` | 规则 b / d 由「丢弃」改「降级保留 + 转确认」；新增 `basis` 参与冲突消解排序 |
| `workflows/customer_profile_l2_fallback.py`（新） | 受约束的 LLM 兜底节点构造（裁决候选集，不得新增值） |
| `templates/customer_profile_pipeline__pipeline_l2_fallback.txt`（新） | 兜底提示词 |

### P4
| 文件 | 改动 |
|---|---|
| `workflows/customer_profile_l3.py`（新） | 代码校验 + LLM 复审 + 表达层；`template-transform` 引用 6 个既有预置资产（**正文一字不改**） |
| `templates/customer_profile_l3__*.txt`（新） | 复审与表达提示词 |

### P5
| 文件 | 改动 |
|---|---|
| `workflows/customer_profile_pipeline.py` | 删 `EVIDENCE_LLM`、`NOTES_OVERWRITE`、`PRODUCTION` 旧接线，改接 L3 |
| `workflows/customer_profile_production.py` | 保留文件与 6 个预置节点（资产）、`CODE_SPECS` 中 L3 复用的部分；LLM 节点随接线拆除 |

---

## 4. 轻量级验证方案（零 token）

**语料**（均已在本地，被 gitignore）：

- `experiments/abexperiments/batches/A-pipeline-R1b-20261010/runs`（13 案，带 `pipeline_l1_llm`
  / `pipeline_l1_localize` / `pipeline_l2_project` / `pipeline_l0_records` 全量节点产出）；
- `experiments/batches/baseline-39-dsv41-noThink/runs`（39 案基线，对照用）。

**手段**：

1. **单元测试**（`pytest tests/`）：纯夹具钉住判定语义，干净检出全绿；
2. **离线探针**（`scripts/probe_pipeline_*.py`）：读批次产物，复算 L1/L2 各层产出与卡点；
3. **P1 专项**：断言原定义未变 + 变体零 LLM 节点 + 用 R1b 的 `auc_result` 回放，
   比对 L0 记录层的 `content` 与关闭前逐字一致。

**不做的**：不跑 AB、不出网、不写回、不调用模型。

---

## 5. 风险与回退

| 风险 | 处置 |
|---|---|
| 变体与原件漂移（日后只改一边） | `test_pipeline_l0.py` 的取数声明比对保留并扩展别名映射 |
| `variable-aggregator` 单源未验证 | P1 补一条单源聚合用例 |
| 提示词重写引入回归 | P1 先落地，P2 的提示词改动单独提交，便于二分 |
| 兜底 LLM 越界新增 | 输出 schema 与代码双重校验，越界即拒并留痕 |


---

## 6. 阶段验收记录（零 token 回放）

### 6.1 P1 —— 已通过

`tests/test_pipeline_l0_pure.py` 17 条全绿：原件节点数与 LLM 数**逐字未变**（`audio_content_extract`
12/2、`moments_info` 18/3、两条录音流程各 18/2）；四个变体图内 LLM 节点数为 0；管线三处
`_tool` 已切到变体；两图 ``validate()`` 干净。全量 `pytest` 无失败。

### 6.2 P2 —— 部分通过，第三项待 P3

回放口径：取 R1b 13 案产物里的 `pipeline_l1_llm.result`（**迁移前的 v1 断言账本**），原样喂回
新的 `localize_claims` → `project_claims`，与产物中记录的旧结果对照。

| 判据 | 结果 |
|---|---|
| `fact` 无锚点率 = 0 | **通过**。421 条留存断言中 `fact` 122 条，**全部**带 `quote`（引文切自原文）；`inferred_low` 299 条。 |
| 定位失败不再丢字段 | **通过**。留存 391 → **421**；旧 `dropped` 合计 30 条，新 `kept` 增量恰好 **+30**（逐案核对无残余丢弃）。 |
| 字段数不低于基线 | **测不了，卡在 P3**。见 §6.3。 |

### 6.3 为什么「字段数不低于基线」要等 P3

13 案中有 13 案可在基线 39 案里找到同号对照。逐案比 Notes 条数：

```
13 案均值   基线 27.0 → 回放后 15.0   （差 -12.0）
```

差额**不是 P2 造成的**。R1b 产物里的 `rejected_json` 显示卡点全在 L2 的 b/c/d 三条规则上，
而这三条本轮**仍是 v1 的「丢弃」语义**（`projection.py` 未改，属 P3 范围）：

| 规则 | 含义 | 归属 |
|---|---|---|
| `b_prediction_only` | 身份字段仅由第三方预测支撑 → 整条丢弃 | P3 改降级保留 + 转确认 |
| `c_conflict` | 同档多值冲突 → 整条丢弃 | P3 改按 `basis` 排序消解，冲突转确认 |
| `d_enum` | 枚举值归不到合法键 → 整条丢弃 | P3 改降级保留 + 转确认 |

**两条必须记住的回放失真**，否则会误读上面的数字：

1. **v1 产物没有 `basis_of_inference`。** v2 提示词要求 `inferred_*` 必填推断依据，v1 不要求，
   因此回放时 299 条 `inferred_*` 全被判「无依据」压到 `inferred_low`，`downgraded` 合计 481 条
   （**大于**留存数 421，正是因为一条会被多次降级）。这个数**不代表 v2 的真实降级率**——
   v2 的提示词会给出依据，降级率必然远低于此。它只说明「降级路径本身工作正常」。
2. **唯一逆向的一案**（`15818891947`，20 → 19）：`personality_and_preferences` 与
   `viewing_progress` 被 `c_conflict` 丢弃，`phone_number` 新增。净 -1，**同样归因于 P3 未改**。

**结论：P2 的两条硬判据（无锚点率、不丢字段）已达成；召回判据必须等 P3 落地后重测。**
P3 完成后用同一套回放重跑本节表格，届时「字段数不低于基线」才有意义。


### 6.4 P3 —— 已通过（投影层部分）

同一套回放（R1b 13 案，仍喂 v1 断言账本）。b/c/d 三条规则由「拒绝丢弃」改为
「降级保留」后：

```
13 案均值   基线 27.7   →   P3 20.2      （P2 时是 15.0，回升 +5.2）
```

**关键读数：回放的天花板是 21.5，不是 27.7。** 逐案量出每份 v1 断言账本里出现过的
**不同字段数**，其均值 21.5；P3 输出 20.2，**离天花板只剩 1.4**。也就是说：

- 投影层已基本吃满它拿到的输入（13 案里 4 案零缺口，含 2 案输出**多于**输入字段）；
- 距基线的 7.5 缺口**不是投影层造成的**——v1 的断言账本**压根没提**那些字段。
  提升覆盖要靠 v2 的新提示词（P2 已重写，但零 token 回放看不见它）。
  **这条要等到 AB 才能实测。**

P3 之后仍被拒收的断言只剩三类，逐类都是**正确**行为：

| 规则 | 条数 | 性质 |
|---|---|---|
| `c_conflict` | 15 | 同档单值语义多值冲突 → 留白转确认（**设计稿 §6.2 明确保留此行为**）；回放不跑 LLM 兜底，故这部分在回放口径下必然损失 |
| `a_attribution` | 12 | 主体非客户本人 → 不得写客户身份字段（**保留不变**，设计稿 §6.2 规则 a） |
| `f_deregistered` | 1 | 关联企业已注销 → 不得当作在职企业（**保留不变**） |

`to_confirm` 合计 113 条，即 LLM 兜底的实际输入规模。

#### 兜底接线（已完成）

新增两个节点，接在投影层之后：

| 节点 | 类型 | 职责 |
|---|---|---|
| `pipeline_l2_fallback` | `llm` | 受约束的裁决者。输入只有 `candidates_json`（规则判不了的字段），输出终判 |
| `pipeline_l2_apply` | `code` | 校验终判并并入 Notes。**越界值一律拒收留痕**（`apply_l2_verdict`） |

`NOTES_OVERWRITE` 的输入改指 `L2_APPLY`。管线 40 节点 / 3 个 LLM 节点（L1 取证、
L2 兜底、既有证据线索整理），`validation.validate` 报 0 错误；四个纯识别变体各 0 错误。

**越界拒收已实测**：合成一份含「候选集外的『航天员』」的终判，`apply_l2_verdict`
`out_of_bounds=1`、只并入候选集内的取值。这是设计稿 §6.3 那条硬边界
（「兜底是裁决者，不是作者」）的验收。

**另一处收口**：候选集与 `updates` 按**同一 `tier` 口径**收窄。`thin` 档刻意只留
登记档坐实的字段，若候选集仍按全量建，兜底就能把该档排除的非登记字段重新写回去，
等于绕过分诊层的保守约束。


### 6.5 P4 —— 已通过（编码层）

**L3 校验与表达层**：新建 `workflows/customer_profile_l3.py`（11 节点 / 1 个 LLM），
替代 production 在成文段的位置。

```
facts（L2 已定稿）
  ↓ l3_validate  代码校验
  ↓ l3_express   1 次 LLM：把同一份 facts 写成 9 个 section
  ↓ l3_assemble  代码装配 + 同源检查
result1（含 9 个 section + basic_notes_updates）
```

| 判据 | 结果 |
|---|---|
| 9 个 section 与 Notes 同源 | **通过**。两者都读**同一份 L2 `facts`**；Notes 由代码直取，不经表达层转手 |
| 表达不新增事实 | **结构保证**。表达层的绑定只有「已定稿事实 + 业务资产」，**拿不到证据原文**，无处编造 |
| 无依据确定值率 = 0 | **通过**。13 案回放 `unbacked_count = 0`，`valid = 13/13` |
| 9 节齐全 | **通过**。13 案全部 `section_count = 9`（表达层缺哪节就补空形态，**不编造**） |

**6 个业务资产按原名引用**：`PRODUCTION_ASSET(node_id) → customer_profile_production__<id>`。
不复制副本——「正文一字不改」因此**可验证**（用例断言模板名指回生产图那份）。

**P3×P4 接口冲突（已修）**：P3 规则 d 刻意保留归不到合法枚举键的客户原话（「想上广州
那边的牌」）并降为 `inferred_low`，而 L3 校验原本把 facts 里的非法枚举一律判为阻塞，
两条规则在同一份数据上对冲（实测 13 案报 12 条 error）。修法是让校验**按 `basis` 放行**：
已降级的记 **warning**（仍可见、仍待人工归一键），未降级的才是 error。设计稿 §7.2 的本意
是拦「写成了确定值却没有依据」，不是拦「已明确降级的软值」。

**图校验**：L3 11 节点 0 错误；四个纯识别变体各 0 错误。

### 6.6 P5 —— 已通过

| 删除项 | 结果 |
|---|---|
| 管线 9 个死常量（`EVIDENCE_INPUT` / `EVIDENCE_LLM` / `STANDARD_AGGREGATE` / `PROFILE` / `EVIDENCE_MODULE` / `EVIDENCE_PROMPT_TEMPLATE` / `PROFILE_WORKFLOW` / `PRODUCTION_WORKFLOW` / `NOTES_OVERWRITE`） | 已删，全仓无引用 |
| `pipeline_evidence_input` / `pipeline_evidence_llm` / `pipeline_profile` / `pipeline_standard_aggregate` 四个节点 | 已删（换 L3 后从入口不可达，而不可达判 **error**，故与接线必须同批） |
| `NOTES_OVERWRITE` 节点 + `overwrite_notes_updates` 函数 + 只服务它的 6 条用例 | 已删；`END` 改由 L3 直取 `result1` |

**管线终态：35 节点 / 2 个 LLM 节点**（L1 取证、L2 兜底），`validate` 0 错误 0 警告。

**一处刻意的保留**：`customer_profile_production` 模块**不动**——基线臂的入口图
（`customer_profile_entry`）正是通过它产出的，删它会让基线臂产物变样、`definition_hash`
漂移（见 §1 的硬约束）。v2 臂只是**不再走它**。设计稿 §12 的「删除 production 的 12 个
LLM 节点」，在 AB 跑完、基线臂退役之前，执行方式是**弃用而非删除**。

### 6.7 收官读数与仍未闭合的一项

同一套回放（13 案，仍喂 v1 断言账本）：

```
13 案均值   基线 27.7   →   终态 20.2   （差 -7.5）
13 案 section 数          全部 9
13 案校验                 13/13 通过，无依据 fact = 0
```

**这 -7.5 不是本轮的缺陷，且回放口径下测不闭。** 逐案量出每份 v1 断言账本里出现过的
**不同字段数**，均值 **21.5**；终态 20.2，**离这份输入的天花板只剩 1.3**。也就是说：

- 投影与装配已吃满它们拿到的输入（13 案中 4 案零缺口，2 案输出字段多于输入）；
- 距基线的缺口来自 **L1 的字段覆盖率**——v1 的断言账本压根没提那些字段。
  v2 的新提示词（P2 已重写）目标正是覆盖率，但**零 token 回放看不见提示词的效果**。

**因此「字段数不低于基线」这条判据要等 AB 才闭合**，届时看的是 v2 提示词带来的覆盖增量。

### 6.8 与设计稿成本预估的对应（未实测）

设计稿 §9 估「删 production 12 个 LLM、改为一次表达 + 一次复审」降 **约 −32.2%**。
本轮把它落成了 **1 次 LLM**（L3 表达；复审并入校验与装配的同源检查，未单开调用），
方向上不低于预估，但**实际降幅要等 AB 实测**——回放不调用模型，测不出成本。

P4/P5 的单元用例在 `tests/test_l3.py`（31 条）。全量 `pytest` 无失败。
