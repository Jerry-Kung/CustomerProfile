# 系统优化：模板节点与 LLM 节点合并（方案）

> 状态：**已执行**（2026-10-05）。D1–D4 均按本方案的建议落地，17 对 A 类节点已折叠；
> 全量测试 374 passed / 30 skipped。
> 上游：用户指出「`XXXPrompt - XXX工具` 这种粗笨的原始搭配在前端仍然存在」，要求收掉。
> 与前一份的关系：`docs/history/系统优化-Dify残留清理（计划）.md` 的 §1 把「1:1 模板与 llm
> 合并」明确划到范围之外，本方案即该遗留项。前一份已完成的是**改名**（去前缀、去后缀），
> 本方案动的是**结构**。
> 归属：`docs/specs/`；任务收口后归档至 `docs/history/`。**已于 2026-10-05 归档。**

## 1. 问题

前端流程列表里，每一处 LLM 调用占**两行**：

```
template-transform | 客户沟通话术生成 提示词
llm                | 客户沟通话术生成
```

第一行只做一件事——把上游变量填进提示词正文，产出 `output`；第二行接住它发起调用。
这是迁移前「提示词节点 + 工具节点」的形态原样搬过来的：`customer_profile_production.py`
的 `_llm()` docstring 自称「由工具节点归一而来的 `llm` 节点」。

## 2. 现状盘点

全项目 **46** 个 `template-transform` 节点，按下游分三类：

| 类 | 数量 | 形态 | 分布 |
|---|---|---|---|
| **A 类** | 17 | 产提示词 → `llm`（`llm` 的 `template=None`、`prompt_field="input_prompt"`） | production 11 / profile_features 3 / chat_history 1 / evidence_subagent 1 / user_feedback 1 |
| **B 类** | 8 | `join` 步骤 → `llm`（`llm` **自带** `template=`，正文含 `{{#1775811655657.output#}}`） | 截图类 6 / 录音类 2 |
| 独立 | 21 | 非 `llm` 下游：聚合前预置、无数据兜底、有数据原样输出 | 各工作流 |

**只有 A 类是用户所指的搭配。** B 类的两行是「多张图片内容聚合 / 多图片内容总结」，
不是 Dify 味命名（见 §5）。独立类不涉及 LLM。

## 3. 关键事实（已核实）

1. **A 类的 17 个节点 ID 不被任何模板正文引用。** 全项目正文里的 `{{#id.field#}}` 只出现
   9 个 ID，A 类一个都不在内。
2. **A 类模板的占位符与绑定目标一一对应。** 逐个核对：正文中的 `{{ name }}` 集合等于该节点
   `bindings` 的 `target` 集合，`strict=True` 下不会缺变量。
3. **`llm` 执行器已支持模板渲染。** `execute_llm` 的判定顺序是「先看显式提示词入参，再看
   `config['template']`」，且渲染变量表**同时**按来源 `node.field` 与目标名建键
   （`variables.setdefault(name, value)`）。因此把绑定原样搬到 `llm` 节点后，正文不必改。
4. **`outputs` / `entries` / `exits` 均不引用 A 类节点。**
5. **`node_id` 不动。** 45 份历史快照与 9 处正文硬引用都依赖它；删除 A 类节点只影响**新**
   快照，旧快照多两行、读取端不假设键全集，无需数据迁移。

## 4. 方案：A 类折叠为单节点

对每一对：

- 删除 `template-transform` 节点；
- 把它的 `template=` 与全部 `bindings` 搬到 `llm` 节点；
- `llm` 节点**保留自己的 `node_id` 与标题**（标题已是业务动作名）；
- 去掉 `prompt_field="input_prompt"`——提示词改由 `template=` 渲染。

结果：一行一个 LLM 节点。A 类 17 处，节点总数从 46 降到 29（独立 21 + B 类 8）。

**同名问题已解决**：上一轮已把模板节点标题改成「`X 提示词`」，合并后该节点消失，
「提示词」这个词自然从列表里退场，`llm` 节点独留业务名。

## 5. B 类为何不合并

B 类的 `llm` 节点**已经自带**提示词模板；上游那行只做 `{{ arr | join('\n') }}`。

