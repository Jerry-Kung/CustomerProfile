# 8 案 AB 对照 · 优化前后输出比对

> 本目录是 `docs/business_opt01_v2/` 下的一次**产物归档**，不属于文档主体。
> 文档主体（方案设计与调整建议）在同级目录的 `.md` 文件中。

## 1. 这是什么

v2 管线（`customer_profile_pipeline`：L0 记录 → L1 取证 → L2 投影 → L4 分诊 → L3 成文）
与基线（`customer_profile_entry`：证据子代理 → 特征分析 → 画像生产）在**同一批 8 个用例**上的
输出直接比对，并复检 10 类历史质量问题。

- 抽样：从 39 案中随机抽取 8 案（20%）
- v2 批次：`experiments/abexperiments/batches/A-pipeline-L8-20261012/`
- 基线批次：`experiments/batches/baseline-39-dsv41-noThink/`
- 判据来源：`docs/business_opt01/基线画像质量诊断.md`

对照内容包括：完整原始输入（录音转写、各渠道取数、历史接口、L0 记录层、L1 提示词全文）、
两臂 Notes、两臂 9 个画像卡 section、L1 断言与投影降级流水。

## 2. 文件清单

| 文件 | 内容 |
|---|---|
| `画像管线AB对照.html` | **成品**：单文件自包含页面，双击即可打开，无需起服务。数据已内联 |
| `ab对照/index.html` | 页面骨架与样式（成品的第一部分源文件） |
| `ab对照/app.js` | 渲染器（成品的第二部分源文件） |
| `ab对照/build.py` | 打包脚本：把上面两者与 `data/` 合成单文件成品 |
| `ab对照/data/payload.json` | 8 案的完整输入与全部输出字段（约 4.1 MB） |
| `ab对照/data/findings.json` | v2 内容质量排查结果（44 条） |
| `ab对照/data/old_problems.json` | 两臂 10 类老问题的逐案复检结果 |
| `ab对照/scripts/extract2.py` | 从批次目录抽取 payload 与 findings（只读） |
| `ab对照/scripts/audit_old.py` | 两臂老问题复检（只读） |
| `ab对照/scripts/probe_inputs.py` | 原始取数结构探针（只读） |
| `ab对照/scripts/probe_degrade.py` | 退化项样例探针（只读） |
| `ab对照/scripts/smoke7.mjs` | 成品渲染冒烟测试（需 node） |

重建成品：

```
cd ab对照 && python build.py
```

重跑抽取与复检（需先有批次数据）：

```
python scripts/extract2.py
python scripts/audit_old.py
```

## 3. 数据敏感性与入库口径

`画像管线AB对照.html` 与 `ab对照/data/*.json` **含真实客户姓名、手机号、企业名与录音转写**，
属「真实客户数据」，**禁止入库**（与 `json_by_phone/`、`test_phone_numbers.txt` 同一口径，
见 `docs/business_opt01/README.md` §5）。本目录的 `.gitignore` 已将其排除。

若需将成品纳入版本控制，须先做脱敏（替换姓名、手机号、录音转写文本）。

脚本与源文件本身不含客户数据，可正常入库。

## 4. 结论摘要

**确认为改善**：证据锚点引用自产画像 172 → 0；模板外 key 11 → 0；职业仅靠第三方预测 2 → 0；
L1→投影的丢弃漏斗全案归零。成本 LLM 调用 182 → 49、token 2,615,341 → 417,510。

**确认为退化**：① 渠道标注整体塌陷（证据串退化为「社交自呈现（未知渠道）」，8 案全中，
单案最多 27 条）；② 三分类中间档塌成最低档（7/8 案 `inferred_high` 与 `fact` 在字段级
全部降为 `inferred_low`）；③ section 空缺增多（最差一案 9 个 section 仅剩 6 个有内容）。

**口径提醒**：⑦ age 是写法不一致（号码段相同），非取值冲突；⑨ summary 与 steps 的
「字面零重叠」有相当比例源自 v2 把 steps 改为「字段名+取值」形式，属检查器口径差；
① 的枚举违规中，字典型取值会被误判，实际违规是 `unknown` 出现在
`family_structure` / `payment_method` / `license_plate_status` / `planned_purchase_time`
四个合法集不含该值的字段上。

**已知缺陷**：本次运行的 `manifest.code_commit` 记为 HEAD，实际执行的是未提交的工作区。
作为正式 AB 结论前，P1–P5 的代码需先提交。
