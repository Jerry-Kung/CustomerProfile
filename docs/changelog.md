# 变更日志

按时间倒序记录重大功能与里程碑事件。

## 2026-09-24 — V0.5.4 中断处理、人工出口与运维文档

- **需求**：补齐 V0.5 原定「生产运行保障」的第四项——重启中断处理（交人工）与运维说明。
  本版是「做前三版」这一范围（用户 2026-09-24 决定）的收口版。
- **新增人工出口 `POST /runs/{run_id}/abandon`**：把 `interrupted` 运行显式终结为
  `cancelled`。此前 v0.5.2 已把「残留 running 标为 interrupted 并保留已完成结果」做对，
  但**没有任何出口**——这些运行会永远留在列表里，看不出是「待处理」还是「已处理」。
- **用条件 UPDATE 而不是「先查后写」**：先查后写之间的窗口里，该运行可能已被另一个
  worker 重新领取并开始执行，此时把它改成 cancelled 会与一个正在跑的运行冲突——界面上
  看不到任何在跑的东西，但它确实在写外部接口。**只接受 `interrupted`**，其它状态返回 409。
- **与 `/cancel` 的语义区分**（写进了端点 docstring 与 runbook）：`/cancel` 是「停止一条
  正在跑的运行」，`/abandon` 是「结掉一条已经停掉的运行」。两者混用会出现「我点了取消但
  任务还在跑」这种最难查的状态。
- **终结时保留原中断原因**（`error` 追加而非覆盖）并记 `finished_at_ms`：中断原因才是
  「为什么停下」的答案，被覆盖掉的话人工回看时无从判断。
- **运维文档首次落地**（此前 `docs/architecture.md` 是 0 字节，`runbooks/`、`adr/` 三目录
  全空，而 `docs/README.md` 早已为它们定义了收录标准）：
  - `docs/architecture.md`：系统边界、双进程形态与队列/租约、四层留痕、并发与限额的四层
    闸门、外部依赖、SQLite 的三条硬约束、明确的「不做」清单。
  - `docs/runbooks/部署.md`：安装、两种部署形态（单机内联 / API 与 worker 分离）、
    数据库约束（**库不可放网络挂载**，WAL 依赖 `-shm`）、升级即补列、停机语义、提交与运行台。
  - `docs/runbooks/故障定位.md`：按现象组织 9 条——任务停在 queued、卡在 running、
    interrupted 堆积、`database is locked`、限流与慢的区分、回写「成功」却没写、
    `duplicate column name`、限额不可整除、前端 404。
  - `docs/adr/0001`、`0002`：队列为什么用 SQLite 表而非 Redis/Celery（含被否方案与代价）、
    配额为什么显式分配而非共享。
- **新文件**：`docs/adr/0001-队列用-sqlite-表而非-redis.md`、
  `docs/adr/0002-配额用显式分配而非共享.md`、`docs/runbooks/部署.md`、
  `docs/runbooks/故障定位.md`；`docs/architecture.md` 由 0 字节首次填充。
- **测试**：438 passed / 13 skipped（基线 430，增量 8，即 `tests/test_interrupt.py`）。
  新用例覆盖：abandon 只接受 interrupted（running/succeeded 均不动）、重复终结不二次生效、
  终结留时刻与原因、**不覆盖原中断原因**、中断运行保留已完成节点结果、三个 API 层用例
  （409 / 200 / 404）。
- **测试写法上踩到一处**：API 层用例起初拿一个已在别处的 `Store` 塞进服务，而服务 lifespan
  退出时会关掉它自己持有的那个——症状是运行期报「Store 尚未 connect()」，且看不出是谁关的。
  改为让服务按同一文件路径自建连接后成立。
- **本版未交付（显式记录，不伪装完成）**：
  1. **「回写失败复用已保存结果重试」**——这是原规划 §3 V0.5 验收标准之一，依赖幂等键与
     回写协议，随幂等回写一并**顺延至 V0.5.5**。
  2. **中断运行的自动续跑**——刻意不做（差异 W28），重领保留为显式动作。
  3. 真实模型冒烟未在本版复跑（用户 2026-09-24 明确「按实际需求做，不再必须」）。
  4. `MAX_QUEUED_RUNS=50` 与 `MAX_ACTIVE_RUNS=4` 仍是保守值，未压测。

## 2026-09-24 — V0.5.3 全局配额与限额

