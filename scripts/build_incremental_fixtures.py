#!/usr/bin/env python
"""增量画像夹具构建：从 UAT 归档抽同号码时间序列，按增量口径标注预期档位。

## 产出

每个序列落在 ``experiments/incremental/<set>/<phone>/``::

    sequence.json          时点清单、相邻差分、预期档位
    t1/history.json        GET /data/history 响应原文
    t1/profile.json        GET /data/profile 响应原文（本次运行开始前读到的）
    t1/callback.json       POST update-profile 请求体
    t1/result1.json        本次运行的 result1
    t1/transcripts/*.json  本时点新增录音的转写（转写缓存格式）
    t2/ ...

## 序列口径

- 只收状态 ``succeeded`` 的运行；第一次落 ``<号码>/``，之后 ``<号码>__run2``……
- 相邻运行**开始早于上一次结束**的（重叠对）剔除：两次取到的 history 可能互相穿插，
  不能当作「后一次看到前一次之后的数据」。
- 档位判定顺序与 ``01`` §4 一致：无基线 → T3；记录键集合无差异 → T0；新增含试驾录音
  → T3（阶段跃迁）；新增含其他录音 → T2；仅文本 → T1。

## 变体（``--variant``）

边角场景不手改 JSON，由本脚本从真实序列复制后改写，改写步骤写进 ``sequence.json``。

用法::

    python scripts/build_incremental_fixtures.py
    python scripts/build_incremental_fixtures.py --only 15344445417
    python scripts/build_incremental_fixtures.py --variant pure_t0 --only 15344445417
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from baseline_artifacts import dump_json  # noqa: E402
from customer_profile.execution.auc import build_submit_body  # noqa: E402
from customer_profile.execution.transcript_cache import TranscriptCache  # noqa: E402
from customer_profile.incremental.diff import (  # noqa: E402
    consumed_keys,
    diff_histories,
    is_removed,
    logical_key,
    record_key,
)
from customer_profile.settings import Settings  # noqa: E402

BATCH_ROOT = REPO_ROOT / "experiments" / "uat_data"
DEFAULT_SOURCE = "uat-traces-20261102-20261103"
DEFAULT_PHONES = REPO_ROOT / "experiments" / "incremental" / "sequence_phones.txt"
DEFAULT_OUT = REPO_ROOT / "experiments" / "incremental"

RECORDING_PREFIX = "TestDriveRecordingFile"
"""试驾录音渠道。它的首次出现即阶段跃迁（``level_stage`` 的唯一判据）。"""

AUDIO_SUFFIX = "RecordingFile"
"""全部录音渠道的共同后缀（试驾、人工外呼、AI 外呼）。"""


# ---------------------------------------------------------------- 读归档

def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _http_attempts(run_dir: Path) -> list[dict]:
    rows = [r for r in _load(run_dir / "attempts.json") if r.get("kind") != "llm"]
    return sorted(rows, key=lambda r: int(r.get("created_at_ms") or 0))


def _first_ok_http(run_dir: Path, marker: str) -> dict | None:
    for row in _http_attempts(run_dir):
        url = str((row.get("request_json") or {}).get("url") or row.get("target") or "")
        if marker in url and row.get("status_code") == 200:
            return row
    return None


def history_rows(run_dir: Path) -> list[dict] | None:
    row = _first_ok_http(run_dir, "/data/history/")
    if row is None:
        return None
    try:
        payload = json.loads(row.get("response_text") or "")
    except ValueError:
        return None
    return payload if isinstance(payload, list) else None


def profile_payload(run_dir: Path) -> dict | None:
    row = _first_ok_http(run_dir, "/data/profile/")
    if row is None:
        return None
    try:
        payload = json.loads(row.get("response_text") or "")
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def callback_payload(run_dir: Path) -> dict | None:
    row = _first_ok_http(run_dir, "update-profile")
    if row is None:
        return None
    body = (row.get("request_json") or {}).get("request_body")
    return body if isinstance(body, dict) else None


def result1_payload(run_dir: Path) -> dict | None:
    payload = _load(run_dir / "result.json")
    raw = payload.get("result1")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def run_index(source: Path) -> dict[str, list[dict]]:
    """``{phone: [{started, finished, dir, status}]}``，按开始时刻升序。"""
    out: dict[str, list[dict]] = {}
    runs = source / "runs"
    for entry in sorted(runs.iterdir()):
        if not entry.is_dir():
            continue
        result_path = entry / "result.json"
        if not result_path.is_file():
            continue
        result = _load(result_path)
        phone = entry.name.split("__")[0]
        out.setdefault(phone, []).append(
            {
                "dir": entry,
                "started": int(result.get("started_at_ms") or 0),
                "finished": int(result.get("finished_at_ms") or 0),
                "status": str(result.get("status") or ""),
            }
        )
    for items in out.values():
        items.sort(key=lambda item: (item["started"], item["dir"].name))
    return out


def usable_runs(items: Iterable[dict]) -> list[dict]:
    """去掉失败运行与重叠对，保留可用时间序列。"""
    kept: list[dict] = []
    for item in items:
        if item["status"] != "succeeded":
            continue
        if kept and item["started"] < kept[-1]["finished"]:
            continue
        kept.append(item)
    return kept


# ---------------------------------------------------------------- 转写

def transcript_entries(run_dir: Path, settings: Settings) -> list[dict]:
    """从 submit/query 配对还原转写（与 ``replay_from_archive`` 同口径）。

    归档只存翻译后的 ``auc_result``，这里按逆变换还原成云格式，让复用路径与真实路径
    走同一个解析函数。按 ``task_id`` 配对，不按顺序——归档里存在被拒的提交。
    """
    attempts = _http_attempts(run_dir)
    submits: dict[str, dict] = {}
    for item in attempts:
        url = str((item.get("request_json") or {}).get("url") or "")
        if "/submit_analyze" not in url or str(item.get("status_code")) != "200":
            continue
        try:
            payload = json.loads(item.get("response_text") or "{}")
        except ValueError:
            continue
        task_id = payload.get("task_id")
        if task_id:
            submits[str(task_id)] = item

    entries: list[dict] = []
    for item in attempts:
        url = str((item.get("request_json") or {}).get("url") or "")
        if "/query_analyze" not in url:
            continue
        body = (item.get("request_json") or {}).get("request_body") or {}
        submit = submits.get(str(body.get("task_id") or ""))
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
                        "text": u.get("text"),
                        "additions": {
                            "emotion": u.get("emotion"),
                            "gender": u.get("gender"),
                            "speaker": u.get("speaker"),
                            "speech_rate": u.get("speech_rate"),
                            "volume": u.get("volume"),
                        },
                    }
                    for u in utterances
                    if isinstance(u, dict)
                ]
            }
        }
        entries.append(
            {"file_url": str(file_url), "raw_json": json.dumps(cloud, ensure_ascii=False)}
        )
    return entries


def recording_urls(rows: Iterable[dict]) -> set[str]:
    """记录里出现的录音直链集合。"""
    urls: set[str] = set()
    for row in rows:
        if not str(row.get("channel") or "").endswith(AUDIO_SUFFIX):
            continue
        media = row.get("media_urls")
        if isinstance(media, str):
            urls.add(media)
        elif isinstance(media, (list, tuple)):
            urls.update(str(x) for x in media if x)
        raw = row.get("raw_payload")
        if isinstance(raw, dict) and raw.get("recording_url"):
            urls.add(str(raw["recording_url"]))
    return urls


def write_transcripts(
    out_dir: Path, entries: list[dict], settings: Settings, urls: set[str]
) -> int:
    """把指定录音 URL 的转写写成转写缓存条目。返回写入条数。"""
    cache = TranscriptCache(out_dir, read=False, write=True)
    written = 0
    for entry in entries:
        if entry["file_url"] not in urls:
            continue
        params = build_submit_body(entry["file_url"], settings)["request"]
        cache.store(
            cache.make_entry(
                entry["file_url"], params, entry["raw_json"], silent=bool(entry.get("silent"))
            )
        )
        written += 1
    return written


# ---------------------------------------------------------------- 档位

def expected_tier(previous: list[dict] | None, current: list[dict], delta: dict) -> str:
    """按 ``01`` §4 的判定顺序给一个时点定档。"""
    if previous is None:
        return "T3"
    touched = delta["added_records"] + delta["changed_records"]
    if not touched and not delta["removed"]:
        return "T0"
    channels = {str(row.get("channel") or "") for row in touched}
    if RECORDING_PREFIX in channels:
        return "T3"
    if any(ch.endswith(AUDIO_SUFFIX) for ch in channels):
        return "T2"
    return "T1"


def step_summary(delta: dict) -> dict[str, Any]:
    channels: dict[str, int] = {}
    for row in delta["added_records"] + delta["changed_records"]:
        channel = str(row.get("channel") or "")
        channels[channel] = channels.get(channel, 0) + 1
    return {
        "added": len(delta["added"]),
        "changed": len(delta["changed"]),
        "removed": len(delta["removed"]),
        "unchanged": len(delta["unchanged"]),
        "touched_channels": dict(sorted(channels.items())),
    }


# ---------------------------------------------------------------- 构建

def build_sequence(
    phone: str,
    runs: list[dict],
    out_root: Path,
    source_name: str,
    settings: Settings,
) -> dict[str, Any]:
    target = out_root / phone
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    timepoints: list[dict] = []
    steps: list[dict] = []
    previous_rows: list[dict] | None = None
    previous_entries: list[dict] = []

    for index, item in enumerate(runs, start=1):
        run_dir: Path = item["dir"]
        rows = history_rows(run_dir)
        if rows is None:
            rows = []
        point_dir = target / f"t{index}"
        point_dir.mkdir()

        dump_json(point_dir / "history.json", rows)
        profile = profile_payload(run_dir)
        if profile is not None:
            dump_json(point_dir / "profile.json", profile)
        callback = callback_payload(run_dir)
        if callback is not None:
            dump_json(point_dir / "callback.json", callback)
        result1 = result1_payload(run_dir)
        if result1 is not None:
            dump_json(point_dir / "result1.json", result1)

        entries = transcript_entries(run_dir, settings)
        delta = diff_histories(previous_rows, rows)
        new_urls = recording_urls(delta["added_records"] + delta["changed_records"])
        # 本时点新增录音的转写：优先取本运行自己的 entry，缺了再看前一轮（同一通录音
        # 可能上一轮已识别过，本轮只是首次「出现在新增里」）。
        trans_dir = point_dir / "transcripts"
        trans_dir.mkdir()
        written = write_transcripts(trans_dir, entries + previous_entries, settings, new_urls)
        # 归档里并没有每一通录音的转写：某次运行可能只识别了部分文件。缺的必须记下来，
        # 否则回放时这些录音会落到回放守卫的 599，以「解析失败」的形态混进提示词。
        available = {entry["file_url"] for entry in entries + previous_entries}
        missing = sorted(new_urls - available)

        timepoints.append(
            {
                "index": index,
                "run_dir": run_dir.name,
                "started_at_ms": item["started"],
                "finished_at_ms": item["finished"],
                "batch_id": str((callback or {}).get("batch_id") or ""),
                "history_records": len(rows),
                "consumed_keys": len(consumed_keys(rows)),
                "new_transcripts": written,
                "missing_transcripts": missing,
            }
        )
        if previous_rows is not None:
            steps.append(
                {
                    "from": index - 1,
                    "to": index,
                    "expected_tier": expected_tier(previous_rows, rows, delta),
                    **step_summary(delta),
                }
            )
        previous_rows = rows
        previous_entries = entries

    sequence = {
        "set": out_root.name,
        "phone": phone,
        "source_batch": source_name,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "timepoints": timepoints,
        "steps": steps,
        "variant": None,
    }
    dump_json(target / "sequence.json", sequence)
    return sequence


# ---------------------------------------------------------------- 变体

def _shift(sequence: dict) -> int:
    return max(point["index"] for point in sequence["timepoints"])


def _append_timepoint(target: Path, sequence: dict, rows: list[dict], source_from: int) -> int:
    """把某个时点复制成新时点，history 换成给定内容，batch_id 换新。"""
    index = _shift(sequence) + 1
    src = target / f"t{source_from}"
    dst = target / f"t{index}"
    shutil.copytree(src, dst)
    dump_json(dst / "history.json", rows)
    new_batch = f"variant-{sequence['phone']}-t{index}"
    callback_path = dst / "callback.json"
    if callback_path.is_file():
        callback = _load(callback_path)
        callback["batch_id"] = new_batch
        dump_json(callback_path, callback)
    result_path = dst / "result1.json"
    if result_path.is_file():
        result = _load(result_path)
        result["batch_id"] = new_batch
        dump_json(result_path, result)
    sequence["timepoints"].append(
        {
            "index": index,
            "run_dir": f"{sequence['phone']}__variant-t{index}",
            "started_at_ms": sequence["timepoints"][-1]["started_at_ms"] + 3_600_000,
            "finished_at_ms": sequence["timepoints"][-1]["finished_at_ms"] + 3_600_000,
            "batch_id": new_batch,
            "history_records": len(rows),
            "consumed_keys": len(consumed_keys(rows)),
            "new_transcripts": 0,
        }
    )
    return index


def variant_pure_t0(target: Path, sequence: dict) -> dict:
    last = _shift(sequence)
    rows = _load(target / f"t{last}" / "history.json")
    index = _append_timepoint(target, sequence, rows, last)
    sequence["steps"].append(
        {
            "from": last,
            "to": index,
            "expected_tier": "T0",
            "added": 0,
            "changed": 0,
            "removed": 0,
            "unchanged": len(rows),
            "touched_channels": {},
        }
    )
    return {
        "variant": "pure_t0",
        "steps": ["复制最后一时点的 history 原样作为新时点，batch_id 换新"],
    }


def variant_profile_edit(target: Path, sequence: dict, kind: str) -> dict:
    """补缺三情形：改最后一时点的 profile.json。

    - ``missing_a``：删掉 3 个 ``basic_notes`` 键（A 缺失，用副本补）；
    - ``same_ab``：把若干字段值与上一时点 callback 对齐（沿用副本证据）；
    - ``diff_ab``：把若干字段值改成另一个值（以 A 为准）。
    """
    last = _shift(sequence)
    profile_path = target / f"t{last}" / "profile.json"
    if not profile_path.is_file():
        raise SystemExit(f"t{last} 没有 profile.json，换一个号码")
    profile = _load(profile_path)
    notes = profile.get("basic_notes") or {}
    change: dict[str, Any] = {}

    if kind == "missing_a":
        removed = [key for key in notes if str(notes[key].get("value") or "").strip()][:3]
        for key in removed:
            notes.pop(key, None)
        profile["basic_notes"] = notes
        change = {"removed_keys": removed}
    elif kind == "same_ab":
        prev_callback = target / f"t{last - 1}" / "callback.json"
        base = _load(prev_callback) if prev_callback.is_file() else {}
        values = {
            item.get("attribute_key"): str(item.get("attribute_value") or "")
            for item in (base.get("analysis_result") or {}).get("basic_notes_updates") or []
            if item.get("attribute_key")
        }
        aligned = []
        for key, item in notes.items():
            if key in values and values[key] and isinstance(item, dict):
                item["value"] = values[key]
                aligned.append(key)
        profile["basic_notes"] = notes
        change = {"aligned_keys": aligned[:5]}
    elif kind == "diff_ab":
        touched = []
        for key, item in list(notes.items()):
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip()
            if not value or len(touched) >= 2:
                continue
            item["value"] = value + "（人工修订）"
            touched.append(key)
        profile["basic_notes"] = notes
        change = {"mutated_keys": touched}
    else:  # pragma: no cover - 参数由 argparse 限定
        raise SystemExit(f"未知变体 {kind}")

    dump_json(profile_path, profile)
    # 这一时点的差分不受影响：改的是接口画像，不是客户数据。
    last_step = sequence["steps"][-1]
    last_step["expected_tier"] = "T0"
    last_step["expected_change"] = change
    return {"variant": kind, "steps": [f"改写 t{last}/profile.json", change]}


def variant_removed(target: Path, sequence: dict, use_operation: bool) -> dict:
    """记录移除：从最后一时点的 history 删掉一条上一时点存在的跟进记录。"""
    last = _shift(sequence)
    prev = _load(target / f"t{last - 1}" / "history.json")
    rows = _load(target / f"t{last}" / "history.json")
    candidates = [
        row
        for row in prev
        if row.get("source_key") and str(row.get("channel") or "") == "OntologyLeadFollowRecord"
    ]
    if not candidates:
        candidates = [row for row in prev if row.get("source_key")]
    if not candidates:
        raise SystemExit("该序列没有可移除的过程资料记录，换一个号码")
    victim_key = record_key(candidates[-1])
    victim_logical = logical_key(candidates[-1])
    # 同一逻辑记录在最后一轮可能已换版本（跟进更新）：按逻辑键删除，才真的删掉它。
    kept = [row for row in rows if logical_key(row) != victim_logical]
    if len(kept) == len(rows):
        raise SystemExit("最后一时点没有这条记录（可能中途已换版本），换一个号码或时点")
    if use_operation:
        marker = dict(candidates[-1])
        marker["raw_payload"] = {"operation": "removed", "reason": "变体构造：上游标记删除"}
        kept = kept + [marker]
    dump_json(target / f"t{last}" / "history.json", kept)
    step = sequence["steps"][-1]
    step["expected_tier"] = "T1"
    step["expected_change"] = {
        "removed_source_key": str(candidates[-1].get("source_key") or ""),
        "channel": str(candidates[-1].get("channel") or ""),
        "via_operation_marker": use_operation,
    }
    return {
        "variant": "removed",
        "steps": [f"从 t{last}/history.json 删除记录 {victim_key}"]
        + (["并以 operation=removed 形式回填一行"] if use_operation else []),
    }


def variant_stage_jump(target: Path, sequence: dict, donor: dict | None) -> dict:
    """阶段跃迁：给最后一时点的 history 加一条真实的试驾录音记录。"""
    if donor is None:
        raise SystemExit("归档里找不到任何 TestDriveRecordingFile 记录，无法构造阶段跃迁夹具")
    last = _shift(sequence)
    rows = _load(target / f"t{last}" / "history.json")
    if any(str(row.get("channel") or "") == RECORDING_PREFIX for row in rows):
        raise SystemExit(f"t{last} 已有试驾录音，换一个尚未进入商机阶段的号码")
    rows = rows + [donor]
    dump_json(target / f"t{last}" / "history.json", rows)
    step = sequence["steps"][-1]
    step["expected_tier"] = "T3"
    step["expected_change"] = {"injected_channel": RECORDING_PREFIX}
    return {
        "variant": "stage_jump",
        "steps": [f"向 t{last}/history.json 注入一条来自归档的试驾录音记录"],
    }


def variant_compat_key(target: Path, sequence: dict) -> dict:
    last = _shift(sequence)
    step = sequence["steps"][-1]
    step["expected_tier"] = "T3"
    step["expected_change"] = {"baseline_compat_key": "mutated"}
    return {"variant": "compat_key", "steps": [f"回放时把 t{last - 1} 的基线 compat_key 改成随机串"]}


def variant_no_baseline(target: Path, sequence: dict) -> dict:
    """基线缺失：删掉除最后时点以外的全部时点，从中间某点直接开跑。"""
    points = sorted(p["index"] for p in sequence["timepoints"])
    for index in points[:-1]:
        shutil.rmtree(target / f"t{index}")
    last = points[-1]
    sequence["timepoints"] = [p for p in sequence["timepoints"] if p["index"] == last]
    sequence["steps"] = [
        {
            "from": 0,
            "to": last,
            "expected_tier": "T3",
            "added": 0,
            "changed": 0,
            "removed": 0,
            "unchanged": sequence["timepoints"][0]["history_records"],
            "touched_channels": {},
        }
    ]
    return {"variant": "no_baseline", "steps": ["删除除最后时点外的全部时点，模拟首次运行"]}


def find_donor_drive_row(source: Path, exclude: str = "") -> dict | None:
    runs = source / "runs"
    for entry in sorted(runs.iterdir()):
        if not entry.is_dir() or entry.name.split("__")[0] == exclude:
            continue
        rows = history_rows(entry)
        for row in rows or []:
            if str(row.get("channel") or "") == RECORDING_PREFIX and not is_removed(row):
                return row
    return None


# ---------------------------------------------------------------- 主流程

def parse_phones(path: Path) -> dict[str, dict]:
    """号码清单：``尾号 <TAB> 完整号码 <TAB> 用途``。返回 ``{phone: {suffix, purpose}}``。"""
    out: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("\t") if p.strip()]
        if len(parts) < 2:
            continue
        out[parts[1]] = {"suffix": parts[0], "purpose": parts[2] if len(parts) > 2 else ""}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建增量画像序列夹具与边角变体")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="归档批次目录名或路径")
    parser.add_argument("--phones-file", default=str(DEFAULT_PHONES))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--set", default="sequences", help="输出子目录名")
    parser.add_argument("--only", default="", help="只处理这些号码，逗号分隔")
    parser.add_argument(
        "--variant",
        default="",
        help="变体名：pure_t0 / missing_a / same_ab / diff_ab / removed / removed_op / "
        "stage_jump / compat_key / no_baseline",
    )
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.is_dir():
        source = BATCH_ROOT / args.source
    if not source.is_dir():
        raise SystemExit(f"归档批次不存在：{source}")

    manifest = parse_phones(Path(args.phones_file))
    wanted = [p.strip() for p in args.only.split(",") if p.strip()]
    if not wanted:
        wanted = list(manifest)

    runs = run_index(source)
    settings = Settings()
    out_root = (
        Path(args.out) / (f"variants/{args.variant}" if args.variant else args.set)
    ).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    if args.variant and len(wanted) != 1:
        raise SystemExit("--variant 一次只处理一个号码（--only）")

    donor = (
        find_donor_drive_row(source, exclude=wanted[0]) if args.variant == "stage_jump" else None
    )
    summary: list[dict] = []

    for phone in wanted:
        items = runs.get(phone, [])
        kept = usable_runs(items)
        if not kept:
            summary.append({"phone": phone, "skipped": "没有成功的运行"})
            print(f"{phone}: skipped (没有成功的运行)")
            continue
        if len(kept) < 2:
            summary.append({"phone": phone, "skipped": "可用运行少于 2 次"})
            print(f"{phone}: skipped (可用运行少于 2 次)")
            continue

        sequence = build_sequence(phone, kept, out_root, source.name, settings)
        target = out_root / phone
        note: dict[str, Any] = {"variant": None}
        if args.variant:
            if args.variant == "pure_t0":
                note = variant_pure_t0(target, sequence)
            elif args.variant in ("missing_a", "same_ab", "diff_ab"):
                note = variant_profile_edit(target, sequence, args.variant)
            elif args.variant in ("removed", "removed_op"):
                note = variant_removed(target, sequence, args.variant == "removed_op")
            elif args.variant == "stage_jump":
                note = variant_stage_jump(target, sequence, donor)
            elif args.variant == "compat_key":
                note = variant_compat_key(target, sequence)
            elif args.variant == "no_baseline":
                note = variant_no_baseline(target, sequence)
            else:
                raise SystemExit(f"未知变体：{args.variant}")
            sequence["variant"] = note
            dump_json(target / "sequence.json", sequence)

        tiers = [step["expected_tier"] for step in sequence["steps"]]
        summary.append(
            {
                "phone": phone,
                "suffix": manifest.get(phone, {}).get("suffix", phone[-4:]),
                "purpose": manifest.get(phone, {}).get("purpose", ""),
                "timepoints": len(sequence["timepoints"]),
                "steps": len(sequence["steps"]),
                "tiers": tiers,
            }
        )
        print(
            f"{phone}: {len(sequence['timepoints'])} timepoints, "
            f"tiers={','.join(tiers) or '-'}"
        )

    report = {
        "source_batch": source.name,
        "set": out_root.name,
        "variant": args.variant or None,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "cases": summary,
    }
    dump_json(out_root / "build-report.json", report)
    print(f"artifacts under {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
