"""把调度事件写进留痕存储。

实现 :class:`~customer_profile.execution.scheduler.RunRecorder` 接口。所有写库操作
失败都不向上抛——留痕是观测手段，不应因为它自身的问题让业务运行失败；但会记录到
错误日志，避免「静默丢痕迹」被误当成「完整留痕成功」。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from loguru import logger

from ..definitions import NodeStatus, RunStatus
from ..execution.context import NodeResult
from ..execution.scheduler import NodeExecution, RunOutcome
from .schema import RunRecordRow
from .store import Store


_STATUS_LABELS = {
    RunStatus.SUCCEEDED: "成功",
    RunStatus.FAILED: "失败",
    RunStatus.CANCELLED: "已取消",
    RunStatus.INTERRUPTED: "已中断",
    RunStatus.SKIPPED: "已跳过",
    NodeStatus.BLOCKED: "前置阻塞",
}


def _status_label(status: str) -> str:
    label = _STATUS_LABELS.get(status)
    return f"{label}（{status}）" if label else status


def _error_summary(error: str | None) -> str:
    """保留技术错误，避免将 LLM HTTP 错误中附带的响应正文输出到日志。"""
    if not error:
        return "无"
    match = re.search(r"\bHTTP \d{3}:", error)
    if match:
        return f"{error[:match.end() - 1]}（响应详情见留痕）"
    return error


class RunTracker:
    """四层留痕的写入端。"""

    def __init__(
        self,
        store: Store,
        *,
        is_replay: bool = False,
        code_commit: str | None = None,
        fail_soft: bool = True,
        persist_mode: str | None = None,
        archive_dir: Path | None = None,
        compress_level: int = 6,
    ) -> None:
        self.store = store
        self.is_replay = is_replay
        self.code_commit = code_commit
        self.fail_soft = fail_soft
        self._version_ids: dict[str, int] = {}

        self.persist_mode = persist_mode
        """留痕留存档位；``None`` 表示不打包（单测与不需要归档的调用方）。

        **缺省 None 是刻意的**：打包会清空库内载荷并写文件，不适合作为任何调用方的
        隐性行为。只有正式装配路径（``runner.build_service``）才按配置打开。

        ``none`` 档不走「先写库再打包清空」这条路——它**一开始就不写载荷列**，
        因此下面三个写入方法各自按档位裁掉载荷字段，而不是靠事后的清空补上。
        """
        self._drop_payloads = persist_mode == "none"
        """``none`` 档：不写节点输入输出、不写请求体与响应原文。

        留在行上的只有骨架——状态、时间、耗时、错误、尝试的目标与状态码。这些是
        「哪一步失败、第几次调用、什么错误」的全部依据，体积可忽略（设计稿 §3.1 实测
        每主任务 33 KB），删掉之后连「这次运行存在过」都无从查起。
        """
        self.archive_dir = archive_dir
        self.compress_level = compress_level
        self._media_index: dict[str, str] = {}
        """本进程内的媒体索引：``sha256(原始字节) → 下载时的请求地址``。

        写包时把 ``data:`` URI 换成引用要靠它。**只在内存里**、按进程收集；
        查不到就写 ``url: null`` 并计数，不猜一个地址出来。"""

    # ------------------------------------------------------------ 版本快照

    async def register_definition(self, workflow: Any) -> int:
        """登记该工作流的定义快照。历史运行绑定当时的版本，新版本不覆盖旧展示。"""
        if workflow.workflow_id in self._version_ids:
            return self._version_ids[workflow.workflow_id]
        version_id = await self.store.ensure_definition_version(
            workflow.workflow_id,
            workflow.asdict(),
            code_commit=self.code_commit,
        )
        self._version_ids[workflow.workflow_id] = version_id
        return version_id

    # ------------------------------------------------------------ 运行层

    async def run_started(self, run: Any) -> None:
        logger.bind(run_id=run.run_id, workflow_id=run.workflow_id).info(
            "工作流开始，名称={}", run.workflow_display_name
        )
        try:
            version_id = await self.register_definition(run.workflow)
            row = RunRecordRow(
                run_id=run.run_id,
                workflow_id=run.workflow_id,
                workflow_display_name=run.workflow_display_name,
                parent_run_id=run.parent_run_id,
                parent_node_id=run.parent_node_id,
                call_path=run.call_path,
                business_ref=run.business_ref,
                status=RunStatus.RUNNING,
                definition_version_id=version_id,
                inputs=run.inputs,
                started_at_ms=run.started_at_ms,
                is_replay=self.is_replay,
            )
            await self.store.insert_run(row)
        except Exception as exc:
            self._report("run_started", run.run_id, exc, run_id=run.run_id)

    async def run_finished(self, run: Any, outcome: RunOutcome) -> None:
        level = "INFO"
        if outcome.status == RunStatus.FAILED:
            level = "ERROR"
        elif outcome.status in {RunStatus.INTERRUPTED, RunStatus.CANCELLED}:
            level = "WARNING"
        logger.bind(run_id=run.run_id, workflow_id=outcome.workflow_id).log(
            level,
            "工作流结束，状态={}，耗时={} ms，错误={}",
            _status_label(outcome.status),
            outcome.duration_ms,
            _error_summary(outcome.error),
        )
        try:
            # ``none`` 档只留根运行的最终输出。
            #
            # 子运行的 ``outputs_json`` 是中间结果，可由重跑重建；根运行的那一份是
            # 「当时我们回写了什么」，profile 接口返回的却是被人工确认覆盖后的当前
            # 值——这是整个 ``none`` 档唯一的载荷例外（设计稿 §4.2，实测 5.07 KB/主任务）。
            is_root = getattr(run, "parent_run_id", None) is None
            await self.store.update_run_finished(
                run.run_id,
                status=outcome.status,
                outputs=None if (self._drop_payloads and not is_root) else outcome.outputs,
                error=outcome.error,
                duration_ms=outcome.duration_ms,
                # 子运行的载荷本来就没写，标 skeleton 让读侧说「按档位不留」而不是
                # 「载荷还在库里」；根运行也一样，它只留最终输出这一项。
                payload_state="skeleton" if self._drop_payloads else None,
            )
        except Exception as exc:
            self._report("run_finished", run.run_id, exc, run_id=run.run_id)
            return
        # 只有根运行收尾才搬整棵树——子运行结束只是树的一枝，此时子树还没跑完。
        # ``none`` 档不写包：它连载荷都不写，没有可搬的东西。
        if self.persist_mode and self.persist_mode != "none" and getattr(run, "parent_run_id", None) is None:
            await self.archive_tree(run.run_id)

    # ------------------------------------------------------------ 节点层

    async def node_started(
        self, run: Any, node: Any, call_path: str | None = None
    ) -> None:
        """记下节点开始执行。

        ``call_path`` 由调度层显式给出，**不再回落到 ``run.call_path``**：那个字段曾被
        循环体覆盖，导致并发结束的兄弟节点把自己的开始行写进另一条调用路径。
        为兼容直接调用本方法且不传该参的旧路径，缺省仍回落一次。
        """
        effective_call_path = call_path if call_path is not None else run.call_path
        logger.bind(
            run_id=run.run_id, node_id=node.node_id, call_path=effective_call_path
        ).debug("节点开始，名称={}，类型={}", node.title, node.node_type)
        try:
            await self.store.upsert_node_execution(
                run_id=run.run_id,
                node_id=node.node_id,
                node_title=node.title,
                node_type=node.node_type,
                call_path=effective_call_path,
                status="running",
                inputs=None if self._drop_payloads else self._collect_inputs(run, node),
                queued_at_ms=run.node_started_at.get(node.node_id),
            )
        except Exception as exc:
            self._report(
                "node_started", node.node_id, exc,
                run_id=run.run_id, node_id=node.node_id,
            )

    async def node_finished(self, run: Any, execution: NodeExecution) -> None:
        """收尾一条节点记录，并把 ``attempt_count`` **派生**出来落库。

        为什么在这里派生而不是在 ``attempt_recorded`` 里自增：``fork_iteration``
        复制父上下文的 ``call_path``，而迭代循环体节点在库里是按
        ``<父路径>/iter:<id>/<n>`` 存的。按 ``node_ref.call_path`` 自增会更新到
        **零行**——静默失效，行上仍显示 0，且看不出哪里错了。这里用节点行自己的
        ``call_path`` 去数来源表，键必然对得上。
        """
        call_path = getattr(execution, "call_path", None) or run.call_path
        level = "ERROR" if execution.status == NodeStatus.FAILED else "DEBUG"
        logger.bind(
            run_id=run.run_id, node_id=execution.node_id, call_path=call_path
        ).log(
            level,
            "节点结束，名称={}，状态={}，耗时={} ms，错误={}",
            execution.title,
            _status_label(execution.status),
            execution.duration_ms,
            _error_summary(execution.error),
        )
        try:
            attempt_count = await self.store.count_attempts(
                run.run_id,
                node_id=execution.node_id,
                call_path=call_path,
            )
        except Exception as exc:
            # 数不出来不该让节点记录写不进去；保持原值（插入时为 0）。
            self._report(
                "count_attempts", execution.node_id, exc,
                run_id=run.run_id, node_id=execution.node_id,
            )
            attempt_count = None
        try:
            if self._drop_payloads and self._is_evidence_free(run, execution, attempt_count or 0):
                # ``node_started`` 已经写过一行（在飞状态要看得见）；这里收尾时判定它
                # 没有排障价值，直接删掉而不是再写一遍空载荷——后者只是把行留下。
                await self.store.delete_node_execution(
                    run_id=run.run_id,
                    node_id=execution.node_id,
                    call_path=call_path,
                )
                return
            await self.store.upsert_node_execution(
                run_id=run.run_id,
                node_id=execution.node_id,
                node_title=execution.title,
                node_type=execution.node_type,
                call_path=call_path,
                status=execution.status,
                branch=execution.branch,
                outputs=None if self._drop_payloads else execution.outputs,
                error=execution.error,
                queued_at_ms=execution.queued_at_ms,
                started_at_ms=execution.started_at_ms,
                duration_ms=execution.duration_ms,
                sub_run_id=execution.sub_run_id,
                attempt_count=attempt_count,
            )
        except Exception as exc:
            self._report(
                "node_finished", execution.node_id, exc,
                run_id=run.run_id, node_id=execution.node_id,
            )

    def _is_evidence_free(
        self, run: Any, execution: NodeExecution, attempt_count: int
    ) -> bool:
        """这一行节点记录在 ``none`` 档里有没有排障价值。没有就整行不留。

        **为什么能删行而不只是删列。** ``none`` 档不存载荷，一行节点记录剩下的信息量
        只有「这个节点跑过、成功/跳过、多久、有没有外部调用」。实测 UAT 18 小时
        （792 主任务、169,866 行）：子运行里的节点占 79%，其中绝大多数是「成功且无
        任何外部调用」——它们只说明图的结构，而图本身在 ``definition_versions`` 里，
        可由定义快照完整重建。按本规则裁剪后节点行剩 30%，整档从 41 KB/主任务
        降到 14 KB。

        四条**保留**理由，每条都对应一类真实问题：

        1. **有外部调用**（``attempt_count > 0``）——排障主证据在 ``request_attempts``
           里，节点行是把它挂到图上的钩子。删了行，尝试就成了孤儿记录。
        2. **失败/阻塞**——原因只在节点行上，必须留。
        3. **有 ``branch``**——``if-else`` 走了哪条分支的唯一记录。实测 9,177 行带
           ``true``/``false``，「这个分支为什么没走」只能由它回答。这类行必须留下，
           不能因为「没有外部调用」就一并删。
        4. **``sub_run_id`` 非空**——父子运行链接。子运行自己那行也要能被找到。

        ``skipped`` 行**不留**：实测 54,250 行，每行内容只有一句「前置 xxx 未具备分支
        条件，节点未执行」——分支是否走到由第 3 条的 ``branch`` 行回答，这一行不提供
        额外信息。代价是图上看到缺行时无法区分「跳过」与「按档位不留」，因此运行台
        必须如实标注档位（见设计稿 §4.4）。

        另外：**根运行的节点行一律保留**。根图是排障时第一眼要看的东西，而它只占
        全部节点行的 21%（实测 35,496 / 169,866），删它的收益远小于让根图变残缺。
        """
        if getattr(run, "parent_run_id", None) is None:
            return False
        if attempt_count > 0:
            return False
        if getattr(execution, "branch", None):
            return False
        if getattr(execution, "sub_run_id", None):
            return False
        # 成功与跳过都不留行；失败、阻塞、在飞一律留。
        return getattr(execution, "status", None) in (
            NodeStatus.SUCCEEDED,
            NodeStatus.SKIPPED,
        )

    def _collect_inputs(self, run: Any, node: Any) -> dict[str, Any]:
        """把节点的实际入参快照下来：来源节点与字段名，以及已解析出的值。

        来源与值都记。只记值会丢掉「这个值是从哪个节点的哪个字段来的」，而排查
        变量错位恰恰需要这一条；只记来源则无法复现当次运行喂进去的是什么。
        """
        snapshot: dict[str, Any] = {}
        ctx = self._context_for(run)
        for binding in node.bindings:
            source = f"{binding.source[0]}.{binding.source[1]}"
            entry: dict[str, Any] = {"source": source}
            if ctx is not None:
                try:
                    entry["value"] = ctx.resolve(binding.source)
                except Exception:
                    # 前置未产出时值为未知，如实标记，不猜测
                    entry["value"] = None
                    entry["unavailable"] = True
            snapshot[binding.target] = entry
        return snapshot

    def _context_for(self, run: Any) -> Any:
        """取该运行的上下文。调度层把上下文挂在 scheduler 上，按 run_id 索引。"""
        scheduler = getattr(self, "scheduler", None)
        if scheduler is None:
            return None
        lookup = getattr(scheduler, "context_of", None)
        if lookup is None:
            return None
        return lookup(run.run_id)

    # ------------------------------------------------------------ 尝试层

    async def attempt_recorded(
        self, node_ref: Any, attempt: Any, payload: Mapping[str, Any] | None = None
    ) -> None:
        """写入一次外部请求尝试。LLM 与 HTTP 共用这一入口。"""
        try:
            run_id = getattr(node_ref, "run_id", None)
            node_id = getattr(node_ref, "node_id", None)
            call_path = getattr(node_ref, "call_path", "")
            if not run_id or not node_id:
                return

            kind = "http" if hasattr(attempt, "status_code") else "llm"
            target = (
                f"{attempt.method} {attempt.url}"
                if kind == "http"
                else (payload or {}).get("model")
            )
            # 两种尝试对象的字段不完全一致（HttpAttempt 没有 provider_request_id、
            # usage）。统一用 getattr 读取，避免因字段差异让整条记录写不进去——
            # 那正是「看起来记了、其实没记」的成因。
            usage = None
            if kind == "llm" and not self._drop_payloads:
                usage_obj = getattr(attempt, "usage", None)
                usage = usage_obj.asdict() if usage_obj is not None else None

            self._index_media(attempt)

            await self.store.insert_attempt(
                run_id=run_id,
                node_id=node_id,
                call_path=call_path,
                attempt_no=attempt.attempt_no,
                kind=kind,
                target=target,
                request=None
                if self._drop_payloads
                else (attempt.asdict().get("request") or attempt.asdict()),
                response_text=None if self._drop_payloads else attempt.response_text,
                status_code=getattr(attempt, "status_code", None),
                duration_ms=attempt.duration_ms,
                error=attempt.error,
                error_code=attempt.error_code,
                usage=usage,
                provider_request_id=None
                if self._drop_payloads
                else getattr(attempt, "provider_request_id", None),
                replayed=attempt.replayed,
                anomaly=getattr(attempt, "anomaly", None),
                finish_reason=getattr(attempt, "finish_reason", None),
            )
            # 先保存原始尝试，再记录摘要；兼容没有 succeeded 属性的录制/测试对象。
            status_code = getattr(attempt, "status_code", None)
            succeeded = getattr(attempt, "succeeded", None)
            if succeeded is None:
                succeeded = not (attempt.error or getattr(attempt, "anomaly", None))
                if kind == "http":
                    succeeded = succeeded and isinstance(status_code, int) and status_code < 400
            logger.bind(
                run_id=run_id, node_id=node_id, call_path=call_path
            ).log(
                "DEBUG" if succeeded else "WARNING",
                "外部请求尝试结束，类型={}，次数={}，结果={}，状态码={}，耗时={} ms，错误码={}，错误={}",
                kind,
                attempt.attempt_no,
                _status_label(RunStatus.SUCCEEDED if succeeded else RunStatus.FAILED),
                status_code,
                attempt.duration_ms,
                attempt.error_code,
                _error_summary(attempt.error or getattr(attempt, "anomaly", None)),
            )
        except Exception as exc:
            self._report(
                "attempt_recorded", getattr(node_ref, "node_id", "?"), exc,
                run_id=getattr(node_ref, "run_id", None),
                node_id=getattr(node_ref, "node_id", None),
            )

    def _index_media(self, attempt: Any) -> None:
        """记下「这份字节是从哪个地址下载来的」，供写包时把 data URI 换回 URL。

        盘上唯一可靠的对应关系就在下载那一刻：``response_bytes`` 与随后生成的
        ``data:...;base64,`` 是同一份字节，按 sha256 即可对上。这里只记 http 下载的
        地址；LLM 请求里的图片引用不单独记——它们的来源同样是个 http 下载。

        地址按**服务相对路径**留（http 留痕本来就按相对路径寻址），不是完整 URL：
        完整 URL 带环境差异，换环境后反而对不上。
        """
        raw = getattr(attempt, "response_bytes", None)
        url = getattr(attempt, "url", None)
        if not raw or not url:
            return
        try:
            digest = hashlib.sha256(raw).hexdigest()
        except TypeError:
            return
        self._media_index.setdefault(digest, str(url))

    async def archive_tree(self, root_run_id: str) -> None:
        """把整棵运行树搬进包；失败**不抛**，库内载荷保持原样。

        留痕是观测手段，不能因为归档写不出来就让业务运行失败。失败时状态停在
        ``in_db``，由补收尾扫描（worker 定时任务）重试。
        """
        if not self.persist_mode or self.persist_mode == "none" or self.archive_dir is None:
            return
        try:
            written = await self.store.finalize_tree(
                root_run_id,
                mode=self.persist_mode,
                archive_dir=self.archive_dir,
                media_index=dict(self._media_index),
                code_commit=self.code_commit,
                compress_level=self.compress_level,
            )
        except Exception as exc:
            self._report("archive_tree", root_run_id, exc, run_id=root_run_id)
            return
        logger.bind(run_id=root_run_id).info(
            "运行树已归档，档位={}，包={}，大小={} KB，运行={}，节点={}，尝试={}",
            written.get("mode"),
            written.get("path"),
            round(int(written.get("bytes") or 0) / 1024, 1),
            (written.get("counts") or {}).get("runs"),
            (written.get("counts") or {}).get("nodes"),
            (written.get("counts") or {}).get("attempts"),
        )

    # ------------------------------------------------------------ 内部

    def _report(
        self,
        where: str,
        subject: str,
        exc: Exception,
        *,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        message = f"[留痕] {where} 写入失败（{subject}）：{type(exc).__name__}: {exc}"
        if self.fail_soft:
            logger.bind(run_id=run_id, node_id=node_id).error(message)
            return
        raise RuntimeError(message) from exc
