"""留痕导出：把一次运行及其整棵子运行树的全部中间结果落盘。

这个模块只做「读库 → 落文件」，不执行工作流、不改任何 src/ 代码——导出用的是既有的
Store 读接口。

四条设计要点：

1. **必须递归。** 根运行的 node_executions 里，子流程节点的 sub_run_id 指向另一行运行，
   那一行有自己的节点与自己的子运行。实测主入口一次运行是一棵 18 个运行、250 条节点
   执行、深度 3 的树，只导根运行会丢掉绝大部分中间结果。
2. **定位键是 (node_id, call_path)。** 迭代类工作流的同一个静态 node_id 每轮都会重跑
   （call_path 形如 /0/0/iter:1775811095168/0），只按 node_id 定位会把各轮混成一堆。
   产物里保留 call_path 正是为此。
3. **原子落盘。** 先写同目录下的临时文件再 os.replace。批次可能连续跑几个小时，中途被
   中断时，已完成的号码必须是完整 JSON，而不是半截。
4. **显式 UTF-8。** result1 是约 31.7 KB 的中文 JSON；依赖平台默认编码会抛
   UnicodeEncodeError 或落成乱码。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

MAX_INLINE_BYTES_DEFAULT = 524_288
"""单条节点输出超过此字节数就替换为摘要。默认 512 KB，在实测最大单条（438 KB）之上。"""

MAX_DEPTH_DEFAULT = 8
"""递归深度上限。实测深度 3，设 8 是廉价的安全阀——数据异常时避免无限递归。"""

PREVIEW_CHARS = 200
"""截断时保留的原始字符数。够看出这是什么内容，又不至于把体积带回来。"""

EXPORT_FILENAMES = ("result.json", "nodes.json", "tree.json", "attempts.json")


def _fallback(obj: Any) -> str:
    """不可序列化对象的回落表示。转字符串而不是抛错，避免导出被一个奇怪对象卡住。"""
    return f"<{type(obj).__name__}>"


def _canonical(value: Any) -> str:
    """稳定序列化：sort_keys 保证同一内容永远得到同一份字节，哈希才有意义。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=_fallback)


def serialized_size(value: Any) -> int:
    """值落成 UTF-8 JSON 后的字节数。"""
    return len(_canonical(value).encode("utf-8"))


def truncate_value(value: Any, limit: int) -> Any:
    """超过 limit 字节的值替换为摘要：类型、字节数、sha256 与预览。

    保留 sha256 是关键——需要比对「这个大字段有没有变」时，哈希比原文更准且便宜；
    要读原文就按 run_id + node_id + call_path 回 trace.db 取，那几个定位键在
    tree.json 与 nodes.json 里都有。

    ``limit <= 0`` 表示「一律摘要」，不是「一律保留」：调用方用它把大字段整体换成
    指纹（例如 ``include_attempt_bodies=False`` 时的请求体）。
    """
    payload = _canonical(value)
    size = len(payload.encode("utf-8"))
    if limit > 0 and size <= limit:
        return value
    return {
        "_truncated": True,
        "type": type(value).__name__,
        "bytes": size,
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "preview": payload[:PREVIEW_CHARS],
    }


def shrink_mapping(value: Any, limit: int) -> Any:
    """对映射逐值判断是否截断；非映射则整体判断。

    逐值而不是整体：节点输出里可能有一个巨大的字段和一堆小字段，整体判断会因为一个大
    字段把其余小字段一起摘要掉，而那些小字段恰恰是比对时最想直接读的。
    """
    if isinstance(value, Mapping):
        return {key: truncate_value(item, limit) for key, item in value.items()}
    return truncate_value(value, limit)


