# 系统优化：Dify 残留清理与呈现方式整改（计划表）

> 状态：**已完成**（2026-10-04 收口）。四批（A/B/C/D/E）全部落地，全量测试 374 passed / 13 skipped。
> 上游依据：用户 2026-10-04 提出的三项整改方向，以及针对评估的三项答复（D1 / D2 / D3）。
> 文档归属：`docs/specs/`——开发任务期间的临时产出，任务收口后归档至 `docs/history/`。

## 1. 目标

清掉项目中最后一层「因迁移而存在、如今已成为阻碍」的设计：

1. 前端从「画布 + 连线」改为「按时间顺序的流程列表」，并移除画布模块本身；
2. 后端移除只为画布而存在的布局层与坐标字段；
3. 移除对原平台（Dify）的命名、台账与术语残留，使后续开发不再有任何指向原平台的强依赖。

**不在本次范围**：节点类型的增删（`if-else` / `variable-aggregator` / `iteration` 一律保留）；提示词正文改动；工作流图结构的重构（1:1 模板与 llm 合并、聚合器折叠等留待后续专项）。

## 2. 已确认的决策（用户 2026-10-04）

| 编号 | 决策 |
|---|---|
| D1 | 画布式呈现改为流程式呈现；「工作流结构」页直接移除，不留降级形态，后续有需要再做 |
| D2 | 台账不要，项目彻底与 Dify 解绑；保证后续不再出现 Dify 强依赖项 |
| D3 | 流程列表只按时间顺序显示，不另做并列分组；并发/并列关系由「前置依赖项」字段读出 |

## 3. 现状盘点

### 3.1 已完成（上一提交 `000fb98`）

Dify 侧**工具链**已删除：`scripts/export_templates.py`、`scripts/extract_node_code.py`、`scripts/generate_workflows.py`、`scripts/inventory/`（3 文件），以及把代码钉在 DSL 上的三个测试（`test_code_verbatim.py`、`test_ledger_crosscheck.py`、`test_templates.py`）。仓库内已无任何 `.yml` / `.yaml` DSL 文件。

**遗留一处**：`tests/__pycache__/` 下仍有 `test_code_verbatim` 与 `test_ledger_crosscheck` 的 `.pyc`（2026-09-29 编译，源码已删且从未入库）。pytest 不收集它们，无功能影响，但应清理。

### 3.2 当前的强依赖点（本次要清掉）

| # | 残留 | 位置 | 规模 |
|---|---|---|---|
| R1 | 画布模块 | `frontend/` 的 TopologyGraph.tsx、App.tsx 结构页、`@xyflow/react` 依赖 | 145 行 + 1 依赖 |
| R2 | 布局层 | `src/customer_profile/layout.py`、`runner.topology()`、`api.py` 的 topology 端点与 `_with_layout` | 1 文件 + 3 处 |
| R3 | 坐标字段 | `NodeDef.coords` 及 22 处定义、`asdict()` 导出、前端类型 | 22 处 |
| R4 | 原节点 ID 冗余字段 | `original_node_id=`，运行期零读取方 | 134 处 |
| R5 | 台账数据 | `docs/history/ledger/`（node_ledger.json 395KB、variable_ledger.json 139KB 等） | 8 文件 |
| R6 | Dify 味命名 | 5 个 Dify 味节点标题、6 个数字后缀标题、1 个裸 `LLM`、19 个工作流 `DISPLAY_NAME` 的「（新）SubAgent - 」前缀 | 约 30 处 |
| R7 | Dify 术语常量与注释 | `DIFY_REF_PATTERN`、`DIFY_REF_IN_BODY`、definitions.py 与各 executors 的「原始定义 / value_selector / sourceHandle / isInIteration / parallel_nums」注释 | 37 行 |
| R8 | 孤儿模板 | `templates/gemini_retry__*.txt`（3 份，无对应工作流模块，引用一个定义中不存在的节点 ID） | 3 文件 |
| R9 | 测试装置混入生产列表 | `synthetic_fanout` 出现在运行台工作流下拉框 | 1 处 |

### 3.3 不能动的东西

| 项 | 原因 |
|---|---|
| `node_id`（雪花 ID） | ① 模板正文以 `{{#node_id.field#}}` 硬引用 10 个节点 ID，而正文受「不得改动」约束；② `definition_versions` 已入库 45 份历史快照，`node_id` 是稳定身份 |
| `{{#node#}}` 引用**语法** | 23 个模板文件、26 处引用依赖它；常量名可改，语法不可改 |
| `if-else` / `variable-aggregator` / `iteration` 节点 | 调度层的 `branch_gates` / `SKIPPED` 语义、聚合器「按是否执行过取值」口径、迭代串行语义均已是既有行为，删节点等于重造实现 |
| 提示词正文 | §6.5 约束：文本改动须单独提出并获确认 |

