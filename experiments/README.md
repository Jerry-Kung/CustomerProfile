# experiments —— 功能验证与问题回归场地

> 本目录**整体被 gitignore**（见根 `.gitignore`），是本地实验数据，不入库。
> 例外：本文件与 `docs/` 子目录（与批次绑定的操作说明）随仓库分发。
> 需要长期留存的结论写入 `docs/`，不要写在这里。

## 这个目录的定位

从「AB 实验专属目录」改为**功能验证与问题回归**：存放从 UAT 同步回本地的真实运行记录，
用于后续功能开发、测试验证与问题定位。本地跑批的批次仍放在 `batches/`，两者结构同构。

**重要：本地环境访问不到 UAT 的取数接口。** 使用 UAT 数据时不要尝试通过 API 取数，
跳过取数环节、直接使用归档里保存的客户全量原始响应（`runs/<号码>/attempts.json` 的
`response_text`）作为输入往下跑。回放工具：`scripts/replay_from_archive.py`。

## 保留什么

| 目录 | 内容 | 说明 |
|---|---|---|
| `uat_data/uat-all-tasks-20261026-20261027/` | **UAT 全量任务导出** 69 根运行 / 57 号码 | 由 `runtime-all-tasks-last24h-20261027-092607.zip` 整理入库，`scripts/ingest_uat_export.py` 可复现 |
| `uat_data/uat-18-20261026/` | **UAT 快照归档** 18 根运行 / 16 号码 | 写回开启、无代码提交锚点，**不是受控基线**，见其 `README.md` |
| `uat_data/phones.txt` | 上述批次的号码总清单 | 人读用；按批次另见各自 `phones.txt` |
| `batches/` | 本地跑批批次 | 由 `scripts/run_baseline.py` 生成，结构与 `uat_data/` 同构 |
| `transcript_cache/` | 录音转写归档（58 条） | **跨批次共用**，与工作流逻辑无关，删掉等于毁掉复用设计 |

每个批次目录含：`runs/<手机号>/{result,nodes,attempts,tree}.json`、`manifest.json`、
`phones.txt`、`summary.{json,md}`、批次独立 `trace.db`；UAT 批次另含 `source/`（原始导出）。

## 入库一批新的 UAT 数据

```
# 1. 在内网按 runbooks/内网取数与本地复现.md 导出全量任务 JSON
# 2. 本地整理入库（zip 原封复制进 source/）
python scripts/ingest_uat_export.py \
  --export <解包目录>/all-tasks-complete.json \
  --out-dir experiments/uat_data/<batch-id> \
  --archive <导出包>.zip
```

## 不保留什么

历史实验批次（`A-pipeline-*`、`FAILED-*`、`english-residue-*`、`pipeline-v3.3-39`、
`baseline-39-dsv41-noThink`、`rules-opt01-baseline-8-*` 及旧 UAT 重放批次）已于
2026-10-27 清理（已另行手工归档）。对照实验的**结论**留在 `docs/changelog.md` 与
`docs/history/`，**原始数据**不再保留。

## 跑一次新批次

见 `docs/runbooks/基线实验.md`（通用流程）与 `experiments/docs/AB实验.md`（两臂对照）。
入口是 `scripts/run_baseline.py`，业务测试臂 `customer_profile_pipeline` 为缺省臂。

**转写复用**：跑批时用 `--transcript-cache-dir experiments/transcript_cache`，
同一个录音不会重复识别。

## 注意

`git clean -x` / `-X` 会**连带删除本目录**（整个 `experiments/` 被 gitignore）。
不要使用这两个命令。