@dataclass(frozen=True)
class ExportPolicy:
    """导出的取舍开关。"""

    max_inline_bytes: int = MAX_INLINE_BYTES_DEFAULT
    max_depth: int = MAX_DEPTH_DEFAULT
    include_attempt_bodies: bool = True
    """是否归档请求体与响应正文全文。

    缺省 True：请求体（LLM 提示词）是体积最大的单项，但它是「模型当时看到了什么」的
    唯一直接证据，基线要长期沉淀，值得留存。置 False 时只留摘要与哈希。
    """


@dataclass
class RunTreeBundle:
    """一次运行的整棵树导出内容，尚未落盘。"""

    root_run_id: str
    result: dict[str, Any] = field(default_factory=dict)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    tree: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    observed_depth: int = 0
    missing_runs: list[str] = field(default_factory=list)
    """sub_run_id 指向但库里查不到的行。不为空说明留痕不完整，需在汇总里暴露。"""

    @property
    def run_count(self) -> int:
        return len(self.tree)

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    def to_summary(self) -> dict[str, Any]:
        return {
            "run_id": self.root_run_id,
            "run_count": self.run_count,
            "node_count": self.node_count,
            "attempt_count": self.attempt_count,
            "observed_depth": self.observed_depth,
            "missing_runs": list(self.missing_runs),
            "workflow_ids": sorted(
                {str(item.get("workflow_id")) for item in self.tree}
            ),
        }


def _attempt_payload(
    row: Mapping[str, Any], run_id: str, depth: int, policy: ExportPolicy
) -> dict[str, Any]:
    """一条出网尝试的归档形态。

    request_json / usage_json 已由 Store.list_attempts 解析成对象，response_text 是原样文本。
    """
    payload = dict(row)
    payload["run_id"] = run_id
    payload["depth"] = depth
    if not policy.include_attempt_bodies:
        if "request_json" in payload:
            payload["request_digest"] = truncate_value(payload.pop("request_json"), 0)
        if "response_text" in payload:
            payload["response_digest"] = truncate_value(payload.pop("response_text"), 0)
    return payload


async def _definition_of(store: Any, run: Mapping[str, Any]) -> dict[str, Any]:
    """取该次运行所用的定义快照。

    基线是否自洽全看这个：definition_hash 变了说明中途换过工作流定义，code_commit 变了
    说明换过代码。
    """
    version_id = run.get("definition_version_id")
    if version_id is None:
        return {}
    row = await store.definition_version(int(version_id))
    if row is None:
        return {"version_id": version_id, "found": False}
    return {
        "version_id": row.get("version_id"),
        "workflow_id": row.get("workflow_id"),
        "definition_hash": row.get("definition_hash"),
        "code_commit": row.get("code_commit"),
        "template_hashes": row.get("template_hashes_json"),
        "found": True,
    }


def _result_payload(
    run: Mapping[str, Any], bundle: RunTreeBundle, definition: dict[str, Any]
) -> dict[str, Any]:
    """result.json 的内容：根运行行 + 最终输出 + 树与版本信息。"""
    outputs = run.get("outputs_json")
    if not isinstance(outputs, Mapping):
        outputs = {}
    return {
        "run_id": run.get("run_id"),
        "workflow_id": run.get("workflow_id"),
        "workflow_display_name": run.get("workflow_display_name"),
        "business_ref": run.get("business_ref"),
        "status": run.get("status"),
        "error": run.get("error"),
        "duration_ms": run.get("duration_ms"),
        "started_at_ms": run.get("started_at_ms"),
        "finished_at_ms": run.get("finished_at_ms"),
        "is_replay": run.get("is_replay"),
        "inputs": run.get("inputs_json"),
        "outputs": outputs,
        "result1": outputs.get("result1"),
        "definition": definition,
        "tree_summary": bundle.to_summary(),
    }


