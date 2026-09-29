# 模板资源

DSL 里的提示词/模板正文（81 份），逐字符外置为受 Git 管理的资源文件。

- 命名：`<workflow_slug>__<node_id>.txt`；slug 见 `scripts/export_templates.py` 的 `SLUGS`。
- **唯一运行期来源就是本目录**；`docs/specs/prompts/` 是台账与评审副本，两边由测试断言逐字符一致。
- 重新生成：`python scripts/export_templates.py`；只比对：`python scripts/export_templates.py --check`。
- **正文默认不可改**。任何提示词文本改动都属业务优化，须单独提出并获得确认（规划 §6.5）。
- **已确认的正文修订要登记为有意差异**：`scripts/export_templates.py` 的 `TEMPLATE_OVERRIDES`
  以 `{文件名: {DSL 原文片段: 修订后片段}}` 记录偏离，导出与比对都据此应用。**不登记就会让
  `--check` 失败**——这条断言刻意不允许正文与 DSL 悄悄分叉。当前仅一处（W49，见
  `docs/specs/ledger/differences.md` §1.7）。
  登记片段若在 DSL 原文中找不到，导出脚本**报错退出**：DSL 可能已变，需复核修订是否仍必要。
- 文本里保留 Dify 的 `{{#node_id.field#}}` 引用与 `{{ "字面量" }}` 形态原样；由
  `templates.render_text` 在渲染时把引用映射成本节点的入参名，见该函数的说明。
