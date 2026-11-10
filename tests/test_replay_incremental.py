"""增量序列回放的夹具读取与逐轮判定（``T3-2``）。

回放脚本最容易错的不是跑不跑得起来，而是**读错了输入**：同号码取到同一份运行、
夹具的 history 被归档响应盖掉、转写条目按新参数重算键而全部未命中。三者都不会报错，
只会让每一轮看起来「跑通了但档位不对」。这里把它们逐个钉住。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import replay_from_archive as rfa  # noqa: E402
import replay_incremental as ri  # noqa: E402
from customer_profile.replay import http_key  # noqa: E402

PHONE = "13900000001"


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _sequence(tmp_path: Path, points: int = 2, source_batch: str = "batch-a") -> Path:
    phone_dir = tmp_path / "sequences" / PHONE
    _write(
        phone_dir / "sequence.json",
        {
            "phone": PHONE,
            "source_batch": source_batch,
            "timepoints": [
                {"index": index, "run_dir": PHONE if index == 1 else f"{PHONE}__run{index}"}
                for index in range(1, points + 1)
            ],
            "steps": [
                {"from": index - 1, "to": index, "expected_tier": "T1"}
                for index in range(2, points + 1)
            ],
        },
    )
    for index in range(1, points + 1):
        point = phone_dir / f"t{index}"
        _write(point / "history.json", [{"k": index}])
        _write(point / "profile.json", {"basic_notes": {"age": {"value": "35-40"}}})
    return phone_dir


def _batch(tmp_path: Path, day: int, run_name: str) -> Path:
    run_dir = tmp_path / "batches" / "batch-a" / "runs" / run_name
    _write(
        run_dir / "attempts.json",
        [
            {
                "kind": "http",
                "created_at_ms": day,
                "status_code": 200,
                "request_json": {"method": "GET", "url": "/api/v1/remote/data/other/x"},
                "response_text": "{}",
            },
            {
                "kind": "http",
                "created_at_ms": day + 1,
                "status_code": 200,
                "request_json": {"method": "GET", "url": f"/api/v1/remote/data/history/{PHONE}"},
                "response_text": '{"stale": true}',
            },
        ],
    )
    return run_dir


# ---------------------------------------------------------------- 时点与夹具

def test_timepoints_sort_numerically(tmp_path):
    """``t10`` 不能被排到 ``t2`` 前面——顺序错了，每轮读的输入就整体错位。"""
    phone_dir = _sequence(tmp_path, points=2)
    for index in range(3, 11):
        (phone_dir / f"t{index}").mkdir()
        _write(phone_dir / f"t{index}" / "history.json", [])
    names = [path.name for path in ri._timepoint_dirs(phone_dir)]
    assert names == [f"t{index}" for index in range(1, 11)]


def test_history_from_the_fixture_wins_over_the_archived_response(tmp_path, monkeypatch):
    """夹具里的 history 必须覆盖归档里那条同名响应。

    归档运行自己的 history 是「当时」读到的，与本时点可能不同；不覆盖就等于用旧输入
    跑新一轮，档位判定必然偏。
    """
    phone_dir = _sequence(tmp_path)
    source = tmp_path / "batches"
    monkeypatch.setattr(ri, "BATCH_ROOT", source)
    _batch(tmp_path, 1_000, PHONE)
    _batch(tmp_path, 2_000, f"{PHONE}__run2")

    frames, notes = ri.timepoint_frames(phone_dir / "t1", PHONE)
    history_key = http_key("GET", f"/api/v1/remote/data/history/{PHONE}")
    assert len(frames[history_key]) == 1
    assert json.loads(frames[history_key][0]["text"]) == [{"k": 1}]
    # 归档里其余端点照样带上，否则那些渠道会落空。
    assert http_key("GET", "/api/v1/remote/data/other/x") in frames
    assert notes["source_run"] == PHONE


def test_missing_profile_is_replayed_as_an_empty_object(tmp_path, monkeypatch):
    """归档里没有 profile 响应是真实情形：用空对象顶上，并让 manifest 记下来。"""
    phone_dir = _sequence(tmp_path)
    (phone_dir / "t1" / "profile.json").unlink()
    monkeypatch.setattr(ri, "BATCH_ROOT", tmp_path / "batches")
    _write(tmp_path / "batches" / "batch-a" / "runs" / PHONE / "attempts.json", [])

    frames, notes = ri.timepoint_frames(phone_dir / "t1", PHONE)
    profile_key = http_key("GET", f"/api/v1/remote/data/profile/{PHONE}")
    assert frames[profile_key][0]["text"] == "{}"
    assert notes["profile_missing"] is True


def test_missing_archive_run_still_replays_history_and_profile(tmp_path, monkeypatch):
    phone_dir = _sequence(tmp_path)
    monkeypatch.setattr(ri, "BATCH_ROOT", tmp_path / "nowhere")
    frames, notes = ri.timepoint_frames(phone_dir / "t1", PHONE)
    assert notes["archive_missing"] is True
    assert len(frames) == 2


def test_expected_tier_of_the_first_point_is_full_rebuild(tmp_path):
    """首轮没有基线，预期必是全量；用 ``steps`` 去查会得到空串，被误读成「无预期」。"""
    phone_dir = _sequence(tmp_path, points=3)
    assert ri._expected(phone_dir, 0) == "T3"
    assert ri._expected(phone_dir, 1) == "T1"


# ---------------------------------------------------------------- 转写播种

def test_fixture_transcripts_are_copied_without_recomputing_keys(tmp_path):
    """夹具条目已是缓存格式：照抄，不重算键（重算会让整批静默未命中）。"""
    point = tmp_path / "t1"
    entry = {
        "format": "1",
        "key": "ab" + "0" * 62,
        "file_url": "https://example/1.wav",
        "params": {},
        "silent": "False",
        "captured_at": "",
        "raw_json": "",
    }
    _write(point / "transcripts" / "ab" / f"{entry['key']}.json", entry)

    from customer_profile.execution.transcript_cache import TranscriptCache

    cache = TranscriptCache(tmp_path / "cache", read=True, write=False)
    assert ri.seed_transcripts(point, cache) == 1
    assert (cache.root / "ab" / f"{entry['key']}.json").is_file()


# ---------------------------------------------------------------- 归档取数

def test_case_dirs_can_take_the_nth_run(tmp_path):
    """``--run-index`` 取第 N 次运行：增量序列就是靠它区分「第几轮」。"""
    source = tmp_path / "source"
    for name in (PHONE, f"{PHONE}__run2"):
        (source / "runs" / name).mkdir(parents=True)
    first = rfa.case_dirs(source, [PHONE])
    second = rfa.case_dirs(source, [PHONE], 1)
    third = rfa.case_dirs(source, [PHONE], 2)
    assert first[PHONE].name == PHONE
    assert second[PHONE].name == f"{PHONE}__run2"
    assert third == {}
