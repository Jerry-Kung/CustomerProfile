#!/usr/bin/env python
"""把 UAT 运行台的 ``/traces/export`` 导出（JSONL）整理成与跑批批次同构的归档。

## 输入是什么

导出包是一份 zip，内含::

    README.txt
    manifest.json                    导出元数据（窗口、条数、sha256）
    customer-profile-traces.jsonl    每行一棵**完整**根运行树
    SHA256SUMS.txt

单行结构::

    {"root_run_id": "...", "captured_at_ms": ..., "runs": {run_id: run, ...},
     "definitions": {version_id: 定义快照, ...}}

与旧的 ``all-tasks-complete.json`` 相比有三处不同，本脚本按新结构处理：

1. 子运行不再是 ``child_runs`` 数组，而是 ``runs`` 字典，且**已带**
   ``parent_run_id`` / ``parent_node_id`` / ``call_path``，不需要从 ``node.sub_run_id`` 反推。
2. 定义快照在 ``definitions`` 里按 ``version_id`` 共享，同一文件内每个版本只出现一次，
   且跨行一致，因此整批只落一份 ``source/definitions.json``。
3. 记录里不带 ``depth``，按 ``parent_run_id`` 逐层推导。

## 输出

与 :mod:`ingest_uat_export` 产出的批次同构，因此 ``compare_batches`` / ``rules_report`` /
``probe_*`` / ``replay_from_archive`` 都能直接吃：

- ``runs/<号码>/``（同号码多次运行依次 ``__run2``、``__run3``）：
  ``result.json`` / ``nodes.json`` / ``tree.json`` / ``attempts.json``
- ``phones.txt`` / ``summary.{json,md}`` / ``manifest.json`` / ``README.md``
- ``source/``：原始 zip 原样复制 + ``definitions.json``（按 version_id 去重）
- ``trace.db``：按 ``schema.py`` 的 DDL 重建的批次库，**全量**（节点输入输出不截断）

## 用法

    python scripts/ingest_trace_export.py \
      --zip experiments/uat_data/customer-profile-traces-<window>.zip \
      --out-dir experiments/uat_data/uat-traces-<window>

    # 只做体检不落盘
    python scripts/ingest_trace_export.py --zip <zip> --out-dir <out> --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sqlite3
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile.persistence.schema import (  # noqa: E402
    SCHEMA_STATEMENTS,
    definition_hash,
)

JSONL_NAME = "customer-profile-traces.jsonl"


# ---------------------------------------------------------------- 读写

def _dump(path: Path, payload: Any) -> int:
    """原子写 UTF-8 JSON，返回字节数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)
    return len(text.encode("utf-8"))


def _iter_records(stream) -> Iterator[dict[str, Any]]:
    for line in stream:
        line = line.strip()
        if line:
            yield json.loads(line)


def _phone_of(business_ref: str | None) -> str:
    """``cdp:<手机号>:<批次号>`` → 手机号。不带前缀时原样返回。"""
    parts = (business_ref or "").split(":")
    if len(parts) >= 2 and parts[0] == "cdp":
        return parts[1]
    return business_ref or "unknown"


# ---------------------------------------------------------------- 转换

def _payload_kind(record: dict[str, Any], root: dict[str, Any]) -> str:
    """一份导出记录的载荷完整度：``full`` / ``sliced`` / ``skeleton``。

    导出端点按同一口径写过 ``X-Trace-Payload``，这里逐行复核：批次可能由多次导出拼接
    而成，只看响应头会把混装批次误当成统一档位。
    """
    if record.get("payload_missing") or record.get("payload_state") == "purged":
        return "skeleton"
    if (record.get("payload_mode") or "").lower() == "low":
        return "sliced"
    for run in (record.get("runs") or {}).values():
        for node in run.get("nodes") or []:
            if node.get("payload_truncated") == "low":
                return "sliced"
    return "full"


