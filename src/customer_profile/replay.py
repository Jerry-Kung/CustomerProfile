"""固定响应回放。

用途：在不接真实模型、不出网的前提下验证**流程等价**（模板渲染、确定性代码输出、
请求 payload）。真实模型效果须用真实样本验证，两者不可互相替代
（`Dify迁移任务说明.md` §9.2）。

回放分两层，共用同一种 fixture 文件格式：

- LLM：按「请求体指纹」匹配。命中后按出现序号依次取响应，支持同一请求的
  多次尝试返回不同响应（用来验证重试）。
- HTTP：按「方法 + URL」匹配，同样支持按序号排队多个响应。

严格模式下未命中直接失败。这是刻意的：静默返回空响应会让「没跑通」看起来像「跑通了」。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


@dataclass(slots=True)
class LlmFrame:
    """一条固定的 LLM 响应。"""

    text: str | None = None
    reasoning_content: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    model: str | None = None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "LlmFrame":
        return cls(
            text=raw.get("text"),
            reasoning_content=raw.get("reasoning_content"),
            usage=dict(raw.get("usage") or {}),
            model=raw.get("model"),
        )


@dataclass(slots=True)
class HttpFrame:
    """一条固定的 HTTP 响应。"""

    status_code: int = 200
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    bytes_base64: str | None = None
    """二进制响应的 base64。图片下载专用——JSON fixture 放不下裸字节。"""

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "HttpFrame":
        return cls(
            status_code=int(raw.get("status_code", 200)),
            text=raw.get("text", ""),
            headers=dict(raw.get("headers") or {}),
            bytes_base64=raw.get("bytes_base64"),
        )


class ReplayMiss(RuntimeError):
    """回放模式下没有匹配的固定响应。"""


@dataclass(slots=True)
class ReplayStats:
    """回放命中统计。用于验收报告说明「哪些调用是回放、哪些是真实」。"""

    llm_hits: int = 0
    llm_misses: int = 0
    http_hits: int = 0
    http_misses: int = 0

    @property
    def total_hits(self) -> int:
        return self.llm_hits + self.http_hits

    def asdict(self) -> dict[str, int]:
        return {
            "llm_hits": self.llm_hits,
            "llm_misses": self.llm_misses,
            "http_hits": self.http_hits,
            "http_misses": self.http_misses,
        }


class ReplaySource:
    """固定响应来源。

    ``llm`` 段：``{指纹: [响应, ...]}``；``http`` 段：``{"METHOD url": [响应, ...]}``。
    指纹默认取 ``model + messages`` 的 SHA256——同一提示词在不同模型下是不同请求。
    """

    def __init__(
        self,
        llm: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        http: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        *,
        strict: bool = True,
        name: str = "inline",
    ) -> None:
        self.strict = strict
        self.name = name
        self._llm: dict[str, list[LlmFrame]] = {
            key: [LlmFrame.from_raw(f) for f in frames]
            for key, frames in (llm or {}).items()
        }
        self._http: dict[str, list[HttpFrame]] = {
            key: [HttpFrame.from_raw(f) for f in frames]
            for key, frames in (http or {}).items()
        }
        self._llm_cursor: dict[str, int] = {}
        self._http_cursor: dict[str, int] = {}
        self.stats = ReplayStats()

    # ------------------------------------------------------------ 加载

    @classmethod
    def from_file(
        cls, path: Path | str, *, strict: bool = True
    ) -> "ReplaySource":
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return cls(
            llm=data.get("llm"),
            http=data.get("http"),
            strict=strict,
            name=Path(path).name,
        )

    @classmethod
    def from_directory(
        cls, directory: Path | str, *, strict: bool = True
    ) -> "ReplaySource":
        """把目录下所有 ``*.json`` 合并成一个来源。键冲突时报错，不静默覆盖。"""
        merged_llm: dict[str, list[Mapping[str, Any]]] = {}
        merged_http: dict[str, list[Mapping[str, Any]]] = {}
        files = sorted(Path(directory).glob("*.json"))
        for path in files:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            for key, frames in (data.get("llm") or {}).items():
                if key in merged_llm:
                    raise ValueError(
                        f"回放 fixture 键冲突（llm/{key}）：{path} 与已加载的某个文件重复"
                    )
                merged_llm[key] = frames
            for key, frames in (data.get("http") or {}).items():
                if key in merged_http:
                    raise ValueError(
                        f"回放 fixture 键冲突（http/{key}）：{path} 与已加载的某个文件重复"
                    )
                merged_http[key] = frames
        return cls(
            llm=merged_llm,
            http=merged_http,
            strict=strict,
            name=f"dir:{Path(directory).name}",
        )

    # ------------------------------------------------------------ LLM

    def next_llm(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """取下一条 LLM 固定响应。``strict`` 模式下未命中抛 :class:`ReplayMiss`。"""
        key = llm_fingerprint(payload)
        frames = self._llm.get(key)
        if not frames:
            self.stats.llm_misses += 1
            if self.strict:
                raise ReplayMiss(
                    f"回放 fixture {self.name} 中没有指纹 {key[:16]}… 的 LLM 响应。"
                    f"已知指纹 {len(self._llm)} 个。"
                    "如需录制，请用 scripts/record_fixture.py（V0.3）"
                )
            return None

        index = self._llm_cursor.get(key, 0)
        frame = frames[min(index, len(frames) - 1)]
        self._llm_cursor[key] = index + 1
        self.stats.llm_hits += 1
        return {
            "text": frame.text,
            "reasoning_content": frame.reasoning_content,
            "usage": dict(frame.usage),
            "model": frame.model or payload.get("model"),
        }

    # ------------------------------------------------------------ HTTP

    def next_http(self, method: str, url: str) -> Mapping[str, Any] | None:
        """取下一条 HTTP 固定响应。"""
        key = http_key(method, url)
        frames = self._http.get(key)
        if not frames:
            self.stats.http_misses += 1
            if self.strict:
                raise ReplayMiss(
                    f"回放 fixture {self.name} 中没有 {key!r} 的 HTTP 响应。"
                    f"已知端点 {sorted(self._http)}"
                )
            return None

        index = self._http_cursor.get(key, 0)
        frame = frames[min(index, len(frames) - 1)]
        self._http_cursor[key] = index + 1
        self.stats.http_hits += 1
        return {
            "status_code": frame.status_code,
            "text": frame.text,
            # 二进制响应（图片下载）用 base64 承载：fixture 是 JSON，放不下裸字节。
            # 没有它，录制的图片回放时会退回文本形态，与真实字节不一致。
            "bytes_base64": frame.bytes_base64,
            "headers": dict(frame.headers),
        }

    # ------------------------------------------------------------ 检查

    def missing_llm_keys(self) -> list[str]:
        return [k for k in self._llm if self._llm_cursor.get(k, 0) == 0]

    def missing_http_keys(self) -> list[str]:
        return [k for k in self._http if self._http_cursor.get(k, 0) == 0]

    def reset(self) -> None:
        self._llm_cursor.clear()
        self._http_cursor.clear()
        self.stats = ReplayStats()

    @property
    def keys(self) -> dict[str, list[str]]:
        return {"llm": sorted(self._llm), "http": sorted(self._http)}


def llm_fingerprint(payload: Mapping[str, Any]) -> str:
    """一个 LLM 请求的指纹：模型 + messages。"""
    material = json.dumps(
        {"model": payload.get("model"), "messages": payload.get("messages")},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def http_key(method: str, url: str) -> str:
    return f"{method.upper()} {url}"


def build_fixture(
    *,
    llm: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    http: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """构造 fixture 文件内容（供脚本或测试写出）。"""
    return {"llm": dict(llm or {}), "http": dict(http or {})}