## 4. 批次划分

### 批次 A：前端改为流程式呈现（对应 D1、D3）

| 编号 | 任务 | 落点 |
|---|---|---|
| A1 | 新增流程列表组件：按 `started_at_ms` 升序展示节点；每行显示序号、标题、状态、耗时、**前置依赖项**；可展开显示输入/输出/每次尝试/子运行下钻入口 | 新组件 `frontend/src/components/FlowList.tsx` |
| A2 | 运行详情页以流程列表为默认视图，接替原「依赖图」 | `RunDetail.tsx` |
| A3 | 移除「工作流结构」标签页与工作流下拉框 | `App.tsx` |
| A4 | 删除 `TopologyGraph.tsx` 与 `@xyflow/react` 依赖 | `frontend/src/components/`、`package.json` |
| A5 | 清理 client.ts 死代码：`topology()`、`definitionVersion()`、`Definition` / `GraphNode` / `GraphEdge` 类型；`RunGraph.definition` 改用窄类型（仅留节点前置关系） | `frontend/src/api/client.ts` |
| A6 | 清理仅服务画布的样式：`.graph-canvas`、`.node-card`、`.node-title`、`.node-type`、`.node-meta`、`.app-controls`、`.app-hint` | `index.css`（61-66、82-85、108-137） |
| A7 | 重新构建并提交 `frontend/dist/`（入库约定，差异 W16） | `git add frontend/dist` |

**A5 注意**：流程列表要的「前置依赖项」来自当次定义快照的 `nodes[].direct_predecessors`，`GET /runs/{id}/graph` 已返回，不需要新增端点。

### 批次 B：后端移除画布支撑层（对应 D1）

| 编号 | 任务 | 落点 |
|---|---|---|
| B1 | 删除 `layout.py` 整份 | `src/customer_profile/layout.py` |
| B2 | 删除 `Service.topology()` 与 `attach_layout` 调用，`asdict()` 直接下发 | `runner.py`（import 与 253-261） |
| B3 | 删除 `GET /workflows/{workflow_id}/topology` 端点 | `api.py` 252-258 |
| B4 | 删除 `_with_layout()` 及两处调用 | `api.py` 285、434、564-596 |
| B5 | 删除 `NodeDef.coords` 字段、22 处定义、`asdict()` 导出、`executors_llm.py` 传参 | `definitions.py`、各 `workflows/*.py` |
| B6 | 删除 `original_node_id=`（134 处，运行期零读取方） | `workflows/*.py` |
| B7 | 测试同步：删 `test_layout.py` 整份；删 `test_api.py` 两条 topology 用例；删/改 `test_run_graph.py` 的两条 layout 用例 | `tests/` |
| B8 | `GET /definition-versions/{id}` 端点去留 | 见 Q4 |

**B5 兼容性**：已入库的 45 份快照含 `coords` 键；删字段**只影响新快照**，旧快照多一个键不影响读取，无需数据迁移。

### 批次 C：命名整改（对应 D2）

| 编号 | 任务 | 落点 |
|---|---|---|
| C1 | 拆分 `DISPLAY_NAME`（显示名）与 `SOURCE_DSL`（追溯键），去掉「（新）SubAgent - 」前缀 | 19 个 `workflows/*.py` |
| C2 | 重命名 5 个 Dify 味节点标题（含 `Gemini（异常输出重试版）`、`（新）SubAgent - 画像内容生成&回写（生产环境）（新）` 的重复后缀） | production / entry / evidence_subagent / profile_features_analysis / user_feedback_data |
| C3 | 重命名数字后缀与泛化标题：`输出 2`、`变量聚合器 2`、`HTTP 请求 2`、`代码执行 2`、`代码执行 3`、`模板转换 2`、`模板转换 (1)`、裸 `LLM` | 各处 |
| C4 | **不动 `node_id`**（见 §3.3） | — |

### 批次 D：台账与文档清理（对应 D2）

| 编号 | 任务 |
|---|---|
| D1 | 删除 `docs/history/ledger/` 8 个文件 |
| D2 | `differences.md` 中 W44–W52 属**业务修订与缺陷修复**（如 W46 极光字段功能性修复、W50 企业数据静默丢失修复），非 Dify 差异；须先提取为 `docs/adr/` 或 `docs/design/` 正式文档，再删台账 |
| D3 | 更新引用台账路径的文档与脚本：`docs/changelog.md`、`V0.2`、`V0.3`、`scripts/record_fixture.py` 注释 |
| D4 | `docs/history/prompts/`（81 份评审副本）去留——见 Q5 |
| D5 | `Dify迁移任务说明.md`、`V0迁移规划.md` 等留在 history 归档，不再从当前文档引用 |

