# Phase 00 执行 Prompt：契约冻结与配置模板

> 历史资料：记录编写时的设计与操作方式。当前实现和部署请从[文档导航](../../README.md)进入。


## 目标与边界

本阶段目标是冻结运行契约与配置模板，不实现业务逻辑。

- 目标：
  - 冻结 OpenAPI、输出 JSON Schema、MySQL 迁移的运行时引用方式
  - 定义环境变量清单、默认值策略、脱敏日志规则
  - 明确 local/test/prod 覆盖策略
- 边界：
  - 不实现 API 路由与 Worker 业务流程
  - 不改动合同字段、状态码、状态机语义
  - 不写入真实密钥，不提交 `.env`

## 强制读取文件

- `contracts/customer-profile-api.openapi.yaml`
- `contracts/customer-profile-output.schema.json`
- `db/migrations/001_create_customer_profile_tasks.sql`
- `docs/archive/initial-design/dify-contract-alignment.md`
- `docs/archive/initial-design/mysql-schema-design.md`
- `docs/archive/initial-design/async-architecture-decision.md`

## 输入上下文与实施约束

- 输入上下文：
  - 接口必须保持异步提交语义（`POST` 返回 accepted，不等待 Dify）
  - `card_id`、`nickname` 为可选快捷字段，不参与输出成功判定
  - 数据库唯一业务键必须是 `customer_phonenumber`
- 实施约束：
  - 配置键名需覆盖 Dify/MySQL/应用通用配置
  - 真实值只允许来自环境变量或密钥管理，不得硬编码
  - 启动日志只输出脱敏摘要（例如 `DIFY_API_KEY=app-***`）

## 配置设计规范（Dify/MySQL）

### Dify 配置组

| 键名 | 必填 | 默认值/示例 | 敏感级别 | 说明 |
|---|---|---|---|---|
| `DIFY_BASE_URL` | 是 | `http://118.145.238.55` | L1-非敏感 | Dify 基础地址 |
| `DIFY_RUN_URL` | 是 | `${DIFY_BASE_URL}/v1/workflows/run` | L1-非敏感 | 工作流运行接口 |
| `DIFY_API_KEY` | 是 | `app-***` | L3-高敏感 | Dify 鉴权密钥，只允许环境注入 |
| `DIFY_USER_ID` | 是 | `klh` | L2-内部 | 调用主体标识 |
| `DIFY_IMAGE_MODE` | 否 | `file_id` | L1-非敏感 | 可选 `file_id`/`multipart`/`url` |
| `DIFY_POLL_INTERVAL_MS` | 否 | `2000` | L1-非敏感 | 轮询间隔 |
| `DIFY_POLL_TIMEOUT_MS` | 否 | `900000` | L1-非敏感 | 轮询超时 |

### MySQL 配置组

| 键名 | 必填 | 默认值/示例 | 敏感级别 | 说明 |
|---|---|---|---|---|
| `MYSQL_HOST` | 是 | `118.145.238.55` | L2-内部 | 数据库主机 |
| `MYSQL_PORT` | 否 | `3306` | L1-非敏感 | 数据库端口 |
| `MYSQL_USER` | 是 | `root` | L2-内部 | 数据库账号 |
| `MYSQL_PASSWORD` | 是 | `***` | L3-高敏感 | 数据库密码，只允许环境注入 |
| `MYSQL_DATABASE` | 是 | `customer_profile` | L2-内部 | 数据库名 |

### 覆盖顺序（固定）

`process.env` > 环境专属文件（如 `.env.prod`）> 默认模板（如 `.env.example` 注释值）

约束：
- 运行期最终值必须来自 `process.env`，模板文件仅用于示例与键名声明
- 禁止在代码内写入任何 fallback 真实密钥（例如 `|| "app-xxx"`）
- 生产环境必须通过部署系统注入，不依赖仓库内文件

## 敏感信息注入原则

1. **只注入，不落盘**：高敏感值（`DIFY_API_KEY`、`MYSQL_PASSWORD`）不得写入仓库文件、日志、测试快照。
2. **按级别输出日志**：
   - L1 可明文输出（例如端口、轮询间隔）
   - L2 建议部分脱敏（例如主机、用户名可显示前后缀）
   - L3 必须全程脱敏（仅显示前缀与长度，例如 `app-*** (len=32)`）
3. **错误信息去敏**：连接失败或请求失败日志不得拼接原始凭据。
4. **示例与真实值分离**：`env.example` 仅保留占位符，真实 `.env` 必须加入忽略规则且不入库。
5. **最小权限原则**：MySQL 账号建议按业务库最小权限授权，禁止默认超管长期使用。

## 产出清单

- 配置模板文件（例如 `env.example` 或等价模板）
- 配置说明文档（变量说明、默认值、覆盖顺序、敏感级别）
- 合同引用说明（代码中如何加载 OpenAPI/Schema/SQL 基线）
- 环境分层规则（local/test/prod）与冲突优先级说明

## 验收标准

- 配置清单覆盖以下关键项：
  - Dify：`DIFY_BASE_URL`、`DIFY_RUN_URL`、`DIFY_API_KEY`、`DIFY_USER_ID`、`DIFY_IMAGE_MODE`、`DIFY_POLL_INTERVAL_MS`、`DIFY_POLL_TIMEOUT_MS`
  - MySQL：`MYSQL_HOST`、`MYSQL_PORT`、`MYSQL_USER`、`MYSQL_PASSWORD`、`MYSQL_DATABASE`
- 文档明确真实密钥不入库，并给出脱敏日志规则
- 文档明确敏感信息等级、注入来源、覆盖顺序与禁止硬编码 fallback 规则
- 合同基线路径固定且可被后续阶段复用
- 不产生与 OpenAPI/Schema/迁移冲突的变更

## 自检清单

- [ ] 已核对所有配置键名与文档一致
- [ ] 已明确各环境覆盖优先级（如 process env > env file）
- [ ] 已明确敏感字段脱敏输出策略
- [ ] 已确认高敏感字段仅通过环境注入且无硬编码 fallback
- [ ] 已确认合同文件路径可被运行期加载

## 输出格式要求（执行后汇报）

1. 变更摘要（新增/修改文件）
2. 配置键名总览与分组说明
3. 环境覆盖策略与敏感字段处理规则
4. 验收结果与未决风险