def _depths(root_id: str, runs: dict[str, dict[str, Any]]) -> dict[str, int]:
    """按 ``parent_run_id`` 逐层推导深度；无父节点的非根运行记 -1。"""
    children: dict[str, list[str]] = defaultdict(list)
    for run_id, run in runs.items():
        parent = run.get("parent_run_id")
        if parent and parent != run_id:
            children[parent].append(run_id)
    depth = {root_id: 0}
    frontier = [root_id]
    while frontier:
        nxt: list[str] = []
        for run_id in frontier:
            for child_id in children.get(run_id) or []:
                if child_id not in depth:
                    depth[child_id] = depth[run_id] + 1
                    nxt.append(child_id)
        frontier = nxt
    for run_id in runs:
        depth.setdefault(run_id, -1)
    return depth


def _case_payloads(record: dict[str, Any]) -> tuple[dict, list[dict], list[dict], list[dict], dict]:
    """一行导出 → (result, nodes, attempts, tree, 统计)。"""
    runs: dict[str, dict[str, Any]] = record["runs"]
    root_id: str = record["root_run_id"]
    root = runs[root_id]
    depth = _depths(root_id, runs)

    outputs = root.get("outputs") or {}
    result1 = outputs.get("result1")
    if not isinstance(result1, str):
        result1 = json.dumps(result1, ensure_ascii=False)

    nodes: list[dict] = []
    attempts: list[dict] = []
    for run_id, run in runs.items():
        for node in run.get("nodes") or []:
            nodes.append({
                "execution_id": node.get("execution_id"),
                "run_id": run_id,
                "node_id": node["node_id"],
                "node_title": node.get("node_title"),
                "node_type": node.get("node_type"),
                "call_path": node.get("call_path") or "",
                "status": node.get("status"),
                "branch": node.get("branch"),
                "error": node.get("error"),
                "queued_at_ms": node.get("queued_at_ms"),
                "started_at_ms": node.get("started_at_ms"),
                "duration_ms": node.get("duration_ms"),
                "sub_run_id": node.get("sub_run_id"),
                "attempt_count": node.get("attempt_count"),
                "workflow_id": run["workflow_id"],
                "depth": depth[run_id],
                "inputs": node.get("inputs_json"),
                "outputs": node.get("outputs_json"),
            })
        for attempt in run.get("attempts") or []:
            item = dict(attempt)
            item["depth"] = depth[run_id]
            attempts.append(item)

    tree: list[dict] = []
    for run_id, run in sorted(runs.items(), key=lambda kv: (depth[kv[0]], kv[0])):
        is_root = run_id == root_id
        tree.append({
            "run_id": run_id,
            "workflow_id": run["workflow_id"],
            "workflow_display_name": run.get("workflow_display_name") or "",
            "parent_run_id": run.get("parent_run_id"),
            "parent_node_id": run.get("parent_node_id"),
            "call_path": "" if is_root else (run.get("call_path") or ""),
            "depth": depth[run_id],
            "status": run.get("status"),
            "duration_ms": run.get("duration_ms"),
            "error": run.get("error"),
            "node_count": len(run.get("nodes") or []),
            "attempt_count": len(run.get("attempts") or []),
        })

    result = {
        "run_id": root_id,
        "workflow_id": root["workflow_id"],
        "workflow_display_name": root.get("workflow_display_name") or "",
        "business_ref": root.get("business_ref"),
        "status": root.get("status"),
        "error": root.get("error"),
        "duration_ms": root.get("duration_ms"),
        "started_at_ms": root.get("started_at_ms"),
        "finished_at_ms": root.get("finished_at_ms"),
        "is_replay": int(bool(root.get("is_replay"))),
        "inputs": root.get("inputs") or {},
        "outputs": outputs,
        "result1": result1,
        "definition": {
            "version_id": root.get("definition_version_id"),
            "workflow_id": root.get("workflow_id"),
            "definition_hash": None,
            "code_commit": None,
            "template_hashes": "{}",
            "found": False,
        },
        "tree_summary": {
            "run_id": root_id,
            "run_count": len(runs),
            "node_count": len(nodes),
            "attempt_count": len(attempts),
            "observed_depth": max(depth.values()) if depth else 0,
            "missing_runs": [],
            "workflow_ids": sorted({run["workflow_id"] for run in runs.values()}),
        },
    }
    stats = {
        "runs": len(runs),
        "nodes": len(nodes),
        "attempts": len(attempts),
        "max_depth": max(depth.values()) if depth else 0,
        "missing_parents": [
            run_id for run_id in runs
            if run_id != root_id and not runs[run_id].get("parent_run_id")
        ],
    }
    return result, nodes, attempts, tree, stats