- **需求**：补齐 V0.5 原定「生产运行保障」的第三项——全局请求限额 + 模型侧 RPM/TPM 约束。
  本轮按用户决定只做前三版，幂等回写与影子运行仍顺延。
- **`LLM_RPM_LIMIT` / `LLM_TPM_LIMIT` 从死配置改为真正生效**（差异 W29）。两者自 V0.2
  起就声明在 `Settings` 里、`.env.example` 写着「V0.5 起生效」，但**全仓无任何读取方**
  ——与 `MAX_ACTIVE_RUNS` 在 v0.5.1 之前是同一形态。本机 `.env` 已配
  `LLM_RPM_LIMIT=10000` / `LLM_TPM_LIMIT=500000`，因此**下次真实运行就会开始受限**。
- **用令牌桶而非固定窗口**（差异 W30）：固定窗口在边界会瞬时放行 2× 配额，而本项目一次
  运行 32 次 LLM 调用、历时二十余分钟，恰好会撞在边界上。容量由 `LLM_RPM_BURST` /
  `LLM_TPM_BURST` 配置（置空取对应 limit）。
- **接线点在「每次真实出网」那一层**（差异 W31）。这是本版最容易做错的地方：放在
  `llm_call` 的循环外层会让 3 次尝试只记 1 个请求，配额被**系统性低估 3 倍**。
- **TPM 是「预扣 + 校正」**（差异 W32）：出网前按 prompt 长度保守估算并预扣（不预扣就
  无从在出网前限制），拿到响应后按真实 `usage` 追扣或退还（不校正则偏差在长会话下持续
  累积）。`usage` 缺失时**不校正**——「未知」不能当成 0 退还。
- **回放不消耗额度**（差异 W33）：回放请求没有真的发出去，让它排队只会让全量测试凭空
  变慢，还会掩盖真实的限额行为。
- **多 worker 是显式分配**（差异 W35）：`LLM_RPM_LIMIT` 的语义是全局额度，按
  `WORKER_REPLICAS` 等分给每进程；**不可整除时启动即报错**，避免「RPM=60 配 4 个
  worker 实际打到 240」这类静默超发。不做跨进程共享配额（差异 W36，需中间件，与
  约束 7 冲突）。
- **并发上限与速率限额并存**（差异 W37）。顺序刻意是「先速率、后并发」：速率等待可达
  数十秒，若先占住并发名额等待，会把其它本可以出网的请求一起堵住。
- **新文件**：`src/customer_profile/execution/quota.py`（`TokenBucket` / `LlmQuota` /
  `estimate_prompt_tokens`）。
- **测试**：430 passed / 13 skipped（基线 412，增量 18，即 `tests/test_quota.py`）。用
  **假时钟 + 假 sleeper** 推进令牌桶，不真 sleep——RPM 的等待动辄几十秒。新用例覆盖：
  容量内不等待、等待时长精确、空闲不超容、单次超容报错、未配置完全不干扰、只配一个
  维度不激活另一个、TPM 预扣与双向校正、usage 缺失不退还、图片计入估算、按 worker 数
  等分、不可整除启动报错、回放不耗额度。
- **测试写法上踩到一处**：`Settings` 会自动读仓库根 `.env`，而本机已配 RPM/TPM，导致
  「未配置时不生效」的断言被真实配置污染。测试 helper 改为**显式置空**限额后才成立
  ——这一条对后续写「未配置」类断言同样适用。

## 2026-09-24 — V0.5.2 持久化队列与独立 worker

- **需求**：补齐 V0.5 原定「生产运行保障」的前三项——持久化任务队列 + 独立 worker、
  重启中断处理、全局配额。**范围决定（用户）**：本轮只做前三版，幂等回写与影子运行
  顺延（`docs/specs/V0.5工作台任务触发入口.md` 的 W19 之后续作）。
- **队列复用 `workflow_runs` 表，不新建队列表**（用户决定，差异 W26）。入队即写一行
  `status='queued'`——这个状态从 V0.2 就声明了，**此前全仓无写入方**，只作缺省值。
  现在它终于成为「先持久化入队再返回 run_id」这条验收标准的载体。
- **原子领取用单条 `UPDATE ... WHERE status='queued'` + `RETURNING`**（SQLite 3.50）。
  比「先 SELECT 再 UPDATE」安全：后者之间的窗口会让两个 worker 领到同一个 run_id
  并把它跑两遍。实测本机 SQLite 3.50.4 支持 `RETURNING`。
