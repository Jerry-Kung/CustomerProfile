#!/usr/bin/env python
"""环境预检 —— 跑批前确认一切就绪。

**这一版的要害是「真的执行一遍跑批入口」。** 上一版只 import 了
``customer_profile.*`` 包内的模块，从未拉起 ``scripts/run_baseline.py`` 本身，
于是在 ``collect_test_phones`` 被误删、跑批入口 import 即崩的情况下仍然报绿——
预检若与真实入口走的不是同一条路，它给的绿灯就没有意义。

因此第 1 项直接以子进程跑 ``run_baseline.py --help``：这与真实跑批走的是同一条
import 路径，任何「模块缺失 / 循环导入 / 语法错误」都会在这里现形。

输出一律 ASCII 标记（``[OK]`` / ``[FAIL]`` / ``[WARN]``）：Windows 控制台默认 GBK，
``✓`` / ``✗`` 这类符号会直接抛 ``UnicodeEncodeError``，把预检自己变成一次故障。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ENTRY_SCRIPT = REPO_ROOT / "scripts" / "run_baseline.py"
WORKFLOW_ID = "customer_profile_pipeline"


def check(name: str, ok: bool, detail: str = "") -> bool:
    """打印一行检查结果，返回 ``ok`` 以便调用方累计。"""
    print(f"  [{'OK' if ok else 'FAIL'}] {name}")
    if detail:
        print(f"         {detail}")
    return ok


def run_entry_help() -> tuple[bool, str]:
    """以子进程跑跑批入口的 ``--help``，返回 ``(是否成功, 说明)``。

    用子进程而不是 in-process import：跑批本身就是以「脚本」方式被执行的，
    ``run_baseline.py`` 顶部那几行 ``sys.path.insert`` 只在直接执行时生效。
    in-process import 会绕过它，检查的就不再是真实路径。
    """
    try:
        result = subprocess.run(
            [sys.executable, str(ENTRY_SCRIPT), "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"

    if result.returncode != 0:
        # 取 stderr 的最后几行：那里才是真正的异常信息
        tail = " / ".join(
            line.strip()
            for line in (result.stderr or "").strip().splitlines()[-4:]
            if line.strip()
        )
        return False, f"exit={result.returncode} {tail}"

    missing = [flag for flag in ("--retry-failed", "--workflow-id") if flag not in result.stdout]
    if missing:
        return False, f"--help 缺少参数：{missing}"
    return True, "入口可执行，参数齐备"


def main() -> int:
    print("=" * 60)
    print("v3.3 跑批环境预检")
    print("=" * 60)
    print()

    all_ok = True

    # 1. 跑批入口（最重要的一项：走真实 import 路径）
    print("1. 跑批入口")
    ok, detail = run_entry_help()
    all_ok &= check("run_baseline.py 可执行", ok, detail)
    print()

    # 2. 手机号清单
    print("2. 手机号清单")
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        import run_baseline as rb  # noqa: PLC0415

        phones = rb.read_phone_list(rb.DEFAULT_PHONES)
        all_ok &= check(
            "清单可读",
            True,
            f"{rb.DEFAULT_PHONES.name} -> {len(phones)} 个号码",
        )
        if len(phones) != 39:
            print(f"         [WARN] 预期 39 个，实际 {len(phones)} 个")
    except Exception as exc:
        all_ok &= check("清单可读", False, f"{type(exc).__name__}: {exc}")
    print()

    # 3. 转写缓存
    print("3. 转写缓存")
    cache_dir = REPO_ROOT / "experiments/transcript_cache"
    if cache_dir.is_dir():
        cached = len(list(cache_dir.rglob("*.json")))
        check("缓存目录", True, f"{cached} 个已缓存文件")
        if cached == 0:
            print("         [WARN] 缓存为空：录音将重新识别，非缓存失效")
    else:
        check("缓存目录", True, f"{cache_dir.name} 不存在，首次跑批会创建")
    print()

    # 4. 工作流 ID
    print("4. 工作流 ID")
    try:
        from customer_profile.workflows import load_all  # noqa: PLC0415

        defined = load_all()
        all_ok &= check(
            f"{WORKFLOW_ID} 已定义",
            WORKFLOW_ID in defined,
            f"共 {len(defined)} 个工作流已注册",
        )
    except Exception as exc:
        all_ok &= check("工作流注册表可读", False, f"{type(exc).__name__}: {exc}")
    print()

    # 5. v3.3 改进是否在位
    print("5. v3.3 改进")
    try:
        from inspect import signature  # noqa: PLC0415

        from customer_profile.execution.llm import detect_output_anomaly  # noqa: PLC0415

        params = set(signature(detect_output_anomaly).parameters)
        ok = {"finish_reason", "min_completion_tokens"} <= params
        all_ok &= check("LLM 输出闸门", ok, "detect_output_anomaly 已扩参")
    except Exception as exc:
        all_ok &= check("LLM 输出闸门", False, f"{type(exc).__name__}: {exc}")

    try:
        import sqlite3  # noqa: PLC0415

        sys.path.insert(0, str(REPO_ROOT / "src"))
        from customer_profile.persistence.store import _ADDED_COLUMNS  # noqa: PLC0415

        added = {name for name, _ in _ADDED_COLUMNS.get("request_attempts", ())}
        ok = {"anomaly", "finish_reason"} <= added
        all_ok &= check("attempts 补列已登记", ok, f"migration 列：{sorted(added)}")
    except Exception as exc:
        all_ok &= check("attempts 补列已登记", False, f"{type(exc).__name__}: {exc}")
    print()

    # 6. 输出目录
    print("6. 输出目录")
    out_dir = REPO_ROOT / "experiments"
    all_ok &= check("输出目录", out_dir.is_dir(), str(out_dir.relative_to(REPO_ROOT)))
    batches_dir = out_dir / "batches"
    existing = sorted(p.name for p in batches_dir.glob("*")) if batches_dir.is_dir() else []
    print(f"  已有批次 {len(existing)} 个：{'、'.join(existing) if existing else '（无）'}")
    cache_dir = out_dir / "transcript_cache"
    check(
        "转写缓存目录",
        cache_dir.is_dir(),
        f"{cache_dir.relative_to(REPO_ROOT)}（{len(list(cache_dir.glob('*')))} 条）"
        if cache_dir.is_dir() else "缺失——跑批会重新识别全部录音",
    )
    print()

    # 总结
    print("=" * 60)
    if all_ok:
        print("[OK] 环境检查通过，可以开始跑批")
        print()
        print("下一步:")
        print("  python scripts/run_baseline.py --workflow-id customer_profile_pipeline")
    else:
        print("[FAIL] 环境检查发现问题，请先解决")
    print("=" * 60)

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
