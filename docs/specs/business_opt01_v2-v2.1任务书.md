# business_opt01_v2 · v2.1 整改任务书

> 施工级任务书。设计依据见 `docs/business_opt01_v2/v2.1-整改设计.md`（下称「设计稿」）。
> 分支 `dev-business-opt01-v2`，基线提交 `bea6ea2`。**不改主干。**
> 验收一律**零 token 离线回放**（L8 8 案 + 基线同号对照），不跑 AB、不出网、不写回、不调模型。
>
> 工具：`python -I scripts/probe_v21_audit.py`（读数）、`python -I scripts/probe_v21_replay.py`
> （把 L1 原始输出喂回当前代码重算，测代码改动）、`python -I scripts/probe_v21_leak.py`
> （定位审计词从哪条字段漏出）、`pytest tests/`（回归）。

---

## 1 三条硬约束（先立规矩，再改代码）

1. **对客产出必须真人可读**（设计稿 §1）。`reasoning_detail.steps` / `evidence` 不得出现
   审计与规则词、来源档代号、内部字段名。黑名单见 §4。
2. **不新增事实**。补全与改写只能落到 `inferred_low`，不得造出候选集外的确定取值。
3. **不推翻 v2 架构**。只修三处实现缺陷，P1–P5 的产物形状与 AB 可比性判据不变。

---

## 2 文件级改动清单

### R1 · 可读性（问题 1）

| 文件 | 改动 |
|---|---|
| `src/customer_profile/projection.py` | `Claim` 增 `quote` 字段；`_to_notes_updates` 重写 `reasoning_detail`（evidence 用原文切片/推断依据，steps 写推导链，不留「未知渠道」占位） |
| `src/customer_profile/workflows/customer_profile_pipeline.py` | `project_claims` 把 claims 的 `quote` / `anchor` 透传进 `Claim` |
| `tests/test_projection.py` | 新增：审计词黑名单用例、evidence 形态用例、无 quote 退回用例 |

### R3 · Notes 保真（问题 3）

| 文件 | 改动 |
|---|---|
| `src/customer_profile/workflows/customer_profile_pipeline.py` | `localize_claims` 渠道处理分层：`inferred_*` 无渠道不降级 |
| `tests/test_pipeline_l1.py` | 新增：`inferred_high` 无渠道不被降级；`fact` 无渠道仍降级 |

### R2 · 覆盖（问题 2）

| 文件 | 改动 |
|---|---|
| `src/customer_profile/templates/customer_profile_pipeline__pipeline_l1_llm.txt` | 收紧：`value` 不得为空，要么给值要么整条不输出 |
| `src/customer_profile/workflows/customer_profile_pipeline.py` | `project_claims` 入口对空值断言拒收并计数（`empty_claims` / `empty_claim_names`） |
| `src/customer_profile/projection.py` | `build_l2_candidates` 增 `missing_fields`（L1 未给结论的契约字段） |
| `src/customer_profile/templates/customer_profile_pipeline__pipeline_l2_fallback.txt` | 增「补缺」任务：对 missing_fields 给 `inferred_low`，取值须来自 facts 归纳 |
| `src/customer_profile/templates/customer_profile_l3__l3_express.txt` | 叙事型 section 放宽：可基于 facts + 资产归纳，措辞限 `inferred_*` 口吻 |
| `tests/test_projection.py` / `tests/test_l3.py` | 新增：空值断言计数、missing_fields 候选、叙事 section 措辞 |

### 文档（硬约束落地）

| 文件 | 改动 |
|---|---|
| `CLAUDE.md` | 新增一节「对客产出必须真人可读」 |
| `docs/README.md` | 写作原则补一条 |
| `docs/business_opt01_v2/端到端方案设计.md` | §1 优先级一节补一句指向设计稿 §1 |
| `src/customer_profile/templates/README.md` | 补一句提示词产出的可读性要求 |

---

## 3 施工顺序与验收

