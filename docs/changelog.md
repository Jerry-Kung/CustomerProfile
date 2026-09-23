# 变更日志

按时间倒序记录重大功能与里程碑事件。

## 2026-09-23 — AUC 改接火山引擎云服务 + 录音链路打通 + 两个新缺陷修复

- **AUC 语音识别由内网自建服务改为火山引擎云服务**（用户 2026-09-23 决定，有意差异 W14）。
  原 `192.168.0.5:5005` 长期不可达，导致两个录音类工作流的真实链路一直无法验证。
  云服务的协议与 DSL 契约不同：异步提交 + **由调用方轮询**、处理状态在**响应头**
  `X-Api-Status-Code`、`task_id` 由调用方生成、鉴权需 `x-api-key` 与 `X-Api-Resource-Id`。
  新增 `src/customer_profile/execution/auc.py` 做协议适配，**把云形状翻译回内网契约**：
  `/submit_analyze` 产出 `{task_id, x_tt_logid}`、`/query_analyze` 产出
  `{auc_result, poll_count}`。因此**工作流的图、两个 HTTP 节点与两个逐字符取自 DSL 的
  code 节点一字未改**——迁移基线保持不动。`HttpClient.request` 在 `service="auc"` 时转交该客户端。
  凭据落 `.env`（`AUC_API_KEY` / `AUC_RESOURCE_ID`），定义与代码中不出现；
  原明文硬编码密钥的那份 handler 文件按用户决定**已在整合后删除**。
- **录音链路端到端打通（V0.3 收口证据）。** 先用真实外呼录音（TOS 直链，2.53 MB）单跑
  `outbound_call_audio` 验证链路：12 节点全成功、耗时 249 s，AUC 提交 1.4 s、轮询 9.1 s
  （识别本身很快，耗时主要在 LLM 校对 149 s），有效性判断走**「有效」分支**——
  这是该分支首次被真实执行。
- **主入口端到端跑通（V0.3 最终验收）。** `customer_profile_entry` 全链路 **succeeded**，
  总耗时 **1282 s**；3 个一级子流程全部成功（证据汇总 393 s、人设特征 239 s、
  画像生成&回写 650 s），其下 **13 路扇出全部成功**，`result1` 为 **27950 字字符串**
  （对外契约不变）。本次扇出中的 `outbound_call_audio` 耗时 236 s，产出 **7375 字**
  真实通话分析（含说话人身份推断表）。至此 V0.3「端到端打通」目标达成。
- **第二波真实冒烟又发现两个迁移缺陷**（测试全绿时全部漏过，均为「路径没被走过」而非「代码写错」）：
  1. **模板变量未绑定（10 个节点）**。`llm` 节点的模板正文引用 `{{#节点.字段#}}`，
     但节点未声明对应 `inputs`，渲染期变量表为空，报「模板变量未提供」。
     同一处遗漏经 3 个声明点影响 **10 个节点**（7 个截图类「多图片内容总结」+ 录音类两个节点）；
     截图类因视觉路径未被走到而一直未暴露。修复：3 个声明点补上绑定；新增
     `tests/test_llm_template_bindings.py`，**静态**断言「模板里的每个引用都被绑定覆盖」。
  2. **`expect_json` 把 dict 当文本**。执行器把解析后的 **dict** 放进 `text`，而两个消费方
     （`判断结果提取`、`Notes结果拆分`）都对 `text` 做 `json.loads`——传 dict 抛异常，
     被各自 `except` 吞掉并按缺省值返回，**静默给出错误业务结论**。实测同一份录音里
     模型正确判出 `is_valid: true`，code 节点却输出 `false`。此前「单方录音」样本恰好也应判
     无效，错误与正确无从区分，因此一直没被发现。修复：`text` 改为 `json.dumps(...)` 字符串
     （Dify 契约：llm 输出一律是文本），解析后的对象保留在 `parsed_json`；补 `import json`；
     新增 `tests/test_expect_json_output.py` 钉住该契约。**该缺陷亦影响主入口链路的
     `Notes结果拆分`**，不只是录音流程。
- **`scripts/probe_auc.py` 改为探云服务**：TCP → 空查询体（`45000000 cannot find task`
  即凭据与协议全对）→ 三档结论。实测退出码 0「可用」。不打印凭据值，只报「已配置 / 未配置」。
- 测试 **342 项**（19 个文件）全绿，13 skipped（需被 gitignore 的 DSL 或数据条件的用例）。
- 文档：`ledger/differences.md` 新增 W14；`v0.3_smoke_report.md` §2/§3/§6.1/§7 按实测更新
  （缺陷表扩到六项，AUC 缺口标记收口）；`V0.3全量迁移与端到端打通.md` §4.1 扩为五个缺陷、§6.2 收口；
  `V0迁移规划.md` §7.1 与三条 AUC 事实按实测刷新。

## 2026-09-23 — V0.3 收口：提示词评审副本重新导出 + AUC 可达性探测脚本

