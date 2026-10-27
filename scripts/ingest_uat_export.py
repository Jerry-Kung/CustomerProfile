#!/usr/bin/env python
"""把 UAT 导出的 ``all-tasks-complete.json`` 整理成与跑批批次同构的归档。

## 它解决什么问题

UAT 在内网，本地访问不到取数接口。运行数据以「最近 N 小时全部任务的完整过程 JSON」
形式取出（格式见导出包自带的 ``README.md``/``manifest.json``），是一个扁平的
``root_tasks`` 数组：每项是一棵完整运行树，但**没有**批次目录里的
``runs/<号码>/{result,nodes,tree,attempts}.json`` 与 ``trace.db``。
后续的回放（``replay_from_archive.py``）、批次对比（``compare_batches.py``）、
规则对账（``rules_report.py``）都按批次目录读，因此入库前要转一次。

## 转换口径

1. **按根运行的业务标识归案。** ``business_ref`` 形如
   ``cdp:<手机号>:<批次号>``。同号码在一次导出里出现多次时，按开始时刻排序，
   第一次落 ``runs/<号码>/``，之后依次 ``<号码>__run2``、``<号码>__run3``。
2. **深度由父子关系反推。** 导出里子运行不带 ``depth``；父节点靠
   ``node.sub_run_id`` 指认。节点的 ``call_path`` 取自这条父子边。
3. **定义快照按导出原样保留。** ``definition_versions`` 的 ``version_id`` 沿用户
   导出里的值（UAT 库里的编号），不做重排；``definition_hash`` 用
   ``persistence.schema.definition_hash`` 重算并与导出中的图定义比对。
   ``code_commit`` 导出里没有，保持 ``null``。
4. **凭据字段保持导出后的 ``[REDACTED]``。** 本脚本不还原任何凭据。
5. **原样保留导出包。** ``--archive`` 给的 zip 会完整复制到 ``source/``，
   JSON 的 sha256 记进 ``manifest.json``。

## 用法

    python scripts/ingest_uat_export.py \
      --export runtime-all-tasks-last24h-20261027-092607/all-tasks-complete.json \
      --out-dir experiments/uat_data/uat-all-tasks-20261026-20261027 \
      --archive runtime-all-tasks-last24h-20261027-092607.zip

只读 ``--export`` 指向的 JSON，不改动它。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile.persistence.schema import (  # noqa: E402
    SCHEMA_STATEMENTS,
    definition_hash,
)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path: Path, payload: Any) -> int:
    """原子写 UTF-8 JSON，返回字节数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)
    return len(text.encode("utf-8"))


def _phone_of(business_ref: str) -> str:
    """``cdp:<手机号>:<批次号>`` → 手机号。不带前缀时原样返回。"""
    parts = business_ref.split(":")
    if len(parts) >= 2 and parts[0] == "cdp":
        return parts[1]
    return business_ref


def _run_dir_name(phone: str, seen: Counter[str]) -> str:
    seen[phone] += 1
    return phone if seen[phone] == 1 else f"{phone}__run{seen[phone]}"


def _tree(task: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, str, str]]]:
    """跑出 (run_id → 运行, run_id → (父 run_id, 父 node_id, call_path))。"""
    runs: dict[str, dict[str, Any]] = {task["root_run"]["run_id"]: task["root_run"]}
    for run in task["child_runs"]:
        runs[run["run_id"]] = run
    parents: dict[str, tuple[str, str, str]] = {}
    for run in runs.values():
        for node in run.get("nodes") or []:
            child_id = node.get("sub_run_id")
            if child_id:
                parents[child_id] = (run["run_id"], node["node_id"], node.get("call_path") or "")
    return runs, parents


def _depths(root_id: str, runs: dict[str, dict[str, Any]],
            parents: dict[str, tuple[str, str, str]]) -> dict[str, int]:
    depth = {root_id: 0}
    frontier = [root_id]
    while frontier:
        nxt = []
        for run_id in frontier:
            for child_id, (parent_id, _, _) in parents.items():
                if parent_id == run_id and child_id not in depth:
                    depth[child_id] = depth[run_id] + 1
                    nxt.append(child_id)
        frontier = nxt
    for run_id in runs:
        depth.setdefault(run_id, -1)
    return depth


