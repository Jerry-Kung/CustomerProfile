# AI 画像 v2 回传改造核查清单

> 前端（业务架构师侧）已指定新的回传契约：**6 个分析区块 + 一份 45 字段的 Notes 清单**。
> 本文逐项比对新契约与当前实现的差异，并列出项目内部需要跟着动的地方。
> **本文只做核查，不含设计与实现方案。**
>
> 依据：`docs/business_opt01_v3/AI画像远程回传接口数据结构说明.md`、`docs/business_opt01_v3/Notes_new.txt`
> （线上 `GET /api/v1/remote/config/note-attributes` 实测返回）。
> 当前实现：`notes_contract.py` / `projection.py` / `customer_profile_l3.py` /
> `customer_profile_production.py`。

## 0. 结论摘要

| 项 | 数 |
|---|---|
| 新 Notes 字段总数 | 45（7 分类） |
| 新增字段 | 15 |
| 下线字段 | 17 |
| 同名但定义已变 | 9 |
| 分类数变化 | 6 → 7（新增「地域与生活圈」） |
| `analysis_result` 键数 | 10 → 6 |

---

## 1. 回传协议层的差异

新文档定义的请求体与仓库里**实际发出去的请求体不是同一种形状**。这一条要先对齐，
否则后面所有字段改动都会落在错误的信封里。

### 1.1 请求体形状

新文档（顶层三键，`analysis_result` 是**对象**）：

```jsonc
POST /api/v1/remote/callback/update-profile
X-API-Key: <M2M_API_KEY>

{
  "phone": "...",
  "batch_id": "...",
  "analysis_result": {
    "profile_summary":     { },
    "basic_notes_updates": [ ],
    "ai_insight":          { },
    "purchase_decisions":  [ ],
    "deal_battle":         { },
    "decision_psychology": [ ]
  }
}
```

当前实现（外层多包一层 `data[]`，内层是**字符串**而非对象）：

```jsonc
{
  "data": [
    { "id": "key-value-149", "key": "", "type": "text",
      "value": "<整个 analysis_result 的 JSON 字符串>" }
  ],
  "type": "json"
}
```

### 1.2 逐项对照

| 项 | 当前实现 | 新文档 | 判定 |
|---|---|---|---|
| 端点 | `/api/v1/remote/callback/update-profile` | 同左 | 一致 |
| 鉴权 | 请求头 `X-API-Key`（`PROFILE_API_KEY` 注入） | 请求头 `X-API-Key`（M2M API Key） | 一致 |
| 服务/端口 | `118.145.238.50:8084` | `cdp-m2m`，8084 | 一致 |
| 失败判定 | HTTP ≥400 即失败 | 200 成功 / 403 鉴权失败 / 500 回写异常 | 一致 |
| 请求体形状 | `{"data":[…],"type":"json"}` 信封 | 顶层三键 | **要变** |
| `analysis_result` 类型 | 字符串（塞在信封 `value` 里） | 对象 | **要变** |
| `analysis_result` 键数 | 10（9 板块 + `basic_notes_updates`） | 6 | **要变** |
| 版本判定 | 无此概念 | 含任一 v2 区块即视为 v2；只回传前两键不改判定 | 新增 |
| 幂等 | POST 不自动重试；`(phone, batch_id)` 未落地 | 同 `(phone, batch_id)` 重复投递按成功处理 | 新增 |

> **已定口径（2026-10-15）**：一切以文档为准，本轮按**顶层三键**实现。
> 线上现役回写节点用的 `data[]` 信封与之不符，回写节点的 `@body_template`
> 需重写。真实链路若收信封，属契约文档待更正，登记见
> [`遗留问题清单.md`](遗留问题清单.md) L-003。
> **本轮先跑单号真实回写验证，不要直接全量跑批。**

---

## 2. Notes 字段清单：45 字段 / 7 分类

新清单没有的键，后台一律不认。下表按「需动手的程度」排序。

### 2.1 新增 15 个字段