合并必须删掉 join 节点 `1775811655657`，而该 ID 写在 **8 份模板正文**里
（`{{#1775811655657.output#}}`）。删节点就要改正文，正文按 §6.5 属「不可动、须单独确认」，
本轮不申请。故 B 类保留——它本就不是用户所指的「Prompt - 工具」搭配。

## 6. 决策与执行结果

| 编号 | 决策 | 执行结果 |
|---|---|---|
| D1 | A 类按 §4 合并 | **已执行**。17 对折叠为 17 个 `llm` 节点，`template=` 与 `bindings` 搬到 `llm`，节点标题不变 |
| D2 | 模板文件名保持 `<slug>__<提示词节点ID>` | **未改**。17 份模板资源与 17 处 `template=` 名字原样保留；资源名是不可见键，留待模板命名专项（承前份 Q2） |
| D3 | 丢弃 `profile_features_analysis` 三处悬空门 | **已执行**。三处 `on_branch` 指向的 if-else 节点本就不在定义里，调度层 `gates` 也只对有门节点生效，丢弃后行为不变；`user_feedback_data` 的那一处门指向**存活**的 `BRANCH`，已保留 |
| D4 | 去掉 `prompt_field="input_prompt"` | **已执行**。提示词改由 `config['template']` 渲染，`execute_llm` 的判定顺序「先入参、后模板」保持不变 |

## 7. 批次与验收

| 批次 | 任务 | 落点 |
|---|---|---|
| A | A 类 17 处折叠：删模板节点、搬 `template=` 与 `bindings` 到 `llm`、去 `prompt_field` | production / profile_features / chat_history / evidence_subagent / user_feedback |
| B | 同步构造器：`customer_profile_production.py` 的 `_llm()` 增 `template=` 参数；`chat_history_data` / `evidence_subagent` 的对应声明 | 3 个模块 |
| C | 判定 D3：丢弃三处悬空门（若确认安全） | profile_features_analysis |
| D | 同步文档：`docs/changelog.md`；若 D3 落地，`docs/design/执行语义口径.md` 记一句悬空门处置 | 2 份 |
| E | 重建并提交 `frontend/dist/`（显示名与标题来自 API，本次若只改结构则产物不变；仍须核对） | `frontend/dist` |

**验收结果（2026-10-05）**：

1. A 类 17 对各自只剩一个节点，`template-transform` 从 46 降到 29，`llm` 从 27 升到 44；
2. 17 个原提示词节点**不再作为节点**出现在定义中（`template-transform` 总数 46 → 29），
   但它们的 ID 仍以常量形式存在——那是模板资源名 `<slug>__<ID>.txt` 的键，按 D2 保留。
   模板正文（`templates/*.txt`）**零改动**；
3. `python -m pytest tests/` → **374 passed / 30 skipped**（新增 17 条 skip 来自
   `test_llm_template_bindings.py`：A 类模板不含 `{{#node#}}` 引用，故跳过）。
   结构校验零悬空引用、零占位符缺绑定、`load_all()` 21 个工作流全数通过校验；
4. 实现中修掉一处自查缺陷：首版脚本把单元素 `after=(X),` 写成 `after=(X)`，Python
   解析成字符串后 `tuple(...)` 展开为字符，前置引用全悬空、4 个工作流被校验剔除。
   已改为保尾随逗号，`/health` 与 `load_all()` 重新一致。

## 8. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 绑定搬运遗漏 | `llm` 只剩 `input_prompt` 一个绑定，搬到 llm 后漏掉某个来源会让提示词缺值 | §3.2 的「占位符 == 目标名」逐对静态核对；`test_llm_template_bindings.py` 在合并后会自动覆盖这些节点（它们在 `llm` 且 `config['template']` 非空） |
| 分支门悬空 | D3 三处门指向已删节点 | 先丢弃；实现前确认调度层行为 |
| 历史快照形状变化 | 旧快照两行、新快照一行 | 读取端不假设键全集，不做迁移（与前份同口径） |
| 前端产物未同步 | 结构变化若影响 `dist` 而未重建 | `git status` 核对 `frontend/dist` 无改动或已重建 |