async def collect_run_tree(
    store: Any, root_run_id: str, policy: ExportPolicy
) -> RunTreeBundle:
    """从库里收集整棵运行树。BFS，逐层下钻。"""
    root = await store.get_run(root_run_id)
    if root is None:
        raise KeyError(f"库中没有运行 {root_run_id!r}")

    bundle = RunTreeBundle(root_run_id=root_run_id)
    seen: set[str] = set()
    frontier: list[tuple[str, int]] = [(root_run_id, 0)]

    while frontier:
        next_frontier: list[tuple[str, int]] = []
        for run_id, depth in frontier:
            if run_id in seen:
                continue
            seen.add(run_id)
            row = root if run_id == root_run_id else await store.get_run(run_id)
            if row is None:
                bundle.missing_runs.append(run_id)
                continue

            bundle.observed_depth = max(bundle.observed_depth, depth)
            nodes = await store.list_node_executions(run_id)
            attempts = await store.list_attempts(run_id)

            bundle.tree.append(
                {
                    "run_id": run_id,
                    "workflow_id": row.get("workflow_id"),
                    "workflow_display_name": row.get("workflow_display_name"),
                    "parent_run_id": row.get("parent_run_id"),
                    "parent_node_id": row.get("parent_node_id"),
                    "call_path": row.get("call_path"),
                    "depth": depth,
                    "status": row.get("status"),
                    "duration_ms": row.get("duration_ms"),
                    "error": row.get("error"),
                    "node_count": len(nodes),
                    "attempt_count": len(attempts),
                }
            )

            for node in nodes:
                item = dict(node)
                item["run_id"] = run_id
                item["workflow_id"] = row.get("workflow_id")
                item["depth"] = depth
                item["inputs"] = shrink_mapping(
                    item.pop("inputs_json", None), policy.max_inline_bytes
                )
                item["outputs"] = shrink_mapping(
                    item.pop("outputs_json", None), policy.max_inline_bytes
                )
                bundle.nodes.append(item)

            for attempt in attempts:
                bundle.attempts.append(_attempt_payload(attempt, run_id, depth, policy))

            if depth >= policy.max_depth:
                children = await store.list_child_runs(run_id)
                if children:
                    bundle.missing_runs.extend(
                        f"{run_id} 的子运行（超出深度上限 {policy.max_depth}）"
                        for _ in children
                    )
                continue
            for child in await store.list_child_runs(run_id):
                child_id = child.get("run_id")
                if child_id and child_id not in seen:
                    next_frontier.append((child_id, depth + 1))
        frontier = next_frontier

    bundle.result = _result_payload(root, bundle, await _definition_of(store, root))
    return bundle


def dump_json(path: Path, payload: Any) -> int:
    """原子写入 UTF-8 JSON（ensure_ascii=False、LF）。返回写入字节数。

    临时文件放在目标同目录：跨文件系统的 os.replace 不是原子的。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=_fallback).encode(
        "utf-8"
    )
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return len(body)


def leftover_temp_files(target_dir: Path) -> list[Path]:
    """目录里遗留的临时文件。有残留说明上次写入被中断，值得在汇总里提示。"""
    if not target_dir.is_dir():
        return []
    return sorted(p for p in target_dir.iterdir() if p.suffix == ".tmp")


async def export_run_tree(
    store: Any, root_run_id: str, target_dir: Path, policy: ExportPolicy
) -> tuple[RunTreeBundle, dict[str, int]]:
    """收集并落盘四个产物文件。返回 (bundle, 每个文件的字节数)。

    文件分工：result.json 最终结果与版本信息、nodes.json 全部节点执行、tree.json 子运行
    索引、attempts.json 出网尝试。四者都以 run_id 与 (node_id, call_path) 互相可定位。
    """
    bundle = await collect_run_tree(store, root_run_id, policy)
    target_dir.mkdir(parents=True, exist_ok=True)
    written = {
        name: dump_json(target_dir / name, payload)
        for name, payload in (
            ("result.json", bundle.result),
            ("nodes.json", bundle.nodes),
            ("tree.json", bundle.tree),
            ("attempts.json", bundle.attempts),
        )
    }
    return bundle, written
