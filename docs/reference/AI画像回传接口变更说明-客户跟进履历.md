# AI 画像远程回传接口变更说明：客户跟进履历

> 适用范围：客户画像 v2 远程 AI 分析结果回传接口
>
> 变更内容：`analysis_result` 新增 `follow_up_history`（客户跟进履历）区块
>
> 兼容性：接口路径、鉴权方式、请求顶层结构均不变；旧工作流不传该区块时行为不变。

---

## 1. 变更概述

本次在客户画像 v2 中新增「客户跟进履历」能力。客户画像分析任务根据客户全量信息，
梳理出销售顾问跟进客户的关键节点列表，通过原画像回传接口回写。

变更点：

1. `analysis_result` 新增 `follow_up_history` 数组；
2. 跟进履历按客户全量替换，写入新表 `customer_profile_follow_up_record`；
3. 画像版本判定增加 `follow_up_history`：仅回传该区块也可将客户标记为 `profile_version="v2"`；
4. 每次成功回写后，画像分析快照新增 `follow_up_history_snapshot`；
5. 画像详情、M2M 画像查询、MCP 画像查询、画像历史接口均返回 `follow_up_history`。

未变更：

- 回写接口地址、鉴权方式、请求顶层字段不变；
- `profile_summary`、`basic_notes_updates`、`ai_insight`、`purchase_decisions`、
  `deal_battle`、`decision_psychology` 原有结构不变；
- 同一 `(phone, batch_id)` 的幂等处理不变。

---

## 2. 回写接口

| 项 | 说明 |
| --- | --- |
| 服务 | `cdp-m2m`（M2M API） |
| 默认端口 | `8084` |
| 方法 | `POST` |
| 路径 | `/api/v1/remote/callback/update-profile` |
| 鉴权 | 请求头 `X-API-Key: <M2M_API_KEY>` |
| Content-Type | `application/json` |

请求顶层结构不变：

```jsonc
{
  "phone": "18682284385",
  "batch_id": "<批次号>",
  "analysis_result": {
    "profile_summary": { /* 原有结构 */ },
    "basic_notes_updates": [ /* 原有结构 */ ],
    "ai_insight": { /* 原有结构 */ },
    "purchase_decisions": [ /* 原有结构 */ ],
    "deal_battle": { /* 原有结构 */ },
    "decision_psychology": [ /* 原有结构 */ ],

    // 本次新增
    "follow_up_history": [ /* 客户跟进履历 */ ]
  }
}
```

---

## 3. 新增区块 `follow_up_history`

### 3.1 业务含义

`follow_up_history` 是由远程 AI 根据客户全量信息梳理出的关键跟进节点列表，用于在客户画像
中展示销售顾问跟进客户的时间线。

每个节点包含：

1. 序号；
2. 节点名称；
3. 跟进内容（多项文本列表）；
4. 跟进时间；
5. 附加信息。

### 3.2 字段说明

