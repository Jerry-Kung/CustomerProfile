"""worker 进程入口：从持久化队列领取任务并执行。

    python -m customer_profile.worker

与 API 进程的关系（既定约束「部署 = Docker Compose，含 API、
按任务类型隔离的执行 worker、数据库」）：

- API 进程负责提交（把运行写进队列）与查询；
- worker 进程负责执行。两者共用一个 SQLite 文件，靠 ``Store`` 的原子领取避免双跑。

**只从队列领取，不创建队列。** 入队是提交方的职责；worker 启动时按配置把上次崩溃
遗留的 ``running`` 标为 ``interrupted``（交人工），未开始的 ``queued`` 继续消费。
租约过期的运行不自动重领，避免重复产生外部副作用。
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
from typing import Any

from loguru import logger

from . import liveness
from .definitions import RunStatus
from .logging_config import configure_logging
from .runner import Service, build_service
from .settings import Settings, get_settings


class Worker:
    """队列消费者。

    领取→执行→写终态。未开始的任务保留在队列中；中断的运行交人工处理，不自动续跑。
    """

    def __init__(self, service: Service, settings: Settings) -> None:
        self.service = service
        self.settings = settings
        self.worker_id = settings.resolved_worker_id
        self._stopping = asyncio.Event()
        self._consumers: list[asyncio.Task[None]] = []
        self._executions: set[asyncio.Task[None]] = set()
        self._execution_error: BaseException | None = None
        self._last_retention_ms: int | None = None
        """上次跑保留期清理的时刻。按天节流，不引入外部 cron。"""

        self._last_claim_ms: int | None = None
        """最近一次成功领取的时刻。写进存活信号，用来区分「空闲无任务」与「领不到任务」。"""

        self._liveness_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------ 生命周期

    def request_stop(self) -> None:
        """请求停机。**不打断在飞运行**——让它们自然结束，未开始的留给别的 worker。

        不立即取消是刻意的：一次画像流程跑二十余分钟，中途取消会留下一个需要人工
        处理的中间状态；而队列本身已经保证「不丢任务」，因此优雅停机更划算。
        """
        self._stopping.set()

    async def run_forever(self) -> None:
        """主循环。收到停机请求后等所有在飞运行结束再返回。"""
        settings = self.settings
        if self.service.store is None:
            raise RuntimeError("worker 需要留痕存储；请确认 DATABASE_URL 可用")

        # 启动恢复：把上次崩溃遗留的 running 标为 interrupted 交人工处理。
        #
        # **刻意不自动重领租约过期的运行**（见 `docs/history/` 的交付报告）：
        # 重领会把一条跑到一半的运行**整条重跑**，而根目录文档明确「不做任意节点断点
        # 自动续跑」，且「worker 停机/取消/客户端断开不证明外部副作用未发生」——在
        # 幂等回写落地（V0.5.5）之前，自动重跑会重复触发外部副作用。
        # 因此重领保留为**显式的人工动作**（Store.requeue_expired_leases，
        # 供后续版本的人工处理入口调用），不在启动路径上自动执行。
        #
        # 「worker 重启不丢任务」由**未开始的 queued 任务不会被标成 interrupted** 保证：
        # 它们仍在队列里，本进程下一轮就会领走。
        if settings.interrupt_on_start:
            marked = await self.service.store.mark_running_as_interrupted(
                workflow_scope=settings.workflow_scope
            )
            if marked:
                logger.warning("启动恢复：{} 个残留任务已标为中断（interrupted），待人工处理", marked)

        logger.info(
            "执行进程已启动，标识={}，任务范围={}，执行模式={}，回写启用={}，回放模式={}",
            self.worker_id, settings.workflow_scope,
            settings.worker_concurrency or "跟随 backend 派发",
            settings.writeback_enabled, settings.replay_mode,
        )
        # 存活信号与消费**分开**驱动：消费循环卡死时这里也停更，健康检查才看得出
        # 「执行已停止」。放在同一循环里刷新，恰恰会在最需要它的时刻一起停掉。
        self._liveness_task = asyncio.create_task(
            self._liveness_loop(), name="worker-liveness"
        )
        try:
            if settings.worker_concurrency is None:
                await self._dispatch()
            else:
                # 保留本地兼容模式；Compose 不配置第二套业务并发。
                self._consumers = [
                    asyncio.create_task(self._consume(), name=f"queue-consumer-{index}")
                    for index in range(settings.worker_concurrency)
                ]
                await asyncio.gather(*self._consumers)
        finally:
            # 任一槽位异常时停止其他槽位的新领取，等已领取的运行收敛后再退出。
            self.request_stop()
            await self._drain()
            if self._liveness_task is not None:
                self._liveness_task.cancel()
                await asyncio.gather(self._liveness_task, return_exceptions=True)
        if self._execution_error is not None:
            raise self._execution_error
        logger.info("执行进程已停止，标识={}", self.worker_id)

    async def _dispatch(self) -> None:
        """把 backend 已派发的持久化任务交给执行协程。

        **在飞主运行有上限**（``WORKER_CLAIM_LIMIT``）。不设上限的重启表现是：
        积压的几十条被同时领走，共用同一批出网请求名额，录音、模型分析与回写
        互相等待——每条都在跑、每条都很慢，看起来像「重启后长时间不动」。

        上限按**已完成收敛**而非定时轮询：一处 ``await`` 等待任一在飞任务结束，
        再领下一条。这样既不会空转，也不会在积压时一次性抽干队列。
        """
        limit = max(1, self.settings.worker_claim_limit)
        while not self._stopping.is_set():
            # 名额已满：等在飞任务中任意一个结束（或收到停机请求）。
            while len(self._executions) >= limit and not self._stopping.is_set():
                await self._await_any_execution()

            if self._stopping.is_set():
                break

            claimed = await self._claim()
            if claimed is None:
                await self._wait_for_work()
                continue
            task = asyncio.create_task(
                self._execute_claimed(claimed), name=f"queued-run-{claimed['run_id']}"
            )
            self._executions.add(task)
            task.add_done_callback(self._execution_finished)

    async def _await_any_execution(self) -> None:
        """等待任一在飞任务结束或收到停机请求，先到者为准。"""
        pending = [task for task in self._executions if not task.done()]
        if not pending:
            return
        stopper = asyncio.create_task(self._stopping.wait())
        try:
            await asyncio.wait(
                [*pending, stopper], return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not stopper.done():
                stopper.cancel()

    def _write_liveness(self) -> None:
        """刷新存活信号。失败不阻断执行，见 ``liveness.write`` 的说明。"""
        now_ms = int(time.time() * 1000)
        if self._last_claim_ms is None:
            self._last_claim_ms = now_ms
        try:
            liveness.write(
                self.settings,
                worker_id=self.worker_id,
                active_runs=len(self._executions),
                last_claim_at_ms=self._last_claim_ms,
            )
        except Exception:  # 观测手段不该成为新的故障源
            pass

    async def _liveness_loop(self) -> None:
        """周期性刷新存活信号。**与消费循环分开**，这样消费卡住时信号也会停更。"""
        interval = max(1.0, self.settings.worker_liveliness_interval_s())
        while not self._stopping.is_set():
            self._write_liveness()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def _execution_finished(self, task: asyncio.Task[None]) -> None:
        self._executions.discard(task)
        if task.cancelled():
            self._execution_error = self._execution_error or asyncio.CancelledError()
            self.request_stop()
        elif error := task.exception():
            self._execution_error = self._execution_error or error
            self.request_stop()

    async def _maybe_run_retention(self) -> None:
        """按天清理过期包。**只删包，不删骨架**，也不碰正在写的树。"""
        from .persistence import retention

        now_ms = int(time.time() * 1000)
        if not retention.should_run(self._last_retention_ms, now_ms=now_ms):
            return
        self._last_retention_ms = now_ms
        try:
            await asyncio.to_thread(retention.run_once, self.settings)
        except Exception as exc:
            # 清理失败不该让 worker 退出：包多一点只是占空间，停了就不干活了。
            logger.warning("留痕包保留期清理失败：{}: {}", type(exc).__name__, exc)

    async def _wait_for_work(self) -> None:
        await self._maybe_run_retention()
        try:
            await asyncio.wait_for(
                self._stopping.wait(), timeout=self.settings.worker_poll_interval_ms / 1000
            )
        except asyncio.TimeoutError:
            pass

    async def _consume(self) -> None:
        """一个固定消费槽位：每次等待当前运行完成后再领取下一个。"""
        while not self._stopping.is_set():
            ran = await self.run_once()
            if not ran:
                await self._wait_for_work()

    async def _drain(self) -> None:
        """等各消费槽位完成已领取的运行，不取消正在执行的工作流。"""
        pending = [task for task in self._consumers if not task.done()]
        pending.extend(self._executions)
        if pending:
            logger.info("执行进程正在关停，等待 {} 个执行任务结束", len(pending))
            await asyncio.gather(*pending, return_exceptions=True)
        self._consumers.clear()
        self._executions.clear()

    async def aclose(self) -> None:
        await self.service.aclose()
        if self.service.store is not None:
            self.service.store.close()

    # ------------------------------------------------------------ 单轮

    async def run_once(self) -> bool:
        """领取并执行一个任务。领取到并执行完成返回 ``True``，队列空返回 ``False``。

        手动调用时内联等待；生产自动派发模式使用同一领取与执行逻辑。
        """
        claimed = await self._claim()
        if claimed is None:
            return False
        await self._execute_claimed(claimed)
        return True

    async def _claim(self) -> dict[str, Any] | None:
        if self._stopping.is_set():
            return None
        store = self.service.store
        if store is None:
            return None
        claimed = await store.claim_next_queued_run(
            self.worker_id, lease_ms=self.settings.worker_lease_ms,
            workflow_scope=self.settings.workflow_scope,
        )
        if claimed is not None:
            self._last_claim_ms = int(time.time() * 1000)
        return claimed

    async def _execute_claimed(self, claimed: dict[str, Any]) -> None:
        run_id = str(claimed["run_id"])
        workflow_id = str(claimed["workflow_id"])
        inputs = _decode_inputs(claimed.get("inputs_json"))
        business_ref = claimed.get("business_ref")
        with logger.contextualize(run_id=run_id):
            logger.info("任务已领取，工作流={}，执行进程={}", workflow_id, self.worker_id)
            heartbeat = asyncio.create_task(self._heartbeat(run_id))
            try:
                await self._execute(run_id, workflow_id, inputs, business_ref)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                # 在飞条数变了，立刻刷新一次：健康检查看到的是真实占用，
                # 不必等下一个周期。
                self._write_liveness()

    async def _execute(
        self,
        run_id: str,
        workflow_id: str,
        inputs: dict[str, Any],
        business_ref: str | None,
    ) -> None:
        """执行一个已领取的运行。

        失败**不重新抛**：运行的结果已由留痕层写进库（``failed`` 或终态），worker
        继续领下一个。把异常抛出去只会让进程退出，而队列里的其它任务还等着跑。
        """
        scheduler = self.service.scheduler
        if scheduler is None:
            raise RuntimeError("worker 需要调度器；服务尚未 initialize()")
        try:
            workflow = self.service.require_workflow(workflow_id)
        except KeyError as exc:
            # 定义已不存在的运行：如实标失败，否则它会永远停在 running。
            logger.bind(run_id=run_id).error("任务执行失败，工作流不可用：{}", exc)
            await self.service.store.update_run_finished(
                run_id, status=RunStatus.FAILED, error=str(exc), duration_ms=0
            )
            return

        from .execution.scheduler import RunRequest

        try:
            await scheduler.run(
                RunRequest(
                    workflow=workflow,
                    inputs=inputs,
                    run_id=run_id,
                    business_ref=business_ref,
                )
            )
        except Exception as exc:  # 调度器已自行落终态；这里只防进程退出
            logger.bind(run_id=run_id).exception("任务执行异常：{}: {}", type(exc).__name__, exc)

    async def _heartbeat(self, run_id: str) -> None:
        """周期性续租。租约失效会导致该运行被别的 worker 重领并跑第二遍。"""
        store = self.service.store
        if store is None:
            return
        interval = max(1.0, self.settings.worker_heartbeat_ms / 1000)
        while True:
            await asyncio.sleep(interval)
            ok = await store.renew_lease(
                run_id, self.worker_id, lease_ms=self.settings.worker_lease_ms
            )
            if not ok:
                # 运行已不在本 worker 名下（被取消或已终态），停止续租。
                return


def _decode_inputs(raw: Any) -> dict[str, Any]:
    """把库里的 ``inputs_json`` 解回入参。取不到时返回空字典而不是抛错。"""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _install_signal_handlers(worker: Worker, loop: asyncio.AbstractEventLoop) -> None:
    """SIGINT / SIGTERM 转成优雅停机请求。"""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows 上 SIGTERM 不可用；SIGINT 走 KeyboardInterrupt 的缺省处理。
            continue


async def main_async() -> int:
    # 独立入口自带消费者，不能再由 build_service 创建第二个内联消费者。
    settings = get_settings().model_copy(update={"inline_worker": False})
    configure_logging(settings.log_level, component="worker")
    # 启动恢复统一由 Worker.run_forever 在消费前执行一次。
    service = await build_service(settings, recover_running_runs=False)
    worker = Worker(service, settings)
    loop = asyncio.get_running_loop()
    _install_signal_handlers(worker, loop)
    try:
        await worker.run_forever()
    except KeyboardInterrupt:
        worker.request_stop()
        await worker._drain()
    finally:
        await worker.aclose()
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