- **修正一处与验收标准直接冲突的既有行为**（差异 W24）：`mark_running_as_interrupted`
  此前把 `running` 与 `queued` 一并标为中断——worker 一重启，所有还没开始的任务都变成
  需要人工处理的中断态，「重启不丢任务」随之不成立。现在只标 `running`。
  `tests/test_persistence.py` 的那条用例相应改为断言 `queued` **存活**。
- **租约过期不自动重领**（差异 W28）。租约与心跳已实现（崩溃后可回收），但启动路径
  刻意不自动重跑：重领会把跑到一半的运行**整条重跑**，而根目录文档明确「不做任意节点
  断点自动续跑」，且 worker 停机不证明外部副作用未发生。`requeue_expired_leases`
  保留为显式动作，留给后续版本的人工处理入口。
- **`POST /runs` 的 429 触发条件改为队列深度**（差异 W23，新增 `MAX_QUEUED_RUNS`）。
  `RunSlotGate` 保留但只作用于进程内路径（`Service.submit_local`）——CLI 与单测仍走它。
- **保留 `INLINE_WORKER`（缺省 true）**（差异 W27）：API 进程同时充当一个消费者。
  五个既有测试文件依赖「POST /runs 后立刻能查到并跑完」的契约，单机部署也不应被迫
  另起进程；生产按 §4 分离部署时置 false 即可。
- **新增最小补列机制**（差异 W25）：`CREATE TABLE IF NOT EXISTS` 对已存在的表不会补列，
  而 `data/customer_profile.db` 已在磁盘上。读 `PRAGMA table_info` 后按需 `ALTER TABLE
  ADD COLUMN`，**不引入 alembic**（规划 §5 约束 7）。另补 `PRAGMA busy_timeout`——
  API 与 worker 双进程共库后，不设它时写相遇会直接报 `database is locked`。
- **新文件**：`src/customer_profile/worker.py`（`python -m customer_profile.worker`）。
- **测试**：412 passed / 13 skipped（基线 398，增量 14，即 `tests/test_worker_queue.py`）。
  新增用例覆盖：入队即落库（无消费者参与）、两 Store 并发领取只成功一次、子运行不参与
  排队、租约过期回收、队深上限拒绝且不留半条记录、`QueueFull`→429、旧库补列后旧数据
  完好、双连接并发写不抛锁错、worker 单轮执行、`INLINE_WORKER=false` 时不自动执行。

## 2026-09-24 — V0.5.1 工作台任务触发入口（V0.5 开工）

- **需求**：在 V0.4 只读运行台上增加任务触发入口——输入 `phone_number` 提交任务，
  就地观察完整运行过程与结果。**范围决定（用户）**：V0.5 只做触发入口，迁移规划里
  原定的「生产运行保障」（持久化队列、独立 worker、全局配额、幂等回写、影子运行）
  整体后移（差异 W19）。
- **盘点后有意外收获：缺口比预想的小。** `POST /runs` 早在 V0.2 就存在，V0.4 明确
  「不改动」它（不是删掉），且已满足「先落库再返回 run_id」。过程展示也齐备
  （`/runs/{id}/graph` + 节点尝试 + 子运行下钻 + 时间线），`RunDetail` 已有运行中 2s 轮询。
  **真缺口只有两处**：前端没有任何写方法，以及 `MAX_ACTIVE_RUNS` 是死配置。
  因此本次**不新增提交端点**。
- **`MAX_ACTIVE_RUNS` 由死配置改为生效**（差异 W20）。它此前只在 `settings.py` 声明、
  `.env.example` 写了语义、`.env` 里配了值，而**全仓无任何读取方**——唯一真正生效的并发
  限制是 `Scheduler._active_slot`（`MAX_ACTIVE_NODES`，限制一次运行**内部**的节点数）。
  新增 `runner.RunSlotGate`，装在**服务层**（对 CLI 与 API 同样生效），满则抛
  `RunSlotUnavailable` → API 翻成 **HTTP 429**，**不排队**：排队会让调用方拿到 run_id
  却迟迟不开始，看上去像提交失败。
  - 用**计数器而非 `Semaphore`**：申请严格非阻塞（满了立刻抛），而 `Semaphore` 没有
    非阻塞 `acquire`，用它就得去动 `_value` 私有属性。
  - **子运行不占名额**：它们由 `SubWorkflowRunner` 直接调 `Scheduler.run`，不走
    `Service.submit`——主入口一次扇出十几个子运行，若子运行也申请名额，上限一低就自锁。
  - 名额在 `add_done_callback` 里释放；`_active` 对多余释放**直接忽略**，掉到负数会让
    闸门永久多放行若干个运行，比少放行危险。
