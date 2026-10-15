#!/usr/bin/env python
"""v3.3 全量39用例基线跑批

用途：使用最新代码（含可靠性改进）重新跑39个手机号的完整基线
输出：结果保存到 docs/business_opt01_v3/baseline-v3.3-final/

可靠性改进：
- 自动重跑失败案2轮
- 记录 anomaly 和 finish_reason
- 复用转写缓存（11段录音不重复识别）
"""
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PHONE_LIST = REPO_ROOT / "experiments/batches/baseline-39-dsv41-noThink/phones.txt"
CACHE_DIR = REPO_ROOT / "experiments/transcript_cache"


def main():
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    batch_id = f"baseline-v3.3-final-{timestamp}"

    temp_batch_dir = REPO_ROOT / "experiments/batches" / batch_id
    final_dest = REPO_ROOT / "docs/business_opt01_v3/baseline-v3.3-final"

    print("=" * 60)
    print("v3.3 全量39用例基线跑批")
    print("=" * 60)
    print(f"手机号清单: {PHONE_LIST.relative_to(REPO_ROOT)}")
    print(f"批次ID:     {batch_id}")
    print(f"临时输出:   {temp_batch_dir.relative_to(REPO_ROOT)}")
    print(f"最终保存:   {final_dest.relative_to(REPO_ROOT)}")
    print(f"转写缓存:   {CACHE_DIR.relative_to(REPO_ROOT)}")
    print()
    print("工作流:     customer_profile_pipeline (v3.3 优化版)")
    print("可靠性:     --retry-failed 2 (最多自动重跑2轮)")
    print()

    if not PHONE_LIST.exists():
        print(f"❌ 手机号清单不存在: {PHONE_LIST}")
        return 1

    response = input("确认开始跑批? (y/N): ").strip().lower()
    if response not in ("y", "yes"):
        print("已取消")
        return 0

    # 执行跑批
    print()
    print("开始跑批...")
    print("预计耗时: 30-60分钟（取决于录音案例数量）")
    print()

    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts/run_baseline.py"),
        "--workflow-id", "customer_profile_pipeline",
        "--phones", str(PHONE_LIST),
        "--batch-id", batch_id,
        "--out-dir", str(REPO_ROOT / "experiments"),
        "--transcript-cache-dir", str(CACHE_DIR),
        "--retry-failed", "2",
        "--concurrency", "4",
    ]

    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    exit_code = result.returncode

    print()
    print("=" * 60)
    if exit_code == 0:
        print("✓ 跑批全部成功")
    else:
        print(f"⚠ 部分用例失败 (exit code: {exit_code})")
        print(f"  查看详情: {temp_batch_dir / 'summary.md'}")
    print("=" * 60)

    # 持久化保存
    print()
    print("持久化保存结果...")

    # 备份旧结果
    if final_dest.exists():
        backup = final_dest.parent / f"{final_dest.name}.backup-{timestamp}"
        print(f"  备份旧结果到: {backup.relative_to(REPO_ROOT)}")
        shutil.move(str(final_dest), str(backup))

    # 复制到最终位置
    final_dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(temp_batch_dir), str(final_dest), dirs_exist_ok=True)

    # 生成数据说明书
    readme_content = """# baseline-v3.3-final 数据说明

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
cursor = conn.execute(\"\"\"
    SELECT run_id, business_ref, duration_ms
    FROM workflow_runs
    WHERE status = 'succeeded'
\"\"\")

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
"""

    (final_dest / "README.md").write_text(readme_content, encoding="utf-8")

    print(f"✓ 数据已保存到: {final_dest.relative_to(REPO_ROOT)}")
    print()
    print("生成文件:")
    print("  - README.md      (数据说明书)")
    print("  - manifest.json  (批次元数据)")
    print("  - summary.json   (机读汇总)")
    print("  - summary.md     (人读汇总)")
    print("  - trace.db       (留痕数据库)")
    print("  - runs/          (39个号码的详细结果)")
    print()

    # 简要统计
    summary_path = final_dest / "summary.json"
    if summary_path.exists():
        with summary_path.open(encoding="utf-8") as f:
            summary = json.load(f)

        succeeded = summary["succeeded"]
        total = summary["phone_count"]
        success_rate = succeeded / total * 100 if total > 0 else 0

        print("=" * 60)
        print("最终统计")
        print("=" * 60)
        print(f"成功: {succeeded} / {total}")
        print(f"成功率: {success_rate:.1f}%")
        print()
        print(f"详细结果: {(final_dest / 'summary.md').relative_to(REPO_ROOT)}")
        print(f"数据说明: {(final_dest / 'README.md').relative_to(REPO_ROOT)}")
        print("=" * 60)

    # 验证 v3.3 改进
    print()
    print("验证 v3.3 可靠性改进...")
    trace_db = final_dest / "trace.db"
    if trace_db.exists():
        try:
            conn = sqlite3.connect(str(trace_db))
            cursor = conn.execute("PRAGMA table_info(request_attempts)")
            columns = {row[1] for row in cursor.fetchall()}
            conn.close()

            has_anomaly = "anomaly" in columns
            has_finish_reason = "finish_reason" in columns

            if has_anomaly and has_finish_reason:
                print("✓ anomaly 和 finish_reason 字段已记录")
            else:
                print("⚠ 新字段缺失（需检查 migration）")
        except Exception as exc:
            print(f"⚠ 无法检查数据库: {exc}")

    print()
    print("完成！")
    return 0


if __name__ == "__main__":
    sys.exit(main())