| attribute_key | 名称 | 分类 | 类型 |
|---|---|---|---|
| `education` | 学历 | 基础身份 | enum 单选 |
| `industry` | 行业 | 基础身份 | text |
| `consumption_level` | 消费等级 | 基础身份 | enum 1–6 档 |
| `life_key_nodes` | 人生关键节点 | 基础身份 | enum 多选 |
| `customer_type` | 客户类型 | 基础身份 | enum 单选 |
| `current_vehicle_info` | 现车信息 | 基础身份 | text |
| `trade_in_model` | 置换车型 | 购车需求 | text |
| `credit_status` | 征信情况 | 预算与金融 | text |
| `internet_preference` | 上网偏好 | 决策与节奏 | enum 多选 |
| `optional_config` | 配置 | 个人偏好与备注 | text |
| `knowledge_of_erev` | 了解混动 | 个人偏好与备注 | enum 单选 |
| `knowledge_of_adas` | 了解智驾 | 个人偏好与备注 | enum 单选 |
| `contact_preference` | 沟通方式 | 个人偏好与备注 | enum 多选 |
| `residence_address` | 居住地址 | 地域与生活圈（**新分类**） | text |
| `license_plate_address` | 上牌地址 | 地域与生活圈 | text |

### 2.2 下线 17 个字段

这 17 个键在新清单里完全不存在，若仍照旧回传会被当作未知键处理。

| attribute_key | 名称 | 新契约里的替代 |
|---|---|---|
| `old_car_model` | 旧车车型 | 改用 `current_vehicle_info` + `trade_in_model` |
| `owned_brand_model` | 拥车车型（本品） | 改用 `current_vehicle_info` |
| `owned_car_age` | 拥车车龄 | 无直接替代 |
| `company` | 企业 | 无直接替代 |
| `company_type` | 企业属性 | 无直接替代 |
| `seating_requirement` | 乘坐需求 | 无直接替代 |
| `primary_driver` | 用车人 | 无直接替代 |
| `usage_frequency` | 用车频次 | 无直接替代 |
| `driving_preference` | 驾驶感受 | 可能并入 `core_concerns` |
| `special_requirements` | 特殊要求 | 可能并入 `optional_config` |
| `intended_brand` | 意向品牌 | 可能并入 `intended_model` |
| `personality_and_preferences` | 性格偏好 | 迁往画像卡 `decision_psychology` |
| `communication_rhythm` | 沟通节奏 | 迁往 `decision_psychology` |
| `emotion` | 沟通风格 | 迁往 `decision_psychology` |
| `resistances` | 购车抗性/顾虑 | 迁往 `deal_battle.loss_conditions` |
| `douyin_id` | 抖音号 | 无替代 |
| `xiaohongshu_id` | 小红书号 | 无替代 |

### 2.3 同名但定义已变 9 个字段

**这一类最危险**：键名对得上，不会被「未知键」拦下，会把旧格式的值写进新语义的字段。

| attribute_key | 变化 | 影响 |
|---|---|---|
| `purchase_type` | 枚举整体替换：`first`/`additional`/`replacement`/`company`/`unknown` → `company`/`personal` | 首购/增购/换购的区分**没有位置**了 |
| `family_structure` | 枚举键更换：`married`/`unmarried`/`has_children`(+人数) → `has_children`/`married_no_child`/`unmarried_no_child` | 仅 `has_children` 一个键重合，其余旧值成非法枚举 |
| `purchase_purpose` | 显示名「购车用途」→「用车场景」；选项 5 → 3，去掉 `commute` 通勤与 `transport` 代步 | 通勤/代步失去落点（实际最高频场景） |
| `follow_up_frequency` | 类型变更：text「跟进频率」→ enum 多选「跟进时段」`morning`/`afternoon`/`evening`/`weekday`/`weekend` | 旧值「每 3 天联系一次」与新字段形态完全不同 |
| `planned_purchase_time` | `within_6_months`「6 个月内」→ `over_3_months`「3 个月以上」 | 四档变四档，末档含义与粒度变了 |
| `driver_license_status` | 去掉 `novice` 新手项 | 注入只产 `yes`，影响有限；L1 提示词要同步 |
| `electronic_ecosystem` | 新增 `other` 其他、`unknown` 未知 | 低风险，多两个合法键 |
| `knowledge_of_mengshi` | 显示名「了解本品牌」→「了解猛士」 | 仅措辞；需与新增的「了解混动」「了解智驾」区分口径 |
| `trade_in_requirement` | 分类由「预算与金融」移到「购车需求」 | 键与枚举不变，仅归属变化 |

### 2.4 分类归属的变化

