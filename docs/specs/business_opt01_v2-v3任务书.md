# business_opt01_v2 · v3 任务书

> 施工级任务书。设计依据见 `docs/business_opt01_v2/v3-方案设计.md`（下称「设计稿」）。
> 分支 `dev-business-opt01-v2`。**不改主干。**
> 验收一律**零 token 离线回放**（v2.1 同批 8 案 + 基线同号对照），不跑 AB、不出网、不写回、不调模型。
>
> 工具：`python -I scripts/probe_v3_replay.py`（v3 回放读数）、
> `python -I scripts/probe_v21_audit.py`（同口径对照）、`pytest tests/`（回归）。
>
> **范围**：接口一/二/三 + L1 三分层 + L2 审查层。
> **L3 整改不在本任务书范围**，留到 v3 正式版（设计稿 §7）。

---

## 1 三条硬约束（先立规矩，再改代码）

1. **对客产出必须真人可读**（设计稿 §1）。`reasoning_detail.steps` / `evidence` 不得出现
   审计与规则词、来源档代号、内部字段名、占位串。黑名单见 §4。
2. **人设层不进证据位**（设计稿 §5.1）。`persona` 只作一致性参考；写进 Notes 或卡片的
   取值，其证据仍须回到原始数据或带坐标的 `fact`。
3. **不新增事实**。补全与改写只能落到 `inferred_low`，不得造出候选集外或 facts 外的确定取值。

---

## 2 文件级改动清单

### P1 · 可靠性（接口三，对应问题 5）

| 文件 | 改动 |
|---|---|
| `src/customer_profile/execution/llm.py` | 新增 `parse_jsonl_output()`（逐行解析，坏行留痕不丢整段）；新增 `salvage_json()`（括号配平走查，截到最后一个完整顶层对象）；`llm_call` 增 `expect_jsonl` 与「JSON 解析失败先抢救」路径 |
| `src/customer_profile/execution/executors_llm.py` | `llm` 节点读 `output_format`（`text` / `json` / `jsonl`）；`jsonl` 输出 `records`（列表）+ `dropped_lines`（坏行留痕） |
| `tests/test_jsonl_output.py` | 新增：JSONL 逐行解析、坏行只丢一行、括号未配平时抢救、抢救失败仍抛 |

### P2 · 证据链（接口一 / 二，对应问题 1、2、3）

| 文件 | 改动 |
|---|---|
| `src/customer_profile/projection.py` | `Claim` 增 `anchor_kind`（`verbatim` / `normalized`）+ `source_quote`；`_readable_evidence` / `_readable_steps` 对当归型事实写「原始→归一 + 理由」；新增身份字段来源白名单与 `title` 优先级降级 |
| `src/customer_profile/workflows/customer_profile_pipeline.py` | `localize_claims` 透传 `anchor_kind` / `source_quote`；新增审计判据：证据位自指、无坐标事实 |
| `tests/test_projection.py` | 新增：当归型事实证据形态、身份字段白名单、`title` 降级 |
| `tests/test_pipeline_l1.py` | 新增：`normalized` 事实的 `source_quote` 不被 `_locate` 当逐字锚处理 |

### P3 · L1 三分层（设计稿 §3）

| 文件 | 改动 |
|---|---|
| `src/customer_profile/templates/customer_profile_pipeline__pipeline_l1_facts.txt` | 新增：事实提取层提示词（只聚焦全 / 准 / 事实，输出 JSONL） |
| `src/customer_profile/templates/customer_profile_pipeline__pipeline_l1_persona.txt` | 新增：人设画像层提示词（半结构化 + 一段整体描述，每字段带 `provenance`） |
| `src/customer_profile/templates/customer_profile_pipeline__pipeline_l1_fields.txt` | 新增：分析投影层提示词（补齐字段，证据须回溯到 facts） |
| `src/customer_profile/workflows/customer_profile_pipeline.py` | 节点图改造：`pipeline_l1_llm` / `pipeline_l1_localize` → `l1_facts` + `l1_persona` + `l1_fields` + `l1_localize`（**实施改为直接绑定，不设装配代码节点**） |
| `tests/test_pipeline_l1.py` | 新增：三层输入装配、persona 不进证据位 |

### P4 · L2 审查核验层（设计稿 §3、§7-5）

| 文件 | 改动 |
|---|---|
| `src/customer_profile/workflows/customer_profile_pipeline.py` | 新增 `pipeline_l2_review`（代码审查：枚举合法、无效占位清除、同源、fact 必有锚点、证据位自指）+ 一次受约束 `l2_review_llm`（局部重写）；审查清单每条出「命中数」 |
| `src/customer_profile/templates/customer_profile_pipeline__pipeline_l2_review.txt` | 新增：审查层局部重写提示词 |
| `tests/test_pipeline_l1.py` | 新增：无效占位清除、审查命中计数 |

