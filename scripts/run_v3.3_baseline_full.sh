#!/usr/bin/env bash
# v3.3 全量39用例基线跑批
#
# 用途：使用最新代码（含可靠性改进）重新跑39个手机号的完整基线
# 输出：结果保存到 docs/business_opt01_v3/baseline-v3.3-final/
#
# 可靠性改进：
# - 自动重跑失败案2轮
# - 记录 anomaly 和 finish_reason
# - 复用转写缓存（11段录音不重复识别）

set -e

REPO_ROOT="D:/KLH/Projects/dfms/CustomerProfile"
cd "$REPO_ROOT"

# 时间戳
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BATCH_ID="baseline-v3.3-final-${TIMESTAMP}"

# 输入：复用之前基线的手机号清单
PHONE_LIST="experiments/batches/baseline-39-dsv41-noThink/phones.txt"

# 输出：临时跑在 experiments/batches，跑完后复制到 docs
TEMP_BATCH_DIR="experiments/batches/${BATCH_ID}"
FINAL_DEST="docs/business_opt01_v3/baseline-v3.3-final"

# 转写缓存：复用现有缓存，避免重复识别
CACHE_DIR="experiments/transcript_cache"

echo "=========================================="
echo "v3.3 全量39用例基线跑批"
echo "=========================================="
echo "手机号清单: ${PHONE_LIST}"
echo "批次ID:     ${BATCH_ID}"
echo "临时输出:   ${TEMP_BATCH_DIR}"
echo "最终保存:   ${FINAL_DEST}"
echo "转写缓存:   ${CACHE_DIR}"
echo ""
echo "工作流:     customer_profile_pipeline (v3.3 优化版)"
echo "可靠性:     --retry-failed 2 (最多自动重跑2轮)"
echo ""
read -p "确认开始跑批? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "已取消"
    exit 1
fi

# 执行跑批
echo ""
echo "开始跑批..."
echo "预计耗时: 30-60分钟（取决于录音案例数量）"
echo ""

python scripts/run_baseline.py \
    --workflow-id customer_profile_pipeline \
    --phones "${PHONE_LIST}" \
    --batch-id "${BATCH_ID}" \
    --out-dir experiments \
    --transcript-cache-dir "${CACHE_DIR}" \
    --retry-failed 2 \
    --concurrency 4

EXIT_CODE=$?

echo ""
echo "=========================================="
if [ $EXIT_CODE -eq 0 ]; then
    echo "✓ 跑批全部成功"
else
    echo "⚠ 部分用例失败 (exit code: ${EXIT_CODE})"
    echo "  查看详情: ${TEMP_BATCH_DIR}/summary.md"
fi
echo "=========================================="

# 持久化保存
echo ""
echo "持久化保存结果..."

# 清理旧的final目录（如果存在）
if [ -d "${FINAL_DEST}" ]; then
    BACKUP="${FINAL_DEST}.backup-${TIMESTAMP}"
    echo "  备份旧结果到: ${BACKUP}"
    mv "${FINAL_DEST}" "${BACKUP}"
fi

