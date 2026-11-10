"""把一棵运行树的运行时数据裁成一个自包含的归档包。

本模块**不碰数据库**：输入是已读出的运行树（结构与 :meth:`Store.execution_trace`
的返回值一致），输出是一个可直接 `json.dumps` 的字典。写盘也只做「字节落盘」这一件事。
这种切分让「裁成什么」可以只用纯函数测，不必起库、不必跑工作流。

三条口径，改动时不要破坏：

1. **按能不能重建来裁**（``low``），不是按大小随手砍。诊断切片只取单处常量
   :data:`DIAGNOSTIC_FIELDS` 列出的键，其余中间数据一律不留。
2. **多模态只存引用**。``data:`` URI 换成长度、哈希与来源 URL；URL 查不到就如实写
   ``null`` 并计数，**不猜**。
3. **包内失败不会连累库**。写盘失败由调用方决定保留库内载荷，本模块只负责抛出。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

PACKAGE_VERSION = 1
"""包格式版本。字段增删改必须升版；读侧按版本决定是否兼容。"""

DIAGNOSTIC_FIELDS: tuple[str, ...] = (
    "notes_updates_json",
    "facts_json",
    "candidates_json",
    "to_confirm_json",
    "rule_trace_json",
    "rejected_json",
    "checker_status",
    "enum_illegal",
    "unbacked_fact",
    "placeholder_removed",
)
"""``low`` 档在所有节点上保留的节点输出键。

**集中在常量里，不散落到裁剪逻辑**——这份清单就是「``low`` 还能回答什么」的定义，
分散之后没有人能一眼看出某个键是被刻意排除还是漏掉了。

``callback_json`` 明确不收：它与根运行输出逐字节相同，留它是纯冗余。
"""

NODE_DIAGNOSTIC_FIELDS: Mapping[str, tuple[str, ...]] = {
    "incremental_dispatch": (
        "tier",
        "tier_label",
        "tier_reason",
        "baseline_found",
        "compat_ok",
        "added_count",
        "removed_count",
        "unchanged_count",
        "touched_channels",
        "stage",
        "previous_level",
        "previous_level_detail",
        "compat_key",
        "adapt_error",
        "history_records",
        "base_stats_json",
    ),
    "incremental_tier_branch": ("tier",),
    "incremental_baseline_commit": (
        "baseline_saved",
        "baseline_error",
        "baseline_keys",
        "baseline_tier",
    ),
    "pipeline_l1_input": (
        "new_record_count",
        "incremental",
        "chars",
        "guide_chars",
        "section_count",
    ),
    "pipeline_l1_inject": (
        "base_merged",
        "base_retired",
        "added",
        "replaced",
        "injected",
        "license_injected",
        "parse_ok",
        "merge_rule_trace_json",
    ),
    "l3_skip_express": ("skip", "reason"),
    "pipeline_level_apply": (
        "level",
        "level_detail_json",
        "level_reverted",
        "stage",
        "rule_clamped",
        "evidence_fragments_removed",
        "parse_ok",
    ),
}
"""``low`` 档**按节点**额外保留的键，与 :data:`DIAGNOSTIC_FIELDS` 取并集。

**为什么按节点而不是加进全局清单**：``tier``、``stage``、``reason``、``parse_ok`` 这类
短键在别的节点上也存在，但语义不同（``pipeline_l2_project`` 的 ``tier`` 是证据档位，
不是增量档位）。全局清单只能表达「任何节点上都有诊断意义的键」；增量证据是「只有这个
节点才有」的，混进全局清单会让全量路径的节点莫名其妙多留几个键，也说不出为什么。

**这份清单的准入标准**：参与回答「这次为什么走这一档、底座补了多少、基线写没写成功、
表达跳过命中没有、等级为什么变」这五个问题，且**单案体积 << 1 KB**。刻意排除的大键：

- ``incremental_dispatch.incremental_ctx``（实测 25 KB）——它是喂给 L1 的上下文，
  由 ``base_stats_json`` 与入参可复核；