### 批次 E：代码内术语与残留（对应 D2）

| 编号 | 任务 | 落点 |
|---|---|---|
| E1 | `DIFY_REF_PATTERN` → `TEMPLATE_REF_PATTERN`；`DIFY_REF_IN_BODY` → `TEMPLATE_REF_IN_BODY`（仅改名，语法不动） | `templates.py`、`executors_http.py` |
| E2 | 清理 `definitions.py` 的 Dify 术语注释（`value_selector`、`sourceHandle`、「原始定义」等 9 处） | `definitions.py` |
| E3 | 清理各 executors 与 workflows 的「原始定义 / gemini_retry_2_times / 有意差异 Wxx」注释 | `execution/*.py`、`workflows/*.py` |
| E4 | 删除 3 份孤儿模板 `templates/gemini_retry__*.txt` | `templates/` |
| E5 | `synthetic_fanout` 从运行台列表隐藏（保留注册，测试仍直接引用） | `api.py` 237、`test_api.py` 87、91 |
| E6 | 清理 `scripts/record_fixture.py` 与 `.env.example` 的台账/原始定义注释 | 2 文件 |
| E7 | 清理 `tests/__pycache__/` 的孤儿 `.pyc` | 2 文件 |

## 5. 验收标准

1. `grep -ri "dify"` 在 `src/`、`frontend/src/`、`scripts/*.py`、`tests/` 下无功能性命中；
2. 仓库内无 `layout` 模块、无 topology 端点、无 `coords` / `original_node_id` 字段；
3. 运行台只剩「任务列表」与「提交任务」两个标签页；运行详情为流程式列表；
4. `docs/history/ledger/` 不存在，其业务价值内容已迁入 `docs/adr/` 或 `docs/design/`；
5. `python -m pytest tests/` 全绿，减少的用例仅为布局与 topology 相关；
6. 前端 `npm run build` 成功，`frontend/dist/` 已同步提交。

## 6. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 历史快照形状变化 | 45 份已入库快照含 `coords` / `layout`，新快照不含 | 读取端只按需取字段，不假设键全集；不做数据迁移 |
| 流程列表丢失并发语义 | 只按时间排序会把并行节点排成先后 | D3 已定：靠「前置依赖项」读出；列表显式给出该字段 |
| `node_id` 误改 | 模板正文硬引用 10 个 ID，改了会静默渲染失败 | C4 禁止改动；E1 后补一条测试断言「模板引用的 ID 必须在定义中存在」 |
| 删除 layout 测试造成覆盖下滑 | 其中有全量定义的环普查 | 若仍需环普查，改为直接调用 `WorkflowDef.structural_order()`，不依赖 layout |

## 7. 待确认项

| 编号 | 问题 | 建议 | 阻塞批次 |
|---|---|---|---|
| Q1 | `source_dsl` 字段与 19 个 `SOURCE_DSL` 常量：保留为追溯说明还是删除？ | 保留字段、值改为业务名（不指向 .yml），避免动 `asdict()` 形状 | C |
| Q2 | 模板文件名（`<slug>__<node_id>.txt`）是否重命名为语义名？ | 暂缓：牵动 81 个文件与全部 `template=` 引用；留到批次 C 之后单独做 | C / D |
| Q3 | 100 余处「有意差异 Wxx」注释如何处理？ | E3 一并替换为直白解释；台账删除后编号即失效 | E |
| Q4 | `GET /definition-versions/{id}` 无前端调用方（结构页是唯一消费者），保留还是删？ | 删端点，保留 `store.definition_version()`（`/runs/{id}/graph` 内部仍用） | B |
| Q5 | `docs/history/prompts/` 81 份提示词评审副本：保留还是删？ | 保留但去 `node_id` 重命名：它是提示词评审资产，不是 Dify 台账 | D |
| Q6 | 运行详情是否保留「执行顺序时间线」（甘特式条形）作为第二视图？ | 保留：它显示并发，与流程列表互补 | A |
| Q7 | `synthetic_fanout` 的隐藏方式：加内部标记，还是移出 `WORKFLOW_MODULES` 改由测试直接 import？ | 加标记（改动最小，测试的 API 路径不变） | E |

## 8. 建议的开工顺序

**A → B → E → C → D**。

理由：A 与 B 是同一件事的两端（前端换视图、后端拆支撑），须同批收口；E 是纯机械改名、风险最低，可随时插入；C 改节点标题、D 删台账，两者都牵动文档与注释，放最后可少改一轮。
