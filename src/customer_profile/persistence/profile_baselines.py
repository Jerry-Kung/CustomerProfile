"""回传体基线：按手机号一行，保存上次**实际发出**的 ``analysis_result``。

**为什么单独一张表**（``02`` §1.1）：回传体是业务数据，寿命与客户相同；而
``workflow_runs.outputs_json`` 在 ``finalize_tree`` 收尾时会被置空、运行包 30 天清理
（``TRACE_RETENTION_DAYS``）。放错了表等于没存。

**为什么不是版本链**（``01`` §2.1）：不做三方比对、不识别未锁定的人工修改，只留
「我们上次发出去的那一份」用于补缺与证据沿用。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .store import Store

JSON_FIELDS = ("consumed_keys_json", "callback_json")


@dataclass(slots=True)
class ProfileBaseline:
    """一个手机号的基线。"""

    phone: str
    compat_key: str = ""
    source_run_id: str | None = None
    updated_at_ms: int = 0
    stage: str = ""
    deal_level: str = ""
    deal_level_detail: str = ""
    consumed_keys: tuple[str, ...] = ()
    callback: dict[str, Any] = field(default_factory=dict)
    """回传体全文（顶层 ``phone`` / ``batch_id`` / ``analysis_result`` 三键）。"""

    def as_row(self) -> tuple[Any, ...]:
        return (
            self.phone,
            self.compat_key,
            self.source_run_id,
            self.updated_at_ms or int(time.time() * 1000),
            self.stage,
            self.deal_level,
            self.deal_level_detail,
            json.dumps(list(self.consumed_keys), ensure_ascii=False),
            json.dumps(self.callback, ensure_ascii=False),
        )

    def consumed_rows(self) -> list[dict]:
        """把已消费键还原成 ``diff_histories`` 能吃的形态。

        **只还原到「键」这一层**：基线里没有原始 history 行，也不需要——差分只按
        ``(渠道, source_key, source_version)`` 或内容哈希比对。返回的每一行带一个
        ``_baseline_key`` 标记，由 :func:`customer_profile.incremental.diff.diff_by_keys`
        直接消费。
        """
        return [{"_baseline_key": key} for key in self.consumed_keys]


def _row_to_baseline(row: Mapping[str, Any]) -> ProfileBaseline:
    def _obj(name: str) -> Any:
        raw = row.get(name)
        if isinstance(raw, str) and raw:
            try:
                return json.loads(raw)
            except ValueError:
                return None
        return raw

    keys = _obj("consumed_keys_json")
    callback = _obj("callback_json")
    return ProfileBaseline(
        phone=str(row.get("phone") or ""),
        compat_key=str(row.get("compat_key") or ""),
        source_run_id=row.get("source_run_id"),
        updated_at_ms=int(row.get("updated_at_ms") or 0),
        stage=str(row.get("stage") or ""),
        deal_level=str(row.get("deal_level") or ""),
        deal_level_detail=str(row.get("deal_level_detail") or ""),
        consumed_keys=tuple(str(k) for k in keys) if isinstance(keys, list) else (),
        callback=callback if isinstance(callback, dict) else {},
    )


class BaselineRepository:
    """基线表的读写。全部走 :class:`~customer_profile.persistence.store.Store` 的单连接线程。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def load(self, phone: str) -> ProfileBaseline | None:
        """读一个手机号的基线。缺失或损坏都返回 ``None``——调用方据此落 T3。"""
        if not phone:
            return None
        rows = await self.store._read(
            "SELECT * FROM profile_baselines WHERE phone = ?", (str(phone),)
        )
        if not rows:
            return None
        baseline = _row_to_baseline(dict(rows[0]))
        if not baseline.callback or not isinstance(baseline.callback.get("analysis_result"), dict):
            # 结构异常：当作没有基线。宁可多跑一次全量，也不要拿半份底座往下叠。
            return None
        return baseline

    async def save(self, baseline: ProfileBaseline) -> None:
        """单行覆盖写。不保留历史——这是刻意的（``01`` §2.1）。"""
        await self.store._write(
            "INSERT INTO profile_baselines (phone, compat_key, source_run_id, updated_at_ms, "
            " stage, deal_level, deal_level_detail, consumed_keys_json, callback_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (phone) DO UPDATE SET "
            " compat_key = excluded.compat_key, "
            " source_run_id = excluded.source_run_id, "
            " updated_at_ms = excluded.updated_at_ms, "
            " stage = excluded.stage, "
            " deal_level = excluded.deal_level, "
            " deal_level_detail = excluded.deal_level_detail, "
            " consumed_keys_json = excluded.consumed_keys_json, "
            " callback_json = excluded.callback_json",
            baseline.as_row(),
        )

    async def delete(self, phone: str) -> None:
        await self.store._write(
            "DELETE FROM profile_baselines WHERE phone = ?", (str(phone),)
        )

    async def count(self) -> int:
        rows = await self.store._read("SELECT COUNT(*) AS n FROM profile_baselines")
        return int(rows[0]["n"]) if rows else 0


def baseline_path(store: Store) -> Path:
    """基线表所在库的路径。仅供脚本与排障使用，不做任何 I/O。"""
    return store.path


def keys_of(baseline: ProfileBaseline | None) -> set[str]:
    return set(baseline.consumed_keys) if baseline else set()


def summarize(baseline: ProfileBaseline | None) -> dict[str, Any]:
    """给运行台与日志用的一句话摘要。不含回传体正文。"""
    if baseline is None:
        return {"found": False}
    return {
        "found": True,
        "compat_key": baseline.compat_key,
        "stage": baseline.stage,
        "deal_level": baseline.deal_level,
        "consumed_keys": len(baseline.consumed_keys),
        "updated_at_ms": baseline.updated_at_ms,
        "source_run_id": baseline.source_run_id,
    }


def merge_keys(existing: Iterable[str], new: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(existing) | set(new)))
