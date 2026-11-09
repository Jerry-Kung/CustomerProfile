#!/usr/bin/env python
"""增量画像的序列回放：按时点逐轮跑，先建基线，再走增量入口。

## 它解决什么问题

增量是否靠谱，只能在**同一客户的连续几轮**上才看得出来：第一轮没有基线必须全量，
第二轮才有机会命中 T0/T1/T2；某一轮到底是哪一档、产物与「同时点全量」差多少，
都要靠逐轮复现。单点跑一次只能验证「能跑通」。

## 三条口径（与 ``replay_from_archive.py`` 一致）

1. **取数层取自归档。** 每个时点的 history / profile 用夹具里的原文；同一次归档运行
   的其余出网响应（极光、朋友圈、录音清单等）一并回放，否则那些渠道会在本地落空、
   把「夹具不全」伪装成「增量失效」。录音走转写缓存，不走回放帧。
2. **模型真实调用。** 回放来源只接到 HTTP 客户端；LLM 一律出网。拿旧结论比新旧代码
   是没有意义的。
3. **回写强制关闭**（``build_settings`` 里写死）。基线照写——那是本地库，不是生产。

## 逐轮做什么

- ``t1`` 走**增量入口**（``customer_profile_incremental``）：此时没有基线，它必然落 T3
  （全量重建），并在收尾写基线。这既是对客户的第一轮画像，也是基线的唯一来源。
- ``t2`` 起仍走增量入口，由 dispatch 读基线定档。

**为什么 t1 不能跑现行全量管线**：基线由增量入口的 ``baseline_commit`` 节点写，
``customer_profile_pipeline`` 里没有这个节点。让 t1 跑管线等于一轮也没写基线，从 t2 起
全部退化成 T3，整条增量臂变成空跑——而且**不会报错**。

**等价性对照**：同一组夹具，把 ``--t1-workflow`` 与 ``--next-workflow`` 都换成
``customer_profile_pipeline``，跑成另一份批次；两边用 ``compare_incremental.py`` 对比。

- 每个时点落在 ``<batch>/t<N>/runs/<phone>/``，与真实跑批同构，可直接喂
  ``compare_incremental.py``（把它指向 ``<batch>/t2`` 就是该时点的批次目录）。

用法::

    python scripts/replay_incremental.py --sequence experiments/incremental/sequences/13125000455
    python scripts/replay_incremental.py --sequence experiments/incremental/sequences/13125000455 \\
        --batch-id inc-0455-full --next-workflow customer_profile_pipeline
    python scripts/replay_incremental.py --set sequences --only 15344445417 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from baseline_artifacts import ExportPolicy, dump_json  # noqa: E402
from replay_from_archive import (  # noqa: E402
    _log,
    build_http_frames,
    build_transcript_entries,
    wire_replay,
)
from run_baseline import (  # noqa: E402
    ENTRY_WORKFLOW,
    GATE_FOR_CONCURRENCY,
    build_settings,
    run_one,
    write_summary,
)
from customer_profile.execution.transcript_cache import TranscriptCache  # noqa: E402
from customer_profile.replay import ReplaySource, http_key  # noqa: E402
from customer_profile.runner import build_service  # noqa: E402
from customer_profile.workflows.customer_profile_incremental import (  # noqa: E402
    DISPATCH,
    HISTORY_PATH,
    PROFILE_PATH,
    WORKFLOW_ID as INCREMENTAL_WORKFLOW,
)

BATCH_ROOT = REPO_ROOT / "experiments" / "uat_data"
FIXTURE_ROOT = REPO_ROOT / "experiments" / "incremental"
DEFAULT_SET = "sequences"
DEFAULT_SOURCE_BATCH = "uat-traces-20261102-20261103"
"""夹具构建时用的归档批次（``sequence.json`` 里有同名记录，缺省只在它缺失时兜底）。"""


# ---------------------------------------------------------------- 夹具读取

def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sequence_dirs(root: Path) -> dict[str, Path]:
    """``{phone: 序列目录}``。目录名是号码，与批次 ``runs/`` 同形。"""
    if not root.is_dir():
        return {}
    return {
        path.name: path
        for path in sorted(root.iterdir())
        if path.is_dir() and (path / "sequence.json").is_file()
    }


def _timepoint_dirs(sequence_dir: Path, limit: int = 0) -> list[Path]:
    """``t1``、``t2``…… 按时点号排序（字符串排序会把 t10 排到 t2 前面）。

    ``limit`` 只取前 N 个时点。验收「首轮全量 + 第二轮增量」只需两个时点；把整条
    5–7 个时点的序列跑满，成本与时长都翻几倍，多出来的轮次并不回答新增的问题。
    """
    found = [
        path
        for path in sequence_dir.iterdir()
        if path.is_dir() and path.name.startswith("t") and path.name[1:].isdigit()
    ]
    ordered = sorted(found, key=lambda path: int(path.name[1:]))
    return ordered[:limit] if limit > 0 else ordered


def _frame_text(payload: Any) -> str:
    """夹具是解析后的 JSON，回放帧要的是原始文本。"""
    return json.dumps(payload, ensure_ascii=False)


def timepoint_frames(point_dir: Path, phone: str) -> tuple[dict[str, list[dict]], dict[str, Any]]:
    """一个时点的 HTTP 固定响应。

    归档运行的**全部**出网响应先铺开，再用夹具里的 history / profile 覆盖这两个端点。
    只用夹具里的两个端点是不够的：其余渠道会全部落空，产物看起来「变空了」，而根因
    是夹具不全，不是增量的问题。
    """
    notes: dict[str, Any] = {"source_run": None, "profile_missing": False}
    archive_run = _runner_dir(point_dir)
    frames: dict[str, list[dict]] = {}
    if archive_run is not None:
        frames, _unresolved = build_http_frames(archive_run)
        notes["source_run"] = archive_run.name
    else:
        notes["source_run"] = ""
        notes["archive_missing"] = True

    history = _load(point_dir / "history.json")
    frames[http_key("GET", f"{HISTORY_PATH}{phone}")] = [
        {"status_code": 200, "text": _frame_text(history), "headers": {}}
    ]

    profile_path = point_dir / "profile.json"
    if profile_path.is_file():
        profile = _load(profile_path)
    else:
        # 读回画像缺失是真实存在的情形（归档里没有这条响应）。用空对象顶上：
        # 底座整份来自回传体副本，档位判定仍可进行；manifest 里明确记下来。
        profile = {}
        notes["profile_missing"] = True
    frames[http_key("GET", f"{PROFILE_PATH}{phone}")] = [
        {"status_code": 200, "text": _frame_text(profile), "headers": {}}
    ]
    return frames, notes


def _runner_dir(point_dir: Path) -> Path | None:
    """时点对应的归档运行目录。

    不在 ``sequence.json`` 里另存路径，直接读它记的 ``run_dir`` 名，再去源批次里找——
    同一份事实只有一个来源。
    """
    sequence = _load(point_dir.parent / "sequence.json")
    run_name = ""
    for item in sequence.get("timepoints") or []:
        if f"t{item.get('index')}" == point_dir.name:
            run_name = str(item.get("run_dir") or "")
            break
    if not run_name:
        return None
    source = BATCH_ROOT / str(sequence.get("source_batch") or DEFAULT_SOURCE_BATCH)
    candidate = source / "runs" / run_name
    return candidate if candidate.is_dir() else None


def seed_transcripts(point_dir: Path, cache: TranscriptCache) -> int:
    """把夹具里的转写条目原样放进缓存目录。

    夹具已经是缓存格式（``<前两位>/<key>.json``，条目自带 ``key``），复制即可。**不重新
    计算键**：键含识别参数，重算会把「参数默认值变过一次」变成一次静默的全量未命中。
    """
    root = point_dir / "transcripts"
    if not root.is_dir():
        return 0
    count = 0
    for path in sorted(root.glob("*/*.json")):
        target = cache.root / path.parent.name / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def seed_from_archive(point_dir: Path, cache: TranscriptCache, settings: Any) -> int:
    """夹具没覆盖到的录音，从归档运行的提交/查询配对里还原。"""
    run_dir = _runner_dir(point_dir)
    if run_dir is None:
        return 0
    from customer_profile.execution.auc import build_submit_body

    count = 0
    for entry in build_transcript_entries(run_dir):
        params = build_submit_body(entry["file_url"], settings)["request"]
        cache.store(
            cache.make_entry(
                entry["file_url"],
                params,
                entry["raw_json"],
                silent=bool(entry.get("silent")),
            )
        )
        count += 1
    return count


# ---------------------------------------------------------------- 收尾观测

def observed(outcome: Any) -> dict[str, Any]:
    """从导出的运行树里取出这轮实际走了哪一档、以及有没有基线错误。"""
    summary = getattr(outcome, "export_summary", None) or {}
    result: dict[str, Any] = {
        "status": getattr(outcome, "status", ""),
        "run_id": getattr(outcome, "run_id", ""),
        "duration_ms": getattr(outcome, "duration_ms", 0),
        "wall_ms": getattr(outcome, "wall_ms", 0),
        "run_count": summary.get("run_count", 0),
        "node_count": summary.get("node_count", 0),
        "tier": "",
        "tier_reason": "",
        "baseline_saved": "",
    }
    return result


def read_tier(runs_dir: Path, root_run_id: str) -> dict[str, Any]:
    """从 ``nodes.json`` 里读 dispatch 的出参。

    运行树是唯一直接证据：档位是图里的一个节点输出，而不是事后从产物倒推的猜测。
    """
    path = runs_dir / "nodes.json"
    if not path.is_file():
        return {}
    try:
        nodes = _load(path)
    except ValueError:
        return {}
    for node in nodes:
        if not isinstance(node, dict) or node.get("node_id") != DISPATCH:
            continue
        outputs = node.get("outputs") or {}
        if not isinstance(outputs, dict):
            continue
        return {
            "tier": str(outputs.get("tier") or ""),
            "tier_reason": str(outputs.get("tier_reason") or ""),
            "baseline_found": str(outputs.get("baseline_found") or ""),
            "added_count": outputs.get("added_count"),
        }
    return {}


# ---------------------------------------------------------------- 主流程

def _resolve_sequence(value: str) -> Path:
    path = Path(value)
    if path.is_dir():
        return path.resolve()
    return (FIXTURE_ROOT / value).resolve()


async def run(args: argparse.Namespace) -> int:
    picked: list[tuple[str, Path]] = []
    if args.sequence:
        directory = _resolve_sequence(args.sequence)
        if not (directory / "sequence.json").is_file():
            raise SystemExit(f"序列目录里没有 sequence.json：{directory}")
        picked.append((directory.name, directory))
    else:
        root = _resolve_sequence(args.set)
        found = _sequence_dirs(root)
        only = [item.strip() for item in (args.only or "").split(",") if item.strip()]
        for phone, directory in found.items():
            if only and phone not in set(only):
                continue
            picked.append((phone, directory))
        if only:
            missing = [phone for phone in only if phone not in found]
            if missing:
                raise SystemExit(f"{root.name} 下没有这些号码的序列：{missing}")
    if not picked:
        raise SystemExit("没有选中任何序列")

    if args.dry_run:
        for phone, directory in picked:
            sequence = _load(directory / "sequence.json")
            points = _timepoint_dirs(directory, args.max_timepoints)
            steps = sequence.get("steps") or []
            print(f"{phone}: {len(points)} timepoints")
            for index, point in enumerate(points):
                tier = steps[index - 1].get("expected_tier", "") if index else "T3(first)"
                print(f"  {point.name}: expected={tier}")
        return 0

    batch_id = args.batch_id or f"inc-replay-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    batch_dir = (BATCH_ROOT / batch_id).resolve()
    batch_dir.mkdir(parents=True, exist_ok=True)
    log_path = batch_dir / "logs" / "run.log"

    settings = build_settings(batch_dir / "trace.db", read_timeout=args.read_timeout)
    cache = TranscriptCache(batch_dir / "transcript_cache", read=True, write=True)
    policy = ExportPolicy()
    manifest: dict[str, Any] = {
        "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "incremental-replay",
        "entry": {"t1": args.t1_workflow, "rest": args.next_workflow},
        "llm": "live",
        "cases": {},
    }

    service = await build_service(
        settings,
        max_active_nodes=GATE_FOR_CONCURRENCY,
        transcript_cache=cache,
    )
    try:
        for phone, directory in picked:
            cases = manifest["cases"].setdefault(phone, {"timepoints": []})
            for index, point in enumerate(
                _timepoint_dirs(directory, args.max_timepoints)
            ):
                workflow_id = args.t1_workflow if index == 0 else args.next_workflow
                frames, notes = timepoint_frames(point, phone)
                seeded = seed_transcripts(point, cache)
                seeded += seed_from_archive(point, cache, settings)
                point_batch = batch_dir / point.name
                point_batch.mkdir(parents=True, exist_ok=True)

                replay = ReplaySource(
                    http=frames, llm=None, strict=True, name=f"sequence:{directory.name}/{point.name}"
                )
                wire_replay(service, replay, cache)

                _log(
                    log_path,
                    f"{phone} {point.name}: workflow={workflow_id} "
                    f"endpoints={len(frames)} transcripts={seeded}",
                )
                if notes.get("archive_missing"):
                    _log(log_path, f"WARNING {point.name}: 归档运行目录缺失，只回放 history/profile")

                outcome = await run_one(
                    service,
                    phone,
                    timeout=args.per_phone_timeout,
                    policy=policy,
                    batch_dir=point_batch,
                    workflow_id=workflow_id,
                )
                record = observed(outcome)
                record.update(read_tier(point_batch / "runs" / phone, outcome.run_id))
                record.update(
                    {"timepoint": point.name, "workflow_id": workflow_id, **notes,
                     "transcripts_seeded": seeded,
                     "expected_tier": _expected(directory, index)}
                )
                cases["timepoints"].append(record)
                _log(
                    log_path,
                    f"{phone} {point.name}: {record['status']} tier={record['tier'] or '-'} "
                    f"expected={record['expected_tier'] or '-'} wall={record['wall_ms']}ms",
                )
                write_summary(
                    point_batch,
                    [outcome],
                    f"{batch_id}/{point.name}",
                )
                dump_json(batch_dir / "manifest.json", manifest)
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()

    total = sum(len(case["timepoints"]) for case in manifest["cases"].values())
    ok = sum(
        1
        for case in manifest["cases"].values()
        for record in case["timepoints"]
        if record["status"] == "succeeded"
    )
    _log(log_path, f"SUMMARY {ok}/{total} timepoints succeeded")
    _log(log_path, f"artifacts under {batch_dir}")
    return 0 if ok == total else 1


def _expected(directory: Path, index: int) -> str:
    """该时点的预期档位：首轮必是全量，其余取 ``steps`` 里对应那一步。"""
    if index == 0:
        return "T3"
    steps = _load(directory / "sequence.json").get("steps") or []
    if index - 1 < len(steps):
        return str(steps[index - 1].get("expected_tier") or "")
    return ""


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="增量序列回放：逐时点跑，模型真实调用")
    parser.add_argument("--sequence", default=None, help="序列目录，直接指向 <phone>/")
    parser.add_argument("--set", default=DEFAULT_SET, help="序列集合名（experiments/incremental 下）")
    parser.add_argument("--only", default=None, help="只跑这些号码，逗号分隔")
    parser.add_argument("--batch-id", default=None, help="新批次目录名")
    parser.add_argument(
        "--t1-workflow",
        default=INCREMENTAL_WORKFLOW,
        help="首个时点的工作流（缺省增量入口：无基线必落 T3，并写基线）",
    )
    parser.add_argument(
        "--next-workflow",
        default=INCREMENTAL_WORKFLOW,
        help="后续时点的工作流；换成全量管线即「同时点全量」对照组",
    )
    parser.add_argument("--per-phone-timeout", type=float, default=3600.0)
    parser.add_argument("--read-timeout", type=float, default=1800.0)
    parser.add_argument(
        "--max-timepoints",
        type=int,
        default=0,
        help="每个序列只跑前 N 个时点（0 = 全跑）；首轮全量 + 第二轮增量取 2",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印时点与预期档位")
    args = parser.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
