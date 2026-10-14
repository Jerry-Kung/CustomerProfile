# v3 修复版对照页（ab对照-v3-fix）

> `docs/business_opt01_v2/` 下的一次**产物归档**，不属于文档主体。
> 上一版（未修复）见 `../ab对照-v3/`，本目录是**修掉 L1 三层合并遗漏**后的新版。

## 1. 修了什么

`ab对照-v3/` 那版暴露了一个严重 Bug：**1a 事实层已检出、最终 Notes 却为空**。

**根因**：v3 三分层把原先「一次直出全部字段」拆成「1a 出硬事实 + 1c 补推断」，而 1c
提示词明说「事实清单已给的字段不用重复」——两层是**互补**而非重叠。但节点图里
`pipeline_l1_localize` 的 `claims_json` **只接了 1c**，1a 的输出仅作为提示词上下文喂给
1b/1c，从未进入账本。于是 1a 检出的事实（如 `intended_brand=埃安`，带原文锚点的 `fact`）
在最终 Notes 里直接变空值。

**修法**：新增纯代码合并节点 `pipeline_l1_merge`（`merge_l1_layers`，0 token），把 1a 与 1c
按 `attribute` 求并集后再喂 `localize`。同字段两边都有时按 `basis` 档位取更硬的一条
（`fact` > `inferred_high` > `inferred_low`），同档以 1a 为准。落下位置：
`src/customer_profile/workflows/customer_profile_pipeline.py`。

## 2. 修复效果（同号 7 案，修复前 vs 修复后）

| 手机号 | 修复前 Notes | 修复后 Notes | 1a 检出但 Notes 为空 |
|---|---|---|---|
| 13415100087 | 12 | 27 | 8 → 0 |
| 13435844484 | 25 | 37 | 9 → 0 |
| 13527911093 | 28 | 31 | 1 → 1 |
| 13530559591 | 17 | 25 | 4 → 0 |
| 17119850673 | 10 | 30 | 0 → 2 |
| 18898883341 | 30 | 42 | 8 → 0 |
| 19820803068 | 17 | 42 | 16 → 0 |
| **合计** | **139** | **234** | **46 → 3** |

- `intended_brand` 在全部 7 案均回到 Notes（修复前 6/8 案丢失）。
- 残留 3 条：`13527911093` 的 `old_car_model`（修复前就缺，单点问题）；`17119850673` 的
  `company_type` / `occupation`（跑批为独立 LLM 调用，两次 1a 检出集合本就有差异，属抖动非回归）。

## 3. 本次批次

- 修复批：`experiments/abexperiments/batches/A-pipeline-L8-v3-20261014-fix/`（**7 案**）
- 基线批次：`experiments/batches/baseline-39-dsv41-noThink/`
- 未修复对照批：`experiments/abexperiments/batches/A-pipeline-L8-v3-20261013/`（8 案，本页不引用）

**为何只有 7 案**：第 8 案 `13829156876` 运行时卡在下游某次调用上，超 9 分钟未产出任何
产物，进程已按指示终止。补跑该案后本页可恢复为 8 案对照。

## 4. 文件清单

| 文件 | 内容 |
|---|---|
| `../画像管线AB对照-v3-fix.html` | **成品**：单文件自包含页面，双击即开，数据已内联 |
| `index.html` / `app.js` | 页面骨架与渲染器（成品源文件） |
| `build.py` | 打包脚本 |
| `data/payload.json` | 7 案完整输入与两臂输出、Notes 并集差异（约 3.8 MB） |
| `scripts/extract_v3.py` | 从批次目录抽取 payload（只读） |
| `scripts/smoke.mjs` | 渲染冒烟：核对并集大表与缺侧高亮 |

`画像管线AB对照-v3-fix.html` 与 `data/*.json` **含真实客户数据，禁止入库**
（`../.gitignore` 已排除）。

## 5. 呈现口径

与 `ab对照-v3/` 完全一致：Notes 按 `attribute_key` 求**并集**排成两栏大表，某一侧缺该字段时
该侧单元格**留空并高亮**（红色 ⊘）。未做「单边差异」独立模块（按需求已移除）。

**Windows 注意**：含中文字面量的统计脚本一律用 `python -X utf8 <脚本>` 运行。