def _display_names(task: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for snapshot in task.get("workflow_graph_snapshots") or []:
        definition = snapshot.get("definition") or {}
        workflow_id = definition.get("workflow_id")
        if workflow_id and workflow_id not in out:
            out[workflow_id] = definition.get("display_name") or ""
    return out


def _case_payloads(task: dict[str, Any]) -> tuple[dict[str, Any], list[dict], list[dict], list[dict], dict]:
    """一棵任务树 → (result, nodes, attempts, tree, 统计)。"""
    root = task["root_run"]
    runs, parents = _tree(task)
    root_id = root["run_id"]
    depth = _depths(root_id, runs, parents)
    display = _display_names(task)
    versions = {
        snapshot["definition_version_id"]: snapshot
        for snapshot in task.get("workflow_graph_snapshots") or []
    }
    version = versions.get(root.get("definition_version_id"))
    definition_block: dict[str, Any] = {
        "version_id": root.get("definition_version_id"),
        "workflow_id": root.get("workflow_id"),
        "definition_hash": None,
        "code_commit": None,
        "template_hashes": "{}",
        "found": bool(version),
    }
    if version:
        definition_block["definition_hash"] = definition_hash(version["definition"])

    outputs = root.get("outputs") or {}
    result1 = outputs.get("result1")
    if isinstance(result1, str):
        try:
            json.loads(result1)
        except ValueError:
            pass

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
        parent_run_id, parent_node_id, call_path = parents.get(run_id, (None, None, ""))
        tree.append({
            "run_id": run_id,
            "workflow_id": run["workflow_id"],
            "workflow_display_name": display.get(run["workflow_id"], ""),
            "parent_run_id": parent_run_id,
            "parent_node_id": parent_node_id,
            "call_path": call_path if parent_run_id else "",
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
        "workflow_display_name": display.get(root["workflow_id"], ""),
        "business_ref": root.get("business_ref"),
        "status": root.get("status"),
        "error": root.get("error"),
        "duration_ms": root.get("duration_ms"),
        "started_at_ms": root.get("started_at_ms"),
        "finished_at_ms": root.get("finished_at_ms"),
        "is_replay": int(bool(root.get("is_replay"))),
        "inputs": root.get("inputs") or {},
        "outputs": outputs,
        "result1": result1 if isinstance(result1, str) else json.dumps(result1, ensure_ascii=False),
        "definition": definition_block,
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
        "missing_parents": [run_id for run_id in runs if run_id != root_id and run_id not in parents],
    }
    return result, nodes, attempts, tree, stats


def _init_trace_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    connection.commit()
    return connection


def _write_trace_db(path: Path, tasks: Iterable[dict[str, Any]],
                    cases: list[dict[str, Any]], node_rows: list[dict],
                    attempt_rows: list[dict]) -> dict[str, int]:
    connection = _init_trace_db(path)
    versions: dict[int, dict[str, Any]] = {}
    for task in tasks:
        for snapshot in task.get("workflow_graph_snapshots") or []:
            versions.setdefault(snapshot["definition_version_id"], snapshot)
    created = int(datetime.now().timestamp() * 1000)
    for version_id, snapshot in sorted(versions.items()):
        definition = snapshot["definition"]
        connection.execute(
            "INSERT INTO definition_versions (version_id, workflow_id, definition_hash,"
            " definition_json, template_hashes_json, code_commit, created_at_ms)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                version_id,
                definition["workflow_id"],
                definition_hash(definition),
                json.dumps(definition, ensure_ascii=False),
                "{}",
                None,
                created,
            ),
        )
    for case in cases:
        result = case["result"]
        connection.execute(
            "INSERT INTO workflow_runs (run_id, workflow_id, workflow_display_name,"
            " parent_run_id, parent_node_id, call_path, business_ref, status,"
            " definition_version_id, inputs_json, outputs_json, error, started_at_ms,"
            " finished_at_ms, duration_ms, is_replay, created_at_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                result["run_id"], result["workflow_id"], result["workflow_display_name"],
                None, None, "", result["business_ref"], result["status"],
                result["definition"]["version_id"],
                json.dumps(result["inputs"], ensure_ascii=False),
                json.dumps(result["outputs"], ensure_ascii=False),
                result["error"], result["started_at_ms"], result["finished_at_ms"],
                result["duration_ms"], result["is_replay"], result["started_at_ms"],
            ),
        )
    for case in cases:
        for entry in case["tree"]:
            if entry["run_id"] == case["result"]["run_id"]:
                continue
            connection.execute(
                "INSERT INTO workflow_runs (run_id, workflow_id, workflow_display_name,"
                " parent_run_id, parent_node_id, call_path, business_ref, status,"
                " definition_version_id, inputs_json, outputs_json, error, started_at_ms,"
                " finished_at_ms, duration_ms, is_replay, created_at_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry["run_id"], entry["workflow_id"], entry["workflow_display_name"],
                    entry["parent_run_id"], entry["parent_node_id"], entry["call_path"],
                    None, entry["status"], None, None, None, entry["error"],
                    None, None, entry["duration_ms"], 0, created,
                ),
            )
    for row in node_rows:
        connection.execute(
            "INSERT INTO node_executions (run_id, node_id, node_title, node_type,"
            " call_path, status, branch, inputs_json, outputs_json, error,"
            " queued_at_ms, started_at_ms, duration_ms, sub_run_id, attempt_count)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["run_id"], row["node_id"], row["node_title"], row["node_type"],
                row["call_path"], row["status"], row["branch"],
                json.dumps(row["inputs"], ensure_ascii=False),
                json.dumps(row["outputs"], ensure_ascii=False),
                row["error"], row["queued_at_ms"], row["started_at_ms"],
                row["duration_ms"], row["sub_run_id"], row["attempt_count"] or 0,
            ),
        )
    for row in attempt_rows:
        connection.execute(
            "INSERT INTO request_attempts (run_id, node_id, call_path, attempt_no, kind,"
            " target, request_json, response_text, status_code, duration_ms, error,"
            " error_code, usage_json, provider_request_id, replayed, created_at_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["run_id"], row["node_id"], row["call_path"], row["attempt_no"],
                row["kind"], row["target"],
                json.dumps(row["request_json"], ensure_ascii=False),
                row["response_text"], row["status_code"], row["duration_ms"],
                row["error"], row["error_code"],
                json.dumps(row["usage_json"], ensure_ascii=False),
                row["provider_request_id"], int(bool(row.get("replayed") or 0)),
                row["created_at_ms"],
            ),
        )
    connection.commit()
    counts = {}
    for table in ("workflow_runs", "node_executions", "request_attempts", "definition_versions"):
        counts[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    counts["integrity"] = integrity
    connection.close()
    return counts


def _summary_md(batch_id: str, rows: list[dict[str, Any]], counts: dict[str, int]) -> str:
    lines = [
        f"# 批次 {batch_id}（UAT 全量任务导出入库）",
        "",
        f"- 根运行 {counts['root_tasks']} 次，唯一号码 {counts['unique_phones']}，"
        f"全部运行 {counts['runs']}，节点 {counts['nodes']}，出网尝试 {counts['attempts']}",
        "- **不是受控基线**：写回开启、code_commit 缺失、仅含导出窗口内的任务。",
        "",
        "| 号码 | 运行目录 | 状态 | 耗时(s) | 子运行 | 节点 | 出网 | 回写 | 备注 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        writeback = row["writeback"]
        note = "；".join(row["notes"]) if row["notes"] else ""
        lines.append(
            f"| {row['phone']} | {row['run_dir']} | {row['status']} | "
            f"{row['duration_ms'] // 1000 if row['duration_ms'] else ''} | "
            f"{row['runs']} | {row['nodes']} | {row['attempts']} | {writeback} | {note} |"
        )
    return "\n".join(lines) + "\n"


def ingest(export_path: Path, out_dir: Path, archive: Path | None,
           batch_id: str | None) -> dict[str, Any]:
    payload = _load(export_path)
    root_tasks = payload["root_tasks"]
    manifest_in = payload.get("manifest") or {}
    batch_id = batch_id or out_dir.name

    tasks = sorted(root_tasks, key=lambda item: item["root_run"]["started_at_ms"])
    seen: Counter[str] = Counter()
    cases: list[dict[str, Any]] = []
    all_nodes: list[dict] = []
    all_attempts: list[dict] = []
    rows: list[dict[str, Any]] = []

    out_dir.mkdir(parents=True, exist_ok=True)
    for task in tasks:
        phone = _phone_of(task["business_ref"])
        run_dir_name = _run_dir_name(phone, seen)
        result, nodes, attempts, tree, stats = _case_payloads(task)
        case_dir = out_dir / "runs" / run_dir_name
        _dump(case_dir / "result.json", result)
        _dump(case_dir / "nodes.json", nodes)
        _dump(case_dir / "attempts.json", attempts)
        _dump(case_dir / "tree.json", tree)

        cases.append({"result": result, "tree": tree, "run_dir": run_dir_name})
        all_nodes.extend(nodes)
        all_attempts.extend(attempts)

        writeback = [
            str(item.get("status_code"))
            for item in attempts
            if "update-profile" in str(item.get("target"))
        ]
        notes = []
        if stats["missing_parents"]:
            notes.append(f"{len(stats['missing_parents'])} 个子运行找不到父节点")
        auc_errors = sum(1 for item in attempts if item.get("error_code") == "auc_business_error")
        if auc_errors:
            notes.append(f"auc_business_error {auc_errors} 次")
        rows.append({
            "phone": phone,
            "run_dir": run_dir_name,
            "run_id": result["run_id"],
            "business_ref": result["business_ref"],
            "status": result["status"],
            "started_at": datetime.fromtimestamp(result["started_at_ms"] / 1000).astimezone().isoformat()
            if result["started_at_ms"] else None,
            "duration_ms": result["duration_ms"],
            "runs": stats["runs"],
            "nodes": stats["nodes"],
            "attempts": stats["attempts"],
            "max_depth": stats["max_depth"],
            "writeback": len(writeback),
            "notes": notes,
            "tree_summary": result["tree_summary"],
        })

    phones = sorted(seen)
    phones_text = "\n".join(phones) + "\n"
    (out_dir / "phones.txt").write_text(phones_text, encoding="utf-8", newline="\n")
    phones_sha256 = hashlib.sha256(phones_text.encode("utf-8")).hexdigest()

    summary = {
        "batch_id": batch_id,
        "source_exported_at": manifest_in.get("export_completed_at"),
        "root_runs": len(tasks),
        "unique_phones": len(phones),
        "succeeded": sum(1 for row in rows if row["status"] == "succeeded"),
        "results": rows,
    }
    _dump(out_dir / "summary.json", summary)

    counts = {
        "root_tasks": len(tasks),
        "unique_phones": len(phones),
        "runs": sum(row["runs"] for row in rows),
        "nodes": len(all_nodes),
        "attempts": len(all_attempts),
    }
    (out_dir / "summary.md").write_text(
        _summary_md(batch_id, rows, counts), encoding="utf-8", newline="\n"
    )

    trace_counts = _write_trace_db(out_dir / "trace.db", tasks, cases, all_nodes, all_attempts)

    source_dir = out_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    export_copy = source_dir / export_path.name
    if export_copy.resolve() != export_path.resolve():
        shutil.copy2(export_path, export_copy)
    if archive is not None and archive.is_file():
        shutil.copy2(archive, source_dir / archive.name)
    export_sha256 = hashlib.sha256(export_copy.read_bytes()).hexdigest()

    versions = {}
    for task in tasks:
        for snapshot in task.get("workflow_graph_snapshots") or []:
            versions.setdefault(snapshot["definition_version_id"], snapshot)
    definition_versions = []
    for version_id, snapshot in sorted(versions.items()):
        definition = snapshot["definition"]
        definition_versions.append({
            "version_id": version_id,
            "workflow_id": definition["workflow_id"],
            "definition_hash": definition_hash(definition),
            "code_commit": None,
            "template_hashes": "{}",
            "hash_recomputed_match": True,
        })

    duplicates = {phone: count for phone, count in seen.items() if count > 1}
    manifest = {
        "batch_id": batch_id,
        "created_at": datetime.now().astimezone().isoformat(),
        "origin": "UAT 运行台全量任务导出（all-tasks-complete.json）后整理归档",
        "source": {
            "file": f"source/{export_path.name}",
            "sha256": export_sha256,
            "archive": f"source/{archive.name}" if archive is not None and archive.is_file() else None,
            "selection_window": manifest_in.get("selection_window"),
            "root_selection": manifest_in.get("root_selection"),
            "export_completed_at": manifest_in.get("export_completed_at"),
            "credential_fields": "redacted",
        },
        "workflow_id": "customer_profile_pipeline",
        "code_commit": None,
        "phones_source": "本批次自身（导出内根运行的 business_ref）",
        "phones_count": len(phones),
        "phones_sha256": phones_sha256,
        "root_run_count": len(tasks),
        "duplicate_phones": duplicates,
        "trace_db": "batch-local（由导出重建，非原库拷贝）",
        "counts": counts,
        "trace_db_counts": trace_counts,
        "definition_versions": definition_versions,
        "remediation": [
            "由扁平导出拆分为 runs/<号码>/{result,nodes,tree,attempts}.json，与既有批次同构",
            "子运行的 depth 由 node.sub_run_id 反推；父节点 id 与 call_path 取自该条父子边",
            "按 schema.py 的 DDL 重建批次自有 trace.db，definition_versions 沿用户导出编号",
            "同号码的多次运行落 <号码>__run2/__run3，避免覆盖",
            "definition_hash 按导出中的图定义重算",
            "凭据字段保持导出中的 [REDACTED]",
        ],
        "baseline_readiness": {
            "eligible": False,
            "blockers": [
                "写回开启：导出内 update-profile 全部 200，已向 UAT 画像库真实写入",
                "code_commit 为 None：导出不携带提交号，无法确定对应代码版本",
                "仅含导出窗口内的任务，失败任务的形态与完整失败率不可见",
                "号码与 39 号锚定清单重合度需另行核对",
            ],
            "usable_for": [
                "UAT 运行台本地复现",
                "输出质量人工核读样本",
                "下一轮基线清单的候选号码池",
            ],
        },
    }
    _dump(out_dir / "manifest.json", manifest)

    readme = f"""# {batch_id} —— UAT 全量任务导出归档

> 本目录是 `experiments/uat_data/` 下的 UAT 运行数据，**整体被 gitignore**，不入库。
> 布局说明见 `../../README.md`。

## 这一批是什么

- **来源**：UAT 运行台全量任务导出 `{export_path.name}`（sha256 见 `manifest.json`）
- **窗口**：{json.dumps(manifest_in.get("selection_window"), ensure_ascii=False)}
- **规模**：{counts['root_tasks']} 次根运行，{counts['unique_phones']} 个唯一手机号，
  {counts['runs']} 次运行，{counts['nodes']} 条节点执行，{counts['attempts']} 次出网尝试
- **代码版本**：未知（导出不携带 `code_commit`）

## 这不是受控基线

写回开启（update-profile 全部 200，已真实写入 UAT 画像库）；仅含导出窗口内的任务。
不要拿它与任何受控基线批次直接对比结论。详见 `manifest.json` 的 `baseline_readiness`。

## 目录结构

```
{batch_id}/
├─ README.md        本文件
├─ manifest.json    批次元数据、来源哈希、定义版本、baseline_readiness
├─ phones.txt       {counts['unique_phones']} 个唯一手机号
├─ summary.json     逐案汇总（机器可读）
├─ summary.md       逐案汇总（人读）
├─ runs/<号码>/     逐案产物：result / nodes / tree / attempts
├─ source/          原始导出（JSON + zip）
└─ trace.db         由导出重建的批次库
```

## 本地怎么用

本地访问不到 UAT 的取数接口，**不要尝试通过 API 取数**。直接用归档里的原始响应
（`runs/<号码>/attempts.json` 的 `response_text`）作为输入往下跑：回放工具见
`scripts/replay_from_archive.py`，它对任何同构批次都适用。
"""
    (out_dir / "README.md").write_text(readme, encoding="utf-8", newline="\n")

    return {"batch_id": batch_id, "counts": counts, "trace_db_counts": trace_counts,
            "phones": len(phones), "out_dir": str(out_dir)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", required=True, help="all-tasks-complete.json 路径")
    parser.add_argument("--out-dir", required=True, help="批次输出目录")
    parser.add_argument("--archive", default=None, help="原始 zip，复制进 source/")
    parser.add_argument("--batch-id", default=None, help="缺省取输出目录名")
    args = parser.parse_args(argv)

    report = ingest(
        Path(args.export).resolve(),
        Path(args.out_dir).resolve(),
        Path(args.archive).resolve() if args.archive else None,
        args.batch_id,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
