"""执行进程的存活信号。

**为什么需要这个文件。** 数据库线程死掉时进程仍然存活：Docker 显示 ``Up``，
API 的 ``/health`` 照常返回 ok，于是「执行已经停止」没有任何地方暴露出来，
积压一直堆到人工发现。2026-11 的 UAT 事故就是这样持续了很久。

做法刻意保持最简单：worker 周期性把当前状态写进 ``DATA_DIR`` 下的一个小 JSON，
健康检查读时间戳判断是否过期。不引入消息队列、不依赖跨进程通知——
写文件与 ``os.stat`` 在本地卷上足够可靠，而 ``DATA_DIR`` 本来就是 worker 与
API 共用的持久卷。

**按 ``WORKFLOW_SCOPE`` 分文件**，不是一个全局文件：画像与邀约是两个容器、
两个 worker，共用一个卷。合成一个文件时，一个死了、另一个还活着，检查会双双通过，
恰恰放过要发现的情形。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from .settings import Settings


_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_scope(scope: str) -> str:
    """档位名用作文件名的一段。未知取值一律归一，避免路径穿越。"""
    cleaned = _UNSAFE.sub("-", (scope or "all").strip())
    return cleaned or "all"


def liveness_path(settings: Settings) -> Path:
    """本进程存活信号的落位。"""
    return Path(settings.data_dir) / f"worker-liveness.{_safe_scope(settings.workflow_scope)}.json"


def write(
    settings: Settings,
    *,
    worker_id: str,
    active_runs: int,
    last_claim_at_ms: int | None,
    db: dict[str, Any] | None = None,
) -> None:
    """原子刷新存活信号。

    先写临时文件再 ``os.replace``：健康检查随时可能在读，读到半个 JSON 会把它
    判成「停滞」并触发一次无谓的告警。``replace`` 在同一文件系统上是原子的。

    ``db`` 是数据库健康快照（``Store.health()``）。**进程活着不等于数据库能用**：
    SQLite 时代连接线程死掉后进程仍然 Up；切 MySQL 后多了一种「线程活着但连不上
    远端」的失效——两种都只有把库的状态写进信号才看得出来。
    """
    path = liveness_path(settings)
    payload = {
        "worker_id": worker_id,
        "workflow_scope": settings.workflow_scope,
        "pid": os.getpid(),
        "active_runs": int(active_runs),
        "last_claim_at_ms": last_claim_at_ms,
        "updated_at_ms": int(time.time() * 1000),
    }
    if db:
        payload["db"] = db
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # 写不出存活信号不该让执行停止：它只是观测手段，不是业务路径。
        # 但**不吞异常到无人知晓**——健康检查随后会把「文件缺失」判为停滞。
        pass


def read(settings: Settings) -> dict[str, Any] | None:
    """读存活信号。文件缺失或内容损坏时返回 ``None``。"""
    path = liveness_path(settings)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check(settings: Settings, *, now_ms: int | None = None) -> tuple[bool, str]:
    """判定消费是否停滞。返回 ``(是否健康, 说明)``。

    「健康」的口径是**最近一次刷新仍在阈值内**，不是「文件存在」：线程死掉后
    文件会一直留在卷上，只看存在与否等于永远判健康。

    信号里带 ``db`` 时一并判定：**执行没有停滞、但数据库连不上**，同样是「领不到任务」
    且更难从积压上看出来——它会表现为「队列深度不涨、也没有新日志」。
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    payload = read(settings)
    if payload is None:
        return False, f"未找到存活信号：{liveness_path(settings)}"
    updated = payload.get("updated_at_ms")
    if not isinstance(updated, int):
        return False, "存活信号缺少 updated_at_ms"
    age = now_ms - updated
    if age > settings.worker_liveness_max_age_ms:
        return False, (
            f"执行进程已 {age // 1000}s 未刷新存活信号"
            f"（阈值 {settings.worker_liveness_max_age_ms // 1000}s），判定消费停滞"
        )

    db = payload.get("db") or {}
    if db and db.get("db_alive") is False:
        return False, f"执行进程存活，但数据库连接不可用：{db.get('db_error') or '未知原因'}"

    detail = f"{age // 1000}s 前刷新，在飞 {payload.get('active_runs', 0)} 条"
    last_ok = db.get("db_last_ok_at_ms") if isinstance(db, dict) else None
    if isinstance(last_ok, int):
        detail += f"，数据库 {(now_ms - last_ok) // 1000}s 前成功"
    return True, detail


def main(argv: list[str] | None = None) -> int:
    """容器健康检查入口。停滞时以非零码退出，交给 Docker 标记 unhealthy。

        python -m customer_profile.liveness --check
    """
    from .settings import get_settings

    parser = argparse.ArgumentParser(description="执行进程存活检查")
    parser.add_argument("--check", action="store_true", help="停滞时以状态码 1 退出")
    parser.add_argument("--json", action="store_true", help="输出完整信号内容")
    args = parser.parse_args(argv)

    settings = get_settings()
    healthy, detail = check(settings)
    if args.json:
        print(json.dumps(read(settings), ensure_ascii=False))
        return 0 if healthy else 1
    print(("健康：" if healthy else "停滞：") + detail)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