| 分类 | 新 | 现 | 说明 |
|---|---|---|---|
| 基础身份 | 15 | 16 | 下线 7、新增 6 |
| 购车需求 | 6 | 11 | 下线 7、新增 1、自预算类迁入 1 |
| 预算与金融 | 4 | 4 | 迁出 1、新增 1 |
| 决策与节奏 | 5 | 4 | 新增 1 |
| 个人偏好与备注 | 9 | 8 | 下线 3、新增 4 |
| 竞品对比 | 4 | 4 | 不变 |
| 地域与生活圈（新） | 2 | — | 全新 |
| **合计** | **45** | **47** | 下线 17、新增 15 |

---

## 3. 画像卡的四处结构性变化

当前 9 个板块要被重组成「4 个区块 + 2 个基础键」。

### 3.1 结构对照

当前 9 个板块（`customer_profile_l3.py:SECTION_KEYS`）：

```
profile_summary            对象
inferred_tags              数组
usage_scenarios            数组
communication_strategies   数组
customer_overview          对象
purchase_motivations       数组
product_preferences        数组
resistances                数组
emotion_state              对象
```

新契约 4 区块 + 2 基础键：

```
profile_summary        { name, gender }
basic_notes_updates    [ 45 字段更新项 ]

ai_insight             { purchase_intent_level, customer_segment }
purchase_decisions     [ 6 类 × items[] ]
deal_battle            { win_conditions, loss_conditions }
decision_psychology    [ 4 个固定 key ]
```

### 3.2 变化逐条

1. **`profile_summary` 同名不同义。** 现在是九个 section 之一（内含 `age` /
   `deal_level` / `overall_tag` / `personality_summary` 等）；新契约里它只是客户主档，
   **只剩 `name` 与 `gender`**。

2. **意向等级与客户类型搬家并改形。** 现在散在 `profile_summary.deal_level` 与
   `customer_overview`（`business_opp_level` / `customer_type`）；新契约收进 `ai_insight`
   的两个标准项 `{value, reasoning_detail}`，并由后台派生同步回
   `customer_profile_main.deal_level` / `overall_tag`。
   **注意**：`customer_segment` 收「客户群标签」（如「高效务实中产」），与 Notes 新增的
   `customer_type`（价格价值型/科技尝鲜型…）是两回事。

3. **新增三个条目化结构，每个元素要全局唯一 id。** 唯一约束分别是
   `(phone, item_id)` 与 `(phone, psychology_key)`。建议加前缀
   `visit-*` / `change-*` / `compare-*` / `focus-*` / `barrier-*` / `win-*` / `loss-*`。

4. **`purchase_decisions` 固定 6 个分类**：到店动因、当前用车、换车动因、对比车型、
   核心关注、决策阻力。

5. **`decision_psychology` 固定 4 个 key**：`decision_style` 决策风格 /
   `current_attitude` 当前态度 / `communication_preference` 沟通偏好 /
   `emotion_state` 情绪状态。建议返回全 4 项。

6. **`reasoning_detail` 结构不变**（summary / steps / evidence / confidence），
   现有三键结构可直接复用；`evidence` 为空时不展示该区块。

7. **写库方式**：`purchase_decisions` / `deal_battle` / `decision_psychology`
   为**先删后建**（每次整体替换）；区块缺失或为 `null` 保留原值，显式传空数组才清空。
   灰度逐字段上线靠这个语义。

---

## 4. 项目内部要改的地方

按「不改就一定出错」的强度排序。

### 4.1 契约快照的生成链断了（前置）

契约重建是本轮第一步，但生成器 `scripts/snapshot_note_contract.py` 已在 v3.3 清理中
被删（该文件名出现在 `docs/history/清理报告-v3.3基线.md` 的「已删除的临时脚本」清单里），
仓库内没有可复现的生成途径。需先重建生成器，否则改契约只能手抄，而手抄必然与线上漂移。

> `notes_contract.py` 抬头声称由该脚本生成，但 `scripts/` 现存 6 个脚本均不含它。

### 4.2 投影层的规则依赖 5 个将下线的字段

