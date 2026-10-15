#!/usr/bin/env python
"""环境预检 - 跑批前确认一切就绪"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def check(name: str, ok: bool, detail: str = "") -> bool:
    """打印检查结果"""
    status = "✓" if ok else "✗"
    print(f"  [{status}] {name}")
    if detail:
        print(f"      {detail}")
    return ok


def main():
    print("=" * 60)
    print("v3.3 跑批环境预检")
    print("=" * 60)
    print()

    all_ok = True

    # 1. 代码版本
    print("1. 代码版本")
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        commit = result.stdout.strip()
        ok = "feat(reliability)" in commit or len(commit) > 0
        all_ok &= check("Git 提交", ok, commit[:60] if commit else "无法获取")
    except Exception as exc:
        all_ok &= check("Git 提交", False, str(exc))

    # 检查是否有未提交的改动
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        uncommitted = result.stdout.strip()
        has_uncommitted = bool(uncommitted)
        if has_uncommitted:
            lines = uncommitted.split("\n")
            detail = f"{len(lines)} 个文件未提交"
            check("工作区状态", False, detail)
            print("      建议先提交改动或使用 git stash")
        else:
            check("工作区状态", True, "无未提交改动")
    except Exception:
        pass
    print()

    # 2. 输入文件
    print("2. 输入文件")
    phone_list = REPO_ROOT / "experiments/batches/baseline-39-dsv41-noThink/phones.txt"
    ok = phone_list.exists()
    if ok:
        count = len(phone_list.read_text().strip().split("\n"))
        all_ok &= check("手机号清单", ok, f"{phone_list.relative_to(REPO_ROOT)} ({count} 个)")
    else:
        all_ok &= check("手机号清单", False, f"{phone_list.relative_to(REPO_ROOT)} 不存在")
    print()

    # 3. 转写缓存
    print("3. 转写缓存")
    cache_dir = REPO_ROOT / "experiments/transcript_cache"
    ok = cache_dir.exists()
    if ok:
        cached_files = list(cache_dir.rglob("*.json"))
        detail = f"{len(cached_files)} 个已缓存文件"
        check("缓存目录", ok, detail)
    else:
        check("缓存目录", False, f"{cache_dir.relative_to(REPO_ROOT)} 不存在（首次跑会创建）")
    print()

    # 4. Python 依赖
    print("4. Python 依赖")
    try:
        sys.path.insert(0, str(REPO_ROOT / "src"))
        from customer_profile.execution.llm import detect_output_anomaly
        from customer_profile.persistence.store import Store

        all_ok &= check("核心模块", True, "导入成功")
    except ImportError as exc:
        all_ok &= check("核心模块", False, str(exc))
    print()

    # 5. v3.3 改进代码
    print("5. v3.3 改进验证")
    try:
        # 检查 detect_output_anomaly 是否有新参数
        from inspect import signature
        sig = signature(detect_output_anomaly)
        params = list(sig.parameters.keys())
        has_finish_reason = "finish_reason" in params
        has_min_tokens = "min_completion_tokens" in params

        all_ok &= check("LLM 输出闸门", has_finish_reason and has_min_tokens,
                       "detect_output_anomaly 已更新")
    except Exception as exc:
        all_ok &= check("LLM 输出闸门", False, str(exc))

    # 检查 run_baseline.py 是否有 --retry-failed
    run_baseline = REPO_ROOT / "scripts/run_baseline.py"
    if run_baseline.exists():
        content = run_baseline.read_text(encoding="utf-8")
        has_retry = "--retry-failed" in content
        all_ok &= check("自动重跑", has_retry, "run_baseline.py 已更新")
    else:
        all_ok &= check("自动重跑", False, "run_baseline.py 不存在")
    print()

    # 6. 输出目录
    print("6. 输出目录")
    out_dir = REPO_ROOT / "docs/business_opt01_v3"
    ok = out_dir.exists()
    all_ok &= check("输出目录", ok, str(out_dir.relative_to(REPO_ROOT)))

    final_dest = out_dir / "baseline-v3.3-final"
    if final_dest.exists():
        check("最终保存位置", False, f"{final_dest.relative_to(REPO_ROOT)} 已存在（将备份）")
    else:
        check("最终保存位置", True, f"{final_dest.relative_to(REPO_ROOT)} 不存在（就绪）")
    print()

    # 总结
    print("=" * 60)
    if all_ok:
        print("✓ 环境检查通过，可以开始跑批")
        print()
        print("下一步:")
        print("  1. 小批量测试: python scripts/run_baseline.py --limit 1 ...")
        print("  2. 全量跑批:   python scripts/run_v3.3_baseline_full.py")
    else:
        print("⚠ 环境检查发现问题，请先解决")
        print()
        print("常见问题:")
        print("  - 缺少依赖: pip install -e .")
        print("  - 代码未更新: git pull")
        print("  - 未提交改动: git commit / git stash")
    print("=" * 60)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