# 复制到最终位置
mkdir -p "${FINAL_DEST}"
cp -r "${TEMP_BATCH_DIR}"/* "${FINAL_DEST}/"

# 生成数据说明书
cat > "${FINAL_DEST}/README.md" <<'EOF'
# baseline-v3.3-final 数据说明

## 概述

本目录保存 v3.3 全量39用例的基线跑批结果，用于后续制作结果对比页面。

- **批次ID**: `baseline-v3.3-final-*`
- **工作流**: `customer_profile_pipeline` (v3.3 优化版)
- **用例数**: 39 个手机号
- **代码版本**: 见 `manifest.json` 的 `code_commit`
- **跑批时间**: 见 `manifest.json` 的 `created_at` / `finished_at`

## 目录结构

```
baseline-v3.3-final/
├── README.md                 # 本说明文件
├── manifest.json             # 批次元数据（配置、时间、统计）
├── summary.json              # 机读汇总（成功率、结果列表）
├── summary.md                # 人读汇总（表格、补跑命令）
├── phones.txt                # 手机号清单（39个）
├── trace.db                  # SQLite 留痕数据库（完整执行记录）
├── logs/
│   └── run.log               # 跑批日志
└── runs/
    ├── 13415100087/          # 每个号码一个子目录
    │   ├── result.json       # 最终画像结果（含状态、错误）
    │   ├── tree.json         # 执行树（节点、输入输出、耗时）
    │   ├── attempts.json     # 所有出网尝试（含重试、异常）
    │   └── nodes/            # 每个节点的详细数据
    │       ├── l1_drive_test_l1a.json
    │       ├── l1_drive_test_l1b.json
    │       └── ...
    ├── 13430349943/
    └── ...
```

## 核心文件说明

### 1. manifest.json

批次元数据，包含：
- `workflow_id`: 使用的工作流ID
- `code_commit`: 代码提交号（可追溯）
- `phones_count`: 手机号总数
- `phones_completed`: 成功完成数
- `config`: 运行时配置（模型、超时、并发等）
- `transcript_cache`: 转写缓存统计（hit/miss）
- `retry_rounds`: 自动重跑轮数（v3.3+）

### 2. summary.json

机读汇总：
```json
{
  "batch_id": "baseline-v3.3-final-*",
  "phone_count": 39,
  "succeeded": 38,
  "results": [
    {
      "phone": "13415100087",
      "status": "succeeded",
      "duration_ms": 123456,
      "export_summary": {
        "node_count": 42,
        "attempt_count": 38
      }
    },
    ...
  ]
}
```

### 3. runs/{phone}/result.json

单号码最终结果：
```json
{
  "status": "succeeded",  // 或 "failed" / "timeout"
  "run_id": "...",
  "duration_ms": 123456,
  "error": null,
  "result": {
    "customer_profile_card": {
      "customer_overview": "...",
      "purchase_motivation": "...",
      "budget_and_decision": "...",
      ...
    },
    "insights": [...],
    "competing_models": [...]
  }
}
```

### 4. trace.db

SQLite 数据库，包含4张表：
- `workflow_runs`: 运行记录（状态、耗时、错误）
- `node_executions`: 节点执行（每个节点的输入输出）
- `request_attempts`: 请求尝试（LLM/HTTP 调用，含重试）
  - v3.3+ 新增 `anomaly`、`finish_reason` 列
- `definition_versions`: 工作流定义快照

查询示例：
```sql
-- 查看失败的节点
SELECT node_id, error FROM node_executions WHERE status != 'succeeded';

-- 查看异常重试
SELECT node_id, attempt_no, anomaly, finish_reason
FROM request_attempts
WHERE anomaly IS NOT NULL;

-- 统计出网次数
SELECT kind, COUNT(*) FROM request_attempts GROUP BY kind;
```

## 读取数据示例

### Python 读取单号码结果

```python
import json
from pathlib import Path

phone = "13415100087"
result_path = Path(f"runs/{phone}/result.json")

with result_path.open() as f:
    data = json.load(f)

if data["status"] == "succeeded":
    card = data["result"]["customer_profile_card"]
    print(f"客户画像: {card['customer_overview']}")
else:
    print(f"失败原因: {data['error']}")
```

### Python 读取留痕数据库

```python
import sqlite3

conn = sqlite3.connect("trace.db")

# 查询所有成功的运行
cursor = conn.execute("""
    SELECT run_id, business_ref, duration_ms
    FROM workflow_runs
    WHERE status = 'succeeded'
""")

for row in cursor:
    print(f"号码 {row[1]}: 耗时 {row[2]}ms")

conn.close()
```

## 用于结果页面制作

制作对比页面时，从本目录读取：
1. `summary.json` 获取整体统计
2. `runs/{phone}/result.json` 获取每个号码的画像内容
3. `trace.db` 查询节点耗时、出网次数等性能数据

对比基准：
- v3.1: `docs/business_opt01_v3/v3.1-跑批结果/`
- v3.2: `docs/business_opt01_v3/v3.2-跑批结果/`
- v3.3: 本目录

## 可靠性保障

v3.3 跑批含以下可靠性改进：
1. **自动重跑**: `--retry-failed 2`，失败案自动重试2轮
2. **异常诊断**: `trace.db` 的 `anomaly` / `finish_reason` 字段可追溯异常原因
3. **参数校验**: 日志开头检查 workflow_id 和缓存配置
4. **转写复用**: 复用 `experiments/transcript_cache`，11段录音不重复识别

详见: `CHANGELOG-v3.3-reliability.md`

## 注意事项

- **隐私**: 手机号为真实客户数据，不得外泄
- **完整性**: 删除任何文件前请备份整个目录
- **追溯性**: `manifest.json` 的 `code_commit` 可定位代码版本
EOF

echo "✓ 数据已保存到: ${FINAL_DEST}"
echo ""
echo "生成文件:"
echo "  - README.md      (数据说明书)"
echo "  - manifest.json  (批次元数据)"
echo "  - summary.json   (机读汇总)"
echo "  - summary.md     (人读汇总)"
echo "  - trace.db       (留痕数据库)"
echo "  - runs/          (39个号码的详细结果)"
echo ""

# 简要统计
SUCCEEDED=$(python -c "import json; d=json.load(open('${FINAL_DEST}/summary.json')); print(d['succeeded'])")
TOTAL=$(python -c "import json; d=json.load(open('${FINAL_DEST}/summary.json')); print(d['phone_count'])")

echo "=========================================="
echo "最终统计"
echo "=========================================="
echo "成功: ${SUCCEEDED} / ${TOTAL}"
echo "成功率: $(python -c "print(f'{${SUCCEEDED}/${TOTAL}*100:.1f}%')")"
echo ""
echo "详细结果: ${FINAL_DEST}/summary.md"
echo "数据说明: ${FINAL_DEST}/README.md"
echo "=========================================="

# 检查数据库新字段
echo ""
echo "验证 v3.3 可靠性改进..."
python -c "
import sqlite3
conn = sqlite3.connect('${FINAL_DEST}/trace.db')
cursor = conn.execute('PRAGMA table_info(request_attempts)')
columns = {row[1] for row in cursor.fetchall()}
has_anomaly = 'anomaly' in columns
has_finish_reason = 'finish_reason' in columns
conn.close()

if has_anomaly and has_finish_reason:
    print('✓ anomaly 和 finish_reason 字段已记录')
else:
    print('⚠ 新字段缺失（需检查 migration）')
"

echo ""
echo "完成！"
