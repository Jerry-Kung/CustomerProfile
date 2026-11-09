"""记录差分：本轮 history 与基线已消费键集合之间，谁是新来的。

身份键算法在**一处**定义（``01`` §5），夹具构建脚本与 P1 的调度层都调这里：

- **过程资料**（带 ``source_key``：跟进、订单、录音、聊天……）：``渠道 + source_key``
  是逻辑身份，再加 ``source_version`` 才是物理键。版本递增算**改版**，不是「删一条加一条」。
- **基础渠道**（``source_key`` 为空，每次触发重抓快照）：``渠道 + 去掉 fetched_at 后的
  内容哈希``。按 ``id`` 或 ``fetched_at`` 差分会把每一对相邻运行都判成「有新增」——
  ``04`` §1.2 实测过的坑，310 对里全部误判。

上游标记 ``operation=removed`` 的行按「不存在」处理（与 ``_manifest_index`` 同口径），
否则一条已删除的记录会以「新增」的身份重新进来。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

VOLATILE_KEYS = frozenset({"fetched_at"})
"""不参与内容哈希的键。基础渠道每轮抓取都会刷新它。"""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def strip_volatile(value: Any) -> Any:
    """递归剥掉 ``fetched_at``。基础渠道的 ``raw_payload`` 顶上有它，记录里也可能嵌套。"""
    if isinstance(value, dict):
        return {
            key: strip_volatile(item)
            for key, item in value.items()
            if key not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value


def content_digest(row: dict) -> str:
    """基础渠道的内容指纹（不含 ``fetched_at``）。"""
    material = _canonical(strip_volatile(row.get("raw_payload")))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def is_removed(row: dict) -> bool:
    """上游已删除的记录。与 ``ontology_payload.is_removed`` 同判据，此处避免跨包导入。"""
    raw = row.get("raw_payload")
    return isinstance(raw, dict) and str(raw.get("operation") or "") == "removed"


def logical_key(row: dict) -> str:
    """逻辑身份：改版前后相同，用于把「删一条 + 加一条」认成「改版」。"""
    channel = str(row.get("channel") or "")
    source_key = row.get("source_key")
    if source_key:
        return f"{channel}|{source_key}"
    return f"{channel}|hash:{content_digest(row)}"


def record_key(row: dict) -> str:
    """物理键：进基线 ``consumed_keys_json`` 的那一个。"""
    logical = logical_key(row)
    if row.get("source_key"):
        return f"{logical}|v{row.get('source_version')}"
    return logical


def _usable(rows: Iterable[dict]) -> list[dict]:
    return [row for row in rows if isinstance(row, dict) and not is_removed(row)]


def _describe(row: dict) -> dict[str, str]:
    return {
        "channel": str(row.get("channel") or ""),
        "logical_key": logical_key(row),
        "key": record_key(row),
        "source_version": "" if row.get("source_version") is None else str(row["source_version"]),
        "source_key": str(row.get("source_key") or ""),
        "created_at": str(row.get("created_at") or ""),
    }


def diff_by_keys(previous_keys: Iterable[str] | None, current_rows: Iterable[dict]) -> dict[str, Any]:
    """按**已消费键集合**差分（基线只存键，不存原始 history 行）。

    :func:`diff_histories` 需要上一轮的原始记录才能判「同逻辑键换版本 = 改版」；基线里
    只有键，因此这里退一步：**键不存在即新增**。这会漏掉「版本递增」这一类改版在
    P1 的档位判定上造成的差异——但 P1 只做 T0 / T3，T0 的判据恰恰是「键集合无差异」，
    与 :func:`diff_histories` 在 T0 上的结论完全一致。P2 引入底座与合并账本后，
    若需要区分改版，再让基线多存一份 ``logical_key`` 索引。
    """
    current = _usable(current_rows)
    if previous_keys is None:
        return {
            "added": [_describe(row) for row in current],
            "changed": [],
            "removed": [],
            "unchanged": [],
            "added_records": current,
            "changed_records": [],
            "no_baseline": True,
        }

    previous = set(previous_keys)
    cur_by_key = {record_key(row): row for row in current}
    added: list[dict] = []
    unchanged: list[dict] = []
    added_records: list[dict] = []
    for key, row in cur_by_key.items():
        if key in previous:
            unchanged.append(_describe(row))
        else:
            added.append(_describe(row))
            added_records.append(row)
    removed = [_describe_key(key) for key in sorted(previous - set(cur_by_key))]
    return {
        "added": added,
        "changed": [],
        "removed": removed,
        "unchanged": unchanged,
        "added_records": added_records,
        "changed_records": [],
        "no_baseline": False,
    }


def _describe_key(key: str) -> dict[str, str]:
    """把一条只存在于基线里的键还原成描述。键本身形如 ``渠道|source_key|v3``。"""
    parts = key.split("|")
    return {
        "channel": parts[0] if parts else "",
        "logical_key": "|".join(parts[:2]) if len(parts) >= 2 else key,
        "key": key,
        "source_version": parts[2][1:] if len(parts) >= 3 and parts[2].startswith("v") else "",
        "source_key": parts[1] if len(parts) >= 2 and not parts[1].startswith("hash:") else "",
        "created_at": "",
    }


def consumed_keys(rows: Iterable[dict]) -> list[str]:
    """本轮处理过的记录键集合（进基线）。去重并排序，保证同一输入产出同一份字节。"""
    return sorted({record_key(row) for row in _usable(rows)})


def diff_histories(previous_rows: Iterable[dict] | None, current_rows: Iterable[dict]) -> dict[str, Any]:
    """比对基线已消费记录与本轮 history。

    ``previous_rows`` 为 ``None`` 表示无基线，此时 ``current`` 全部算新增（调用方据此落 T3）。

    返回 ``added`` / ``changed`` / ``removed`` / ``unchanged``，每项是
    :func:`_describe` 的字典；``added_records`` / ``changed_records`` 附原始行，
    供调度层判断「新增里有没有录音渠道」。
    """
    current = _usable(current_rows)
    if previous_rows is None:
        return {
            "added": [_describe(row) for row in current],
            "changed": [],
            "removed": [],
            "unchanged": [],
            "added_records": current,
            "changed_records": [],
            "no_baseline": True,
        }

    previous = _usable(previous_rows)
    prev_by_key = {record_key(row): row for row in previous}
    cur_by_key = {record_key(row): row for row in current}
    prev_logical = {logical_key(row) for row in previous}
    cur_logical = {logical_key(row) for row in current}

    added: list[dict] = []
    changed: list[dict] = []
    unchanged: list[dict] = []
    removed: list[dict] = []
    added_records: list[dict] = []
    changed_records: list[dict] = []

    for key, row in cur_by_key.items():
        if key in prev_by_key:
            unchanged.append(_describe(row))
            continue
        if logical_key(row) in prev_logical:
            # 同一条过程资料换了版本（或同渠道同内容哈希之外的改版）。
            changed.append(_describe(row))
            changed_records.append(row)
        else:
            added.append(_describe(row))
            added_records.append(row)

    for key, row in prev_by_key.items():
        if key in cur_by_key:
            continue
        if logical_key(row) in cur_logical:
            continue  # 已在 changed 里记过一次
        removed.append(_describe(row))

    return {
        "added": added,
        "changed": changed,
        "removed": removed,
        "unchanged": unchanged,
        "added_records": added_records,
        "changed_records": changed_records,
        "no_baseline": False,
    }
