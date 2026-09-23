# 变更日志

按时间倒序记录重大功能与里程碑事件。

## 2026-09-23 — V0.2 最小执行器与留痕骨架完成

- 新增 `src/customer_profile/`（执行器主体）与 `tests/`（145 项测试，全绿）。跑通「定义 → 调度 → 执行 → 落库 → 查询」，载体为规划 §3 V0.2 指定的两个真实工作流 + 一个人造验证图。
- **执行层**：`execution/scheduler.py` 按依赖就绪启动，不做逐层屏障；**两级独立信号量**——`active_slot` 限制同时执行的节点数，`request_limiter` 限制真实出网请求数（LLM + HTTP 共用）。子运行不申请 `active_slot`，因此父节点等待子流程期间不会与子流程互等；`tests/test_scheduler.py::test_no_parent_child_deadlock_with_single_slot` 在名额为 1 的条件下证明这一点。
- **留痕四层落 SQLite**（`persistence/`）：工作流运行 / 节点执行 / 请求尝试 / 版本快照。子运行与父运行共表，用 `parent_run_id` + `call_path`（形如 `/0/2/1`）区分同一静态节点 ID 的多次动态执行。历史运行绑定当次定义版本，新版本不覆盖旧展示。
- **统一 LLM 调用层**（`execution/llm.py`）：17 处 `gemini_retry_2_times` 调用点归一为一次 `llm_call()`；重试下沉到该层，最多 3 次尝试（差异清单 M1 缺省口径）；异常判定沿用原 DSL 判断节点的四条规则。因重试不再由图结构自然分开，**每次尝试独立写入 attempt 表**（规划 §6.6 点名的要求）。
- **固定响应回放**（`replay.py`）：LLM 按 `SHA256(model + messages)` 匹配，HTTP 按 `METHOD + 服务相对路径` 匹配；同一请求可排队多个响应以验证重试。另挂 `httpx.MockTransport` 兜底返回 599，确保回放模式下不可能出网。严格模式缺省，未命中即失败。
- **最小 API**（`api.py` + `main.py`）：提交返回 run_id、查询运行详情（含节点明细与每次尝试）、运行列表、拓扑导出、取消。提交等运行记录真正落库后才返回。
- **迁移两个真实工作流**：`（新）SubAgent - 猛士IT系统数据信息`（纯 code）与 `（新）SubAgent - 人工确认信息提取（生产环境）`（1 个 GET）。两者的 code 节点函数体与 DSL 原文**逐字符一致**，由 `tests/test_code_verbatim.py` 用 AST 定位函数体后机械比对（只忽略函数名与 docstring）。
- 与 V0.1 台账的交叉核对落在 `tests/test_ledger_crosscheck.py`：节点集合、节点类型、直接前置、边集合、code 节点入参绑定来源逐项比对 `node_ledger.json`。**只对已迁移的两个工作流断言**，不假装覆盖其余 16 个。
- 有意差异在测试中显式断言，避免日后被误当成缺陷：W4（`llm_model` 入参删除）、DSL 明文密钥不进入任何定义。
- 一处实现修正需记录：`Store` 的连接**由专属工作线程独占**。先前用 `check_same_thread=False` 让连接被线程池任意线程共用，在 Windows + Python 3.14 上直接导致解释器段错误（不是可捕获异常）。改为单线程独占 + 命令队列后消失。

## 2026-09-23 — V0.1.1 遗留决策落地

- **提示词正文改为入库**（D8 决策反转）：由 gitignored 的 `data/prompts/` 迁至 `docs/specs/prompts/`，共 81 份逐字符原文。理由是正文属后续版本的重要优化对象，需评审与追溯。入库前已扫描确认不含密钥形状字符串与手机号，`verify_ledger.py` 新增对正文的持续校验。
- 删除仓库根目录的受控空壳文件 `Untitled`（内容仅一行 `dify_dsl_data`，误提交）。
- 密钥轮换时机由「V0.3 前」改为「V0 版本完成后手工执行」——V0 开发阶段不触达生产外网。
- `docs/architecture.md` 留待代码架构设计开发阶段编写；`docs/specs/` 定位为各类设计文档的存放目录。
- `differences.md` 中 Q1/Q2/Q5–Q8 按用户指示改为「缺省采用」，右列缺省处理即为实现依据。**这不等于等价验证通过**；P1–P5、M1 仍待以 Dify 实际行为复核。

## 2026-09-22 — V0.1.1 迁移盘点与基线固化完成

- 新增盘点脚本 `scripts/inventory/`（`extract_ledger.py` 生成台账、`verify_ledger.py` 复核），纳入 Git 管理，仅依赖 PyYAML。
- 台账落于 `docs/specs/ledger/`：节点/边台账、变量引用台账、资源指纹清单、`tool_name` 映射表、工作流摘要。DSL 实测 **19 工作流 / 293 节点 / 324 边**，与规划文档的 16 项断言逐项一致。
- 新增 `docs/specs/ledger/differences.md`：24 条差异逐项标注「已确认 / 待确认 / 缺省采用」，无空白项。
- 新增 `docs/specs/ledger/secrets_report.md`：修正规划文档中「4 处明文密钥」的口径错误——实测 **5 个节点、2 个密钥值**；确认密钥从未进入 Git 历史。轮换时机已按 D7 改为 V0 版本完成后手工执行（原「V0.3 前」建议作废）。
- 修正规划文档中 `phone_numbers.txt` 为「0 字节空文件」的过期描述（实测 20 个真实手机号，258 字节）。
- 提示词正文导出至 `data/prompts/`（81 份），台账只存 SHA256 与长度，不含正文。**【次日修订】** 该落盘位置已于 2026-09-23 按 D8 决策改为入库的 `docs/specs/prompts/`，见下一条。

## 2026-09-22 — V0 规划确立工程优化规则

- 规划文档 `docs/specs/V0迁移规划.md` 新增 §6「工程优化规则」，明确迁移产出的 Python 应是人写的普通代码，而非 Dify 节点图的逐节点转写。
- 关键收敛项（均属**有意差异**，须记入差异清单）：
  - 17 处 `Gemini（异常输出重试版）` 调用点统一简化为单次 `llm_call()`，重试由 `llm_call()` 内部承担。
  - 全项目 LLM 统一为 `.env` 中的单一模型，`temperature=0.5`、thinking 开启、不配置其他参数。
  - `llm_model` 工作流入参废弃，相关条件分支归一。
- 关闭原待决问题 Q4（`llm_model` 取值），新增 1 项待确认：重试次数口径（3 次尝试 vs 首次+3 次重试）。
- 记录一项新硬约束：统一模型**必须具备多模态能力**，因 12 个截图解析节点依赖图片输入。
- `.env.example` 的三模型配置收敛为单一 `LLM_MODEL`。
