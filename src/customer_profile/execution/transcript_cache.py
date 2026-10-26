"""录音转写结果的归档与复用。

**为什么需要这一层。** 语音识别是整条链路里最慢的一段——实测单条录音的 AUC 调用可达
20–30 分钟，而它又是流程里**唯一与被优化对象无关**的环节。识别结果是「录音内容 + 识别
参数」的函数：同一段录音、同一组参数，结果必然相同。把这份结果落在盘上，后续重跑工作流
时直接复用，就不必为「验证一处工作流改动的效果」反复支付识别的时间成本。

**为什么存云服务响应体原文，而不是裁剪后的六字段数组。** ``auc.parse_auc_result`` 的输出
字段与校对提示词的示例逐字段绑定（由 ``tests/test_auc.py`` 钉住）。存**原文**则复用路径与
真实路径**走同一个解析函数**，解析规则日后变化时复用路径自动跟随；存裁剪结果会把某一时刻
的解析规则冻进归档，形成一处不会随代码演进的静默分叉。原文本身也有独立价值：它是语音识别
的**原始**结论。

**键的构成**：``sha256(file_url + 识别参数)``。参数进键是刻意的——日后改了 ``model_name``
/ ``model_version`` / ``enable_ddc`` 等参数，旧条目**自然未命中**，走真实识别并存成新条目，
不会拿旧参数的转写冒充新配置的结果。

**为什么按 URL 做键是可行的。** 录音直链是稳定的、无签名的 TOS 链接（形如
``https://dfms.tos-cn-beijing.volces.com/cdp_customer_data/<号码>/<渠道>/<文件>``），不带
过期参数，因此同一个键在多次运行之间有效。若日后 URL 带上过期签名，命中率会掉到 0——
``CacheStats`` 与 runbook 记的正是这个判据。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from loguru import logger

CACHE_FORMAT = 1
"""归档格式版本。日后条目结构若变，据此判别旧条目，而不是靠猜。"""


def _canonical(value: Any) -> str:
    """稳定序列化。``sort_keys`` 保证同一内容永远得到同一份字节，键才有意义。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """一条录音的转写归档条目。"""

    file_url: str
    raw_json: str
    """云服务 ``/query`` 响应体**原文**。``silent=True`` 时为空串。"""

    params: dict[str, Any] = field(default_factory=dict)
    """识别参数（``build_submit_body(...)["request"]``）。进键，也落进文件便于人读。"""

    silent: bool = False
    """静音音频（``X-Api-Status-Code=20000003``）。服务端没有 utterances，故单独标记。"""

    captured_at: str = ""
    key: str = ""

    def asdict(self) -> dict[str, Any]:
        return {
            "format": CACHE_FORMAT,
            "key": self.key,
            "file_url": self.file_url,
            "params": self.params,
            "silent": self.silent,
            "captured_at": self.captured_at,
            "raw_json": self.raw_json,
        }

    @classmethod
    def fromdict(cls, payload: Any) -> "TranscriptEntry | None":
        """从文件内容还原。结构不符或版本不符时返回 ``None``，由调用方当作未命中。

        不抛异常是刻意的：一条坏条目不该让整批号码失败，退回真实识别即可。
        """
        if not isinstance(payload, Mapping):
            return None
        if payload.get("format") != CACHE_FORMAT:
            return None
        file_url = payload.get("file_url")
        raw_json = payload.get("raw_json")
        if not isinstance(file_url, str) or not isinstance(raw_json, str):
            return None
        params = payload.get("params")
        return cls(
            file_url=file_url,
            raw_json=raw_json,
            params=dict(params) if isinstance(params, Mapping) else {},
            silent=bool(payload.get("silent", False)),
            captured_at=str(payload.get("captured_at") or ""),
            key=str(payload.get("key") or ""),
        )


@dataclass
class CacheStats:
    """本次进程内的缓存计数。写进日志与 manifest，用于发现「缓存静默失效」。"""

    hits: int = 0
    misses: int = 0
    writes: int = 0
    write_errors: int = 0

    def asdict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "write_errors": self.write_errors,
        }


class TranscriptCache:
    """转写归档的文件缓存。

    ``read`` 与 ``write`` 分开是刻意的：``--no-transcript-cache`` 要的是「这次不复用」，
    而**仍然写入**才让后续运行有可复用的数据。两个开关正交，合成一个会逼着用户在
    「不复用」与「不记录」之间二选一。
    """

    def __init__(
        self,
        root: Path | str,
        *,
        read: bool = True,
        write: bool = True,
    ) -> None:
        self.root = Path(root)
        self.read_enabled = read
        self.write_enabled = write
        self.stats = CacheStats()

    # ------------------------------------------------------------ 键与路径

    def key_for(self, file_url: str, params: Mapping[str, Any]) -> str:
        """缓存键：录音 URL 与识别参数的稳定哈希。"""
        material = f"{file_url}\x00{_canonical(dict(params))}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        """条目路径。按前两位分桶，避免单目录堆上万文件。"""
        return self.root / key[:2] / f"{key}.json"

    # ------------------------------------------------------------ 读

    def load(self, file_url: str, params: Mapping[str, Any]) -> TranscriptEntry | None:
        """取一条转写。未命中/损坏/禁用读取时返回 ``None``，绝不抛。"""
        key = self.key_for(file_url, params)
        if not self.read_enabled:
            self.stats.misses += 1
            return None
        path = self.path_for(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.stats.misses += 1
            return None
        entry = TranscriptEntry.fromdict(payload)
        # 条目自记的键必须与本次计算的键一致：文件被放错位置（或哈希碰撞）时当作未命中，
        # 而不是拿别人的转写顶上——那会是一条极难察觉的静默错误。
        if entry is None or entry.key != key:
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return entry

    # ------------------------------------------------------------ 写

    def make_entry(
        self,
        file_url: str,
        params: Mapping[str, Any],
        raw_json: str,
        *,
        silent: bool = False,
    ) -> TranscriptEntry:
        """构造一条条目（已算好键）。"""
        return TranscriptEntry(
            file_url=str(file_url),
            raw_json=raw_json or "",
            params=dict(params),
            silent=bool(silent),
            captured_at=_now_iso(),
            key=self.key_for(file_url, params),
        )

    def store(self, entry: TranscriptEntry) -> Path | None:
        """落盘一条转写。失败记错误日志并返回 ``None``，不让整批号码因此失败。"""
        if not self.write_enabled:
            return None
        if not entry.key:
            entry = self.make_entry(
                entry.file_url, entry.params, entry.raw_json, silent=entry.silent
            )
        path = self.path_for(entry.key)
        try:
            _atomic_write_json(path, entry.asdict())
        except OSError as exc:
            self.stats.write_errors += 1
            logger.bind(cache_key=entry.key).error(
                "录音转写缓存写入失败，路径={}，错误={}: {}",
                path,
                type(exc).__name__,
                exc,
            )
            return None
        self.stats.writes += 1
        return path

    # ------------------------------------------------------------ 统计

    def asdict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "read_enabled": self.read_enabled,
            "write_enabled": self.write_enabled,
            "stats": self.stats.asdict(),
        }


def _atomic_write_json(path: Path, payload: Any) -> None:
    """原子写入 UTF-8 JSON（``ensure_ascii=False``、LF）。

    临时文件放在目标同目录：跨文件系统的 ``os.replace`` 不是原子的。4 路并发下两个号码
    可能同时识别同一条录音，后到的写入必须整体替换、不能交错。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
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
