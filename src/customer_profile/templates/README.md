# 模板资源

DSL 里的提示词/模板正文（81 份），逐字符外置为受 Git 管理的资源文件。

- 命名：`<workflow_slug>__<node_id>.txt`；slug 见 `scripts/export_templates.py` 的 `SLUGS`。
- **唯一运行期来源就是本目录**；`docs/specs/prompts/` 是台账与评审副本，两边由测试断言逐字符一致。
- 重新生成：`python scripts/export_templates.py`；只比对：`python scripts/export_templates.py --check`。
- **正文不可改**。任何提示词文本改动都属业务优化，须单独提出并获得确认（规划 §6.5）。
- 文本里保留 Dify 的 `{{#node_id.field#}}` 引用与 `{{ "字面量" }}` 形态原样；由
  `templates.render_text` 在渲染时把引用映射成本节点的入参名，见该函数的说明。
