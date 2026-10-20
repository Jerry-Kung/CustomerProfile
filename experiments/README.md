# experiments —— 实验数据场地

> 本目录**整体被 gitignore**（见根 `.gitignore`），是本地实验数据，不入库。
> 例外：本文件与 `docs/` 子目录（与批次绑定的操作说明）随仓库分发。
> 需要长期留存的结论写入 `docs/`，不要写在这里。

## 保留什么

| 目录 | 内容 | 说明 |
|---|---|---|
| `batches/baseline-39-dsv41-noThink/` | **基线臂** 39 案批次 | 改动前的对照物，只读 |
| `batches/pipeline-v3.3-39/` | **v3.3 实验臂** 39 案批次 | 本轮优化的产出，含全部中间节点输出 |
| `transcript_cache/` | 录音转写归档（58 条） | **跨批次共用**，与工作流逻辑无关，删掉等于毁掉复用设计 |

两个批次目录里都含：`runs/<手机号>/{result,nodes,attempts,tree}.json`、`manifest.json`、
`phones.txt`、`summary.md`、`logs/`、批次独立 `trace.db`。

## 不保留什么

历史实验批次（`A-pipeline-*`、`FAILED-*`、`_smoke/`）与旧的 AB 场地
（`abexperiments/`，v3/v3.1 期间的批次）已于 2026-10-15 清理删除。
对照实验的**结论**留在 `docs/changelog.md` 与 `docs/runbooks/`，**原始数据**不再保留。

## 跑一次新批次

见 `docs/runbooks/基线实验.md`（通用流程）与 `experiments/docs/AB实验.md`（两臂对照）。
入口是 `scripts/run_baseline.py`，用 `--workflow-id` 切换两臂：

- 基线臂：`customer_profile_entry`（已从注册表摘除，要跑须先恢复）
- 实验臂（业务测试臂）：`customer_profile_pipeline`，**2026-10-16 起为缺省臂**

> 注意：`run_baseline.py` 的缺省臂已是 `customer_profile_pipeline`。显式传
> `--workflow-id` 仍建议保留——两臂对照时漏传会让整批跑在同一臂上。

**转写复用**：跑批时用 `--transcript-cache-dir experiments/transcript_cache`，
同一个录音不会重复识别。

## 注意

`git clean -x` / `-X` 会**连带删除本目录**（整个 `experiments/` 被 gitignore）。
不要使用这两个命令。