| 位置 | 依赖 | 后果 |
|---|---|---|
| `projection.py:830` | `old_car_model` → 换购判定 | 规则静默失效 |
| `projection.py:862` | `company` / `company_type` → 注销企业不填在职 | 规则静默失效 |
| `projection.py:196-210` | 9 个文本字段的聚合语义集合 | `special_requirements` / `intended_brand` 下线后集合失真 |
| `pipeline.py:1291-1294` | 「有旧车」旁证：`old_car_model` / `owned_brand_model` / `owned_car_age` | 全部下线 |
| `pipeline.py:1442-1449` | 同上，投影旁证 | 同上 |
| `pipeline.py:1015-1019` | `FIELD_SOURCE_NOTES` 里的取证方向说明 | 键必须真存在于契约，否则用例报错 |
| `projection.py:144` | `ATTITUDE_KEYS` 含 `emotion` | 需换成新契约的态度类字段 |

### 4.3 `purchase_type` 枚举换血会打断换购判定

现有规则把 `purchase_type` 写成 `replacement`，并把 `replacement` / `company`
作为保留值（`projection.py:839`）。新契约里 `replacement` 不存在，这条规则会写进
非法枚举。同处反向用例还断言 `additional` 原样透传（`tests/test_projection.py:287`），
换购相关用例见同文件 311–337 行。

### 4.4 L3 装配层要从 9 板块改成 6 键

`customer_profile_l3.py` 需要改三处：

- `SECTION_KEYS`（81-91 行）：换成新的键集合；
- `resistances` 补挂（371-401 行）：删掉（该字段已下线，内容改走 `deal_battle`）；
- `LIST_SECTIONS`（421-426 行）：数组/对象分野要按新结构重划。

此外还需**新增一层**「facts → 条目化卡片」的投影：`ai_insight` / `purchase_decisions` /
`deal_battle` / `decision_psychology` 四个区块当前完全没有产物结构。

### 4.5 六个新字段是当前完全没有的维度

学历、行业、消费等级、人生关键节点、居住地址、上牌地址：现有取证层既不产出、
提示词里也没有口径。需要在 L1 提示词与字段清单里新增。
**提示词正文改动属业务优化，按项目约定须单独提确认。**

> **已定口径（2026-10-15）**：六个新字段**本轮全部上**，一次做完所有 API 更新，
> 优化留待后续。因此不采用灰度，L1 提示词与 `field_inference_guide()` 的
> 取证口径需一次性补齐。

### 4.6 回写节点目前只在基线臂

全仓只有 `customer_profile_production` 带 `@writeback` 节点
（`customer_profile_production.py:1120-1140`）。实验臂
（`customer_profile_pipeline`）止于 L3 子流程，产出 `result1` 即结束。

> **已定口径（2026-10-15）**：v2 回传**接在实验臂上**。该臂的实验已完成并通过核验，
> 后续对外口径由「实验臂」改为「**业务测试臂**」。
> 因此需在 `customer_profile_pipeline` 新增回写节点（保留 `WRITEBACK_ENABLED`
> 开关语义），基线臂的 `customer_profile_production` 维持原状不再演进。
>
> **连带影响**：改显示名时会碰到「节点标题与模板正文硬引用」与
> `definition_hash` 两处——模板资源名是 `<slug>__<node_id>`，
> `slug` 是模块与模板共用的稳定标识，`node_id` 与模板正文都不得改动。
> 改名限**显示名**层面，`workflow_id` 保持不变，否则会打断
> `scripts/run_baseline.py --workflow-id` 与既有批次的可比性判据。

### 4.7 需要一层显式的「内部 facts → 回传体」裁剪

现在 `_to_notes_updates`（`projection.py:1123-1163`）直接遍历契约键产出回传项，
事实账本与回传体一一对应。保留编外字段后二者必须解耦：
内部 facts 照旧全量保留，回传前由白名单映射成新契约的 6 区块形态。
这层同时也是新增四区块的生成处，两件事可合并成一层做。

---

## 5. 编外字段：建议保留什么

下线字段里有相当一批是管线已稳定产出、且对销售策略有直接作用的维度。
内部保留几乎不需要改代码（facts 本就不受契约约束），只需在回传前裁掉。

