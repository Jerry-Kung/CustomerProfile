#!/usr/bin/env python
"""离线重跑：用归档批次里的**原始取数结果**重放取数层，模型仍真实调用。

## 它解决什么问题

数据接口会变。uat-18 归档于 2026-10-26，同一批号码在本地 API 上重跑时
``GET /data/history/{phone}`` 全部返回 ``[]``、``profile`` 换成了另一种结构，
于是 L0 记录层一条有效内容都拿不到，L4 分诊直接判 ``empty``，Notes 从 43 条掉到 1 条。
**这个差异不是代码改动造成的，却会掩盖真正的代码问题**——两批输入不同，比出来的
结论没有意义。

本脚本把归档里的原始响应原样喂回取数层，让「新代码 vs 旧代码」的比较建立在
**同一份输入**上。

## 三条口径

1. **只回放 HTTP，LLM 真实调用。** 输入侧不可复现，模型侧必须真实——否则测的是
   旧结论而不是新代码。接线走 :func:`wire_replay`（建服务之后再接），不用
   ``build_service(http_replay=...)``：对照组跑的是不含该参数的旧提交，两臂必须用
   同一份测量工具。
2. **录音不靠回放帧。** ``/submit_analyze`` 与 ``/query_analyze`` 的路径里不含号码，
   16 个号码合并成一份 fixture 后帧序会跨案串号。改用**转写缓存**（键为
   ``sha256(file_url + 参数)``，与号码无关），并注入一个不带回放来源的 AUC 客户端。
3. **图片用原始字节。** 归档的 ``response_text`` 是二进制按文本解码的结果，PNG 头
   ``\\x89PNG`` 已被改写；直接从归档 LLM 请求里的 ``data:image/...;base64,`` 还原，
   长度与响应头 ``content-length`` 对得上才采用。

## 用法

    python scripts/replay_from_archive.py --source uat-18-20261026 --limit 1
    python scripts/replay_from_archive.py --source uat-18-20261026
    python scripts/replay_from_archive.py --source uat-18-20261026 --only 13367265521

产物落在 ``experiments/batches/<batch-id>/``，与真实跑批同构（result/nodes/tree/attempts），
因此可以直接用 ``scripts/compare_batches.py`` 对比。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from baseline_artifacts import ExportPolicy, dump_json  # noqa: E402
from run_baseline import (  # noqa: E402
    ENTRY_WORKFLOW,
    GATE_FOR_CONCURRENCY,
    PhoneResult,
    build_settings,
    merge_results,
    run_one,
    write_summary,
)
from customer_profile.execution.auc import AucClient, build_submit_body  # noqa: E402
from customer_profile.execution.transcript_cache import TranscriptCache  # noqa: E402
from customer_profile.replay import ReplaySource, http_key  # noqa: E402
from customer_profile.runner import build_service  # noqa: E402

BATCH_ROOT = REPO_ROOT / "experiments" / "batches"
DEFAULT_SOURCE = "uat-18-20261026"

SKIP_TARGETS = ("/callback/update-profile", "/submit_analyze", "/query_analyze")
"""这些请求不进 HTTP fixture。

- ``update-profile``：基线强制 ``WRITEBACK_ENABLED=false``，该节点根本不会出网。
- ``submit_analyze`` / ``query_analyze``：路径里**不含号码**，16 个号码合并成一份 fixture
  后帧序会跨案串号；而且这两条由 :class:`AucClient` 走「转写缓存」分支，
  不应留下可被命中的回放帧。留一条帧在这里，日后给 AUC 接上回放来源时会被静默错配。