# ---------------------------------------------------------------- trace.db

def _init_trace_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=MEMORY")
    connection.execute("PRAGMA synchronous=OFF")
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.commit()
    return connection


def _insert_run(connection: sqlite3.Connection, run: dict[str, Any],
                business_ref: str | None) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO workflow_runs (run_id, workflow_id, workflow_display_name,"
        " parent_run_id, parent_node_id, call_path, business_ref, status,"
        " definition_version_id, inputs_json, outputs_json, error, started_at_ms,"
        " finished_at_ms, duration_ms, is_replay, queued_at_ms, claimed_at_ms, claimed_by,"
        " lease_expires_at_ms, created_at_ms)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run["run_id"], run["workflow_id"], run.get("workflow_display_name"),
            run.get("parent_run_id"), run.get("parent_node_id"), run.get("call_path") or "",
            business_ref, run.get("status"), run.get("definition_version_id"),
            json.dumps(run.get("inputs") or {}, ensure_ascii=False),
            json.dumps(run.get("outputs") or {}, ensure_ascii=False),
            run.get("error"), run.get("started_at_ms"), run.get("finished_at_ms"),
            run.get("duration_ms"), int(bool(run.get("is_replay"))),
            run.get("queued_at_ms"), run.get("claimed_at_ms"), run.get("claimed_by"),
            run.get("lease_expires_at_ms"), run.get("created_at_ms") or 0,
        ),
    )


def _insert_nodes(connection: sqlite3.Connection, rows: list[dict]) -> None:
    connection.executemany(
        "INSERT OR IGNORE INTO node_executions (run_id, node_id, node_title, node_type,"
        " call_path, status, branch, inputs_json, outputs_json, error,"
        " queued_at_ms, started_at_ms, duration_ms, sub_run_id, attempt_count)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                row["run_id"], row["node_id"], row["node_title"], row["node_type"] or "unknown",
                row["call_path"], row["status"] or "unknown", row["branch"],
                json.dumps(row["inputs"], ensure_ascii=False),
                json.dumps(row["outputs"], ensure_ascii=False),
                row["error"], row["queued_at_ms"], row["started_at_ms"],
                row["duration_ms"], row["sub_run_id"], row["attempt_count"] or 0,
            )
            for row in rows
        ],
    )


def _insert_attempts(connection: sqlite3.Connection, rows: list[dict]) -> None:
    connection.executemany(
        "INSERT OR IGNORE INTO request_attempts (run_id, node_id, call_path, attempt_no,"
        " kind, target, request_json, response_text, status_code, duration_ms, error,"
        " error_code, usage_json, provider_request_id, replayed, created_at_ms)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                row["run_id"], row["node_id"], row.get("call_path") or "",
                row.get("attempt_no") or 0, row.get("kind") or "http",
                row.get("target"),
                json.dumps(row.get("request_json"), ensure_ascii=False),
                row.get("response_text"), row.get("status_code"), row.get("duration_ms"),
                row.get("error"), row.get("error_code"),
                json.dumps(row.get("usage_json"), ensure_ascii=False),
                row.get("provider_request_id"), int(bool(row.get("replayed"))),
                row.get("created_at_ms") or 0,
            )
            for row in rows
        ],
    )


# ---------------------------------------------------------------- 人读产物

