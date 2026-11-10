# 增量画像 · 专项工作目录

本目录是「增量画像」专项的**实施期**资料：策略与方案、改动范围、任务计划、测试验证、风险与开放问题。
业务背景：一个销售周期里销售会多次触发同一客户的画像（留资后、电话后、试驾后……），
现行管线每次都从零全量推理，既浪费成本，也因模型随机性让前后两次结论无故漂移。
本目录只放**可执行的那一层**，不重复管线设计（见 `../design/客户人设画像管线设计.md`）。

## 文档索引

| 文档 | 回答什么 | 什么时候读 |
|---|---|---|
| `01-策略与方案.md` | 目标、底座从哪来（接口优先 + 副本补缺）、四档分流、记录差分、三处 LLM 怎么关进笼子、已拍板口径 | 动手前通读；评审方案时 |
| `02-改动范围清单.md` | 新增哪些模块、改哪些文件、每处为什么改、哪些刻意不动 | 估算工作量、逐条核对时 |
| `03-任务清单与计划.md` | 分几期、每期任务与依赖、验收标准、回滚方式 | 排期、执行、交付验收时 |
| `04-测试与验证方案.md` | UAT 归档里有什么可用序列、怎么手工改造夹具、等价性与稳定性怎么量 | P0 开工前；每期验收时 |
| `05-风险与开放问题.md` | 已解除的外部确认项、待本项目拍板的口径、已知风险、上线观察项 | 每期开工前扫一眼 |
| `06-跑批验证报告-20261109.md` | 四批跑批（首次画像 × 2 臂、增量 × 2 臂）的档位判定、数量与质量对比、唯一劣化案例的根因 | 灰度前；回答「增量是否劣化」时 |

## 当前状态

- 阶段：**P3 已完成（2026-11-09）**，缺一次真实模型的指标实测与 UAT 灰度。
- P3 已落地（2026-11-09）：
  - `scripts/export_snapshot.py` 连带 `profile_baselines`（按号码、不受时间窗），manifest 记行数，`format: 3`；
  - `scripts/replay_incremental.py`：逐时点序列回放（t1 建基线、其后走增量入口），产物与跑批同构；
    `scripts/replay_from_archive.py` 加 `--run-index`；
  - `.env.example` 补 `INCREMENTAL_ENABLED` / `INCREMENTAL_TIERS` / `BASELINE_ENABLED`；
  - runbook：`内网取数与本地复现.md` §3.1、`基线实验.md` §11、`architecture.md` §5.0、`05` §4.1。
  - **未完成**：`04` §4 三项指标真实模型实测、UAT 灰度（见 `03` P3「未完成项」）。
- P2 已落地（2026-11-09）：
  - `incremental/base_claims.py`：接口画像 A 与回传体副本 B 的底座组装与补缺（三规则）；
  - `incremental/merge.py`：合并账本（渠道退场、时近性取舍、`observed_at` 标注）；
  - `incremental/context.py`：增量上下文的形状与解析，空串即非增量；
  - `workflows/customer_profile_pipeline.py` 加可选入参 `incremental_ctx`：L1 只装新记录并附
    《上一版已定稿事实》、L2 前合并底座、定级带上一版锚点；
  - `rules/clusters/recency.py`：`RECENT-01/02/03`（稳态不翻、进程后到者胜、等级须锚定新证据）；
  - `workflows/customer_profile_l3.py`：`l3_skip_express`——事实未变时零 LLM 复用上一版板块；
  - 入口工作流拉 `GET /data/profile`，T1/T2 放开，`INCREMENTAL_TIERS`（缺省 `T0,T1,T2`）是灰度口。
- P1 已落地（2026-11-09）：
  - 表 `profile_baselines`（`persistence/profile_baselines.py`）：按手机号一行存回传体、兼容键、已消费键；
  - 入口工作流 `customer_profile_incremental`：T0 原样回传（零 LLM）/ T3 子运行现行 pipeline；
  - `incremental/compat.py`（兼容键）、`incremental/dispatch.py`（档位判定，纯函数）、`incremental/nodes.py`（code 节点实现）；
  - 开关 `INCREMENTAL_ENABLED`（缺省关）、`BASELINE_ENABLED`；前端从 `GET /runtime` 读入口；
  - 运行详情顶部显示档位与判定理由；测试见 `02` §6。
- P0 已落地（2026-11-09）：
  - `scripts/build_incremental_fixtures.py`：序列夹具与 9 类边角变体，全部由真实归档改写、步骤记入 `sequence.json`；
  - `scripts/compare_incremental.py`：稳定性 / 等价性 / 成本三项指标；
  - `scripts/analyze_incremental_archive.py`：补缺分布与按节点 token 占比（T0-3 第二项、T0-5）；
  - `src/customer_profile/incremental/diff.py`：记录身份键与差分，`tests/test_incremental_diff.py` 覆盖；
  - 序号夹具落在 `experiments/incremental/`（不入库），首批 19 个可用序列 + 9 类变体。
- 数据侧结论（见 `04` §1）：202 个号码有多次运行、310 对相邻运行可直接当增量序列；
  7 对同输入运行里 5 对输出仍变了 11–23 个字段，这是要治的随机性。
  P0 复核补充：这 7 对含 33 对重叠运行，剔除重叠后可用序列里「内容完全相同」只有 3 对（见 `04` §1.2）。
- 方案基线（2026-11-09 拍板）：
  - **底座 = `GET /profile` 取回的画像**，API 为准、**上次实际发出的回传体副本补缺**；
    不做本地版本链、不做三方比对、不识别未锁定的人工修改。
  - 回传体副本落骨架表归档（不进运行包，不随 trace 清理），用于补缺与证据沿用。
  - 任务侧不带增量语义，是否增量由我们自判；T0 无变化也要回传副本以闭环任务。
  - **首次出现有实质内容的试驾录音**（阶段跃迁）触发全量重建。
  - 两次任务间隔以小时或天计，不做并发防护。
- 已解除的外部确认项（见 `05` §1）：后台无字段级人工修改标记且无排期（方案已不依赖它）；
  UAT 读回不一致是环境快照不同步 + 异步落库，不作缺陷上报；不回传内部字段；多行区块不可手改。
- 待本项目拍板：`05` Q1 基线保留期、Q2 进程字段首版清单、Q3 证据退化容忍度。均不阻塞 P0 / P1。
- 文档内只写号码尾号；完整号码在 `experiments/incremental/sequence_phones.txt`（不入库）。
  首批推荐序列里 **2180 建不出夹具**：它的两次运行互相重叠，剔除后可用运行不足 2 次（见 `04` §2.1）。

## 目录约定

- 本目录随仓库分发，属实施期资料；收口后整体移入 `../history/`。
- 只写意图、契约、口径与操作步骤；函数签名、类名与端点表以代码为准。
- 待定条目一律标 `[待定]` 并写清解除条件。