```jsonc
"follow_up_history": [
  {
    "id": "fu-1",
    "sequence": 1,
    "node_name": "首次到店",
    "contents": [
      "客户到店了解 X700，重点体验智驾与空间",
      "销售顾问完成需求确认并邀约试驾"
    ],
    "followed_at": "2026-05-18T14:30:00",
    "followed_at_text": "2026年5月18日下午",
    "extra_info": {
      "跟进渠道": "到店",
      "参与人": ["销售顾问张三", "客户本人"],
      "关联车型": "X700"
    }
  }
]
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 否 | 节点唯一标识；缺省后台生成 `fu-{sequence}` |
| `sequence` | integer | 否 | 序号，≥1；缺省按数组顺序补 `1..N` |
| `node_name` | string | 是 | 节点名称，如 `首次到店`、`首次试驾` |
| `contents` | string[] | 是 | 跟进内容多项文本列表；允许空数组，建议至少一条 |
| `followed_at` | string | 否 | 规范化跟进时间，支持 ISO8601 / `YYYY-MM-DD` / `YYYY-MM-DD HH:MM[:SS]` |
| `followed_at_text` | string | 否 | 原始或模糊时间展示值，如 `2026年5月中旬` |
| `extra_info` | object | 否 | 附加信息，开放键值对象，可放渠道、参与人、关联车型等 |

### 3.3 字段生成与校验规则

| 规则 | 说明 |
| --- | --- |
| 节点数量 | 单客户最多 100 个 |
| `id` | 最长 50 字；同一客户内不可重复；缺省生成 `fu-{sequence}` |
| `sequence` | 正整数；同一客户内不可重复；缺省按数组下标 + 1 |
| `node_name` | 非空，最长 100 字 |
| `contents` | 必须为数组；单条非空且最长 1000 字；最多 20 条 |
| `followed_at` | 可为空；为空字符串时按空处理；格式非法返回 400 |
| `followed_at_text` | 可为空；最长 100 字 |
| `extra_info` | 必须为 JSON 对象；序列化后不超过 64KB |

校验失败时返回 `400`，本次画像回写整体回滚，不会部分写入。

### 3.4 回写语义

| 回传情况 | 处理 |
| --- | --- |
| `follow_up_history` 缺失 | 保留该客户原有跟进履历 |
| `follow_up_history: null` | 保留该客户原有跟进履历 |
| `follow_up_history: []` | 清空该客户全部跟进履历 |
| `follow_up_history: [...]` | 删除原有记录后，按数组内容全量写入 |

> 跟进履历是 AI 每次根据客户全量信息重新梳理的时间线，因此采用**全量替换**，
> 不做按节点增量合并，避免历史节点残留。

### 3.5 落库映射

写入新表 `customer_profile_follow_up_record`：

| 回传字段 | 数据库列 | 说明 |
| --- | --- | --- |
| `follow_up_history[].id` | `item_id` | 节点唯一标识 |
| `follow_up_history[].sequence` | `sequence_no` | 序号 |
| `follow_up_history[].node_name` | `node_name` | 节点名称 |
| `follow_up_history[].contents` | `contents` | JSON 文本列表 |
| `follow_up_history[].followed_at` | `followed_at` | DateTime |
| `follow_up_history[].followed_at_text` | `followed_at_text` | 原始时间展示值 |
| `follow_up_history[].extra_info` | `extra_info` | JSON 对象 |
| 本次回写批次 | `trigger_batch_id` | 用于溯源 |

索引与约束：

- `UNIQUE(customer_phone, item_id)`；
- `INDEX(customer_phone, sequence_no)`。

### 3.6 快照

每次成功回写后，在 `customer_profile_analysis_snapshot` 写入：

| 列 | 说明 |
| --- | --- |
| `follow_up_history_snapshot` | 当次回写后的完整跟进履历快照 |

画像历史接口 `GET /api/v1/customers/{phone}/profile/history` 会返回该字段；
历史快照无数据时返回 `[]`。

---

## 4. 版本判定变化

变更前，只要 `analysis_result` 包含以下任一区块，即判定为 v2：

- `ai_insight`
- `purchase_decisions`
- `deal_battle`
- `decision_psychology`

变更后增加：

- `follow_up_history`

因此：

- 仅回传 `follow_up_history` 也会将客户标记为 `profile_version="v2"`；
- 只回传 `profile_summary` / `basic_notes_updates` 不改变版本判定；
- 旧工作流不传 `follow_up_history` 时，行为与变更前一致。

---

## 5. 查询接口变化

以下接口的 v2 响应新增 `follow_up_history`，无数据时返回 `[]`：

| 接口 | 说明 |
| --- | --- |
| `GET /api/v1/customers/{phone}/profile` | UI 客户画像详情 |
| `GET /api/v1/remote/data/profile/{phone}/new` | M2M 完整画像详情 |
| `GET /api/v1/remote/data/profile/{phone}` | M2M 精简画像 / 远程 AI 参考 |
| MCP `get_customer_profile` / `batch_get_customer_profiles` | MCP 画像查询 |
| `GET /api/v1/customers/{phone}/profile/history` | 画像历史快照 |

查询返回示例：

```jsonc
"follow_up_history": [
  {
    "id": "fu-1",
    "sequence": 1,
    "node_name": "首次到店",
    "contents": [
      "客户到店了解 X700，重点体验智驾与空间",
      "销售顾问完成需求确认并邀约试驾"
    ],
    "followed_at": "2026-05-18T14:30:00",
    "followed_at_text": "2026年5月18日下午",
    "extra_info": {
      "跟进渠道": "到店",
      "参与人": ["销售顾问张三", "客户本人"],
      "关联车型": "X700"
    }
  }
]
```

排序规则：`sequence` 升序，其次按记录 ID 升序。

---

## 6. 完整回传示例

```json
{
  "phone": "18682284385",
  "batch_id": "8f0c2f7a-2b4e-4f0a-9e3d-1234567890ab",
  "analysis_result": {
    "profile_summary": {
      "name": "刘",
      "gender": "男"
    },
    "basic_notes_updates": [
      {
        "attribute_key": "city",
        "attribute_value": "广州",
        "reasoning_detail": {
          "summary": "根据常驻地推测。",
          "steps": ["朋友圈高频定位广州"],
          "evidence": ["朋友圈定位：广州"],
          "confidence": 0.8
        }
      }
    ],
    "ai_insight": {
      "purchase_intent_level": {
        "value": "C",
        "reasoning_detail": {
          "summary": "综合到店行为、对比车型与核心关注，判定当前购车意向等级为 C 级。",
          "steps": [
            "多条关注点集中在智驾与空间等体验验证阶段",
            "已形成明确的对比车型清单",
            "尚未进入价格与金融方案的实质谈判"
          ],
          "evidence": ["到店动因：了解智驾体验、对比同级车型"],
          "confidence": 0.82
        }
      },
      "customer_segment": {
        "value": "高效务实中产",
        "reasoning_detail": {
          "summary": "综合关注点与决策风格，归入高效务实中产人群。",
          "steps": ["关注通勤效率与智能化升级", "决策风格为理性比较型"],
          "evidence": ["换车动因：通勤效率提升、智能化升级"],
          "confidence": 0.78
        }
      }
    },
    "purchase_decisions": [
      {
        "category": "到店动因",
        "items": [
          {
            "id": "visit-driving",
            "label": "了解智驾体验",
            "reasoning_detail": {
              "summary": "画像将智驾体验列为到店动因。",
              "steps": ["到店动因包含了解智驾体验", "核心关注包含智驾"],
              "evidence": ["到店动因：了解智驾体验"],
              "confidence": 0.85
            }
          }
        ]
      },
      {
        "category": "核心关注",
        "items": [
          {
            "id": "focus-driving",
            "label": "智驾",
            "reasoning_detail": {
              "summary": "智驾被直接列为核心关注。",
              "steps": ["核心关注包含智驾"],
              "evidence": ["核心关注：智驾"],
              "confidence": 0.85
            }
          }
        ]
      }
    ],
    "deal_battle": {
      "win_conditions": [
        {
          "id": "win-testdrive",
          "label": "试驾验证智驾优势",
          "reasoning_detail": {
            "summary": "若试驾能验证智驾优势，客户更可能推进决策。",
            "steps": ["核心关注包含智驾", "到店动因包含了解智驾体验"],
            "evidence": ["核心关注：智驾"],
            "confidence": 0.8
          }
        }
      ],
      "loss_conditions": [
        {
          "id": "loss-price",
          "label": "竞品价格优势明显",
          "reasoning_detail": {
            "summary": "竞品价格优势明显时存在战败风险。",
            "steps": ["对比车型包含多个竞品", "决策阻力包含价格敏感"],
            "evidence": ["对比车型：X700、问界 M9、理想 L9"],
            "confidence": 0.75
          }
        }
      ]
    },
    "decision_psychology": [
      {
        "key": "decision_style",
        "label": "决策风格",
        "value": "理性比较型",
        "reasoning_detail": {
          "summary": "综合对比行为与关注点，判断决策风格为理性比较型。",
          "steps": ["同时对比多个同级车型", "关注智驾、空间、舒适、价值感等多项指标"],
          "evidence": ["对比车型：X700、问界 M9、理想 L9"],
          "confidence": 0.8
        }
      },
      {
        "key": "current_attitude",
        "label": "当前态度",
        "value": "积极了解",
        "reasoning_detail": {
          "summary": "客户当前处于积极了解与对比阶段。",
          "steps": ["主动到店体验", "持续关注智驾与空间"],
          "evidence": ["到店动因：了解智驾体验"],
          "confidence": 0.78
        }
      }
    ],
    "follow_up_history": [
      {
        "id": "fu-1",
        "sequence": 1,
        "node_name": "首次到店",
        "contents": [
          "客户到店了解 X700，重点体验智驾与空间",
          "销售顾问完成需求确认并邀约试驾"
        ],
        "followed_at": "2026-05-18T14:30:00",
        "followed_at_text": "2026年5月18日下午",
        "extra_info": {
          "跟进渠道": "到店",
          "参与人": ["销售顾问张三", "客户本人"],
          "关联车型": "X700"
        }
      },
      {
        "id": "fu-2",
        "sequence": 2,
        "node_name": "首次试驾",
        "contents": [
          "客户完成试驾并体验智驾功能",
          "客户对智驾表现认可，但希望进一步对比竞品价格"
        ],
        "followed_at": "2026-05-20T10:30:00",
        "followed_at_text": "2026年5月20日上午",
        "extra_info": {
          "跟进渠道": "试驾",
          "跟进结果": "意向提升",
          "下一步动作": "发送竞品对比与金融方案"
        }
      }
    ]
  }
}
```

---

## 7. 校验与错误响应

| HTTP 状态 | 场景 | 响应体 |
| --- | --- | --- |
| `200` | 回写成功 | `{"msg": "Profile updated successfully via remote webhook."}` |
| `400` | 字段校验失败（如序号重复、时间格式非法） | `{"detail": "<错误详情>"}` |
| `403` | `X-API-Key` 缺失或错误 | `{"detail": "Invalid M2M API Key"}` |
| `500` | 回写异常（含唯一约束冲突） | `{"detail": "<错误详情>"}` |

常见校验失败示例：

```json
{"detail": "duplicate follow_up_history sequence: 1"}
```

```json
{"detail": "follow_up_history.followed_at must be ISO8601 or YYYY-MM-DD[ HH:MM[:SS]]"}
```

---

## 8. 发布注意事项

1. 新表 `customer_profile_follow_up_record` 会由服务启动时的
   `Base.metadata.create_all()` 自动创建。
2. 已存在的 `customer_profile_analysis_snapshot` 表**不会**自动增加
   `follow_up_history_snapshot` 列，发布前需执行：

   ```bash
   python scripts/migrate_profile_v2.py --apply
   ```

   或手工执行：

   ```bash
   deploy/sql/20261113_create_customer_follow_up_history.sql
   ```

3. 建议先完成数据库变更，再部署后端代码，最后更新远程 AI 工作流 Prompt，
   使其在 `analysis_result` 中输出 `follow_up_history`。

---

## 9. 相关文档

- 回传接口完整数据结构：`docs/AI画像远程回传接口数据结构说明.md`
- 数据库表与 M2M 查询接口：`docs/客户画像数据库与机机接口说明.md`
- 落库逻辑：`app/profile_v2.py:apply_v2_update`
- 数据模型：`app/models.py:CustomerProfileFollowUpRecord`