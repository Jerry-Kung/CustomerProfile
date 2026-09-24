"""worker 进程入口：从持久化队列领取任务并执行。

    python -m customer_profile.worker

与 API 进程的关系（`Dify迁移任务说明.md` §4「部署 = Docker Compose，含 API、
单个执行 worker、数据库」）：

- API 进程负责提交（把运行写进队列）与查询；
- worker 进程负责执行。两者共用一个 SQLite 文件，靠 ``Store`` 的原子领取避免双跑。

**只从队列领取，不创建队列。** 入队是提交方的职责；worker 启动时只做两件事：
把上次崩溃遗留的 ``running`` 标为 ``interrupted``（交人工），以及把租约过期的
运行退回队列（这是「重启不丢任务」的落地）。
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from typing import Any

from .definitions import RunStatus
from .runner import Service, build_service
from .settings import Settings, get_settings


class Worker:
    """队列消费者。

    领取→执行→写终态，循环往复。所有状态都在库里，因此进程随时可以被打断——
    下次启动会接着做（经租约过期回收），不需要内存里的状态存活。
    """

    def __init__(self, service: Service, settings: Settings) -> None:
        self.service = service
        self.settings = settings
        self.worker_id = settings.resolved_worker_id
        self._stopping = asyncio.Event()
        self._inflight: dict[str, asyncio.Task[Any]] = {}

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
        # **刻意不自动重领租约过期的运行**（与计划里的一句话不同，见交付报告）：
        # 重领会把一条跑到一半的运行**整条重跑**，而根目录文档明确「不做任意节点断点
        # 自动续跑」，且「worker 停机/取消/客户端断开不证明外部副作用未发生」——在
        # 幂等回写落地（V0.5.5）之前，自动重跑会重复触发外部副作用。
        # 因此重领保留为**显式的人工动作**（Store.requeue_expired_leases，
        # 供后续版本的人工处理入口调用），不在启动路径上自动执行。
        #
        # 「worker 重启不丢任务」由**未开始的 queued 任务不会被标成 interrupted** 保证：
        # 它们仍在队列里，本进程下一轮就会领走。
        if settings.interrupt_on_start:
            marked = await self.service.store.mark_running_as_interrupted()
            if marked:
                print(
                    f"[worker] {marked} 个残留运行标为 interrupted，待人工处理",
                    flush=True,
                )

        print(f"[worker] 启动，标识 {self.worker_id}", flush=True)
        while not self._stopping.is_set():
            ran = await self.run_once()
            if not ran:
                # 空轮询：等一会儿再看。用 wait 而不是 sleep，停机请求能立刻打断等待。
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=settings.worker_poll_interval_ms / 1000,
                    )
                except asyncio.TimeoutError:
                    pass

        await self._drain()

    async def _drain(self) -> None:
        """等在飞运行结束。"""
        if not self._inflight:
            return
        print(f"[worker] 等待 {len(self._inflight)} 个在飞运行结束", flush=True)
        await asyncio.gather(*self._inflight.values(), return_exceptions=True)

    async def aclose(self) -> None:
        await self.service.aclose()
        if self.service.store is not None:
            self.service.store.close()

    # ------------------------------------------------------------ 单轮

    async def run_once(self) -> bool:
        """领取并执行一个任务。领取到并执行完成返回 ``True``，队列空返回 ``False``。

        执行是**内联等待**的（不派生后台任务）：worker 的并发度由
        ``MAX_ACTIVE_RUNS`` 控制，本轮只做单 worker 串行消费，语义最简单也最容易解释。
        需要并行时把并发闸门接在这一层，而不是靠在这里派生多个 task。
        """
        store = self.service.store
        if store is None:
            return False
        claimed = await store.claim_next_queued_run(
            self.worker_id, lease_ms=self.settings.worker_lease_ms
        )
        if claimed is None:
            return False

        run_id = str(claimed["run_id"])
        workflow_id = str(claimed["workflow_id"])
        inputs = _decode_inputs(claimed.get("inputs_json"))
        business_ref = claimed.get("business_ref")
        print(f"[worker] 领取 {run_id}（{workflow_id}）", flush=True)

        heartbeat = asyncio.create_task(self._heartbeat(run_id))
        try:
            await self._execute(run_id, workflow_id, inputs, business_ref)
        finally:
            heartbeat.cancel()
        return True

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
            print(f"[worker] {run_id} 的工作流不可用：{exc}", file=sys.stderr, flush=True)
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
            print(
                f"[worker] {run_id} 执行异常：{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

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
    settings = get_settings()
    service = await build_service(settings)
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