- **`docs/specs/prompts/` 重新导出并补上一致性断言。** V0.1 生成的 81 份评审副本用了另一套命名
  （显示名转写 + 内容哈希 + `__llm` / `__template-transform` 类型后缀）与另一套抽取口径
  （保留 `system` / `user` 角色行、CRLF 换行），且取自更早的 DSL 修订；实测 **43/81 份**与
  包内资源内容不同。已按包内资源（经 `export_templates.py --check` 与 DSL 逐字符一致）重新导出，
  命名统一为 `<slug>__<node_id>.txt`，两边**逐字节相同**；新增
  `test_review_copies_match_package_templates` 断言这一点，删掉了原先那条「把副本已失真写成
  xfail」的用例，并新增 `docs/specs/prompts/README.md` 说明出处、再生顺序与失真史。
  测试 **312 项**（16 个文件）全绿，1 skipped（缺被 gitignore 的 DSL），xfail 归零。
- **新增 `scripts/probe_auc.py`**（临时运维脚本）。AUC 服务 `192.168.0.5:5005` 在本机 TCP 超时，
  该脚本分四步给出可执行的裁决：TCP 连接（翻译 errno）→ HTTP 应答 → 对 `/submit_analyze` 与
  `/query_analyze` 发**故意无效**的请求体（不触发真实任务）→ 三档结论。当前实测：TCP 超时，
  退出码 2「不可达」。服务就绪后由业务方在本机复核，再放开读超时重跑录音类冒烟。

## 2026-09-23 — V0.3 全量工作流迁移与端到端打通完成

- **19 个 Dify 工作流全部迁移**（DSL 计 293 节点 / 324 边 → 18 个独立图 / 272 节点 / 300 边；差额逐项对应有意差异 W1/W2/W4）。新增 `executors_iteration.py`、`executors_aggregator.py`，补上迭代、聚合（含分组）、vision 三类节点语义。测试增至 **300 项**（15 个文件）。
- **迁移后的图与台账逐项一致**：`tests/test_ledger_crosscheck.py` 覆盖全部 18 个工作流，比对节点集合、类型、边集合，并含一条**反向**检查（差异表里声明的每一项都必须真的发生）；`tests/test_code_verbatim.py` 覆盖 47 个随工作流迁移的 code 节点正文。
- **三个只有真实运行才会暴露的缺陷**——它们在 300 项测试下全部通过，是真实模型冒烟才发现的：
  1. **分支门槛丢失（最严重）**。DSL 的边用 `sourceHandle` 区分 `if-else` 的两支，而迁移只记了「有这条边」，于是**两条分支同时执行**。截图类流程里表现为「文件不存在」与「文件存在」两条路一起跑，后者对空串做 JSON 解析直接抛 `JSONDecodeError`。修复：`NodeDef` 新增 `branch_gates`/`branch_from`，调度层按「前置实际走的分支」判定边是否失效，未走到的子树标为 `SKIPPED`（与故障性的 `BLOCKED` 区分开，不影响运行成败）；`edge_list()` 保留 `source_handle`。共 21 个节点补上注解。
  2. **`input_prompt` 提示词入参被忽略**。`gemini_retry_2_times` 子流程的入参名是 `input_prompt`，17 处调用点原样保留，而 llm 执行器只认 `prompt`，导致这 17 个节点全部以「没有提示词入参」失败。修复：执行器同时接受两者。
  3. **`@body_template` 调了不存在的 `ctx.get`**。请求体模板的引用替换写成 `ctx.get(*parse_selector(...))`，而 `get` 是 `NodeResult` 的方法、`RunContext` 上没有，AUC 提交节点抛 `AttributeError`。该路径此前**零测试覆盖**。改用 `ctx.resolve`，新增 `tests/test_body_template.py` 按「引用在字符串外/内」两种语义钉住。
  4. **图片 data URI 用了文本而非原始字节**。`_files_of` 从 `response.text` 取内容再编码 base64，而 httpx 会把二进制按文本解码——实测 PNG 头 `iVBORw0KGgo` 被改写成 `77+9UE5HDQoaCg`，视觉节点把坏 base64 发给模型，模型侧只回一个语焉不详的下载失败。修复：`HttpAttempt` 增加 `response_bytes`，`_files_of` 优先用字节编码；回放 fixture 用 `bytes_base64` 承载二进制；`record_fixture.py` 增加 `--record-binary`（图片可能含客户信息，默认不录）。
  5. **`required_<name>` 空值校验器**（V0.2 遗留）。`.env` 里 `MAX_ACTIVE_RUNS=` 为空串时 pydantic 解析 `int` 直接抛错，服务无法装配。
- **主入口端到端**：`tests/test_entry_end_to_end.py`（真实 tool 执行器 + 真实嵌套调度器，子流程换成同形状微缩图）与 `tests/test_contract.py`（入参口径与 `result1` 为字符串的对外契约）。`scripts/record_fixture.py` 支持按真实链路录制回放 fixture（含手机号与密钥脱敏，fixture 目录已加入 `.gitignore`）；`scripts/smoke_run.py` 强制 `WRITEBACK_ENABLED=false`，不产生生产副作用。
- **提示词外置**：81 份正文的运行期唯一来源是 `src/customer_profile/templates/`，`export_templates.py --check` 逐字符比对 DSL 通过。`docs/specs/prompts/` 的评审副本当时已失真（43/81 份与包内资源不同，W11 声称的「两边一致」无测试支撑），**已于同日收口**，见上一条。
- 文档：新增 `docs/specs/V0.3全量迁移与端到端打通.md`；`ledger/differences.md` 升 v2（P1–P5 由「待确认」改为实测复核结论，其中 **P3 与 P4 的复核改变了实现口径**）；`V0迁移规划.md` §7 基线按实测刷新。

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