def _summary_md(batch_id: str, rows: list[dict[str, Any]], counts: dict[str, int]) -> str:
    lines = [
        f"# 批次 {batch_id}（UAT traces 导出入库）",
        "",
        f"- 根运行 {counts['root_tasks']} 次，唯一号码 {counts['unique_phones']}，"
        f"全部运行 {counts['runs']}，节点 {counts['nodes']}，出网尝试 {counts['attempts']}",
        "- **不是受控基线**：写回开启、导出不携带 code_commit、仅含导出窗口内的任务。",
        "",
        "| 号码 | 运行目录 | 状态 | 耗时(s) | 子运行 | 节点 | 出网 | 回写 | 备注 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        note = "；".join(row["notes"]) if row["notes"] else ""
        lines.append(
            f"| {row['phone']} | {row['run_dir']} | {row['status']} | "
            f"{row['duration_ms'] // 1000 if row['duration_ms'] else ''} | "
            f"{row['runs']} | {row['nodes']} | {row['attempts']} | {row['writeback']} | {note} |"
        )
    return "\n".join(lines) + "\n"


def _payload_note(kinds: dict[str, int]) -> str:
    """批次头部的载荷说明。全 ``full`` 时不加噪音，其余两种各说清少了什么。"""
    parts = []
    if kinds.get("sliced"):
        parts.append(
            f"{kinds['sliced']} 次为 `low` 档：只有外部响应原文、根输出与诊断切片，"
            "**不含完整节点输出与 LLM 请求体**，不要拿它做新旧两版的逐节点对比"
        )
    if kinds.get("skeleton"):
        parts.append(
            f"{kinds['skeleton']} 次只有骨架（存量已清理或包丢失），节点与尝试载荷为空是"
            "「证据缺失」而不是「管线没有产出」"
        )
    return ("- **载荷完整度**：" + "；".join(parts) + "\n") if parts else ""


def _batch_readme(
    batch_id: str,
    counts: dict[str, int],
    source: dict[str, Any],
    payload_kinds: dict[str, int] | None = None,
) -> str:
    return f"""# {batch_id} —— UAT traces 导出归档

> 本目录是 `experiments/uat_data/` 下的 UAT 运行数据，**整体被 gitignore**，不入库。
> 布局说明见 `../../README.md`。

## 这一批是什么

- **来源**：UAT 运行台 `GET /traces/export`，原始 zip 在 `source/`（sha256 见 `manifest.json`）
- **窗口**：{source.get('window_start')} → {source.get('window_end')}（按根运行创建时刻）
- **规模**：{counts['root_tasks']} 次根运行，{counts['unique_phones']} 个唯一手机号，
  {counts['runs']} 次运行，{counts['nodes']} 条节点执行，{counts['attempts']} 次出网尝试
- **代码版本**：未知（导出不携带 `code_commit`）
{_payload_note(payload_kinds or {})}
## 这不是受控基线

写回开启（update-profile 请求真实写入 UAT 画像库）；导出窗口内没有失败形态的对照，
无法据此估计失败率。不要拿它与受控基线批次直接对比结论。

## 目录结构

```
{batch_id}/
├─ README.md        本文件
├─ manifest.json    批次元数据、来源哈希、定义版本、baseline_readiness
├─ phones.txt       {counts['unique_phones']} 个唯一手机号
├─ summary.json     逐案汇总（机器可读）
├─ summary.md       逐案汇总（人读）
├─ runs/<号码>/     逐案产物：result / nodes / tree / attempts
├─ source/          原始 zip + 去重后的 definitions.json
└─ trace.db         由导出重建的批次库（节点输入输出全量，不截断）
```

## 同号码多次运行

一批窗口内同一号码可能运行多次（本批最多 9 次）。第一次落 `runs/<号码>/`，之后依次
`<号码>__run2`、`<号码>__run3`。`compare_batches` 与 `replay_from_archive` 都只取
**第一份**，需要对齐口径时按同样规则取。

## 本地怎么用

以本目录的 `runs/<号码>/attempts.json` 为准：`response_text` 就是当时的客户全量原始响应。
回放工具 `scripts/replay_from_archive.py --source {batch_id}` 会把它当作固定输入往下跑，
不依赖 UAT 取数接口的当前状态。
"""


# ---------------------------------------------------------------- 主流程

def _open_source(zip_path: Path | None, jsonl_path: Path | None):
    if zip_path is not None:
        archive = zipfile.ZipFile(zip_path)
        return archive, archive.open(JSONL_NAME), archive.read("manifest.json")
    raw = jsonl_path.read_bytes()
    return None, io.BytesIO(raw), b"{}"


def ingest(zip_path: Path | None, jsonl_path: Path | None, out_dir: Path,
           batch_id: str | None, dry_run: bool, make_trace_db: bool) -> dict[str, Any]:
    archive, stream, manifest_bytes = _open_source(zip_path, jsonl_path)
    manifest_in = json.loads(manifest_bytes or b"{}")
    batch_id = batch_id or out_dir.name

    # 第一遍：按根运行开始时刻排序（__run2 的编号必须按时间先后）。
    order: list[tuple[int, int, dict]] = []
    definitions: dict[int, dict] = {}
    payload_kinds: Counter[str] = Counter()
    root_workflows: Counter[str] = Counter()
    for idx, record in enumerate(_iter_records(stream)):
        root = record["runs"][record["root_run_id"]]
        payload_kinds[_payload_kind(record, root)] += 1
        root_workflows[str(root.get("workflow_id") or "")] += 1
        order.append((int(root.get("started_at_ms") or 0), idx, record))
        for version_id, snapshot in (record.get("definitions") or {}).items():
            definitions.setdefault(int(version_id), snapshot)
        if idx and idx % 100 == 0:
            print(f"  read {idx} root traces", flush=True)
    if archive is not None:
        archive.close()
    order.sort(key=lambda item: (item[0], item[1]))

    if dry_run:
        return {"batch_id": batch_id, "root_tasks": len(order),
                "definitions": sorted(definitions), "dry_run": True}

    out_dir.mkdir(parents=True, exist_ok=True)
    source_dir = out_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    if zip_path is not None:
        shutil.copy2(zip_path, source_dir / zip_path.name)

    connection = _init_trace_db(out_dir / "trace.db") if make_trace_db else None
    hash_mismatch: list[int] = []
    for version_id, snapshot in sorted(definitions.items()):
        definition = snapshot.get("definition") or {}
        recomputed = definition_hash(definition)
        if snapshot.get("definition_hash") and snapshot["definition_hash"] != recomputed:
            hash_mismatch.append(version_id)
        if connection is not None:
            connection.execute(
                "INSERT OR IGNORE INTO definition_versions (version_id, workflow_id,"
                " definition_hash, definition_json, template_hashes_json, code_commit,"
                " created_at_ms) VALUES (?,?,?,?,?,?,?)",
                (
                    version_id,
                    definition.get("workflow_id") or snapshot.get("workflow_id"),
                    recomputed, json.dumps(definition, ensure_ascii=False),
                    json.dumps(snapshot.get("template_hashes_json") or {}, ensure_ascii=False),
                    snapshot.get("code_commit"), int(snapshot.get("created_at_ms") or 0),
                ),
            )
    if connection is not None:
        connection.commit()

    seen: Counter[str] = Counter()
    writeback_status: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    for n, (_, _, record) in enumerate(order, start=1):
        root = record["runs"][record["root_run_id"]]
        business_ref = root.get("business_ref")
        phone = _phone_of(business_ref)
        seen[phone] += 1
        run_dir_name = phone if seen[phone] == 1 else f"{phone}__run{seen[phone]}"
        result, nodes, attempts, tree, stats = _case_payloads(record)

        version_id = root.get("definition_version_id")
        snapshot = definitions.get(int(version_id)) if version_id is not None else None
        if snapshot is not None:
            result["definition"]["found"] = True
            result["definition"]["definition_hash"] = snapshot.get("definition_hash")
            result["definition"]["code_commit"] = snapshot.get("code_commit")
            result["definition"]["template_hashes"] = json.dumps(
                snapshot.get("template_hashes_json") or {}, ensure_ascii=False
            )

        case_dir = out_dir / "runs" / run_dir_name
        _dump(case_dir / "result.json", result)
        _dump(case_dir / "nodes.json", nodes)
        _dump(case_dir / "attempts.json", attempts)
        _dump(case_dir / "tree.json", tree)

        if connection is not None:
            for run in record["runs"].values():
                _insert_run(
                    connection, run,
                    business_ref if run["run_id"] == record["root_run_id"] else None,
                )
            _insert_nodes(connection, nodes)
            _insert_attempts(connection, attempts)

        writebacks = [item for item in attempts if "update-profile" in str(item.get("target"))]
        for item in writebacks:
            writeback_status[str(item.get("status_code"))] += 1
        notes = []
        if stats["missing_parents"]:
            notes.append(f"{len(stats['missing_parents'])} 个子运行缺父节点")
        auc_errors = sum(1 for item in attempts if item.get("error_code") == "auc_business_error")
        if auc_errors:
            notes.append(f"auc_business_error {auc_errors} 次")

        rows.append({
            "phone": phone,
            "run_dir": run_dir_name,
            "run_id": result["run_id"],
            "business_ref": business_ref,
            "status": result["status"],
            "started_at": datetime.fromtimestamp(result["started_at_ms"] / 1000)
            .astimezone().isoformat() if result["started_at_ms"] else None,
            "duration_ms": result["duration_ms"],
            "runs": stats["runs"],
            "nodes": stats["nodes"],
            "attempts": stats["attempts"],
            "max_depth": stats["max_depth"],
            "writeback": len(writebacks),
            "notes": notes,
            "tree_summary": result["tree_summary"],
        })
        totals["runs"] += stats["runs"]
        totals["nodes"] += stats["nodes"]
        totals["attempts"] += stats["attempts"]
        if n % 50 == 0 or n == len(order):
            print(f"  wrote {n}/{len(order)}", flush=True)

    if connection is not None:
        connection.commit()
        trace_counts: dict[str, Any] = {}
        for table in ("workflow_runs", "node_executions", "request_attempts",
                      "definition_versions"):
            trace_counts[table] = connection.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        trace_counts["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
        connection.close()
    else:
        trace_counts = {}

    phones = sorted(seen)
    (out_dir / "phones.txt").write_text("\n".join(phones) + "\n",
                                        encoding="utf-8", newline="\n")
    counts = {
        "root_tasks": len(order),
        "unique_phones": len(phones),
        "runs": totals["runs"],
        "nodes": totals["nodes"],
        "attempts": totals["attempts"],
    }
    _dump(out_dir / "summary.json", {
        "batch_id": batch_id,
        "source_exported_at": manifest_in.get("export_started_at"),
        "root_runs": len(order),
        "unique_phones": len(phones),
        "succeeded": sum(1 for row in rows if row["status"] == "succeeded"),
        "results": rows,
    })
    (out_dir / "summary.md").write_text(_summary_md(batch_id, rows, counts),
                                        encoding="utf-8", newline="\n")

    _dump(source_dir / "definitions.json", {
        str(vid): snapshot for vid, snapshot in sorted(definitions.items())
    })
    definition_versions = [
        {
            "version_id": vid,
            "workflow_id": (snapshot.get("definition") or {}).get("workflow_id")
            or snapshot.get("workflow_id"),
            "definition_hash": definition_hash(snapshot.get("definition") or {}),
            "code_commit": snapshot.get("code_commit"),
            "hash_recomputed_match": vid not in hash_mismatch,
        }
        for vid, snapshot in sorted(definitions.items())
    ]

    zip_sha = None
    if zip_path is not None:
        digest = hashlib.sha256()
        with zip_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        zip_sha = digest.hexdigest()

    duplicates = {phone: count for phone, count in seen.items() if count > 1}
    source_block = {
        "archive": f"source/{zip_path.name}" if zip_path is not None else None,
        "archive_sha256": zip_sha,
        "jsonl_sha256": manifest_in.get("sha256"),
        "jsonl_bytes": manifest_in.get("file_bytes"),
        "window_start": manifest_in.get("window_start"),
        "window_end": manifest_in.get("window_end"),
        "selection": manifest_in.get("selection"),
        "export_started_at": manifest_in.get("export_started_at"),
        "root_status_counts": manifest_in.get("root_status_counts"),
        "incremental_export": manifest_in.get("incremental_export"),
        "credential_fields": "redacted",
    }
    manifest = {
        "batch_id": batch_id,
        "created_at": datetime.now().astimezone().isoformat(),
        "origin": "UAT 运行台 GET /traces/export（JSONL，每行一棵根运行树）后整理归档",
        "source": source_block,
        # 从导出行里取，不写死：增量入口（customer_profile_incremental）导出的批次
        # 若标成 pipeline，README 与 manifest 会把来源说错，而数据本身是对的。
        # 一批里混了多个入口时给空串，完整清单在 workflow_ids。
        "workflow_id": (
            next(iter(root_workflows)) if len(root_workflows) == 1 else ""
        ),
        "workflow_ids": dict(root_workflows.most_common()),
        "code_commit": None,
        "phones_count": len(phones),
        "root_run_count": len(order),
        "duplicate_phones": duplicates,
        "counts": counts,
        "payload_kinds": dict(payload_kinds),
        "payload_complete": set(payload_kinds) <= {"full"},
        "trace_db": "batch-local（由导出重建，非原库拷贝）" if make_trace_db else None,
        "trace_db_counts": trace_counts,
        "writeback_status_codes": dict(writeback_status),
        "definition_versions": definition_versions,
        "definition_hash_mismatch": sorted(hash_mismatch),
        "definitions_file": "source/definitions.json（按 version_id 去重；各案 result.json 只引用 id）",
        "remediation": [
            "由 JSONL 拆分为 runs/<号码>/{result,nodes,tree,attempts}.json，与既有批次同构",
            "parent_run_id / call_path 直接取导出行；depth 由父链推导",
            "定义快照按 version_id 去重存 source/definitions.json，并按 schema.definition_hash 复算比对",
            "同号码的多次运行落 <号码>__run2/__run3，按根运行开始时刻排序",
            "凭据字段保持导出中的脱敏值",
        ],
        "baseline_readiness": {
            "eligible": False,
            "blockers": [
                "写回开启：导出内 update-profile 已向 UAT 画像库真实写入",
                "code_commit 为 None：导出不携带提交号，无法确定对应代码版本",
                "仅含导出窗口内的任务，失败任务形态不可见",
            ],
            "usable_for": [
                "输出质量人工核读样本",
                "业务优化问题定位（以 attempts.json 的 response_text 为固定输入）",
                "回放批次的源批次",
            ],
        },
    }
    _dump(out_dir / "manifest.json", manifest)
    (out_dir / "README.md").write_text(
        _batch_readme(batch_id, counts, source_block, payload_kinds),
        encoding="utf-8", newline="\n",
    )
    return {"batch_id": batch_id, "counts": counts, "trace_db_counts": trace_counts,
            "unique_phones": len(phones), "out_dir": str(out_dir)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 UAT /traces/export 的 JSONL 整理成与跑批批次同构的归档",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--zip", help="导出包 zip")
    group.add_argument("--jsonl", help="已解包的 customer-profile-traces.jsonl")
    parser.add_argument("--out-dir", required=True, help="批次输出目录")
    parser.add_argument("--batch-id", default=None, help="缺省取输出目录名")
    parser.add_argument("--dry-run", action="store_true", help="只读体检，不落盘")
    parser.add_argument("--no-trace-db", action="store_true",
                        help="不重建批次 trace.db（省时间与体积）")
    args = parser.parse_args(argv)

    started = time.time()
    report = ingest(
        Path(args.zip).resolve() if args.zip else None,
        Path(args.jsonl).resolve() if args.jsonl else None,
        Path(args.out_dir).resolve(),
        args.batch_id,
        args.dry_run,
        not args.no_trace_db,
    )
    report["elapsed_s"] = round(time.time() - started, 1)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
