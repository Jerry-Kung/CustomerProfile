# execute_prompts 使用说明

> 历史资料：记录编写时的设计与操作方式。当前实现和部署请从[文档导航](../../README.md)进入。


本目录用于存放“分阶段可直接执行”的代码生成 Prompt，覆盖 `customer_profile` 后端从契约冻结到发布运维的 0-6 阶段。

核心目的：

- 固定每阶段输入边界，避免跨阶段漂移
- 固定强制读取上下文，避免遗漏关键合同
- 固定输出格式，保证执行结果可审计、可复现

## 使用顺序

按以下顺序执行，不建议跳阶段：

1. `phase-00-contract-and-config.md`
2. `phase-01-project-skeleton.md`
3. `phase-02-submit-and-queue.md`
4. `phase-03-dify-integration.md`
5. `phase-04-query-apis.md`
6. `phase-05-quality-and-hardening.md`
7. `phase-06-release-and-ops.md`

## 统一执行约束（所有阶段必须遵守）

- 不得改动合同基线语义：
  - `contracts/customer-profile-api.openapi.yaml`
  - `contracts/customer-profile-output.schema.json`
  - `db/migrations/001_create_customer_profile_tasks.sql`
- 必须遵守状态机：`queued -> processing -> succeeded | failed`
- 不得硬编码敏感配置（Dify/MySQL 凭据仅环境变量注入）
- 仅在当前阶段边界内改动；跨阶段事项写入“已知风险/后续建议”

## 强制读取上下文规则

每个阶段 Prompt 的“强制读取文件”都必须包含以下基础三件套：

- `contracts/customer-profile-api.openapi.yaml`
- `contracts/customer-profile-output.schema.json`
- `db/migrations/001_create_customer_profile_tasks.sql`

在基础三件套之上，按阶段附加补充文档：

- `phase-00`：`docs/archive/initial-design/dify-contract-alignment.md`、`docs/archive/initial-design/mysql-schema-design.md`、`docs/archive/initial-design/async-architecture-decision.md`
- `phase-01`：`docs/archive/initial-design/async-architecture-decision.md`、`docs/archive/initial-design/mysql-schema-design.md`、`docs/archive/initial-design/dify-contract-alignment.md`
- `phase-02`：`docs/archive/initial-design/mysql-schema-design.md`、`docs/archive/initial-design/async-architecture-decision.md`
- `phase-03`：`docs/archive/initial-design/dify-contract-alignment.md`、`docs/archive/initial-design/async-architecture-decision.md`
- `phase-04`：`docs/archive/initial-design/mysql-schema-design.md`
- `phase-05`：`docs/archive/initial-design/async-architecture-decision.md`、`docs/archive/initial-design/dify-contract-alignment.md`
- `phase-06`：`docs/archive/initial-design/async-architecture-decision.md`、`docs/archive/initial-design/dify-contract-alignment.md`

## 统一模板（复制到每个阶段 Prompt）

以下结构为固定模板，标题命名与顺序不应变化：

1. `## 目标与边界`
2. `## 强制读取文件`
3. `## 输入上下文与实施约束`
4. `## 产出清单`
5. `## 验收标准`
6. `## 自检清单`
7. `## 输出格式要求（执行后汇报）`

每个章节最低要求：

- 目标与边界：明确“要做什么”和“禁止做什么”
- 强制读取文件：先列基础三件套，再列阶段补充文件
- 输入上下文与实施约束：至少包含合同语义、状态机、敏感配置约束
- 产出清单：明确新增/修改的文件类型与能力点
- 验收标准：可验证、可观察、可判定通过/失败
- 自检清单：可勾选，覆盖主干与错误分支
- 输出格式要求：固定“变更摘要 + 关键设计 + 验证结果 + 风险”

## 验收口径

- 先校验合同一致性，再校验实现完整性
- 先校验主干链路，再校验错误分支
- 每阶段必须给出可复现验证结果（命令、日志或测试）
