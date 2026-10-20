# AI 画像远程回传接口数据结构说明

> 本文档完整描述「AI 画像」（新版 v2）**远程 AI 分析工作流回传画像结果**时，需要返回的
> **全部数据结构**。远程 AI 完成一次画像分析后，调用下方回写接口提交标准化画像 JSON 包；
> 后台写入各画像表，并在画像详情接口中按客户数据返回 `profile_version: "v2"` 及
> `ai_insight` / `purchase_decisions` / `deal_battle` / `decision_psychology` 四个区块。
>
> 前端展示字段需求见前端仓库 `customer-profile-web/docs/AI画像接口数据结构.md`。
> 相关模型见 `app/models.py`，落库逻辑见 `app/services.py:apply_profile_update`。

---

## 1. 回写接口

| 项 | 说明 |
| --- | --- |
| 服务 | `cdp-m2m`（M2M API） |
| 默认端口 | `8084` |
| 方法 | `POST` |
| 路径 | `/api/v1/remote/callback/update-profile` |
| 鉴权 | 请求头 `X-API-Key: <M2M_API_KEY>` |
| Content-Type | `application/json` |

**请求体（顶层）**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `phone` | string | 是 | 客户手机号 |
| `batch_id` | string | 是 | 触发本次分析的批次号（与触发时一致） |
| `analysis_result` | object | 是 | 标准化画像 JSON 包，结构见第 2 节 |

```jsonc
{
  "phone": "18682284385",
  "batch_id": "<批次号>",
  "analysis_result": {
    "profile_summary": { /* 见 2.1 */ },
    "basic_notes_updates": [ /* 见 2.2 */ ],
    "ai_insight": { /* 见 2.3 */ },
    "purchase_decisions": [ /* 见 2.4 */ ],
    "deal_battle": { /* 见 2.5 */ },
    "decision_psychology": [ /* 见 2.6 */ ]
  }
}
```

> **版本判定**：只要 `analysis_result` 中**包含任一 v2 区块**（`ai_insight` / `purchase_decisions` /
> `deal_battle` / `decision_psychology`），该客户即视为「已有新版数据」，详情接口返回 v2 结构。
> 仅回传 `profile_summary` / `basic_notes_updates` 不改变版本判定。
>
> **兼容策略**：`analysis_result` 为开放 JSON，仅处理本文档列出的数据结构。区块缺失或为
> `null` 时保留该客户原有数据（灰度逐个字段上线）；显式回传空数组 / 空对象则清空该客户
> 对应数据。

---

## 2. `analysis_result` 数据结构

> 以下 2.1 ~ 2.6 为回传接口需要返回的**全部数据结构**。

### 2.1 `profile_summary`（画像主档）

写入 `customer_profile_main`。所有字段可选，未识别不覆盖现有值。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 否 | 客户姓名 |
| `gender` | string | 否 | 性别 |

```jsonc
"profile_summary": {
  "name": "刘",
  "gender": "男"
}
```

> 意向等级、客户类型等信息由 `ai_insight` 承载（见 2.3），不再通过 `profile_summary` 回传。
> 后台会把 `ai_insight.purchase_intent_level.value` / `ai_insight.customer_segment.value`
> 自动同步到 `customer_profile_main.deal_level` / `overall_tag`，以支撑画像列表按商机等级筛选。

### 2.2 `basic_notes_updates`（基础属性更新项）

写入 `customer_profile_basic_note`。数组，仅需包含本次有更新的属性。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `attribute_key` | string | 是 | 属性键名（见 `GET /api/v1/remote/config/note-attributes`） |
| `attribute_value` | string | 是 | 属性值（可空字符串） |
| `reasoning_detail` | object | 否 | 推断原因，见第 3 节 |

```jsonc
"basic_notes_updates": [
  {
    "attribute_key": "city",
    "attribute_value": "广州",
    "reasoning_detail": { "summary": "...", "steps": [], "evidence": [], "confidence": 0.8 }
  }
]
```

> 若某属性已被人工锁定（`is_locked=1`），后台不覆盖其值，仅写入 `suggested_value` /
> `suggested_reasoning`。

### 2.3 `ai_insight`（AI 成交洞察）

