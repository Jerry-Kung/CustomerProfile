# Dify 对齐落地说明（align-dify-contract）

> 历史资料：记录编写时的设计与操作方式。当前实现和部署请从[文档导航](../../README.md)进入。


本文件用于落地以下事项：
- 工作流入参/出参对齐
- `output` JSON schema 固化
- 图片上传方式对齐
- 是否支持异步 `run_id` 的能力识别与接入策略

## 1) 工作流契约（建议基线）

### 1.1 输入变量（发送给 Dify）

> 变量名需要与 Dify 工作流节点配置严格一致；如不一致，以 Dify 控制台为准并同步更新本文。

| 变量名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `customer_phonenumber` | string | 是 | 手机号（建议后端先做格式校验） |
| `wechat_homepage_image` | file_id/url/string | 否 | 微信主页截图 |
| `wechat_moments_image` | file_id/url/string | 否 | 朋友圈截图 |

### 1.2 输出约定（Dify -> 后端）

- 以 Dify 返回中的 `output` 字段作为权威结果载体（JSON 字符串）。
- 后端需执行两层处理：
  1. 反序列化 `output` 字符串为对象；
  2. **规范化**：若根级缺少 `nickname` 但存在 `customer.nickname`（字符串），则提升到根级 `nickname`，再提取两个可选快捷字段：
     - `card_id`
     - `nickname`
- 落库原则：
  - 原始 `output` 字符串原样落库（用于审计/回溯）
  - 解析出的 `card_id`、`nickname` 冗余落库（用于查询展示）

## 2) output JSON Schema

- 文件：`contracts/customer-profile-output.schema.json`
- `card_id`、`nickname` 都是可选快捷字段，不参与任务成功判定。
- 字段存在且为非空字符串时冗余写入对应数据库列；缺失、空值或其他类型时列值写
  `NULL`，完整值仍保留在 `output` 中。
- 兼容策略：`additionalProperties: true`
  - 允许 Dify 增量输出其他字段，不阻塞主流程
  - 只有 `output` 不是合法 JSON 对象时才判定为解析失败

## 3) 图片上传方式对齐

根据 Dify 侧配置，支持以下三种对齐方式（优先级按推荐顺序）：

1. **先上传文件，再传 `file_id`（推荐）**
   - 后端先调用 Dify 文件上传接口，获得 `file_id`
   - 运行工作流时传 `file_id`
   - 优点：可控、稳定、链路清晰

2. **直接 multipart 到工作流接口（若 Dify 配置支持）**
   - 提交任务时直接混合文本与文件字段
   - 优点：调用简化；缺点：对接口兼容性要求高

3. **传可访问 URL**
   - 图片先存对象存储，再传 URL
   - 需确保 Dify 侧可访问该 URL（鉴权/有效期/网络连通）

### 推荐落地规则

- 增加配置项 `DIFY_IMAGE_MODE`，可选：
  - `file_id`
  - `multipart`
  - `url`
- 默认值建议：`file_id`
- 服务启动时记录当前模式并打印一次非敏感初始化日志

## 4) 异步 run_id 能力识别与接入

目标：确认 Dify 1.13.0 是否支持“发起运行后返回 `run_id`，再轮询/回调取结果”。

### 4.1 能力识别（一次性联调）

1. 发起一次最小样本运行（仅手机号）
2. 检查响应是否包含：
   - `run_id` / `task_id` / `workflow_run_id`（任一）
3. 检查是否存在状态查询接口（按 run id 查询）
4. 记录接口路径、鉴权方式、状态枚举、完成态取结果方式

### 4.2 接入策略

- 若支持异步 run id：
  - 提交阶段：创建本地任务并保存 `dify_run_id`
  - Worker 阶段：轮询状态（指数退避，最大轮询窗口可配置）
  - 完成后取 `output` 并解析落库
- 若不支持异步 run id：
  - Worker 采用同步等待 Dify 返回（仅后台进程执行，不在 HTTP 同步请求中等待）

### 4.3 最小配置项（建议）

| 配置项 | 示例 | 说明 |
|---|---|---|
| `DIFY_BASE_URL` | `https://dify.example.com` | Dify 地址 |
| `DIFY_API_KEY` | `***` | 鉴权 token（仅环境注入） |
| `DIFY_WORKFLOW_ID` | `wf_xxx` | 工作流 ID |
| `DIFY_IMAGE_MODE` | `file_id` | 图片模式 |
| `DIFY_ASYNC_ENABLED` | `true/false` | 是否启用 run_id 异步路径 |
| `DIFY_POLL_INTERVAL_MS` | `2000` | 轮询间隔 |
| `DIFY_POLL_TIMEOUT_MS` | `900000` | 轮询超时（如 15 分钟） |

## 5) 错误分类（便于排障）

- `DIFY_REQUEST_FAILED`：请求失败/鉴权失败/网络失败
- `DIFY_TIMEOUT`：Dify 长时间未完成
- `DIFY_OUTPUT_INVALID_JSON`：`output` 非法 JSON
- `DIFY_OUTPUT_SCHEMA_MISMATCH`：`output` JSON 根节点不是对象

## 6) 验收清单（本待办完成标准）

- [x] 固化 `output` JSON schema 文件
- [x] 明确输入变量与可选图片字段映射
- [x] 明确三种图片传输模式及默认推荐
- [x] 明确 run_id 异步能力识别步骤与双路径接入策略
- [x] 给出最小配置与错误码分层建议
