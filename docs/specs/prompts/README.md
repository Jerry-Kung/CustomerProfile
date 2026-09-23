# 提示词评审副本

`docs/specs/prompts/` 是**评审副本**，不是运行期来源。

- 运行期唯一来源是包内资源 `src/customer_profile/templates/`（`TemplateRepository.DEFAULT_TEMPLATE_DIR`）。
- 本目录与之**逐字节一致**，由 `tests/test_templates.py::test_review_copies_match_package_templates` 断言。
- 命名与包内资源相同：`<workflow_slug>__<node_id>.txt`；slug 见 `scripts/export_templates.py` 的 `SLUGS`。
- 正文的原始出处是 `dify_dsl_data/` 的 DSL；`python scripts/export_templates.py --check` 断言包内资源与 DSL 逐字符一致。
- **正文不可改**（W11）。任何提示词文本改动属业务优化，须单独提出并获得确认。

## 重新生成的正确顺序

```bash
python scripts/export_templates.py            # 1. 从 DSL 重新导出包内资源
cp src/customer_profile/templates/*.txt docs/specs/prompts/   # 2. 同步副本
python -m pytest tests/test_templates.py      # 3. 断言两边一致
```

## 历史（为何曾失真）

V0.1（`b982115`）生成本目录时用了另一套命名（显示名转写 + 内容哈希 + `__llm` / `__template-transform` 类型后缀）
与另一套抽取口径（保留 `system` / `user` 角色行、CRLF 换行），且取自更早的 DSL 修订。
到 V0.3 收口时，81 份里有 43 份与当时 DSL 的正文不同——W11 声称「两边由测试断言一致」，而那个测试从未存在。
V0.3 收口时按包内资源重新导出并统一命名，并补上了上述断言。