写入 `customer_profile_ai_insight`（**每客户一条宽表**，采用读改写）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `purchase_intent_level` | object | 否 | 购车意向等级项，见下 |
| `customer_segment` | object | 否 | 客户群标签项，见下 |

**标准项结构**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `value` | string | 是 | 展示值：购车意向等级为 `A` / `B` / `C` / `D`（不含「级」字）；客户群标签为人群名称（如 `高效务实中产`） |
| `reasoning_detail` | object | 否 | 推断原因，见第 3 节 |

```jsonc
"ai_insight": {
  "purchase_intent_level": {
    "value": "C",
    "reasoning_detail": { "summary": "...", "steps": [], "evidence": [], "confidence": 0.82 }
  },
  "customer_segment": {
    "value": "高效务实中产",
    "reasoning_detail": { "summary": "...", "steps": [], "evidence": [], "confidence": 0.78 }
  }
}
```

> `updated_at`、`ai_feedback` 不参与回传：前者由后台写库时生成，后者由反馈接口维护。

### 2.4 `purchase_decisions`（购车决策）

写入 `customer_profile_purchase_decision`（**每客户多条**，回写先删后建）。

数组，每个元素为一个分类卡片。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `category` | string | 是 | 分类名称，见下约定 |
| `items` | array | 是 | 该分类下的标签列表 |

**`items[]`**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 标签唯一标识；同一客户内不可重复 |
| `label` | string | 是 | 标签文案，如 `了解智驾体验` |
| `reasoning_detail` | object | 否 | 推断原因，见第 3 节 |
| `rank_order` | integer | 否 | 同分类内排序；缺省按数组顺序 |

约定分类（图标由前端按 `category` 固定映射，无需后台下发）：

| category |
| --- |
| 到店动因 |
| 当前用车 |
| 换车动因 |
| 对比车型 |
| 核心关注 |
| 决策阻力 |

> **唯一性约束**：`(customer_phone, item_id)` 唯一。请保证同一次回传中 `id` 全局唯一
> （建议前缀区分，如 `visit-*` / `change-*` / `compare-*` / `focus-*` / `barrier-*`）。

### 2.5 `deal_battle`（成交攻防）

写入 `customer_profile_deal_battle`（**每客户多条**，回写先删后建）。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `win_conditions` | array | 否 | 成交有利因素（`battle_type=win`） |
| `loss_conditions` | array | 否 | 战败风险因素（`battle_type=loss`） |

两个数组元素结构相同：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 唯一标识 |
| `label` | string | 是 | 条件文案 |
| `reasoning_detail` | object | 否 | 推断原因，见第 3 节 |
| `rank_order` | integer | 否 | 排序；缺省按数组顺序 |

> **唯一性约束**：`(customer_phone, item_id)` 唯一；`win_conditions` 与 `loss_conditions`
> 的 `id` 也需全局唯一（建议 `win-*` / `loss-*` 前缀）。

### 2.6 `decision_psychology`（决策心理）

写入 `customer_profile_decision_psychology`（**每客户 0~4 条**，回写先删后建）。建议返回 4 项。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `key` | string | 是 | 维度标识：`decision_style` / `current_attitude` / `communication_preference` / `emotion_state` |
| `label` | string | 是 | 维度名称，如 `决策风格` |
| `value` | string | 是 | 维度结论，如 `理性比较型` |
| `reasoning_detail` | object | 否 | 推断原因，见第 3 节 |
| `rank_order` | integer | 否 | 排序；缺省按数组顺序 |

> **唯一性约束**：`(customer_phone, psychology_key)` 唯一，即同一 `key` 每客户至多一条。

---

## 3. 通用推断结构 `reasoning_detail`

新版「AI 画像」中每个可点击的值，点击后都会弹出只读的「AI 推断原因」弹窗。因此除展示值外，
每个业务项都可携带 `reasoning_detail`：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `summary` | string | 是 | AI 分析总结 |
| `steps` | string[] | 是 | 逻辑推导链（逐条展示） |
| `evidence` | string[] | 否 | 信源原文匹配；为空则不展示该区块 |
| `confidence` | number | 否 | 置信度，取值 `0 ~ 1`，用于底部置信度条 |

```jsonc
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
```