- **手机号提交前校验**（差异 W21）：此前**无任何前置校验**，`start` 节点的必填性在执行期
  才检查，于是少传或传错会变成一个**失败的运行**而非可解释的 4xx。现在主入口的
  `phone_number` 按 `^1[3-9]\d{9}$` 校验，失败返回 422。**只对主入口生效**——其它工作流
  （录音类要 `customer_data` / `data_source`）入参契约不同，一并约束会挡掉合法调用。
  手机号还会进 `business_ref` 与下游 HTTP 路径（`/data/history/{phone}`），
  空串或带斜杠的值会在外部服务上变成难以归因的 404。
- **新增只读端点 `GET /runtime`**（差异 W22）：报出回放模式、**回写开关**、模型名与并发上限，
  **不含任何凭据**（由测试断言）。存在的理由很具体：`WRITEBACK_ENABLED=false` 时回写节点
  只产出「已跳过」的结果，**而运行仍显示 succeeded**。不在界面上显式标出，用户会把「成功」
  读成「画像已写进生产库」。这正是 `smoke_run.py` 在报告里记 `writeback_enabled` 的理由，
  只是那份记录在 CLI 里，运行台上看不到。
- **前端**：`api/client.ts` 新增 `postJson` / `submitRun` / `runtime`；新增
  `components/SubmitRun.tsx`（手机号 + 批次号，提交中禁用按钮，错误就地显示，并显示运行态
  面板、回写关闭时明确标注）；`App.tsx` 新增「提交任务」标签页，提交成功后直接落到该运行的
  详情页开始轮询。`business_ref` 取手机号——子运行会自动派生它，父运行不设会让列表里
  父运行显示 `—` 而子运行带着号码。
- **一处实现失误，记录备查**：改写 `runner.py` 时用「从 `class RunSlotGate:` 到
  `async def build_service(`」的范围做替换，而 `Service` 类定义在 `RunSlotGate` **之前**，
  于是整个 `Service` 被删掉（首次表现为 5 个测试文件 collection error）。从 `git show HEAD`
  取回时又漏了它上面的 `@dataclass` 装饰器（表现为 `TypeError: Service() takes no arguments`
  与 16 failed / 48 errors）。两处均已修正，最终 diff 只剩意图内的增补。
- 测试 **398 项**（25 个文件）全绿，13 skipped。新增 `tests/test_submit_gate.py`（12 项）。
  基线 386 项，增量 12 项，对得上。
- 版本号 `0.4.4` → `0.5.1`（`pyproject.toml` 与 FastAPI `version`）。
- 文档：新增 `docs/specs/V0.5工作台任务触发入口.md`；`differences.md` 新增 W19–W22；
  `.env.example` §6 补记 `MAX_ACTIVE_RUNS` 的真实语义与 429 行为；
  `frontend/README.md` 不再称前端为「只读」。

## 2026-09-23 — V0.4.4 执行顺序时间线 + 静态资源托管（V0.4 收口）

- **修正一个更早的缺陷：节点 `started_at_ms` 从未落库，且语义是错的。**
  `scheduler._execute_node` 把它与 `duration_ms` 写成同一个表达式
  （`int((self._clock() - started) * 1000)`），即「距节点开始的耗时」而非时间戳；
  更隐蔽的是 `upsert_node_execution` 的 `ON CONFLICT ... DO UPDATE SET` 列表里**没有这一列**，
  `node_started` 插入时留空、收尾时也不覆盖，于是永远是 NULL。
  后果：没有墙钟起点就摆不到同一条时间轴上，也无法判断两个节点是否并发——
  而这正是 V0.4 的验收要求。修复：`started_ms = now_ms()` 取墙钟，
  `duration_ms` 继续用单调钟（墙钟会被系统时间调整影响、算出负时长），
  并把 `started_at_ms` / `queued_at_ms` 补进冲突更新列表。
- **新增 `frontend/src/components/RunTimeline.tsx`**：横向条形按真实起止时间绘制，
  与依赖图**并排切换**而非互相替代——图表达依赖，时间线表达并发，缺一就看不出
  「并行跑的」与「有依赖所以先后跑」的区别（迁移说明 §8.4）。标题如实报出重叠对数。