| 步骤 | 内容 | 验收判据（零 token） |
|---|---|---|
| **S0** | 写 `scripts/probe_v21_audit.py`（只读审计探针） | 跑通，输出三类读数 |
| **S1** | R1 可读性 | 审计词命中 = 0；证据串含原文切片或推断依据 ≥ 90%；「未知渠道」= 0 |
| **S2** | R3 保真 | `unknown_channel` 降级 = 0；`inferred_high` 字段数 > 0 |
| **S3** | R2 覆盖 | Notes 字段数逐案 ≥ 基线；`usage_scenarios` 逐案 ≥ 基线 |
| **S4** | 文档硬约束落地 | `CLAUDE.md` / `docs/README.md` 含新规则 |
| **S5** | 全量回归 | `pytest tests/` 全绿 |

**顺序理由**：S1 只改产物文案，零风险且立刻可见，先做；S2 是 S3 的前提
（渠道塌陷不修，`inferred_high` 无从谈起）；S3 改动面最大（提示词 + 兜底 + 表达），最后做。

---

## 4 验收判据明细

**审计词黑名单**（`reasoning_detail.summary` 与 `steps` 内命中即失败）：
`归属过滤`、`来源分级`、`枚举归一`、`单多值约束`、`契约校验`、`未知渠道`、
`社交自呈现`、`一手交互`、`第三方预测`、`公开登记查询`、`人工/系统登记`、
`按来源分级投影得到`、`断言`

**回放口径**：L8 8 案（`A-pipeline-L8-20261012/runs`）+ 基线同号
（`baseline-39-dsv41-noThink/runs`）。逐案与合计都看。

---

## 5 风险与回退

见设计稿 §5。要点：R2 的提示词改动**单独提交**，便于二分；补全受约束兜底，
不得越界新增；回退只需回退本批 commit。

---

## 6 阶段验收记录

**口径**：`python -I scripts/probe_v21_replay.py` 把 L8 批次 L1 **原始输出**
（`pipeline_l1_llm.parsed_json`）喂回当前代码重算，测的是**代码改动本身**的效果；
不出网、0 token。`pytest tests/` 为全量回归。

> **回放口径的一处修正（施工中发现）**：探针原来读 `pipeline_l1_localize.claims_json`——
> 那是**已经定位过**的账本，`fact` 早被当轮降级，再喂回来只会测出「降级后仍是降级后」。
> 改为读 `pipeline_l1_llm` 的原始输出后才测量到真实效果。

| 步骤 | 判据 | 实测 | 结论 |
|---|---|---|---|
| **S1** | 审计词命中 = 0 | `[]` | ✅ |
| **S1** | 「未知渠道」= 0 | 0 / 303 | ✅ |
| **S1** | 证据串可读 ≥ 90% | 299 / 303 = **99%** | ✅ |
| **S2** | `unknown_channel` 降级 = 0 | 0（余 `anchor_not_found` 15、`anchor_too_short` 2、`inferred_without_basis` 2，均为应有降级） | ✅ |
| **S2** | `inferred_high` 字段数 > 0 | `fact` 29 / `inferred_high` 21 / `inferred_low` 138 | ✅ |
| **S3** | 缺字段候选生成 | 145 条 / 38 个字段（逐案 8–27），交 L2 兜底补全 | ✅（代码侧） |
| **S5** | `pytest tests/` 全绿 | **531 passed, 35 skipped** | ✅ |

**施工中发现并修掉的额外缺陷**：字段级 `basis` 此前**只在降级分支**被写入，未被降级的
字段在档案里没有档位（`facts` 21 个字段只有 11 个有档位）。下游会把「缺失」默认当成
`fact`，探针则误报成「全是 `inferred_low`」——两边都读错。修正为在所有路径落档
（`result.basis.setdefault(attribute, best.effective_basis)`，不覆盖 b 硬规则的降级）。

**未在本轮离线验证、留待 AB 的部分**：

- **S3 的产出侧读数**（Notes 字段数逐案 ≥ 基线、`usage_scenarios` 逐案 ≥ 基线）依赖
  **L2 兜底与 L3 表达两次 LLM 调用**，回放测不到；须在 R2 全量跑批时按基线同号对照。
  历史产物的对照读数（V2=190 / 基线=226，比值 0.84）是**改动前**的账，不作为 v2.1 判据。
- 参照：`python -I scripts/probe_v21_audit.py` 读的是改动前的批次产物，审计词命中仍高，
  属预期——它测的是历史账，不是当前代码。