> 后台原样存储该 JSON，不做结构裁剪。`ai_feedback` 不参与回传。

---

## 4. 落库映射汇总

| `analysis_result` 字段 | 目标表 | 写库方式 |
| --- | --- | --- |
| `profile_summary` | `customer_profile_main` | 字段覆盖 |
| `basic_notes_updates` | `customer_profile_basic_note` | 逐项更新 / 插入（锁定项写建议值） |
| `ai_insight` | `customer_profile_ai_insight` | 读改写（每客户一条），反馈重置 |
| `purchase_decisions` | `customer_profile_purchase_decision` | 先删后建 |
| `deal_battle.win_conditions` | `customer_profile_deal_battle` | 先删后建；`battle_type=win` |
| `deal_battle.loss_conditions` | `customer_profile_deal_battle` | 先删后建；`battle_type=loss` |
| `decision_psychology` | `customer_profile_decision_psychology` | 先删后建 |

字段级映射：

| 回传字段 | 数据库列 |
| --- | --- |
| `profile_summary.name` / `gender` | `customer_profile_main` 同名列 |
| `basic_notes_updates[].attribute_key` | `attribute_key` |
| `basic_notes_updates[].attribute_value` | `attribute_value` |
| `basic_notes_updates[].reasoning_detail` | `reasoning_detail` |
| `ai_insight.purchase_intent_level.value` | `customer_profile_ai_insight.purchase_intent_level`；同时派生同步至 `customer_profile_main.deal_level` |
| `ai_insight.purchase_intent_level.reasoning_detail` | `purchase_intent_level_reasoning` |
| `ai_insight.customer_segment.value` | `customer_profile_ai_insight.customer_segment`；同时派生同步至 `customer_profile_main.overall_tag` |
| `ai_insight.customer_segment.reasoning_detail` | `customer_segment_reasoning` |
| `purchase_decisions[].category` | `category` |
| `purchase_decisions[].items[].id` | `item_id` |
| `purchase_decisions[].items[].label` | `label` |
| `purchase_decisions[].items[].reasoning_detail` | `reasoning_detail` |
| `deal_battle.*[].id` | `item_id`（`battle_type` 区分 win/loss） |
| `deal_battle.*[].label` | `label` |
| `deal_battle.*[].reasoning_detail` | `reasoning_detail` |
| `decision_psychology[].key` | `psychology_key` |
| `decision_psychology[].label` | `label` |
| `decision_psychology[].value` | `value` |
| `decision_psychology[].reasoning_detail` | `reasoning_detail` |

> 每次成功回传都会生成一条新版历史快照，写入 `customer_profile_analysis_snapshot` 的
> `profile_version` / `ai_insight_snapshot` / `purchase_decisions_snapshot` /
> `deal_battle_snapshot` / `decision_psychology_snapshot` 列，供画像历史演变查询
> （`GET /api/v1/customers/{phone}/profile/history`）。

---

## 5. 完整回传示例

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
      }
    ]
  }
}
```

---

## 6. 校验与错误

| HTTP 状态 | 场景 | 响应体 |
| --- | --- | --- |
| `200` | 回写成功 | `{"msg": "Profile updated successfully via remote webhook."}` |
| `403` | `X-API-Key` 缺失或错误 | `{"detail": "Invalid M2M API Key"}` |
| `500` | 回写异常（含唯一约束冲突） | `{"detail": "<错误详情>"}` |

> 幂等：同一 `(phone, batch_id)` 重复回调会被识别为重复投递并按成功处理，不会重复写入
> 快照或重复累计生成数量。请确保 `id` / `key` 在单客户内唯一，避免唯一约束冲突。

---

## 7. 相关接口

| Method | Path | 说明 |
| --- | --- | --- |
| GET | `/api/v1/remote/config/note-attributes` | 获取 `basic_notes_updates.attribute_key` 可用属性字典 |
| GET | `/api/v1/remote/data/batch/{batch_id}` | 获取本次触发的批次资料 |
| GET | `/api/v1/remote/data/profile/{phone}` | 获取客户当前画像，供远程 AI 参考 |

> 新版画像详情为**只读展示**：详情接口不返回 `icon` 与 `ai_feedback`，前端不提交反馈。