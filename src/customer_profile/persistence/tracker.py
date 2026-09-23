"""把调度事件写进留痕存储。

实现 :class:`~customer_profile.execution.scheduler.RunRecorder` 接口。所有写库操作
失败都不向上抛——留痕是观测手段，不应因为它自身的问题让业务运行失败；但会记录到
标准错误，避免「静默丢痕迹」被误当成「完整留痕成功」
（`Dify迁移任务说明.md` §6 的最后一句）。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

from ..definitions import RunStatus
from ..execution.context import NodeResult
from ..execution.scheduler import NodeExecution, RunOutcome
from .schema import RunRecordRow
from .store import Store


class RunTracker:
    """四层留痕的写入端。"""

    def __init__(
        self,
        store: Store,
        *,
        is_replay: bool = False,
        code_commit: str | None = None,
        fail_soft: bool = True,
    ) -> None:
        self.store = store
        self.is_replay = is_replay
        self.code_commit = code_commit
        self.fail_soft = fail_soft
        self._version_ids: dict[str, int] = {}

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
            self._report("run_started", run.run_id, exc)

    async def run_finished(self, run: Any, outcome: RunOutcome) -> None:
        try:
            await self.store.update_run_finished(
                run.run_id,
                status=outcome.status,
                outputs=outcome.outputs,
                error=outcome.error,
                duration_ms=outcome.duration_ms,
            )
        except Exception as exc:
            self._report("run_finished", run.run_id, exc)

    # ------------------------------------------------------------ 节点层

    async def node_started(self, run: Any, node: Any) -> None:
        try:
            await self.store.upsert_node_execution(
                run_id=run.run_id,
                node_id=node.node_id,
                node_title=node.title,
                node_type=node.node_type,
                call_path=run.call_path,
                status="running",
                inputs=self._collect_inputs(run, node),
                queued_at_ms=run.node_started_at.get(node.node_id),
            )
        except Exception as exc:
            self._report("node_started", node.node_id, exc)

    async def node_finished(self, run: Any, execution: NodeExecution) -> None:
        """收尾一条节点记录，并把 ``attempt_count`` **派生**出来落库。

        为什么在这里派生而不是在 ``attempt_recorded`` 里自增：``fork_iteration``
        复制父上下文的 ``call_path``，而迭代循环体节点在库里是按
        ``<父路径>/iter:<id>/<n>`` 存的。按 ``node_ref.call_path`` 自增会更新到
        **零行**——静默失效，行上仍显示 0，且看不出哪里错了。这里用节点行自己的
        ``call_path`` 去数来源表，键必然对得上。
        """
        call_path = getattr(execution, "call_path", None) or run.call_path
        try:
            attempt_count = await self.store.count_attempts(
                run.run_id,
                node_id=execution.node_id,
                call_path=call_path,
            )
        except Exception as exc:
            # 数不出来不该让节点记录写不进去；保持原值（插入时为 0）。
            self._report("count_attempts", execution.node_id, exc)
            attempt_count = None
        try:
            await self.store.upsert_node_execution(
                run_id=run.run_id,
                node_id=execution.node_id,
                node_title=execution.title,
                node_type=execution.node_type,
                call_path=call_path,
                status=execution.status,
                branch=execution.branch,
                outputs=execution.outputs,
                error=execution.error,
                queued_at_ms=execution.queued_at_ms,
                started_at_ms=execution.started_at_ms,
                duration_ms=execution.duration_ms,
                sub_run_id=execution.sub_run_id,
                attempt_count=attempt_count,
            )
        except Exception as exc:
            self._report("node_finished", execution.node_id, exc)

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
            if kind == "llm":
                usage_obj = getattr(attempt, "usage", None)
                usage = usage_obj.asdict() if usage_obj is not None else None

            await self.store.insert_attempt(
                run_id=run_id,
                node_id=node_id,
                call_path=call_path,
                attempt_no=attempt.attempt_no,
                kind=kind,
                target=target,
                request=attempt.asdict().get("request") or attempt.asdict(),
                response_text=attempt.response_text,
                status_code=getattr(attempt, "status_code", None),
                duration_ms=attempt.duration_ms,
                error=attempt.error,
                error_code=attempt.error_code,
                usage=usage,
                provider_request_id=getattr(attempt, "provider_request_id", None),
                replayed=attempt.replayed,
            )
        except Exception as exc:
            self._report("attempt_recorded", getattr(node_ref, "node_id", "?"), exc)

    # ------------------------------------------------------------ 内部

    def _report(self, where: str, subject: str, exc: Exception) -> None:
        message = f"[留痕] {where} 写入失败（{subject}）：{type(exc).__name__}: {exc}"
        if self.fail_soft:
            print(message, file=sys.stderr, flush=True)
            return
        raise RuntimeError(message) from exc
