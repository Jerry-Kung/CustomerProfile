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
from typing import Any, Mapping

from .settings import Settings


_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_scope(scope: str) -> str:
    """档位名用作文件名的一段。未知取值一律归一，避免路径穿越。"""
    cleaned = _UNSAFE.sub("-", (scope or "all").strip())
    return cleaned or "all"


def liveness_path(settings: Settings) -> Path:
    """本进程存活信号的落位。

    ``TRACE_PERSIST_MODE=none`` 时不使用本文件——整套本地存储都不写，见
    :func:`write` 与 :func:`read` 的说明。
    """
    return Path(settings.data_dir) / f"worker-liveness.{_safe_scope(settings.workflow_scope)}.json"


def uses_db(settings: Settings) -> bool:
    """存活信号是否走数据库。

    与 ``TRACE_PERSIST_MODE=none`` 同口径，而不是另一个独立开关：这档的唯一含义
    就是「不写任何本地存储」，存活信号没有第二种解释。
    """
    return not settings.local_storage_enabled


def build_payload(
    settings: Settings,
    *,
    worker_id: str,
    active_runs: int,
    last_claim_at_ms: int | None,
    db: dict[str, Any] | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """存活信号的内容。文件与数据库两条通道共用同一份字段，检查逻辑因此只有一套。"""
    payload = {
        "worker_id": worker_id,
        "workflow_scope": settings.workflow_scope,
        "pid": os.getpid(),
        "active_runs": int(active_runs),
        "last_claim_at_ms": last_claim_at_ms,
        "updated_at_ms": int(time.time() * 1000) if now_ms is None else int(now_ms),
    }
    if db:
        payload["db"] = db
    return payload


def write(
    settings: Settings,
    *,
    worker_id: str,
    active_runs: int,
    last_claim_at_ms: int | None,
    db: dict[str, Any] | None = None,
) -> None:
    """原子刷新存活信号（文件通道）。

    先写临时文件再 ``os.replace``：健康检查随时可能在读，读到半个 JSON 会把它
    判成「停滞」并触发一次无谓的告警。``replace`` 在同一文件系统上是原子的。

    ``db`` 是数据库健康快照（``Store.health()``）。**进程活着不等于数据库能用**：
    SQLite 时代连接线程死掉后进程仍然 Up；切 MySQL 后多了一种「线程活着但连不上
    远端」的失效——两种都只有把库的状态写进信号才看得出来。

    ``none`` 档直接返回：那一档不写任何本地文件，信号改由
    ``Store.write_heartbeat`` 落库（见 ``worker.Worker._write_liveness_db``）。
    """
    if uses_db(settings):
        return
    path = liveness_path(settings)
    payload = build_payload(
        settings,
        worker_id=worker_id,
        active_runs=active_runs,
        last_claim_at_ms=last_claim_at_ms,
        db=db,
    )
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
    """读存活信号（文件通道）。文件缺失或内容损坏时返回 ``None``。"""
    if uses_db(settings):
        return None
    path = liveness_path(settings)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check(
    settings: Settings,
    *,
    now_ms: int | None = None,
    payload: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """判定消费是否停滞。返回 ``(是否健康, 说明)``。

    「健康」的口径是**最近一次刷新仍在阈值内**，不是「文件存在」：线程死掉后
    文件会一直留在卷上，只看存在与否等于永远判健康。

    信号里带 ``db`` 时一并判定：**执行没有停滞、但数据库连不上**，同样是「领不到任务」
    且更难从积压上看出来——它会表现为「队列深度不涨、也没有新日志」。

    ``payload`` 由调用方给定时优先使用。``none`` 档的信号在数据库里，调用方（API
    与健康检查入口）各自取好再传进来，本函数不自己连库——判定逻辑因此不依赖后端。
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if payload is None:
        payload = read(settings)
    if payload is None:
        where = "worker_heartbeats 表" if uses_db(settings) else str(liveness_path(settings))
        return False, f"未找到存活信号：{where}"
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

    ``none`` 档的信号在库里，因此这里按档位先取好再判定——判定逻辑本身只有一套。
    """
    from .settings import get_settings

    parser = argparse.ArgumentParser(description="执行进程存活检查")
    parser.add_argument("--check", action="store_true", help="停滞时以状态码 1 退出")
    parser.add_argument("--json", action="store_true", help="输出完整信号内容")
    args = parser.parse_args(argv)

    settings = get_settings()
    payload = _payload_for_cli(settings)
    healthy, detail = check(settings, payload=payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if healthy else 1
    print(("健康：" if healthy else "停滞：") + detail)
    return 0 if healthy else 1


def _payload_for_cli(settings: Settings) -> dict[str, Any] | None:
    """命令行入口的取信号方式。库通道读不出来时返回 ``None``（判停滞），不猜。"""
    if not uses_db(settings):
        return read(settings)
    import asyncio

    from .persistence.store import Store

    async def fetch() -> dict[str, Any] | None:
        store = Store(settings.build_backend())
        try:
            store.connect()
            return await store.read_heartbeat(workflow_scope=settings.workflow_scope)
        finally:
            store.close()

    try:
        return asyncio.run(fetch())
    except Exception:  # 连不上库本身就该判不健康，而不是让探针崩掉
        return None


if __name__ == "__main__":
    sys.exit(main())
