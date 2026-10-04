# 系统优化：模板节点与 LLM 节点合并（方案）

> 状态：**待确认**（2026-10-04 起草）。
> 上游：用户指出「`XXXPrompt - XXX工具` 这种粗笨的原始搭配在前端仍然存在」，要求收掉。
> 与前一份的关系：`docs/history/系统优化-Dify残留清理（计划）.md` 的 §1 把「1:1 模板与 llm
> 合并」明确划到范围之外，本方案即该遗留项。前一份已完成的是**改名**（去前缀、去后缀），
> 本方案动的是**结构**。
> 归属：`docs/specs/`；任务收口后归档至 `docs/history/`。

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

## 6. 待确认

| 编号 | 问题 | 建议 |
|---|---|---|
| D1 | A 类是否按 §4 合并 | 合并 |
| D2 | 模板文件名是否随之从 `<slug>__<提示词节点ID>` 改为 `<slug>__<llm节点ID>` | **暂不改**。资源名是不可见键，改名牵动 17 份文件与 17 处 `template=`，且与「`node_id` 不动」的既定约束无关；留待模板命名专项（承前份 Q2） |
| D3 | `profile_features_analysis` 三处 `on_branch` 指向**已删除**的 if-else 节点（`1776760124715` / `17767603231790`），现为悬空门 | 合并时一并**丢弃**，不让新快照多一个指不到的门；实现时先确认调度层对悬空门的既有行为 |
| D4 | `prompt_field="input_prompt"` 去掉还是保留 | 去掉。渲染改走 `template=`，保留一个没有绑定来源的入参名只会误导 |

## 7. 批次与验收

| 批次 | 任务 | 落点 |
|---|---|---|
| A | A 类 17 处折叠：删模板节点、搬 `template=` 与 `bindings` 到 `llm`、去 `prompt_field` | production / profile_features / chat_history / evidence_subagent / user_feedback |
| B | 同步构造器：`customer_profile_production.py` 的 `_llm()` 增 `template=` 参数；`chat_history_data` / `evidence_subagent` 的对应声明 | 3 个模块 |
| C | 判定 D3：丢弃三处悬空门（若确认安全） | profile_features_analysis |
| D | 同步文档：`docs/changelog.md`；若 D3 落地，`docs/design/执行语义口径.md` 记一句悬空门处置 | 2 份 |
| E | 重建并提交 `frontend/dist/`（显示名与标题来自 API，本次若只改结构则产物不变；仍须核对） | `frontend/dist` |

**验收标准**：

1. A 类 17 对在定义里只剩一个节点，前端流程列表不再出现「提示词」行；
2. `grep` 确认 A 类节点 ID 从 `workflows/*.py` 消失，且模板正文零改动；
3. `python -m pytest tests/` 全绿——**先确认无测试断言受影响的节点数**
   （已知 `test_contract.py` 只数入口图 5 节点、`test_api.py` 只数 mengshi/hci 各 4 节点，
   三者都不含 `template-transform`；`test_entry_end_to_end.py` 用微缩证据图，须逐条核对）；
4. 抽查一对（如「客户沟通话术生成」）跑通，`attempt` 表记录的模型与 token 与合并前一致。

## 8. 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 绑定搬运遗漏 | `llm` 只剩 `input_prompt` 一个绑定，搬到 llm 后漏掉某个来源会让提示词缺值 | §3.2 的「占位符 == 目标名」逐对静态核对；`test_llm_template_bindings.py` 在合并后会自动覆盖这些节点（它们在 `llm` 且 `config['template']` 非空） |
| 分支门悬空 | D3 三处门指向已删节点 | 先丢弃；实现前确认调度层行为 |
| 历史快照形状变化 | 旧快照两行、新快照一行 | 读取端不假设键全集，不做迁移（与前份同口径） |
| 前端产物未同步 | 结构变化若影响 `dist` 而未重建 | `git status` 核对 `frontend/dist` 无改动或已重建 |
