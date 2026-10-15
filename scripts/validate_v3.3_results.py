#!/usr/bin/env python
"""v3.3 跑批结果验证工具

跑批完成后，快速检查：
1. 成功率是否达标
2. 关键字段是否完整
3. v3.3 改进是否生效
4. 数据质量是否满足制作结果页面的要求
"""
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULT_DIR = REPO_ROOT / "docs/business_opt01_v3/baseline-v3.3-final"


def check_file_exists(path: Path, name: str) -> bool:
    """检查文件是否存在"""
    if path.exists():
        print(f"  [OK] {name}")
        return True
    else:
        print(f"  [FAIL] {name} - 缺失")
        return False


def check_summary(summary_path: Path) -> dict[str, Any]:
    """检查汇总数据"""
    if not summary_path.exists():
        return {"ok": False, "error": "summary.json 不存在"}

    with summary_path.open(encoding="utf-8") as f:
        data = json.load(f)

    total = data.get("phone_count", 0)
    succeeded = data.get("succeeded", 0)
    success_rate = succeeded / total * 100 if total > 0 else 0

    print(f"  总用例: {total}")
    print(f"  成功: {succeeded}")
    print(f"  成功率: {success_rate:.1f}%")

    ok = success_rate >= 95.0  # 期望至少95%成功率
    if ok:
        print(f"  [OK] 成功率达标 (>= 95%)")
    else:
        print(f"  [WARN] 成功率偏低 (< 95%)")

    return {"ok": ok, "total": total, "succeeded": succeeded, "rate": success_rate}


def check_result_completeness(runs_dir: Path, expected_count: int) -> dict[str, Any]:
    """检查结果完整性"""
    if not runs_dir.exists():
        return {"ok": False, "error": "runs/ 目录不存在"}

    phone_dirs = [d for d in runs_dir.iterdir() if d.is_dir()]
    actual_count = len(phone_dirs)

    print(f"  号码目录数: {actual_count} / {expected_count}")

    # 抽样检查几个结果文件
    sample_size = min(5, actual_count)
    complete = 0
    for phone_dir in phone_dirs[:sample_size]:
        result_json = phone_dir / "result.json"
        tree_json = phone_dir / "tree.json"
        if result_json.exists() and tree_json.exists():
            complete += 1

    print(f"  抽样完整性: {complete} / {sample_size}")

    ok = actual_count == expected_count and complete == sample_size
    if ok:
        print("  [OK] 结果文件完整")
    else:
        print("  [WARN] 部分结果文件缺失")

    return {"ok": ok, "actual_count": actual_count, "expected_count": expected_count}


def check_database_schema(trace_db: Path) -> dict[str, Any]:
    """检查数据库 schema（v3.3 改进）"""
    if not trace_db.exists():
        return {"ok": False, "error": "trace.db 不存在"}

    conn = sqlite3.connect(str(trace_db))

    # 检查 request_attempts 表的列
    cursor = conn.execute("PRAGMA table_info(request_attempts)")
    columns = {row[1] for row in cursor.fetchall()}

    has_anomaly = "anomaly" in columns
    has_finish_reason = "finish_reason" in columns

    print(f"  anomaly 列: {'[OK]' if has_anomaly else '[FAIL]'}")
    print(f"  finish_reason 列: {'[OK]' if has_finish_reason else '[FAIL]'}")

    # 检查是否有数据
    cursor = conn.execute("SELECT COUNT(*) FROM request_attempts WHERE kind = 'llm'")
    llm_count = cursor.fetchone()[0]
    print(f"  LLM 请求数: {llm_count}")

    # 检查 anomaly 记录数
    if has_anomaly:
        cursor = conn.execute("SELECT COUNT(*) FROM request_attempts WHERE anomaly IS NOT NULL")
        anomaly_count = cursor.fetchone()[0]
        print(f"  异常记录数: {anomaly_count}")

    # 检查 finish_reason 记录数
    if has_finish_reason:
        cursor = conn.execute("SELECT COUNT(*) FROM request_attempts WHERE finish_reason IS NOT NULL")
        finish_reason_count = cursor.fetchone()[0]
        print(f"  finish_reason 记录数: {finish_reason_count}")

    conn.close()

    ok = has_anomaly and has_finish_reason and llm_count > 0
    if ok:
        print("  [OK] v3.3 改进生效")
    else:
        print("  [WARN] v3.3 改进未完全生效")

    return {
        "ok": ok,
        "has_anomaly": has_anomaly,
        "has_finish_reason": has_finish_reason,
        "llm_count": llm_count,
    }


