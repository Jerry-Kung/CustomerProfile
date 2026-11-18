"""留痕：四层记录落 SQLite。

对齐 既定约束 的四层：

============  ==================================================
层            内容
============  ==================================================
工作流运行     run_id、业务标识、流程版本、输入、最终输出、状态、起止、耗时、错误
节点执行       节点 ID、调用路径、输入输出、状态、排队/执行时间、错误、父子关联
请求尝试       attempt_no、实际模型/接口、实际请求与响应、耗时、错误码、usage
版本快照       当次图结构与提示词的哈希、代码提交标识
============  ==================================================

两条实现口径，都是刻意的：

1. **子流程与子运行共表**，用 ``parent_run_id`` + ``call_path`` 区分。V0.4 要能下钻
   到 17 处 ``Gemini`` 调用的每一次尝试，树形自引用比另建子表更好查。
2. **定义快照随运行一起存**。历史运行展示的是当时的图与提示词版本，新版本不得覆盖
   旧运行的展示（既定约束「版本快照」）。
"""

from __future__ import annotations

import base64
import gzip
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..definitions import RunStatus

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS workflow_runs (
        run_id            TEXT PRIMARY KEY,
        workflow_id       TEXT NOT NULL,
        workflow_display_name TEXT,
        parent_run_id     TEXT REFERENCES workflow_runs(run_id),
        parent_node_id    TEXT,
        call_path         TEXT NOT NULL DEFAULT '',
        business_ref      TEXT,
        status            TEXT NOT NULL,
        definition_version_id INTEGER REFERENCES definition_versions(version_id),
        inputs_json       TEXT,
        outputs_json      TEXT,
        error             TEXT,
        started_at_ms     INTEGER,
        finished_at_ms    INTEGER,
        duration_ms       INTEGER,
        is_replay         INTEGER NOT NULL DEFAULT 0,
        queued_at_ms      INTEGER,
        claimed_at_ms     INTEGER,
        claimed_by        TEXT,
        lease_expires_at_ms INTEGER,
        created_at_ms     INTEGER NOT NULL,
        payload_state     TEXT,
        payload_mode      TEXT,
        archived_at_ms    INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS node_executions (
        execution_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id            TEXT NOT NULL REFERENCES workflow_runs(run_id),
        node_id           TEXT NOT NULL,
        node_title        TEXT,
        node_type         TEXT NOT NULL,
        call_path         TEXT NOT NULL DEFAULT '',
        status            TEXT NOT NULL,
        branch            TEXT,
        inputs_json       TEXT,
        outputs_json      TEXT,
        error             TEXT,
        queued_at_ms      INTEGER,
        started_at_ms     INTEGER,
        duration_ms       INTEGER,
        sub_run_id        TEXT,
        attempt_count     INTEGER NOT NULL DEFAULT 0,
        UNIQUE (run_id, node_id, call_path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS request_attempts (
        attempt_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id            TEXT NOT NULL REFERENCES workflow_runs(run_id),
        node_id           TEXT NOT NULL,
        call_path         TEXT NOT NULL DEFAULT '',
        attempt_no        INTEGER NOT NULL,
        kind              TEXT NOT NULL,
        target            TEXT,
        request_json      TEXT,
        response_text     TEXT,
        status_code       INTEGER,
        duration_ms       INTEGER,
        error             TEXT,
        error_code        TEXT,
        usage_json        TEXT,
        provider_request_id TEXT,
        replayed          INTEGER NOT NULL DEFAULT 0,
        created_at_ms     INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS definition_versions (
        version_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_id       TEXT NOT NULL,
        definition_hash   TEXT NOT NULL,
        definition_json   TEXT NOT NULL,
        template_hashes_json TEXT,
        code_commit       TEXT,
        created_at_ms     INTEGER NOT NULL,
        UNIQUE (workflow_id, definition_hash)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_workflow ON workflow_runs (workflow_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_parent ON workflow_runs (parent_run_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_business ON workflow_runs (business_ref)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_status ON workflow_runs (status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_runs_queue
        ON workflow_runs (status, created_at_ms) WHERE parent_run_id IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_nodes_run ON node_executions (run_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_attempts_run ON request_attempts (run_id, node_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_baselines (
        phone             TEXT PRIMARY KEY,
        compat_key        TEXT NOT NULL DEFAULT '',
        source_run_id     TEXT,
        updated_at_ms     INTEGER NOT NULL,
        stage             TEXT NOT NULL DEFAULT '',
        deal_level        TEXT NOT NULL DEFAULT '',
        deal_level_detail TEXT NOT NULL DEFAULT '',
        consumed_keys_json TEXT NOT NULL DEFAULT '[]',
        callback_json     TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worker_heartbeats (
        scope             TEXT PRIMARY KEY,
        worker_id         TEXT,
        active_runs       INTEGER NOT NULL DEFAULT 0,
        last_claim_at_ms  INTEGER,
        updated_at_ms     INTEGER NOT NULL,
        db_alive          INTEGER,
        db_error          TEXT,
        db_last_ok_at_ms  INTEGER
    )
    """,
)


@dataclass(slots=True)
class RunRecordRow:
    """``workflow_runs`` 的一行。"""

    run_id: str
    workflow_id: str
    workflow_display_name: str | None = None
    parent_run_id: str | None = None
    parent_node_id: str | None = None
    call_path: str = ""
    business_ref: str | None = None
    status: str = RunStatus.QUEUED
    definition_version_id: int | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    duration_ms: int | None = None
    is_replay: bool = False
    created_at_ms: int = 0

    # ---- 队列（V0.5.2）。持久化队列复用本表，故这四个字段与运行记录同表。
    queued_at_ms: int | None = None
    """入队时刻。与 ``created_at_ms`` 分开：一次运行可能被重新入队。"""

    claimed_at_ms: int | None = None
    """被 worker 领取的时刻。"""

    claimed_by: str | None = None
    """领取它的 worker 标识（``主机名:pid``），用于区分「谁在跑」。"""

    lease_expires_at_ms: int | None = None
    """租约到期时刻。worker 崩溃后过期即可被重新领取——这是「重启不丢任务」的实现点。"""

    @property
    def is_child(self) -> bool:
        return self.parent_run_id is not None

    def asdict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "workflow_display_name": self.workflow_display_name,
            "parent_run_id": self.parent_run_id,
            "parent_node_id": self.parent_node_id,
            "call_path": self.call_path,
            "business_ref": self.business_ref,
            "status": self.status,
            "definition_version_id": self.definition_version_id,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "error": self.error,
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": self.finished_at_ms,
            "duration_ms": self.duration_ms,
            "is_replay": self.is_replay,
            "queued_at_ms": self.queued_at_ms,
            "claimed_at_ms": self.claimed_at_ms,
            "claimed_by": self.claimed_by,
            "lease_expires_at_ms": self.lease_expires_at_ms,
            "created_at_ms": self.created_at_ms,
        }


def definition_hash(definition: Mapping[str, Any]) -> str:
    """定义指纹：对导出的 JSON 结构做稳定哈希。"""
    import hashlib

    material = json.dumps(definition, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def json_dumps(value: Any) -> str | None:
    """把任意值序列化成 JSON 文本。不可序列化时退化为其 ``repr``，而不是抛错。

    留痕不应因为某个奇怪类型而让整次运行失败；但退化痕迹里会带上类型名，
    避免把「不可序列化」伪装成正常值。
    """
    if value is None:
        return None

    def fallback(obj: Any) -> dict[str, Any]:
        """把不可序列化的对象转成带类型名的占位。

        用 ``default`` 而不是捕获 ``json.dumps`` 的异常：``json.dumps`` 内部遇到
        不可序列化对象时抛的是 ``TypeError``，且**嵌套**的不可序列化值只会在遍历到
        它时才暴露，外面再套一层 try 会连整体一起丢掉。``default`` 是逐对象的钩子，
        因此嵌套结构里只有那个怪对象被替换，其余数据照常保留。
        """
        return {
            "_unserialisable": f"{type(obj).__name__}",
            "_repr": repr(obj)[:2000],
        }

    try:
        return json.dumps(value, ensure_ascii=False, default=fallback)
    except (TypeError, ValueError):
        return json.dumps(
            {
                "_unserialisable": f"{type(value).__name__}",
                "_repr": repr(value)[:2000],
            },
            ensure_ascii=False,
        )


PACKED_PREFIX = "gzip:"
"""压缩载荷列的前缀标记。

**为什么要有标记，而不是「打开时试两种解码」。** 同一列里会同时存在明文 JSON（历史行、
其它档位）与压缩文本（``none`` 档的根输出）。试错解码会把「恰好能解出来的乱码」当成
数据，而这类静默错误最难查。前缀让读侧只做一次字符串比较，语义是确定的。
"""


def json_dumps_packed(value: Any) -> str | None:
    """把值序列化成压缩后的文本（``gzip:`` + base64）。

    用途单一：``none`` 档的根运行最终画像直存骨架列，原样 JSON 实测 24.2 KB/主任务，
    压缩后 5.2 KB（21%），这一项占了该档体积的近一半。压缩/解压都在库线程里做，
    不额外占用事件循环。
    """
    if value is None:
        return None
    text = json_dumps(value)
    if text is None:
        return None
    blob = gzip.compress(text.encode("utf-8"), compresslevel=6)
    return PACKED_PREFIX + base64.b64encode(blob).decode("ascii")


def json_loads(text: str | None) -> Any:
    """解码 JSON 列。带 ``PACKED_PREFIX`` 的按压缩文本解，其余按明文 JSON 解。

    两种形态共用这一个入口，读路径因此不需要知道某一行是谁写的、按哪一档存的。
    """
    if not text:
        return None
    if isinstance(text, str) and text.startswith(PACKED_PREFIX):
        try:
            raw = gzip.decompress(base64.b64decode(text[len(PACKED_PREFIX) :]))
            return json.loads(raw.decode("utf-8"))
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            # 压缩块损坏是「数据坏了」，不是「没有数据」；如实标出，不冒充当空。
            return {"_unparsable": f"{type(exc).__name__}: {exc}"}
    try:
        return json.loads(text)
    except ValueError:
        return {"_unparsable": text[:2000]}