- **不加新端点**：`/runs/{id}/graph` 已带节点的 `started_at_ms` / `duration_ms` 与运行的
  起点，时间线直接渲染，省掉一次往返。
- **SERVE_UI 收口**：实测 `GET /` 返回页面、哈希资源 200、API 路由仍可达。
- 版本号 `0.4.1` → `0.4.4`。
- 测试 **385 项**（24 个文件）全绿，13 skipped。新增 `tests/test_node_timing.py`（5 项），
  含「起点是墙钟而非耗时」「起点不等于耗时」「时间戳落在运行窗口内」
  「重叠可被算出」四类判别断言。

## 2026-09-23 — V0.4.3 运行详情：节点状态叠加、尝试与子运行下钻

- **修正一个 V0.4 期间引入的缺陷：`attempt_count` 恒为 0。** 节点行上的这一列是
  详情页判断「要不要去拉尝试」的依据，而后端**从无生产代码写它**——`bump_attempt_count`
  只有自己的单测在调（`test_persistence.py:177`）。后果是数据在库里、界面永远空白，
  且不报任何错。修复采用**派生**而非自增：`node_finished` 收尾时用节点行自己的
  ``call_path`` 去数 `request_attempts`（`store.count_attempts` 新增按节点/调用路径过滤）。
  不用自增的原因很具体：`fork_iteration` 复制父上下文的 `call_path`，而迭代循环体节点
  在库里是按 `<父路径>/iter:<id>/<n>` 存的，按 `node_ref.call_path` 自增会更新到**零行**，
  静默失效。派生与来源表天然一致，键必然对得上。
- **新增 `GET /runs/{run_id}/nodes/{node_id}/attempts`**：节点级尝试，供详情页按需拉取。
  **不并进 `/graph`**：`response_text` 单条可达上万字符，主入口跑一次有 17 处 LLM 调用，
  全部内联会让首屏为了一张图加载好几 MB。`call_path` 可选传入以区分迭代各轮。
- **新增 `frontend/src/components/RunDetail.tsx`**：节点状态叠加到**当次定义快照**的图上
  （详情页从不请求当前定义的拓扑）；点节点看输入输出、错误、实际 requests 与
  每次尝试的响应/用量/错误；带 `sub_run_id` 的节点可进入子运行。
  - 长文本折叠显示（默认 1200 字符，可展开），避免一条 LLM 响应把面板撑成几屏。
  - 运行仍在跑时 2s 轮询，进入终态即停。
  - App 用**导航栈**而非单个 runId：主入口 → 画像生成 → 其子流程可逐层下钻，
    单值会让「返回」只能退回列表。
- **测试自身的缺陷一并修正**：新用例原先用 `next(...)` 扫生成器取带尝试的节点，
  没有匹配时抛 `StopIteration`，在协程里变成
  `RuntimeError: coroutine raised StopIteration`——报错信息完全指不到「计数没被写」
  这个真实原因。改为显式断言并打印各节点计数，且**先断言计数非零再验证内容**。
- 测试 **379 项**（23 个文件）全绿，13 skipped。新增 `tests/test_node_attempts.py`（7 项）。

## 2026-09-23 — V0.4.2 任务列表：筛选、排序与分页

- **`GET /runs` 扩展**：新增 `created_after_ms` / `created_before_ms` / `order_by`。
  返回值仍是**裸数组**——现有调用方与测试按数组取，改成 `{items,total}` 信封是破坏性
  变更，而分页需要的只是多一个数字。
- **新增 `GET /runs/count`**：与 `GET /runs` **共用同一组筛选条件**的总数。存储层把
  条件构造抽成 `_run_filter_clause`，列表与计数共用。两者条件一旦分叉，页数就会比实际
  数据多，翻到后半段是空页——那看起来像后端丢了数据，而不是页码越界。测试逐套筛选断言
  「列表条数 == 计数」。
  - 路由顺序是个真陷阱：`/runs/count` 必须注册在 `/runs/{run_id}` **之前**，否则会被
    当成 run_id 吃掉、返回 404。已由 `test_count_route_is_not_shadowed_by_run_detail` 钉住。
- **排序键走白名单**（`_RUN_ORDERINGS`），不拼接进 SQL；未知取值**回落到缺省**而不是
  报错——排序参数写错不该让整个列表打不开。注入形状的取值由
  `test_order_by_cannot_inject_sql` 验证只回落、不执行。
