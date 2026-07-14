# 04 · 数据模型与队列机制

## 表 `customer_profile_tasks`

权威源：`db/migrations/001_create_customer_profile_tasks.sql`（创表）+ `002_add_task_image_columns.sql`（追加列）。
后续可能存在 `work_env / batch_id` 列——`MySQLTaskRepository._has_*_column()` 用 `information_schema` 探测一次后缓存，**对应迁移文件不在仓库**里（说明这两列可能是手工 ALTER 或在另一个未签入的迁移里加上的）。修改 schema 时注意：仓库代码同时兼容"有 / 没有" `work_env` 和 `batch_id` 两种实际表结构。

```sql
CREATE TABLE customer_profile_tasks (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  customer_phonenumber VARCHAR(32) NOT NULL,
  status ENUM('queued','processing','succeeded','failed') NOT NULL DEFAULT 'queued',
  output JSON NULL,
  card_id VARCHAR(128) NULL,
  nickname VARCHAR(255) NULL,
  error_message VARCHAR(1024) NULL,
  wechat_homepage_image VARCHAR(512) NULL,   -- 002 追加
  wechat_moments_image  VARCHAR(512) NULL,   -- 002 追加
  dify_run_id VARCHAR(128) NULL,
  -- 可能存在但仓库迁移里没有:
  -- work_env  VARCHAR(32) NULL,
  -- batch_id  VARCHAR(64) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_customer_profile_tasks_phone (customer_phonenumber),
  KEY idx_customer_profile_tasks_phone_prefix  (customer_phonenumber),
  KEY idx_customer_profile_tasks_status_updated (status, updated_at DESC),
  KEY idx_customer_profile_tasks_updated (updated_at DESC)
) ENGINE=InnoDB CHARSET=utf8mb4;
```

### 字段语义

| 列 | 含义 | 写者 | 读者 |
|---|---|---|---|
| `customer_phonenumber` | 业务唯一键 | API submit | 所有 |
| `status` | 状态机当前位 | submit + Worker | 所有 |
| `output` | Dify 返回的解析后整段 JSON | Worker (succeeded) | 结果接口 |
| `card_id` / `nickname` | 从 `output` 拍平的冗余列，用于列表展示 / 索引 | Worker (succeeded) | 列表/结果接口 |
| `error_message` | `"<CODE>: <detail>"`，截断到 1000 字符 | Worker (failed) + `requeue_stale_processing()` | 结果接口（用于回推 code） |
| `wechat_*_image` | 预留的图片引用列 | （未启用） | （未启用） |
| `dify_run_id` | Dify 工作流运行 ID | （未持久化）当前 `DifyClient` 拿到 `workflow_run_id` 后只在内存里用，没写库 | — |
| `work_env` | `test` / `production`，决定 Dify API key | submit | Worker |
| `batch_id` | 透传给 Dify 工作流的客户端批次号 | submit | Worker |

### 索引解读

- **`UNIQUE uk_*_phone`**：让 upsert（`ON DUPLICATE KEY UPDATE`）和按全号查询命中常数级。
- **`idx_*_phone_prefix`**：列表接口的 `LIKE 'xxx%'` 走它（左前缀匹配）。
- **`idx_*_status_updated`** 与 **`idx_*_updated`**：Worker 的 "最旧 queued 优先" `ORDER BY updated_at ASC, id ASC` 以及看板按状态过滤都从这里走。

## 队列机制（核心）

源码：`src/infrastructure/db/task_repository.py`。

### 入队（`upsert_queued`）

```sql
GET_LOCK('cp_task_queue_admit', 10)        -- 进程间互斥；仅用于"是否超容"判断
START TRANSACTION
SELECT status WHERE customer_phonenumber=? FOR UPDATE
-- 容量检查仅作用于"新增等待者"
IF 现状不是 queued 且 COUNT(status='queued') >= TASK_QUEUE_MAX_WAITING:
    ROLLBACK; raise TaskQueueFullError
INSERT ... ON DUPLICATE KEY UPDATE
    status='queued', output=NULL, card_id=NULL, nickname=NULL,
    error_message=NULL, work_env=VALUES(work_env), batch_id=VALUES(batch_id),
    updated_at=CURRENT_TIMESTAMP
COMMIT
RELEASE_LOCK
```

两件事必须一起想：

1. **`GET_LOCK` 不是行锁**，是 MySQL 命名锁；它把"统计 queued + 判断是否超容 + 写入"串行化，避免并发提交时同时通过 `count_queued()` 的校验。
2. **同号重提不计入新增**：`existed_status == 'queued'` 时跳过容量检查 ⇒ 已经在排队的用户可以无限次"撞页面"重提，不会被自己挤出去。

### 出队（`claim_next_queued`）

```sql
START TRANSACTION
SELECT * FROM customer_profile_tasks
WHERE status='queued'
ORDER BY updated_at ASC, id ASC
LIMIT 1
FOR UPDATE SKIP LOCKED                     -- 关键
UPDATE customer_profile_tasks
SET status='processing', updated_at=CURRENT_TIMESTAMP
WHERE id=? AND status='queued'             -- CAS 兜底，rowcount!=1 即放弃
COMMIT
```

`SKIP LOCKED` 是 MySQL 8 特性。多线程/多进程 Worker 并发抢占无需在应用层加锁，**这也是为什么数据库必须是 MySQL 8.0+**。

### 收尾

- 成功：`mark_succeeded(task_id, output, card_id, nickname)` — 写 `status='succeeded'`、`output=json.dumps(...)`、`card_id`、`nickname`，`error_message=NULL`。
- 失败：`mark_failed(task_id, error_message[:1000])` — `status='failed'`，保留旧 output（如果有）。

### 卡死回收（保留未启用）

`requeue_stale_processing(timeout_seconds, limit=100)` 把"`status='processing'` 且 `updated_at < now - timeout_seconds`"的若干行重置回 `queued`，并把 `error_message` 设为 `PROCESSING_TIMEOUT_RECOVERED`。没人调用它——要启用需要加 cron / 启动钩子。

## 时间戳

- `created_at`：首次 INSERT 时写入；upsert 不更新它。
- `updated_at`：`DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP`，每次任意 UPDATE 都会刷新。**列表排序、队列出队顺序、stale 判定都依赖它**——所以**不要为了"轻量字段更新"绕过它**。
- 序列化层（`_to_iso`）会把 `datetime` 转成 ISO 8601 字符串供前端使用。
