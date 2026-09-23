# V0.1.1 盘点脚本

Dify DSL 基线盘点的提取与复核工具。**只读 DSL，只写台账**——不接模型、不调外部接口、不修改 `dify_dsl_data/` 下任何原始文件。

## 前置条件

需要本地存在 `dify_dsl_data/`（19 份 DSL YAML）。该目录被 `.gitignore` 忽略，不随仓库分发，须由业务方提供后才能重跑。目录缺失时脚本报错退出，不会静默产出空台账。

仅依赖 **PyYAML**（本机 Python 3.14.5 + PyYAML 6.0.3 验证通过）：

```bash
pip install -r scripts/requirements.txt
```

## 使用

```bash
# 生成台账到 docs/specs/ledger/
python scripts/inventory/extract_ledger.py

# 复核：重跑提取并与已入库台账比对，不一致时非零退出
python scripts/inventory/verify_ledger.py

# 附带把提示词正文导出到 docs/specs/prompts/（入库，作为后续版本优化对象）
python scripts/inventory/extract_ledger.py --dump-prompts
```

标准输出刻意只用 ASCII，以适配 GBK 控制台；写入的文件一律 UTF-8。

## 产出

| 文件 | 内容 | 是否入库 |
|---|---|---|
| `docs/specs/ledger/node_ledger.json` | 19 工作流 × 全部节点的身份、类型、前置、绑定、坐标；含边的句柄与迭代标记 | 是 |
| `docs/specs/ledger/variable_ledger.json` | 每条绑定的消费者、生产者、是否直连前置 | 是 |
| `docs/specs/ledger/resource_inventory.json` | 提示词/模板指纹（长度、行数、SHA256、变量名）。**不含正文** | 是 |
| `docs/specs/ledger/tool_mapping.json` | `tool_name` ↔ 本地文件，附实测引用次数 | 是 |
| `docs/specs/ledger/workflow_digest.json` | 每工作流的节点/边/按类型计数与顺序依赖边数 | 是 |
| `docs/specs/prompts/*.txt` | 提示词正文（逐字符原文），供 V0.3 逐字符比对与后续版本优化 | **是** |

## 两点须知的实现口径

**`{{#node.field#}}` 出现次数 ≠ 绑定条目数。** 提取器统计的是「绑定条目」：节点的 `variables[]` 条目、`tool` 节点的每个参数、条件分支的变量选择器，都算一条绑定，即使其值不是引用字面量。DSL 文本中 `{{#...#}}` 的实际出现次数是另一个数（当前 111），两者都打印，不要混用。

**提示词正文入库是有意的（D8）。** 正文是后续版本的重要优化对象，需要评审与追溯，因此作为正式产物存放在 `docs/specs/prompts/`。台账另存哈希与长度，用于快速定位变更；逐字符比对始终针对本地 DSL。

正文入库前已扫描确认不含密钥形状字符串与手机号，`verify_ledger.py` 会持续校验这一点——一旦有人把凭据写进提示词，复核会失败。

## 复核能发现什么

`verify_ledger.py` 会捕获：DSL 文件被修改（比对 SHA256，并提示「台账已过期，请重新生成」）、节点/边/类型计数变化、节点字段结构变化、绑定内容变化、`tool_name` 未在映射表中、映射表里有从未被引用的条目、以及台账中混入密钥形状的字符串。它重新提取后比对，不是拿文件和自己比。