- **新增 `frontend/src/components/RunList.tsx`**：状态徽标、工作流、业务标识、开始时间、
  耗时、错误摘要（截断并带 title 悬浮全文）；筛选按工作流/状态/业务标识、含子运行开关、
  三种排序、分页器。筛选条件变化时**回到第 0 页**：留在第 5 页而新条件下只有 1 页会看到
  空表，那看起来像「查不到数据」而不是「页码越界」。
- 前端工作流下拉**取 `/workflows`**，不写死清单——写死会在新增工作流后静默漏掉它。
- 前端加标签页切换（工作流结构 / 任务列表），刻意不引入路由库：三个视图、无深链接需求。
- 测试 **372 项**（22 个文件）全绿，13 skipped。新增 `tests/test_run_filters.py`（11 项）。
  时间范围参数经独立探针验证：两侧计数与列表均自洽、同值区间返回空。

## 2026-09-23 — V0.4.1 只读运行台：布局与拓扑渲染

- **新增 `src/customer_profile/layout.py`**：按 `predecessors` 求最长路径分层的**纯函数**
  `layout()`，导出时按需计算、不落库。前端因此不需要自己的布局逻辑，「只有一份拓扑」
  这条约束落到了代码上。有 `NodeDef.coords` 的节点优先用其值（保留手工钉位置的能力）。
- **新增两个只读端点**：`GET /runs/{run_id}/graph`（当次版本定义快照 + 节点状态 + 子运行
  映射，一次返回，避免前端三次往返）与 `GET /definition-versions/{version_id}`。
  两者返回**同一种嵌套形状**（图在 `definition` 键下）——同一类东西两种形状会让前端
  出现两套取图代码。
- **`GET /workflows/{id}/topology` 增加 `layout` 字段**：`asdict()` 的 `coords` 保持原样
  （声明），算出来的展示坐标单独放在 `layout`（展示）。混在一起就分不清哪个是人定的。
- **新增 `frontend/`**（Vite + React + TypeScript + `@xyflow/react` 12）：结构视图带缩放、
  平移、点选、缩略图；节点按执行状态着色，**分支边按 `source_handle` 用不同颜色并在边上
  标注分支名**——看不出哪条边是哪一支时，分支图等于没画。构建产物 401.72 kB（gzip 127.10 kB）。
- **`frontend/dist/` 有意入库**（差异 W16）：运行台要能在未安装 node 的机器上直接启动。
  仓库根与 `frontend/` 两处 `.gitignore` 各加例外，缺一处产物就会被忽略。代价是改前端必须
  重新构建并提交产物，已写入 `frontend/README.md`。
- **`SERVE_UI` 开关**（差异 W17）：缺省 `false`；置 true 时把 `dist` 挂在根路径。置 true 但
  未构建时打印告警并跳过，**不**让服务启动失败——可选功能不该变成硬依赖。已实测挂载成功：
  `GET /` 返回页面，哈希资源 200，API 路由仍可达。
- **三处实现弯路，均由实测纠正**（差异 W18）：
  1. 设计初稿称「迭代工作流带环」，**实测 20 个定义全部无环**（`structural_order()` 逐个成功）。
     迭代节点引用其循环体，循环体不反向引用。
  2. 据此实现的第一版按 DFS 回边断环，在自造环图上把链**中间**切断，环上节点错位。
  3. 第二版改用有界松弛并声称「带环时退化为忽略回边」，实测环上节点被推到第 10 层——
     松弛在环上不收敛到有意义的层号。最终口径：**按 DAG 设计，不为环做特殊语义**，
     环普查由 `test_no_real_workflow_is_cyclic` 断言，引入环时直接失败。
  另修正一处**测试自身的缺陷**：环用例只断言相对顺序，退化实现（节点被推到第 10 层）
  照样通过；改为断言精确层号后才暴露。
- 测试 **361 项**（21 个文件）全绿，13 skipped。新增 `tests/test_layout.py`（12 项，含
  「最长路径而非最短路径」的判别用例，用最短路径会把汇合点排到扇出点左边、连线反向）与
  `tests/test_run_graph.py`（7 项）。
- 版本号 `0.2.0` → `0.4.1`（`pyproject.toml` 与 FastAPI `version`，此前落后于已交付的 V0.3）。

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