"""


# ---------------------------------------------------------------- 日志

def _log(log_path: Path, line: str) -> None:
    """进度行一律 ASCII：Windows 控制台默认 GBK，中文会把它自己变成一次故障。"""
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    text = f"[{stamp}] {line}"
    print(text, flush=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")


# ---------------------------------------------------------------- 归档读取

def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_source(value: str) -> Path:
    """``--source`` 可以是批次名（``experiments/batches`` 下），也可以是目录路径。"""
    path = Path(value)
    if path.is_dir():
        return path.resolve()
    return (BATCH_ROOT / value).resolve()


def case_dirs(source: Path, phones: Sequence[str]) -> dict[str, Path]:
    """号码 → 运行目录。同号码有多次运行时取**第一份**。

    归档里 ``13207117251__run2`` 两次运行读到的画像不同（第一次写过库），
    取第一份与 ``compare_batches.py`` 的口径一致，两边才对得上。
    """
    runs = source / "runs"
    picked: dict[str, Path] = {}
    for entry in sorted(runs.iterdir()):
        if not entry.is_dir():
            continue
        phone = entry.name.split("__")[0]
        if phone in picked:
            continue
        if phone in phones:
            picked[phone] = entry
    return picked


def http_attempts(run_dir: Path) -> list[dict]:
    payload = _load(run_dir / "attempts.json")
    rows = [item for item in payload if item.get("kind") != "llm"]
    return sorted(rows, key=lambda item: int(item.get("created_at_ms") or 0))


def llm_image_bytes(run_dir: Path) -> list[bytes]:
    """归档 LLM 请求里出现过的图片原始字节（从 data URI 解码）。"""
    out: list[bytes] = []
    for item in _load(run_dir / "attempts.json"):
        if item.get("kind") != "llm":
            continue
        messages = (item.get("request_json") or {}).get("messages") or []
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                url = str((part.get("image_url") or {}).get("url") or "")
                if url.startswith("data:image") and "," in url:
                    try:
                        out.append(base64.b64decode(url.split(",", 1)[1]))
                    except ValueError:
                        continue
    return out


def _is_corrupt_binary(text: str) -> bool:
    """二进制按文本解码后会出现大量替换字符（U+FFFD）。

    判据用转义写法而不是字面量：字面量会让「文件里有没有乱码」这项体检永远报警，
    掩盖真正的编码事故。
    """
    return bool(text) and text.count(chr(0xFFFD)) > len(text) * 0.1


def build_http_frames(run_dir: Path) -> tuple[dict[str, list[dict]], list[str]]:
    """一个号码的 HTTP 固定响应。返回 ``(frames, 无法还原的目标)``。"""
    frames: dict[str, list[dict]] = {}
    unresolved: list[str] = []
    images = llm_image_bytes(run_dir)
    used: set[int] = set()

    for attempt in http_attempts(run_dir):
        request = attempt.get("request_json") or {}
        url = str(request.get("url") or attempt.get("target") or "")
        method = str(request.get("method") or "GET").upper()
        if not url or any(marker in url for marker in SKIP_TARGETS):
            continue

        headers = request.get("response_headers") or {}
        text = attempt.get("response_text") or ""
        frame: dict[str, Any] = {
            "status_code": int(attempt.get("status_code") or 200),
            "text": text,
            "headers": dict(headers),
        }

        if _is_corrupt_binary(text):
            # 按 content-length 与归档里解出的图片字节配对；长度是唯一可靠的对照。
            declared = None
            for key, value in headers.items():
                if str(key).lower() == "content-length":
                    try:
                        declared = int(value)
                    except (TypeError, ValueError):
                        declared = None
            match = None
            for index, blob in enumerate(images):
                if index in used:
                    continue
                if declared is None or len(blob) == declared:
                    match = index
                    break
            if match is None:
                unresolved.append(url)
                continue
            used.add(match)
            frame["text"] = ""
            frame["bytes_base64"] = base64.b64encode(images[match]).decode("ascii")

        frames.setdefault(http_key(method, url), []).append(frame)
    return frames, unresolved


def build_transcript_entries(run_dir: Path) -> list[dict[str, Any]]:
    """从归档的 submit/query 配对还原转写缓存条目。

    云服务的查询应答被翻译过一次（``parse_auc_result``），归档存的是**翻译后**的
    ``auc_result``。这里按逆变换还原成云格式 ``{"result": {"utterances": [...]}}``，
    让复用路径与真实路径走同一个解析函数（``parse_auc_result`` 已实测可往返）。

    **按 ``task_id`` 配对，不按顺序。** 归档里 17652815077 有一次提交被 401 拒掉，
    之后查询节点报「找不到 task_id」——那条 query 没有对应结果。按顺序 zip 会让
    它把后面那次成功的结果错配给前一条录音，而缓存键是录音 URL，错配后**静默**污染。
    """
    attempts = http_attempts(run_dir)
    submits: dict[str, dict] = {}
    for item in attempts:
        url = str((item.get("request_json") or {}).get("url") or "")
        if "/submit_analyze" not in url:
            continue
        if str(item.get("status_code")) != "200":
            continue
        try:
            payload = json.loads(item.get("response_text") or "{}")
        except ValueError:
            continue
        task_id = payload.get("task_id")
        if task_id:
            submits[str(task_id)] = item

    entries: list[dict[str, Any]] = []
    for item in attempts:
        url = str((item.get("request_json") or {}).get("url") or "")
        if "/query_analyze" not in url:
            continue
        body = (item.get("request_json") or {}).get("request_body") or {}
        task_id = str(body.get("task_id") or "")
        submit = submits.get(task_id)
        if submit is None:
            continue
        file_url = (submit.get("request_json") or {}).get("request_body", {}).get("file_url")
        if not file_url:
            continue
        try:
            payload = json.loads(item.get("response_text") or "{}")
        except ValueError:
            continue
        raw_result = payload.get("auc_result")
        if not isinstance(raw_result, str):
            continue
        try:
            utterances = json.loads(raw_result)
        except ValueError:
            continue
        if not utterances:
            entries.append({"file_url": str(file_url), "raw_json": "", "silent": True})
            continue
        cloud = {
            "result": {
                "utterances": [
                    {
                        "text": entry.get("text"),
                        "additions": {
                            "emotion": entry.get("emotion"),
                            "gender": entry.get("gender"),
                            "speaker": entry.get("speaker"),
                            "speech_rate": entry.get("speech_rate"),
                            "volume": entry.get("volume"),
                        },
                    }
                    for entry in utterances
                    if isinstance(entry, dict)
                ]
            }
        }
        entries.append(
            {"file_url": str(file_url), "raw_json": json.dumps(cloud, ensure_ascii=False)}
        )
    return entries


# ---------------------------------------------------------------- 主流程

def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def wire_replay(service: Any, replay: ReplaySource, cache: TranscriptCache) -> None:
    """把回放来源接到已经建好的服务上。

    **为什么不通过 ``build_service(http_replay=...)``**：对照组跑的是**未改代码**的
    提交（``2f5eab8``），那里没有这个参数。两臂必须用同一份测量工具，否则比出来的是
    「工具差异 + 代码差异」。这里只碰三个公开不了的私有字段，在两臂上都成立：

    - ``http_client._replay``：数据接口按路径取固定响应，命中前不出网；
    - ``http_client._transport``：兜底 599，保证未命中时也发不出去；
    - ``http_client._auc``：换成只认转写缓存的客户端——``/submit_analyze`` 的路径里
      没有号码，让它走回放帧会在号码之间串结果。
    """
    from customer_profile.runner import _build_transport

    http = service.http_client
    http._replay = replay
    http._transport = http._transport or _build_transport(replay)
    http._auc = AucClient(
        service.settings,
        transport=http._transport,
        replay=None,
        request_limiter=http._limiter,
        recorder=http._recorder,
        transcript_cache=cache,
    )
    # 留痕里标成回放：输入侧取自归档，不是真实取数。
    service.replay = replay
    tracker = getattr(service, "tracker", None)
    if tracker is not None and hasattr(tracker, "is_replay"):
        tracker.is_replay = True


async def run(args: argparse.Namespace) -> int:
    source = _resolve_source(args.source)
    if not source.is_dir():
        raise SystemExit(f"归档批次不存在：{source}")

    batch_id = args.batch_id or f"{source.name}-replay"
    batch_dir = (REPO_ROOT / "experiments" / "batches" / batch_id).resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    log_path = batch_dir / "logs" / "run.log"

    phones = [
        line.strip()
        for line in (source / "phones.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    only = [item.strip() for item in (args.only or "").split(",") if item.strip()]
    if only:
        phones = [phone for phone in phones if phone in set(only)]
    if args.limit:
        phones = phones[: args.limit]

    cases = case_dirs(source, phones)
    missing = [phone for phone in phones if phone not in cases]
    if missing:
        raise SystemExit(f"归档里找不到这些号码的运行目录：{missing}")

    # ---- 汇总 fixture。数据接口与图片的路径里都带号码或绝对 URL，不会跨案撞键。
    frames: dict[str, list[dict]] = {}
    unresolved: dict[str, list[str]] = {}
    for phone in phones:
        got, bad = build_http_frames(cases[phone])
        for key, items in got.items():
            frames.setdefault(key, []).extend(items)
        if bad:
            unresolved[phone] = bad

    fixture = {"http": frames, "llm": {}}
    dump_json(batch_dir / "fixture.json", fixture)

    # ---- 转写缓存：录音识别不走回放帧（路径无号码，会跨案串号）。
    cache = TranscriptCache(batch_dir / "transcript_cache", read=True, write=True)
    seeded = 0
    for phone in phones:
        for entry in build_transcript_entries(cases[phone]):
            params = build_submit_body(entry["file_url"])["request"]
            cache.store(
                cache.make_entry(
                    entry["file_url"],
                    params,
                    entry["raw_json"],
                    silent=bool(entry.get("silent")),
                )
            )
            seeded += 1

    _log(log_path, f"source = {source.name}")
    _log(log_path, f"batch {batch_id}: {len(phones)} phones")
    _log(log_path, f"http fixture: {len(frames)} endpoints, "
                   f"{sum(len(v) for v in frames.values())} frames")
    _log(log_path, f"transcript cache: {seeded} entries seeded")
    for phone, urls in unresolved.items():
        _log(log_path, f"WARNING unresolved binary response for {phone}: {len(urls)} url(s)")

    policy = ExportPolicy()
    settings = build_settings(batch_dir / "trace.db", read_timeout=args.read_timeout)
    replay = ReplaySource(http=frames, llm=None, strict=True, name=f"archive:{source.name}")

    manifest = {
        "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "archive-replay",
        "source_batch": source.name,
        "workflow_id": args.workflow_id,
        "phones": phones,
        "http_endpoints": sorted(frames),
        "http_frames": sum(len(v) for v in frames.values()),
        "transcript_entries": seeded,
        "unresolved_binary": unresolved,
        "llm": "live",
        "note": (
            "取数层来自归档原始响应（HTTP 回放），模型调用为真实调用。"
            "回写被强制关闭。"
        ),
    }
    dump_json(batch_dir / "manifest.json", manifest)

    service = await build_service(
        settings,
        max_active_nodes=GATE_FOR_CONCURRENCY,
        transcript_cache=cache,
    )
    wire_replay(service, replay, cache)

    results: dict[str, PhoneResult] = {}
    try:
        semaphore = asyncio.Semaphore(max(1, args.concurrency))

        async def worker(phone: str) -> None:
            async with semaphore:
                _log(log_path, f"start {phone}")
                item = await run_one(
                    service,
                    phone,
                    timeout=args.per_phone_timeout,
                    policy=policy,
                    batch_dir=batch_dir,
                    workflow_id=args.workflow_id,
                )
                results[phone] = item
                summary = item.export_summary or {}
                _log(
                    log_path,
                    f"done  {phone}: {item.status} wall={item.wall_ms}ms "
                    f"nodes={summary.get('node_count', 0)} "
                    f"attempts={summary.get('attempt_count', 0)}",
                )
                _log(
                    log_path,
                    f"transcript cache (cumulative): hit={cache.stats.hits} "
                    f"miss={cache.stats.misses} write={cache.stats.writes}",
                )
                write_summary(
                    batch_dir, merge_results(batch_dir, list(results.values())), batch_id
                )

        await asyncio.gather(*(worker(phone) for phone in phones))
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()

    ordered = merge_results(batch_dir, list(results.values()))
    write_summary(batch_dir, ordered, batch_id)
    ok = sum(1 for item in ordered if item.status == "succeeded")
    _log(log_path, f"SUMMARY {ok}/{len(ordered)} succeeded")
    _log(log_path, f"artifacts under {batch_dir}")
    return 0 if ok == len(ordered) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="用归档批次的原始取数结果离线重跑（HTTP 回放、LLM 真实）"
    )
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="归档批次目录名")
    parser.add_argument("--batch-id", default=None, help="新批次目录名（缺省 <source>-replay）")
    parser.add_argument("--only", default=None, help="只跑这些号码，逗号分隔")
    parser.add_argument("--limit", type=int, default=0, help="只取清单前 N 个号码")
    parser.add_argument("--concurrency", type=int, default=2, help="并发数（缺省 2）")
    parser.add_argument(
        "--per-phone-timeout", type=float, default=3600.0, help="单号码超时秒数"
    )
    parser.add_argument("--read-timeout", type=float, default=1800.0, help="HTTP 读超时秒数")
    parser.add_argument("--workflow-id", default=ENTRY_WORKFLOW)
    args = parser.parse_args(argv)

    if not 1 <= args.concurrency <= 4:
        raise SystemExit(f"--concurrency 须在 1..4 之间，实际 {args.concurrency}")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
