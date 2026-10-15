#!/usr/bin/env python
"""v3.3 可靠性改进 - 快速健康检查

在正式全量跑批前，先用 1-2 个号码验证改进是否生效。
"""
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

def check_database_schema(db_path: Path) -> bool:
    """检查数据库 schema 是否已更新"""
    if not db_path.exists():
        print(f"[FAIL] 数据库不存在: {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(request_attempts)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()

    required = {"anomaly", "finish_reason"}
    missing = required - columns

    if missing:
        print(f"[FAIL] request_attempts 表缺少字段: {missing}")
        return False

    print("[OK] 数据库 schema 已更新 (anomaly, finish_reason 列已添加)")
    return True


def check_recent_attempts(db_path: Path) -> bool:
    """检查最近的尝试是否记录了 anomaly 和 finish_reason"""
    if not db_path.exists():
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("""
        SELECT attempt_id, kind, error, anomaly, finish_reason
        FROM request_attempts
        WHERE kind = 'llm'
        ORDER BY created_at_ms DESC
        LIMIT 5
    """)
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        print("ℹ 尚无 LLM 尝试记录（需跑批后再检查）")
        return True

    print(f"\n最近 {len(rows)} 条 LLM 尝试:")
    for row in rows:
        attempt_id, kind, error, anomaly, finish_reason = row
        print(f"  - attempt #{attempt_id}: error={error or 'None'}, "
              f"anomaly={anomaly or 'None'}, finish_reason={finish_reason or 'None'}")

    # 检查是否有至少一条记录了 finish_reason
    has_finish_reason = any(row[4] for row in rows)
    if has_finish_reason:
        print("[OK] finish_reason 已被记录")
    else:
        print("[WARN] finish_reason 尚未被记录（可能是旧记录或回放模式）")

    return True


def main():
    print("=" * 60)
    print("v3.3 可靠性改进健康检查")
    print("=" * 60)

    # 检查主数据库
    main_db = REPO_ROOT / "data" / "customer_profile.db"
    print(f"\n1. 检查主数据库: {main_db.name}")
    check_database_schema(main_db)
    check_recent_attempts(main_db)

    # 检查最近的批次数据库（如果有）
    batches_dir = REPO_ROOT / "experiments" / "batches"
    if batches_dir.exists():
        print(f"\n2. 检查最近批次:")
        batch_dirs = sorted([d for d in batches_dir.iterdir() if d.is_dir()],
                          key=lambda x: x.stat().st_mtime, reverse=True)[:3]

        if not batch_dirs:
            print("   （无批次记录）")
        else:
            for batch_dir in batch_dirs:
                trace_db = batch_dir / "trace.db"
                if trace_db.exists():
                    print(f"\n   批次: {batch_dir.name}")
                    check_database_schema(trace_db)
                    check_recent_attempts(trace_db)

    print("\n" + "=" * 60)
    print("建议:")
    print("1. 先用 --limit 1 跑 1 个号码验证")
    print("2. 检查 trace.db 确认新字段有数据")
    print("3. 验证通过后再执行全量 39 用例跑批")
    print("=" * 60)


if __name__ == "__main__":
    main()