- ``incremental_dispatch.callback_json``（实测 9 KB）——与根运行输出逐字节相同；
- ``pipeline_l1_input.output``（实测 32 KB）——拼好的提示词正文；
- ``pipeline_l1_inject.claims_json``（实测 20 KB）——L1 账本全量，``facts_json`` 已定稿；
- ``l3_skip_express.sections_json``——命中跳过时是九个板块全文，体积无上界。

``PACKAGE_VERSION`` **不因这份清单而升版**：包的形状（manifest 与节点对象的结构）没变，
变的是切片内容。读侧按键取值，旧包缺这些键时读回空，不需要也不应该让存量包失效。
"""

REPEATED_INPUT_KEYS: frozenset[str] = frozenset({"customer_data"})
"""子运行入参里按整份重复的键，打包时去掉。

真正的客户数据快照在 ``pipeline_l0_records`` 的输出里；子运行再带一份只是同一份数据
在树里重复十几次。只列已知的键，不做「超过多少字节就删」的通用启发式——那会把
偶发的大字段也一并砍掉，且砍掉之后没人能解释为什么。
"""

RAW_INPUT_NODE_IDS: frozenset[str] = frozenset({"pipeline_l0_records"})
"""``low`` 档**原样保留**输出的节点。

取数层的记录是「当时喂进去的那一份客户资料」。虽然 history 接口能重取，取到的却是
**当前**值而不是当时的值，所以 ``low`` 把它整份留住——这是「问题为什么发生」的另一半
证据（另一半是 ``response_text``）。
"""

REPEATED_INPUT_MIN_BYTES = 4096
"""``REPEATED_INPUT_KEYS`` 生效的最小体积。小值留着，删了也省不了多少，反而让包更难读。"""

END_NODE_TYPE = "end"
"""出口节点。其输出与所在运行的输出由调度层合并，包内不再单存一份。"""

_EXPIRING_URL_MARKERS: tuple[str, ...] = (
    "Signature=",
    "Expires=",
    "X-Amz-",
    "token=",
    "Token=",
)
"""URL 带这些参数通常意味着有时效，30 天后可能重下不到。命中只告警，不改写。"""

ATTR_MEDIA = "_media"
"""媒体引用对象的判别键。"""

_TZ_CN = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 时间


def iso_ms(value: int | None) -> str | None:
    """毫秒时间戳 → 带 ``+08:00`` 的 ISO8601。

    包里额外写一份人能直接读的时间，免得拿到包的人手算毫秒；毫秒列本身仍在
    ``runs`` / ``nodes`` 里，两者不互相替代。
    """
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=_TZ_CN).isoformat(timespec="milliseconds")


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _archive_day(created_at_ms: int | None) -> str:
    """包按根运行创建日分目录（东八区），便于按天整目录删除。"""
    if created_at_ms is None:
        return datetime.now(tz=_TZ_CN).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(created_at_ms / 1000, tz=_TZ_CN).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 媒体外化


def _parse_data_uri(value: str) -> tuple[str, bytes] | None:
    """``data:<mime>;base64,<payload>`` → (mime, 原始字节)。不是 data URI 时返回 None。"""
    if not value.startswith("data:") or ";base64," not in value:
        return None
    header, _, payload = value.partition(",")
    mime = header[len("data:") :].split(";", 1)[0] or "application/octet-stream"
    try:
        return mime, base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        # 坏 base64 不当作媒体：原样留着，让它在包里可见，而不是悄悄丢失。
        return None


def url_may_expire(url: str | None) -> bool:
    if not url:
        return False
    return any(marker in url for marker in _EXPIRING_URL_MARKERS)


def _media_ref(mime: str, raw: bytes, digest: str, url: str | None) -> dict[str, Any]:
    return {
        ATTR_MEDIA: mime,
        "bytes": len(raw),
        "sha256": digest,
        "url": url,
        "source": "download" if url else "unknown",
    }


def _externalize(value: Any, media_index: Mapping[str, str], stats: dict[str, Any]) -> Any:
    """递归把 ``data:`` URI 换成引用；其余值原样返回。

    只处理字符串叶子，不试图理解字段语义——节点输出的 ``$files`` 与 LLM 请求里的
    ``image_url.url`` 都以内联串的形式出现，同一条规则即可覆盖。
    """
    if isinstance(value, str):
        parsed = _parse_data_uri(value)
        if parsed is None:
            return value
        mime, raw = parsed
        digest = hashlib.sha256(raw).hexdigest()
        url = media_index.get(digest)
        stats["count"] += 1
        if url is None:
            stats["unresolved"] += 1
        elif url_may_expire(url):
            stats["url_may_expire"] = True
        return _media_ref(mime, raw, digest, url)
    if isinstance(value, dict):
        return {
            key: _externalize(item, media_index, stats) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_externalize(item, media_index, stats) for item in value]
    if isinstance(value, tuple):
        return [_externalize(item, media_index, stats) for item in value]
    return value


# ---------------------------------------------------------------- 裁剪


def _strip_repeated_inputs(inputs: Any) -> Any:
    """子运行入参去掉整份重复的客户数据。"""
    if not isinstance(inputs, dict):
        return inputs
    kept = {}
    for key, value in inputs.items():
        if key in REPEATED_INPUT_KEYS:
            try:
                size = len(json.dumps(value, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                size = 0
            if size >= REPEATED_INPUT_MIN_BYTES:
                continue
        kept[key] = value
    return kept


def _slice_outputs(outputs: Any, node_id: str = "") -> dict[str, Any]:
    """``low`` 档的节点输出：只留诊断字段。

    全局清单与节点专属清单取并集。节点专属的键**只在这个节点上**保留——同名短键在
    别的节点上语义不同（见 :data:`NODE_DIAGNOSTIC_FIELDS`）。
    """
    if not isinstance(outputs, dict):
        return {}
    keep = DIAGNOSTIC_FIELDS + NODE_DIAGNOSTIC_FIELDS.get(node_id, ())
    return {key: outputs[key] for key in keep if key in outputs}


def build_package(
    trace: Mapping[str, Any],
    *,
    mode: str,
    media_index: Mapping[str, str] | None = None,
    code_commit: str | None = None,
) -> dict[str, Any]:
    """把一棵运行树裁成包结构。``mode`` 只接受 ``high`` / ``low``。

    ``trace`` 的形状与 ``Store.execution_trace`` 一致：``runs`` 是 run_id → 运行字典的
    映射，运行字典里是 ``inputs`` / ``outputs``；``nodes`` / ``attempts`` 挂在各自运行
    下面，JSON 字段已解析为对象。
    """
    if mode not in ("high", "low"):
        raise ValueError(f"未知的留痕存储模式 {mode!r}；只支持 high / low")

    index = media_index or {}
    media_stats: dict[str, Any] = {"count": 0, "unresolved": 0, "url_may_expire": False}
    omitted: list[str] = ["end_node_outputs"]

    runs_out: list[dict[str, Any]] = []
    nodes_out: list[dict[str, Any]] = []
    attempts_out: list[dict[str, Any]] = []

    for run_id, run in trace["runs"].items():
        entry = dict(run)
        if entry.get("parent_run_id"):
            entry["inputs"] = _strip_repeated_inputs(entry.get("inputs"))
        entry["nodes"] = []
        entry["attempts"] = []
        entry["child_run_ids"] = list(entry.get("child_run_ids") or [])

        for node in run.get("nodes") or []:
            item = dict(node)
            outputs = item.get("outputs_json")
            if item.get("node_type") == END_NODE_TYPE:
                # 与所在运行的 outputs 逐字节相同，包里不单存。
                item["outputs_json"] = None
            elif mode == "low" and item.get("node_id") in RAW_INPUT_NODE_IDS:
                # 原样输入不进切片：它是「当时喂进去的是什么」的唯一副本。
                item["outputs_json"] = _externalize(outputs, index, media_stats)
            elif mode == "low":
                sliced = _slice_outputs(outputs, str(item.get("node_id") or ""))
                item["outputs_json"] = sliced
                item["payload_truncated"] = "low"
                # 留下「这个节点本来有几个键」。切片之后只剩一两个键时，
                # 读的人分不清是「节点就只有这些产出」还是「被裁掉了」。
                item["outputs_truncated_from"] = (
                    len(outputs) if isinstance(outputs, dict) else 0
                )
            else:
                item["outputs_json"] = _externalize(outputs, index, media_stats)
            item["inputs_json"] = _externalize(
                item.get("inputs_json"), index, media_stats
            )
            nodes_out.append(item)
            entry["nodes"].append(item["execution_id"])

        for attempt in run.get("attempts") or []:
            item = dict(attempt)
            if mode == "low":
                item["request_json"] = None
            else:
                item["request_json"] = _externalize(
                    item.get("request_json"), index, media_stats
                )
            attempts_out.append(item)
            entry["attempts"].append(item["attempt_id"])

        entry["outputs"] = _externalize(entry.get("outputs"), index, media_stats)
        runs_out.append(entry)

    if mode == "low":
        omitted.extend(["node_outputs", "requests"])

    root_id = trace.get("root_run_id")
    root = next((r for r in runs_out if r["run_id"] == root_id), runs_out[0] if runs_out else {})
    created_at = root.get("created_at_ms")

    manifest = {
        "package_version": PACKAGE_VERSION,
        "persist_mode": mode,
        "root_run_id": root_id,
        "workflow_id": root.get("workflow_id"),
        "business_ref": root.get("business_ref"),
        "status": root.get("status"),
        "timestamps": {
            "created_at": iso_ms(root.get("created_at_ms")),
            "started_at": iso_ms(root.get("started_at_ms")),
            "finished_at": iso_ms(root.get("finished_at_ms")),
        },
        "written_at": iso_ms(_now_ms()),
        "code_commit": code_commit,
        "counts": {
            "runs": len(runs_out),
            "nodes": len(nodes_out),
            "attempts": len(attempts_out),
        },
        "definition_version_ids": sorted(
            {
                r.get("definition_version_id")
                for r in runs_out
                if r.get("definition_version_id") is not None
            }
        ),
        "media": {
            "count": media_stats["count"],
            "url_may_expire": bool(media_stats["url_may_expire"]),
        },
        "unresolved_media": media_stats["unresolved"],
        "omitted": omitted,
        "archive_day": _archive_day(created_at),
    }

    return {
        "manifest": manifest,
        "runs": runs_out,
        "nodes": nodes_out,
        "attempts": attempts_out,
        "definitions": dict(trace.get("definitions") or {}),
    }


# ---------------------------------------------------------------- 落盘


def package_path(archive_dir: Path, root_run_id: str, created_at_ms: int | None) -> Path:
    return Path(archive_dir) / _archive_day(created_at_ms) / f"{root_run_id}.json.gz"


def write_package(
    package: Mapping[str, Any], path: Path, *, compress_level: int = 6
) -> dict[str, Any]:
    """原子写盘：临时文件 → fsync → replace。返回体积与 sha256。

    **先写成功再允许清库**（调用方的顺序责任，见设计稿 §3）：任何一步失败都抛异常，
    此时目标文件要么不存在、要么仍是旧的完整包。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(package, ensure_ascii=False, default=str).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()

    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as raw:
            with gzip.GzipFile(
                fileobj=raw, mode="wb", compresslevel=int(compress_level)
            ) as gz:
                gz.write(payload)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": digest,
    }


# ---------------------------------------------------------------- 读取


def read_package(path: Path) -> dict[str, Any]:
    """读回一个包。文件不存在或格式坏时抛异常，由调用方决定降级方式。"""
    with gzip.open(path, "rb") as handle:
        data = json.loads(handle.read().decode("utf-8"))
    version = (data.get("manifest") or {}).get("package_version")
    if version != PACKAGE_VERSION:
        raise ValueError(
            f"包格式版本不兼容：文件为 {version!r}，本代码支持 {PACKAGE_VERSION}"
        )
    return data


def iter_archive_days(archive_dir: Path) -> Iterable[Path]:
    """按日期目录产出（用于保留期清理）。只产出形如 ``YYYY-MM-DD`` 的目录。"""
    root = Path(archive_dir)
    if not root.exists():
        return []
    result = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            datetime.strptime(entry.name, "%Y-%m-%d")
        except ValueError:
            continue
        result.append(entry)
    return sorted(result)