| 内部保留 | 理由 | 建议 |
|---|---|---|
| `old_car_model` / `owned_brand_model` / `owned_car_age` | 换购推断的旁证链，且「客户在开什么车」是最基础的销售信息。新契约的 `current_vehicle_info` 能装现车，但承载不了它与购买类型的联动规则 | 保留 |
| `company` / `company_type` | 关联企业信息是唯一能交叉验证职业与收入的渠道；「已注销企业不得当作在职」这条规则靠它 | 保留 |
| `seating_requirement` / `primary_driver` | 家庭用车决策的核心（谁开、坐几人），直接影响车型推荐。新清单无替代项 | 保留 |
| `intended_brand` / `usage_frequency` / `driving_preference` / `special_requirements` | 意向品牌（区别于意向车型）、用车频次、驾驶感受、个性化要求，可读性高 | 保留 |
| `emotion` / `communication_rhythm` / `personality_and_preferences` | 不是编外，而是**迁到画像卡**：对应 `decision_psychology` 的沟通偏好、决策风格、情绪状态 | 迁移 |
| `resistances` | 同样换位置：抗性内容对应 `deal_battle.loss_conditions` | 迁移 |
| `purchase_type` 的首购/增购/换购语义 | 新枚举只剩个户/公户，把购车性质整块删掉了。这是销售判断逼单策略的第一顺位信息 | 内部保留，回传时映射 |
| `douyin_id` / `xiaohongshu_id` | 账号标识类，分析价值低；平台偏好已由 `frequent_social_media` 覆盖 | 可舍弃 |

> **前提**：编外字段只对「回传前的裁剪」有意义。内部 facts 全量保留不影响接口；
> 额外要做的是让回传体不再等于契约字段的遍历结果，而是经过一个显式映射层。

---

## 6. 决策记录

六项拍板事项于 2026-10-15 答复如下。

| # | 事项 | 口径 | 影响 |
|---|---|---|---|
| 1 | 请求体形状 | **一切以文档为准**，按顶层三键 | 回写节点 `@body_template` 重写；差异登记为 L-003 |
| 2 | 回传由哪条臂承担 | **接在实验臂上**，实验已完成并通过核验；对外口径改为「**业务测试臂**」 | 管线新增回写节点；改名限显示名层 |
| 3 | 6 个分类由谁归类 | **未答复**，按本文建议由代码从 facts 投影 | 见 `遗留问题清单.md` L-002 |
| 4 | `customer_segment` 取值口径 | **先让模型自由发挥**，问题登记待业务方推动 | 见 `遗留问题清单.md` L-001 |
| 5 | 编外字段的最终范围 | **以本文第 5 节建议为准** | 第 5 节即内部保留清单 |
| 6 | 六个新字段是否本轮上 | **全部本轮上**，一次做完 API 更新 | 提示词与字段清单一次性补齐 |

### 6.1 关于第 3 项：建议由代码从 facts 投影

建议**不由表达层 LLM 自由归类**，理由四条：

1. **唯一 id 必须由代码生成。** 契约要求 `(phone, item_id)` 全局唯一，且回写是
   **先删后建**。id 重复或跨区块撞号会导致整次回传 500 失败——让模型生成 id
   等于把「回传能否成功」交给模型抖动。
2. **归类可判定，不该交给模型。** 6 个分类与现有 facts 有明确对应
   （到店动因 ~ `viewing_progress` / `purchase_purpose`；对比车型 ~
   `competing_brand` / `competing_model` / `test_driven_models`；决策阻力 ~
   `brand_concerns` 与抗性；等）。按项目既有口径「能写成代码的约束不交给模型」，
   这层应是 **0 token 的纯代码投影**。
3. **有现成的投影层。** L2 的 `projection.py` 已承担「facts → 结构化产物」，
   含 `reasoning_detail` 的三键结构。在此加 `build_purchase_decisions()`
   与 Notes 产出**同源**，满足「卡片与 Notes 不打架」的设计口径。
4. **表达层职责边界不变。** L3 现在的口径是「结构由代码定、措辞由模型给」。
   改由表达层直接产出分类结构，等于让它同时决定结构与措辞，
   正是 production 图 12 节点各自为政的老路。

残余不确定只在**分类边界**（如「核心关注」与「到店动因」重叠），
那是规则参数问题，改规则远比改架构便宜。

若最终仍决定由 LLM 归类，**必须**加一层代码校验：分类合法性、id 唯一性、
`items` 必须能在 facts 里找到出处。否则契约的唯一约束会成为回传失败的常见原因。

---

## 附：本次核对中的一处计数修正

分类字段数在初版核对中算错，已按 `docs/business_opt01_v3/Notes_new.txt` 逐项重数并更正为
第 2.4 节的数字（基础身份 15、购车需求 6、预算与金融 4、决策与节奏 5、
个人偏好与备注 9、竞品对比 4、地域与生活圈 2，合计 45）。