### 文档

| 文件 | 改动 |
|---|---|
| `docs/business_opt01_v2/v3-方案设计.md` | 施工中发现的口径修正回填 |
| `docs/README.md` | 若目录分工变化再更新 |

---

## 3 施工顺序与验收

| 步骤 | 内容 | 验收判据（零 token） |
|---|---|---|
| **S0** | `scripts/probe_v3_replay.py`（只读回放探针，扩展 v2.1 版） | 跑通，输出五类读数 |
| **S1** | P1 可靠性 | JSONL 坏行只丢一行；整案归零率 = 0 |
| **S2** | P2 证据链 | 证据位自指 = 0；无坐标事实率 = 0；身份字段白名单命中 = 100% |
| **S3** | P3 L1 三分层 | persona 不出现在任何证据位；三层产物形状正确 |
| **S4** | P4 L2 审查层 | 无效占位命中 = 0；枚举非法（未降级）= 0 |
| **S5** | 全量回归 | `pytest tests/` 全绿 |

**顺序理由**：**先修「整案归零」**——它不稳，其余改动的效果无法稳定观测（设计稿 §9）。
P1 只动执行层与解析，零业务语义变化，最先做且可独立回退。

---

## 4 验收判据明细

**审计词黑名单**（`reasoning_detail.summary` 与 `steps` 内命中即失败）：
`归属过滤`、`来源分级`、`枚举归一`、`单多值约束`、`契约校验`、`未知渠道`、
`社交自呈现`、`一手交互`、`第三方预测`、`公开登记查询`、`人工/系统登记`、
`按来源分级投影得到`、`断言`。

**新增机器判据（v3）**：

- **证据位自指**：`evidence` / `steps` 中出现「推断为…（置信度…）」这类「结论当依据」的形态 → 命中即失败；
- **无坐标事实率**：`basis=fact` 的条目，既无 `quote`（verbatim）又无 `source_quote`（normalized）
  且非登记档 → 命中即失败；
- **身份字段来源白名单**：`customer_name` / `title` 等的来源渠道须在白名单内；
- **整案归零率**：8 案全部产出 `result1`。

**回放口径**：v2.1 同批 8 案（`A-pipeline-L8-v21-20261013/runs`）+ 基线同号
（`baseline-39-dsv41-noThink/runs`）。逐案与合计都看。

---

## 5 风险与回退

| 风险 | 处置 |
|---|---|
| 人设层虽不入证据位，仍可能诱导后续写死人设结论 | 验收加「证据位自指」「无坐标事实」两条机器判据 |
| 身份字段白名单过紧，拦掉有效姓名 | 白名单按「一手自述 / 登记」给足形态，先宽后可收 |
| JSONL 改后下游消费方需同步改造 | 改动集中在装配层，按节点逐个评估，不散落 |
| L1 加两层引入新的归零点 | P1 的「节点级降级」为硬要求，先于 L1 拆分落地 |
| 回退 | P1–P4 各自单独提交，便于二分 |

---

## 6 阶段验收记录

> 施工中逐条填入实测读数与结论。

| 步骤 | 判据 | 实测 | 结论 |
|---|---|---|---|
| S0 | 探针跑通、五类读数 | `probe_v3_replay.py` 输出 5 类读数 | ✅ |
| S1 | JSONL 坏行只丢一行 | 新增 9 用例全过；全量 pytest 绿 | ✅ |
| S1 | 整案归零率 = 0 | **8/8 succeeded**（v3 批 `A-pipeline-L8-v3-20261013`；v2.1 崩溃的 `13435844484` 本次产出完整，JSONL + 括号抢救生效） | ✅ |
| S2 | 证据位自指 = 0 | 11 → **0** | ✅ |
| S2 | 无坐标事实率 = 0 | 0 / 56 | ✅ |
| S2 | 身份字段白名单命中 = 100% | 越界 4 → **0**（白名单拒收 4 案） | ✅ |
| S3 | persona 不进证据位 | L1 三分层落地；`test_l3` 结构用例过 | ✅（结构） |
| S4 | 无效占位命中 = 0 | `review_notes` 落地；6 条用例过 | ✅（代码） |
| S5 | `pytest tests/` 全绿 | 见下 | ✅ |

**v2.1 基线（改动前）**：整案归零 1 案（`13435844484`）、证据位自指 11、无坐标事实 0、
`customer_name` 社交档越界 4、审计词 0。

**P4 范围说明**：本任务书的 P4 原列「代码审查 + 一次受约束 LLM 局部重写」。实测两条验收
判据（无效占位、枚举非法）**都是确定性的**，代码即可判定并修补，故只落地代码审查节点
`pipeline_l2_review`；LLM 局部重写**延后**——它需新增一处 LLM 调用与错误面，而当前无
代码修不了的语义类 case 支撑它。若后续出现代码修不动的语义问题，再加。
