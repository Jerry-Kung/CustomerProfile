# customer_overview 透传 —— 堡垒机验证包

> 背景：`customer_profile` 的回传体构造（`callback_build.build_analysis_result`）历史上只投影
> 6 个键，把表达层的 `customer_overview` 整节丢弃，导致画像概览表除 `business_opp_level`
> 之外全部为空（现象：接口返回 `"closing_probability": null`）。
> 本次修复：新增 `build_customer_overview()` 把该节按契约透传。
>
> 本文档给出**不依赖模型、不依赖权限**的手工验证方法：直接向 m2m 回调接口 POST 一份模拟
> 回传体，再看画像接口是否读到了概览。

---

## 0. 一分钟说明验证逻辑

```
POST 模拟回传体  →  ① 概览写入 customer_profile_overview
                 →  ② GET 画像接口读到 customer_overview
```

只要这两步通，就说明**回传链路已经通了**；模型那头能否产出内容，是另一件事（靠重跑画像）。

---

## 1. 准备：取 Key 与地址

```bash
# 进 API 目录（UAT 单节点 Compose）
cd /home/profile/customer-profile-api

# 取 m2m Key
M2M_KEY=$(docker compose exec -T cdp-m2m printenv M2M_API_KEY | tr -d '\r\n')
echo "key 长度: ${#M2M_KEY}"

# m2m 地址（容器内 8084，宿主机映射同为 8084）
BASE=http://127.0.0.1:8084
```

---

## 2. 用模拟数据打回调

模拟体在 `docs/verify/mock_callback_overview.json`（本目录），**用一个不存在的测试手机号**
`13600000001`，不会污染真实客户。

```bash
# 2.1 确认回调接口可达
curl -s -o /dev/null -w "openapi=%{http_code}\n" -H "X-API-Key: ${M2M_KEY}" ${BASE}/openapi.json

# 2.2 打入模拟回传体
curl -sS -X POST "${BASE}/api/v1/remote/callback/update-profile" \
  -H "X-API-Key: ${M2M_KEY}" \
  -H "Content-Type: application/json" \
  --data-binary @docs/verify/mock_callback_overview.json \
  -w "\nhttp=%{http_code}\n"
```

期望输出：

```
{"msg": "Profile updated successfully via remote webhook."}
http=200
```

> 若首次报 `该客户画像不存在` 或类似，需要先建档。可以走 m2m 建档或在 ui 后台建一条测试客户，
> 手机号保持 `13600000001`。

---

## 3. 验证画像接口读到了概览

```bash
curl -sS -H "X-API-Key: ${M2M_KEY}" \
  "${BASE}/api/v1/remote/data/profile/13600000001/new" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
ov = d.get('customer_overview')
print('profile_version   :', d.get('profile_version'))
print('main_info 有概率  :', 'deal_probability' in (d.get('main_info') or {}))
print('customer_overview :', 'None（没读到）' if ov is None else 'OK')
if ov:
    print('  客户类型  :', ov.get('customer_type'))
    print('  当前阶段  :', ov.get('current_stage'))
    print('  成交概率  :', ov.get('closing_probability'))
    print('  概率依据  :', ((ov.get('closing_probability_reasoning') or {}).get('summary') or '')[:60])
    print('  商机等级  :', ov.get('business_opp_level'))
    print('  核心问题  :', ov.get('core_issue'))
    print('  突破点    :', ov.get('breakthrough_point'))
"
```

**期望输出**（与模拟体逐项对应）：

```
profile_version   : v2
main_info 有概率  : False
customer_overview : OK
  客户类型  : 务实顾家六座刚需客
  当前阶段  : 价格核算阶段
  成交概率  : 72%
  概率依据  : 需求与车型已收口、支付条件具备，仅试驾未兑现与交付周期两处不确定，综合判断七成左右。
  商机等级  : A
  核心问题  : 试驾未完成，交付时间不确定
  突破点    : 锁定试驾与交付节点
```

---

## 4. 反向验证（确认旧行为确实会丢）

想确认「以前为什么是 null」，可以看改动前后的差异：

```bash
# 在仓库里查看该函数的键清单（改后应含 customer_overview）
grep -n 'analysis_result\["' src/customer_profile/callback_build.py | head -12
```

改动前只有：`profile_summary` / `basic_notes_updates` / `ai_insight` /
`purchase_decisions` / `deal_battle` / `decision_psychology` / `follow_up_history`。
改动后多一行 `analysis_result["customer_overview"] = overview`。

对应地，生产上那条 `docker compose logs cdp-api-m2m | grep -c customer_overview` 为 **0**，
正是「回传体里从来没有这个键」的直接证据，而不是模型没算。

---

## 5. 本地（不连库）复现这份回传体

如果只想看回传体长什么样，不需要任何服务：

```bash
cd <CustomerProfile 仓库根>
PYTHONPATH=src python -c "
import json
from customer_profile import callback_build as cb
facts = {'deal_level': 'A'}
express = {'analysis_result': {'customer_overview': {
    'closing_probability': {'value': '72%', 'reasoning_detail': {'summary': '综合判断'}},
    'customer_type': {'value': '务实型', 'reasoning_detail': {'summary': '增购家用'}},
}}}
out = json.loads(cb.build_analysis_result('13600000001', 'b1', json.dumps(facts), '[]', json.dumps(express)))
print(json.dumps(out['analysis_result']['customer_overview'], ensure_ascii=False, indent=2))
"
```

---

## 6. 已知边界

| 项 | 说明 |
| --- | --- |
| `closing_probability` 格式 | **强制 `NN%`**（整数 + 百分号）。`82` / `0.82` / `约八成` 一律不下发，前端显示为空。这是刻意的：无百分号的数字无法与金额、分数区分 |
| 单个字段为空 | 该项**整条不下发**，而不是下发 `null`（契约里 `null` 会被当成显式清空） |
| 全部为空 | `customer_overview` 整个键不出现，库里已有值保持不变 |
| 等级来源 | 一律取 `facts.deal_level`（定级环节结论），表达层写的等级被忽略，保证与 `ai_insight` 同源 |
| `待识别` 等级 | 不下发，避免覆盖库里已有等级 |
| 存量客户 | 不会自动补上，需要重跑画像 |

---

## 7. 测完清理

```bash
# 删掉测试客户（若已建档）
# 走 ui 后台或按你们的清理流程处理 13600000001

# 清掉临时拉取的 json
rm -f /tmp/profile_*.json
```
