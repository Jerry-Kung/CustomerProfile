# MySQL Schema Design (design-mysql-schema)

> 历史资料：记录编写时的设计与操作方式。当前实现和部署请从[文档导航](../../README.md)进入。


## Table

- `customer_profile_tasks` stores one logical task per phone number.
- Business uniqueness is enforced by `customer_phonenumber` unique key.
- Re-submit with same phone can be handled by `INSERT ... ON DUPLICATE KEY UPDATE`.

## Fields

- `customer_phonenumber` (`VARCHAR(32)`, NOT NULL): unique business key.
- `status` (`ENUM`): `queued`, `processing`, `succeeded`, `failed`.
- `output` (`JSON`, nullable): full normalized output payload.
- `card_id`, `nickname`: parsed scalar fields for list display/filtering.
- `error_message` (nullable): failure context.
- `dify_run_id` (nullable): optional async correlation id.
- `created_at`, `updated_at`: operational timestamps.

## Indexing Strategy

- `UNIQUE uk_customer_profile_tasks_phone (customer_phonenumber)`:
  - Guarantees one row per phone number.
  - Supports exact result lookup by full phone number.
- `KEY idx_customer_profile_tasks_phone_prefix (customer_phonenumber)`:
  - Supports list query using prefix pattern like `WHERE customer_phonenumber LIKE '138%'`.
  - Note: only left-anchored prefixes can use this index efficiently.
- `KEY idx_customer_profile_tasks_status_updated (status, updated_at DESC)`:
  - Supports status-based dashboard/list filtering with recent-first order.
- `KEY idx_customer_profile_tasks_updated (updated_at DESC)`:
  - Supports default recent-first list retrieval.

## Suggested Query Templates

```sql
-- Submit or overwrite same phone number.
INSERT INTO customer_profile_tasks (customer_phonenumber, status)
VALUES (?, 'queued')
ON DUPLICATE KEY UPDATE
  status = VALUES(status),
  output = NULL,
  card_id = NULL,
  nickname = NULL,
  error_message = NULL,
  updated_at = CURRENT_TIMESTAMP;
```

```sql
-- List by phone prefix.
SELECT customer_phonenumber, card_id, nickname, status, updated_at
FROM customer_profile_tasks
WHERE customer_phonenumber LIKE CONCAT(?, '%')
ORDER BY updated_at DESC
LIMIT ? OFFSET ?;
```

```sql
-- Fetch final result by full phone.
SELECT output, status, updated_at, error_message
FROM customer_profile_tasks
WHERE customer_phonenumber = ?;
```
