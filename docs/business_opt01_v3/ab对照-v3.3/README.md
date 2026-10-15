# v3.3 全量对照页（ab对照-v3.3）

> `docs/business_opt01_v3/` 下的一次**产物归档**，不属于文档主体。
> 本目录是 **v3.3 全量 39 案**的两臂对照页；v3.2 抽样版（8 案）已随本轮清理删除。

## 1. 这一版比什么

**同 39 个手机号、两条不同工作流**的输出直接比对：

| | 基线臂 | 实验臂 |
|---|---|---|
| 工作流 | `customer_profile_entry` | `customer_profile_pipeline` |
| 批次 | `experiments/batches/baseline-39-dsv41-noThink/` | `experiments/batches/pipeline-v3.3-39/` |
| 案数 | 39 | 39（全部 succeeded） |

两臂跑的是**同一批号码**，因此差异来自实现本身，不来自输入。

## 2. 关键读数（机器判定，零 token）

| 读数 | 基线 | v3.3 | 备注 |
|---|---|---|---|
| 跑通案数 | 39/39 | **39/39** | 两臂均无失败 |
| Notes 条数 | 1005 | **1564** | 156% of 基线 |
| 画像卡条目（九段） | 1524 | 1344 | 88% of 基线 |
| 单边字段 | 仅基线 112 | 仅 v3.3 634 | 两侧都有 893 |
| LLM 调用次数 | 897 | 317 | v3.3 的 L1 三层收敛为 3 次调用 |
| Token 用量 | 14.72M | 7.09M | |
| 单案耗时（中位） | 281.4 s | 117.3 s | |
| 重试救回 | — | 4 案 | `16602088554` `16761543238` `17119850673` `17266999882` |
| 异常留痕 | 未记录 | 0 条 | 新库有 `anomaly` 列，旧库没有 |

**十段逐项**（基线 → v3.3）：画像摘要 273→205、推断标签 195→190、用车场景 117→121、
沟通策略 140→89、客户概览 234→234、购车动机 106→113、产品偏好 170→132、
抗性/顾虑 173→108、情绪状态 116→152、基础信息更新 1005→1564。

**唯一留空案 `16761543251`**：5 段为空（用车场景 / 购车动机 / 产品偏好 / 抗性顾虑 / 情绪状态）。
这不是故障——该案事实簿只有「姓吴」「称呼先生」两条，L1 的 `fields` 节点合法返回
`{"fields": []}`（5 token，`finish_reason=stop`）。页面把它如实标出，**不补空**。

## 3. 数据存储结构（制作结果页面时按此读取）

单文件页面 `../画像管线AB对照-v3.3.html` 内的 `<script id="d-payload">` 即全部数据，
结构与 `data/payload.json` 完全相同：

```
{
  version, batch_new, batch_base, new_workflow, base_workflow,
  sections: [10 个段落键，顺序即 analysis_result 的键序],
  section_labels: { 段落键 -> 中文名 },
  tier_label: { fact|inferred_high|inferred_low -> 中文档位 },
  only_new: [...], only_base: [...],       // 两臂案号对不齐时非空（本次均为空）
  cases: [
    {
      phone,
      base: { status, error, duration_ms, workflow_id, llm, tok, max_attempt,
              anomaly, sections: {段落键 -> 该段原文}, notes: [...], sec_counts: {...} },
      new:  { ...同上... }
    }
  ]
}
```

要点：

- **`sections` 的十个键**（与 `analysis_result` 一致）：
  `profile_summary` `inferred_tags` `usage_scenarios` `communication_strategies`
  `customer_overview` `purchase_motivations` `product_preferences` `resistances`
  `emotion_state` `basic_notes_updates`。第五项之后**不含** v3.2 页的 `budget_and_decision`
  之类键——那些是该页自造的聚合名，不是本管线的输出键。
- **`notes`** 即 `basic_notes_updates` 里带 `attribute_key` 的条目；两臂按 `attribute_key`
  求并集即页面里的对照大表。
- **`sec_counts`** 逐段条数：列表按条数，值字典（如 `customer_overview`）按非空字段数。
  它只反映**内容量**，不代表准确性。
- **`anomaly`** 只在新版侧有值；基线库没有这一列，故基线段为 `null`——
  「列不存在」不等于「无异常」，页面按此口径呈现。

## 4. 文件清单

| 文件 | 内容 |
|---|---|
| `../画像管线AB对照-v3.3.html` | **成品**：单文件自包含页面，双击即开，数据已内联（4.09 MB） |
| `index.html` / `app.js` | 页面骨架与渲染器（成品源文件） |
| `build.py` | 打包脚本：`python -X utf8 build.py` |
| `data/payload.json` | 39 案两臂输出 + trace 读数（4.04 MB） |
| `scripts/extract_ab33.py` | 从两侧批次目录抽取 payload（**只读**） |
| `scripts/smoke.mjs` | 渲染冒烟：39 案 × 3 分段全部渲染，异常即红 |

生成顺序：`python -X utf8 scripts/extract_ab33.py` → `python -X utf8 build.py` → `node scripts/smoke.mjs`。

成品页与 `data/payload.json` **含真实客户数据，禁止入库**——本目录 `.gitignore` 已排除
（根 `.gitignore` 不覆盖此处，故单列一条，理由见 `.gitignore` 内注释）。

**Windows 注意**：含中文字面量的脚本一律用 `python -X utf8` 运行。
