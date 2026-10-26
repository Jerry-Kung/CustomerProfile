"""转写归档缓存：键的构成、原文往返、损坏条目降级。

**这一层要守住的东西。** 缓存是「用旧结果冒充新结果」的天然入口，因此本文件重点钉两类
性质：

1. **键把识别参数算进去。** 改了 ``model_version`` 之类的参数后必须换键，否则会拿旧参数
   的转写冒充新配置的结果——一条静默的质量污染，比报错难查得多。
2. **坏条目退回未命中，不抛、不顶替。** 一条半截 JSON、一个放错位置的文件，都只应让该
   录音走真实识别，不该让整批号码失败，也不该把别人的转写当成自己的。

中文往返是第三条：转写正文全是中文，而 CLAUDE.md 记载过本机工具的编码异常。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from customer_profile.execution.transcript_cache import (  # noqa: E402
    CACHE_FORMAT,
    TranscriptCache,
    TranscriptEntry,
)

URL = (
    "https://dfms.tos-cn-beijing.volces.com/cdp_customer_data/13430349943/"
    "TestDriveRecordingFile/GDA3500337~2091400940486721894~1787460180.mp3"
)

PARAMS = {
    "model_name": "bigmodel",
    "model_version": "400",
    "enable_ddc": True,
    "corpus": {"correct_table_name": "", "context": ""},
}

# 一份形状贴近真实云响应体的原文（含中文，且含六字段之外的内容——这正是必须存原文的理由）
RAW = json.dumps(
    {
        "result": {
            "utterances": [
                {
                    "text": "哎，您好，先生打扰了，我这边是猛士汽车总部线上体验顾问。",
                    "start_time": 0,
                    "end_time": 4200,
                    "additions": {
                        "emotion": "neutral",
                        "gender": "female",
                        "speaker": "2",
                        "speech_rate": "6.00",
                        "volume": "73.04",
                    },
                }
            ],
            "duration": 4200,
        },
        "extra_field_not_in_the_six": {"kept": True},
    },
    ensure_ascii=False,
)


def _cache(tmp_path: Path, **kw) -> TranscriptCache:
    return TranscriptCache(tmp_path / "cache", **kw)


# ---------------------------------------------------------------- 键


def test_key_is_stable_for_the_same_url_and_params(tmp_path):
    """同一 URL + 同一参数 → 同一键。键不稳定则缓存永远不命中。"""
    cache = _cache(tmp_path)
    first = cache.key_for(URL, PARAMS)
    second = cache.key_for(URL, PARAMS)
    assert first == second
    assert len(first) == 64


def test_key_ignores_mapping_order(tmp_path):
    """参数字典的书写顺序不影响键（键按 sort_keys 计算）。"""
    cache = _cache(tmp_path)
    reordered = {k: PARAMS[k] for k in reversed(list(PARAMS))}
    assert cache.key_for(URL, PARAMS) == cache.key_for(URL, reordered)


def test_changing_a_recognition_param_changes_the_key(tmp_path):
    """改识别参数必须换键——这是「参数变更视为未命中」的实现点。"""
    cache = _cache(tmp_path)
    base = cache.key_for(URL, PARAMS)

    for field, value in (
        ("model_version", "500"),
        ("model_name", "bigmodel-v2"),
        ("enable_ddc", False),
    ):
        changed = dict(PARAMS)
        changed[field] = value
        assert cache.key_for(URL, changed) != base, f"改 {field} 后键未变"


def test_changing_the_url_changes_the_key(tmp_path):
    """换录音必然换键。"""
    cache = _cache(tmp_path)
    other = URL.replace("/13430349943/", "/13435844484/")
    assert cache.key_for(URL, PARAMS) != cache.key_for(other, PARAMS)


# ---------------------------------------------------------------- 往返


def test_store_then_load_round_trips_the_raw_body(tmp_path):
    """核心往返：存原文，读回来的原文解析后与原件**相等**。"""
    cache = _cache(tmp_path)
    entry = cache.make_entry(URL, PARAMS, RAW)
    path = cache.store(entry)
    assert path is not None and path.is_file()

    loaded = cache.load(URL, PARAMS)
    assert loaded is not None
    assert loaded.raw_json == RAW, "原文必须逐字节一致"
    assert json.loads(loaded.raw_json) == json.loads(RAW)
    assert loaded.file_url == URL
    assert loaded.silent is False


def test_round_trip_preserves_chinese_and_never_writes_mojibake(tmp_path):
    """中文往返：读回后内容相等，且文件字节是 UTF-8 无 BOM、无 U+FFFD。"""
    cache = _cache(tmp_path)
    cache.store(cache.make_entry(URL, PARAMS, RAW))

    path = cache.path_for(cache.key_for(URL, PARAMS))
    raw_bytes = path.read_bytes()
    assert not raw_bytes.startswith(b"\xef\xbb\xbf"), "不应有 BOM"
    text = raw_bytes.decode("utf-8")
    assert "�" not in text, "出现了替换字符，说明写入时编码坏了"

    loaded = cache.load(URL, PARAMS)
    assert loaded is not None
    utterances = json.loads(loaded.raw_json)["result"]["utterances"]
    assert utterances[0]["text"] == "哎，您好，先生打扰了，我这边是猛士汽车总部线上体验顾问。"


def test_six_field_parse_still_works_from_the_stored_body(tmp_path):
    """存原文的用意：复用路径能调用**同一个**解析函数得到六字段结果。"""
    from customer_profile.execution.auc import parse_auc_result

    cache = _cache(tmp_path)
    cache.store(cache.make_entry(URL, PARAMS, RAW))
    loaded = cache.load(URL, PARAMS)
    assert loaded is not None

    parsed = json.loads(parse_auc_result(loaded.raw_json))
    assert parsed[0]["text"].startswith("哎，您好")
    assert parsed[0]["speech_rate"] == 6.0
    assert parsed[0]["volume"] == 73.04


def test_silent_entry_round_trips_without_a_body(tmp_path):
    """静音音频没有 utterances 体，用 ``silent`` 单独表示并如实读回。"""
    cache = _cache(tmp_path)
    cache.store(cache.make_entry(URL, PARAMS, "", silent=True))

    loaded = cache.load(URL, PARAMS)
    assert loaded is not None
    assert loaded.silent is True
    assert loaded.raw_json == ""


# ---------------------------------------------------------------- 降级


def test_load_misses_when_no_entry_exists(tmp_path):
    """没有条目就是未命中。"""
    cache = _cache(tmp_path)
    assert cache.load(URL, PARAMS) is None
    assert cache.stats.misses == 1
    assert cache.stats.hits == 0


def test_load_returns_none_for_a_corrupt_entry(tmp_path):
    """半截 JSON 当作未命中，而不是抛错让整批号码失败。"""
    cache = _cache(tmp_path)
    path = cache.path_for(cache.key_for(URL, PARAMS))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert cache.load(URL, PARAMS) is None


def test_load_rejects_an_entry_whose_recorded_key_differs(tmp_path):
    """文件里的 key 与本次计算的 key 不符 → 未命中。

    这条防的是「文件放错位置」：拿别人的转写顶上去会是一条极难察觉的静默错误。
    """
    cache = _cache(tmp_path)
    key = cache.key_for(URL, PARAMS)
    path = cache.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "format": CACHE_FORMAT,
                "key": "0" * 64,
                "file_url": URL,
                "raw_json": RAW,
                "params": PARAMS,
                "silent": False,
                "captured_at": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert cache.load(URL, PARAMS) is None


def test_load_rejects_an_unknown_format_version(tmp_path):
    """格式版本不符 → 未命中，而不是按旧结构硬解析。"""
    cache = _cache(tmp_path)
    key = cache.key_for(URL, PARAMS)
    path = cache.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"format": 999, "key": key, "file_url": URL, "raw_json": RAW},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert cache.load(URL, PARAMS) is None


def test_a_different_params_set_does_not_read_another_sets_entry(tmp_path):
    """用 A 参数存的条目，拿 B 参数去读必须未命中（不是报错，也不是命中）。"""
    cache = _cache(tmp_path)
    cache.store(cache.make_entry(URL, PARAMS, RAW))

    other = dict(PARAMS, model_version="500")
    assert cache.load(URL, other) is None


# ---------------------------------------------------------------- 开关


def test_read_disabled_never_returns_an_entry(tmp_path):
    """``--no-transcript-cache``：不复用，但条目仍在盘上。"""
    writer = _cache(tmp_path)
    writer.store(writer.make_entry(URL, PARAMS, RAW))

    reader = _cache(tmp_path, read=False)
    assert reader.load(URL, PARAMS) is None
    assert reader.stats.hits == 0


def test_write_disabled_still_reads(tmp_path):
    """只读不写：能复用既有条目，但不再落新的。"""
    writer = _cache(tmp_path)
    writer.store(writer.make_entry(URL, PARAMS, RAW))

    reader = _cache(tmp_path, write=False)
    assert reader.load(URL, PARAMS) is not None
    assert reader.store(reader.make_entry(URL + "?x", PARAMS, RAW)) is None
    assert reader.stats.writes == 0


def test_store_failure_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    """落盘失败只打 stderr，不让整批号码因磁盘问题失败。"""
    cache = _cache(tmp_path)
    entry = cache.make_entry(URL, PARAMS, RAW)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "customer_profile.execution.transcript_cache._atomic_write_json", boom
    )
    sink_id = logger.add(lambda message: sys.stderr.write(str(message)), diagnose=False)
    try:
        assert cache.store(entry) is None
        assert cache.stats.write_errors == 1
        captured = capsys.readouterr()
    finally:
        logger.remove(sink_id)
    assert "写入失败" in captured.err
    assert "disk full" in captured.err


# ---------------------------------------------------------------- 统计


def test_stats_count_hits_misses_and_writes(tmp_path):
    """计数如实反映行为——runbook 用 ``hits=0`` 持续出现来发现 URL 形态变了。"""
    cache = _cache(tmp_path)
    assert cache.load(URL, PARAMS) is None          # miss
    cache.store(cache.make_entry(URL, PARAMS, RAW))  # write
    assert cache.load(URL, PARAMS) is not None       # hit

    assert cache.stats.asdict() == {
        "hits": 1,
        "misses": 1,
        "writes": 1,
        "write_errors": 0,
    }
    assert cache.asdict()["read_enabled"] is True


def test_two_caches_over_the_same_root_share_entries(tmp_path):
    """跨进程/跨批次共用同一目录——这正是缓存放在批次之外的原因。"""
    first = _cache(tmp_path)
    first.store(first.make_entry(URL, PARAMS, RAW))

    second = _cache(tmp_path)
    loaded = second.load(URL, PARAMS)
    assert loaded is not None and loaded.raw_json == RAW
