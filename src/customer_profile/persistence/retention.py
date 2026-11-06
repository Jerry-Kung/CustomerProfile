"""保留期：包文件按天清理。

挂在 worker 主循环里按天节流一次（已有秒级轮询），不引入外部 cron——多一个需要单独
部署、单独监控的调度组件，换来的只是一天少扫一次目录。

**只删包，不删骨架。** 运行、节点、尝试的非载荷列永久保留：它们是「跑过什么、成没成」
的索引，体积可忽略，而删掉之后连「这次运行存在过」都无从查起。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from . import archive

_TZ_CN = timezone(timedelta(hours=8))


def prune_packages(
    archive_dir: Path,
    *,
    retention_days: int,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """删掉早于 ``retention_days`` 的日期目录。返回删除统计。

    按**日期目录**整目录删，而不是逐个文件判断：包名里只有 run_id、没有时间，逐个判断
    就得读一遍 manifest，代价远大于收益。日期目录本来就是按这个用途分的。
    """
    root = Path(archive_dir)
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if retention_days <= 0 or not root.exists():
        return {"deleted_days": [], "deleted_files": 0, "deleted_bytes": 0}

    cutoff = datetime.fromtimestamp(now_ms / 1000, tz=_TZ_CN) - timedelta(days=retention_days)
    cutoff_day = cutoff.strftime("%Y-%m-%d")

    deleted_days: list[str] = []
    deleted_files = 0
    deleted_bytes = 0
    for day_dir in archive.iter_archive_days(root):
        if day_dir.name >= cutoff_day:
            continue
        for path in day_dir.rglob("*"):
            if path.is_file():
                deleted_files += 1
                try:
                    deleted_bytes += path.stat().st_size
                except OSError:
                    pass
        for path in sorted(day_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                path.unlink() if path.is_file() else path.rmdir()
            except OSError:
                pass
        try:
            day_dir.rmdir()
        except OSError:
            logger.warning("包目录清理后仍非空：{}", day_dir)
            continue
        deleted_days.append(day_dir.name)

    return {
        "deleted_days": deleted_days,
        "deleted_files": deleted_files,
        "deleted_bytes": deleted_bytes,
    }


def run_once(settings: Any) -> dict[str, Any] | None:
    """一次保留期检查。返回统计；没有需要清理的返回 ``None``。

    只清理包。骨架不在此处删除——两档都保留。
    """
    result = prune_packages(
        settings.trace_archive_path,
        retention_days=settings.trace_retention_days,
    )
    if result["deleted_days"]:
        logger.info(
            "留痕包保留期清理：删除 {} 个日期目录、{} 个文件、约 {:.1f} MB",
            len(result["deleted_days"]),
            result["deleted_files"],
            result["deleted_bytes"] / 1024 / 1024,
        )
    return result or None


def should_run(last_run_ms: int | None, *, now_ms: int | None = None, interval_s: int = 86_400) -> bool:
    """是否该跑一次。按天节流，进程刚起时立即跑（``last_run_ms is None``）。"""
    if last_run_ms is None:
        return True
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return now_ms - last_run_ms >= interval_s * 1000