def check_result_quality(runs_dir: Path, sample_size: int = 5) -> dict[str, Any]:
    """抽样检查结果质量"""
    if not runs_dir.exists():
        return {"ok": False, "error": "runs/ 目录不存在"}

    phone_dirs = sorted([d for d in runs_dir.iterdir() if d.is_dir()])[:sample_size]

    issues = []
    for phone_dir in phone_dirs:
        result_json = phone_dir / "result.json"
        if not result_json.exists():
            issues.append(f"{phone_dir.name}: result.json 缺失")
            continue

        with result_json.open(encoding="utf-8") as f:
            data = json.load(f)

        if data.get("status") != "succeeded":
            continue

        # 检查画像卡片
        result = data.get("result", {})
        card = result.get("customer_profile_card", {})

        required_fields = [
            "customer_overview",
            "purchase_motivation",
            "budget_and_decision",
            "concerns_and_objections",
            "communication_strategy",
        ]

        for field in required_fields:
            value = card.get(field)
            if not value or len(str(value).strip()) < 10:
                issues.append(f"{phone_dir.name}: {field} 过短或缺失")

    if issues:
        print(f"  [WARN] 发现 {len(issues)} 个质量问题:")
        for issue in issues[:3]:  # 只显示前3个
            print(f"    - {issue}")
        if len(issues) > 3:
            print(f"    ... 还有 {len(issues) - 3} 个")
    else:
        print(f"  [OK] 抽样 {len(phone_dirs)} 个结果无明显问题")

    ok = len(issues) == 0
    return {"ok": ok, "issues": issues, "sample_size": len(phone_dirs)}


def main():
    print("=" * 60)
    print("v3.3 跑批结果验证")
    print("=" * 60)
    print(f"检查目录: {RESULT_DIR.relative_to(REPO_ROOT)}")
    print()

    if not RESULT_DIR.exists():
        print("[FAIL] 结果目录不存在")
        print()
        print("请先运行: python scripts/run_v3.3_baseline_full.py")
        return 1

    all_ok = True

    # 1. 文件结构检查
    print("1. 文件结构")
    files = [
        ("README.md", RESULT_DIR / "README.md"),
        ("manifest.json", RESULT_DIR / "manifest.json"),
        ("summary.json", RESULT_DIR / "summary.json"),
        ("summary.md", RESULT_DIR / "summary.md"),
        ("phones.txt", RESULT_DIR / "phones.txt"),
        ("trace.db", RESULT_DIR / "trace.db"),
        ("run.log", RESULT_DIR / "logs/run.log"),
        ("runs/", RESULT_DIR / "runs"),
    ]
    for name, path in files:
        if not check_file_exists(path, name):
            all_ok = False
    print()

    # 2. 汇总统计
    print("2. 汇总统计")
    summary_result = check_summary(RESULT_DIR / "summary.json")
    if not summary_result.get("ok", False):
        all_ok = False
    print()

    # 3. 结果完整性
    print("3. 结果完整性")
    expected_count = summary_result.get("total", 39)
    completeness_result = check_result_completeness(RESULT_DIR / "runs", expected_count)
    if not completeness_result.get("ok", False):
        all_ok = False
    print()

    # 4. 数据库 schema (v3.3 改进)
    print("4. 数据库 schema (v3.3 改进)")
    db_result = check_database_schema(RESULT_DIR / "trace.db")
    if not db_result.get("ok", False):
        all_ok = False
    print()

    # 5. 结果质量
    print("5. 结果质量（抽样）")
    quality_result = check_result_quality(RESULT_DIR / "runs", sample_size=5)
    if not quality_result.get("ok", False):
        all_ok = False
    print()

    # 总结
    print("=" * 60)
    if all_ok:
        print("[OK] 验证通过，数据可用于制作结果页面")
    else:
        print("[WARN] 验证发现问题，建议检查后再使用")
    print("=" * 60)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
