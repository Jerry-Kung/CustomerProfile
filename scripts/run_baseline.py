"""数据基线：用测试手机号清单逐号跑真实工作流，归档全部中间结果。

三项硬约束与冒烟脚本一致，另有两条本脚本特有的：

1. **不出生产回写。** 强制 ``writeback_enabled=False``——基线要的是画像数据，不是往生产
   回写。验收方式是查 ``request_attempts`` 里有没有 ``update-profile`` 目标，而不是看运行
   状态（回写被跳过的运行**仍然是 succeeded**，只看状态会误判）。
2. **真实链路。** 强制 ``replay_mode="off"``；回放模式下工作流会「静默跑通」，那不能当基线。
3. **不动别人的留痕。** ``interrupt_on_start=False``，且批次用自己的 ``trace.db``。
   ``build_service`` 默认会在启动时把共享库里残留的 running 标记为中断——若指向生产库而
   恰好有 worker 在跑，那一步会改坏对方正在执行的运行。
4. **入参恒为 ``batch_id=""``。** 该字段会一路透传进 ``result1``，模板要求它是固定值；
   把实验批次号填进去会让结果因实验元数据而与基线不同，直接污染对比。批次身份只存在于
   目录名与 ``manifest.json``；给运行打标记用 ``business_ref``（纯元数据，不进结果）。
5. **run_id 自己生成。** 这样才能在单号码超时后仍按该 run_id 导出已完成的留痕——超时不
   等于没有数据，半棵树也比没有强。

用法：

    python scripts/run_baseline.py --batch-id smoke-1 --only 13415100087
    python scripts/run_baseline.py                       # 全量，4 并发
    python scripts/run_baseline.py --concurrency 2 --limit 4
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from baseline_artifacts import (  # noqa: E402
    EXPORT_FILENAMES,
    ExportPolicy,
    dump_json,
    export_run_tree,
    leftover_temp_files,
)
from collect_test_phones import read_phone_list  # noqa: E402
from customer_profile.execution.scheduler import RunRequest  # noqa: E402
from customer_profile.execution.transcript_cache import TranscriptCache  # noqa: E402
from customer_profile.runner import build_service  # noqa: E402
from customer_profile.settings import Settings  # noqa: E402

ENTRY_WORKFLOW = "customer_profile_entry"
DEFAULT_OUT_DIR = REPO_ROOT / "experiments"
DEFAULT_PHONES = REPO_ROOT / "test_phone_numbers.txt"
DEFAULT_TRANSCRIPT_CACHE_DIR = DEFAULT_OUT_DIR / "transcript_cache"
"""录音转写归档目录。

**与批次同级、跨批次共用**：同一段录音（键含 URL）不必每个批次重录一份。放在
``experiments/`` 下还有一层用意——runbook §7 要求批次跑完后整目录复制到仓库外备份，
缓存因此在同一次复制里被带走，不会因为「另行备份」而被遗漏。
"""
DEFAULT_PER_PHONE_TIMEOUT = 5400.0
"""单号码超时（90 分钟）。

实测依据（几次真实运行的墙钟）：
- 不走录音取证分支的号码：26.3 / 29.2 分钟；
- 走该分支的号码会额外跑 iteration 循环体，仅循环体两个 tool 就 298 s + 1388 s（28.1 分钟），
  且循环体之前的节点照跑。

因此 2700 s（45 分钟）**不够**：实测有一次恰好在 2700 s 被取消，当时仍在跑后续 LLM 节点。
90 分钟给循环体这条最慢路径留了约 1.5 倍余量。超时不是质量判定，只是「别无限等」的保险，
设小了会把本来能成的号码误判成失败。
"""

DEFAULT_READ_TIMEOUT = 1800.0
DEFAULT_CONCURRENCY = 4
MAX_CONCURRENCY = 4
"""并发上限。用户口径是「4 并发之内」；不提供更高档位。"""

GATE_FOR_CONCURRENCY = 16
"""随并发放宽的出网位（``MAX_CONCURRENT_REQUESTS`` 与 ``max_active_nodes``）。

4 路并发共享**同一进程**的闸门：默认 8 个出网位会让每股平均只有 2 个，并发收益被闸门
吃掉大半。提到 16 后每股平均 4 个位，外部压力约为串行的 2 倍。只写在本脚本里，不落 .env。
"""

CONFIG_ALLOWLIST: tuple[str, ...] = (
    "writeback_enabled",
    "replay_mode",
    "llm_model",
    "llm_base_url",
    "profile_api_base_url",
    "auc_base_url",
    "mhero_base_url",
    "http_read_timeout",
    "http_connect_timeout",
    "max_concurrent_requests",
    "max_active_runs",
    "max_queued_runs",
    "worker_replicas",
    "llm_rpm_limit",
    "llm_tpm_limit",
    "llm_enable_thinking",
    "llm_max_tokens",
)
"""写进 manifest 的配置项白名单。

**禁止 dump 整个 Settings**——那会把 ``llm_api_key`` / ``profile_api_key`` 等凭据写进
归档目录。
"""


def make_batch_id(now: datetime, code_commit: str | None) -> str:
    """批次 ID：UTC 时间戳 + 短提交号，如 ``20260924T1530Z-7c4c8cf5``。"""
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%MZ")
    short = (code_commit or "nogit")[:8]
    return f"{stamp}-{short}"


def git_commit() -> str | None:
    """取当前提交号。取不到时返回 ``None``，不伪造。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def config_snapshot(settings: Settings, extra: dict[str, Any]) -> dict[str, Any]:
    """按白名单取配置快照，附上脚本级参数。"""
    snapshot: dict[str, Any] = {}
    for name in CONFIG_ALLOWLIST:
        value = getattr(settings, name, None)
        snapshot[name] = value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
    snapshot.update(extra)
    return snapshot


def build_settings(batch_db: Path, *, read_timeout: float) -> Settings:
    """构造基线专用配置。三处硬编码见模块 docstring。

    ``database_url`` 用**绝对路径**：``Settings.sqlite_path`` 对相对路径是按仓库根解析的
    （不是按脚本工作目录），``--out-dir`` 一旦指向仓库外就会静默写错位置。
    """
    settings = Settings(
        writeback_enabled=False,
        replay_mode="off",
        interrupt_on_start=False,
        database_url=f"sqlite:///{batch_db.resolve()}",
        http_read_timeout=read_timeout,
        max_concurrent_requests=GATE_FOR_CONCURRENCY,
    )
    if settings.writeback_enabled:
        raise SystemExit("基线禁止开启生产回写（WRITEBACK_ENABLED 必须为 false）")
    if settings.replay_mode != "off":
        raise SystemExit(f"REPLAY_MODE={settings.replay_mode!r}，基线要求 off（否则测的是回放）")
    return settings


def build_transcript_cache(args: argparse.Namespace, out_dir: Path) -> TranscriptCache:
    """按命令行参数构造转写归档。

    ``--no-transcript-cache`` 关的是**读取**（本次不复用），写入仍默认进行——否则一次
    「只跑一遍看看」之后的批次就没有可复用的数据了。要连写入也关掉，加 ``--no-cache-write``。
    """
    root = (
        Path(args.transcript_cache_dir).resolve()
        if args.transcript_cache_dir
        else (out_dir / "transcript_cache")
    )
    return TranscriptCache(
        root,
        read=not args.no_transcript_cache,
        write=not args.no_cache_write,
    )


def phone_artifact_dir(batch_dir: Path, phone: str) -> Path:
    return batch_dir / "runs" / phone


def has_succeeded_artifact(batch_dir: Path, phone: str) -> bool:
    """产物存在且状态为 succeeded 才算完成。产物是价值，库是过程。"""
    path = phone_artifact_dir(batch_dir, phone) / "result.json"
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return payload.get("status") == "succeeded"


def pending_phones(
    phones: Sequence[str], batch_dir: Path, *, resume: bool, only: Sequence[str] = ()
) -> list[str]:
    """本次要跑的号码。``resume`` 时跳过已有成功产物的号码。"""
    selected = list(phones)
    if only:
        wanted = {item.strip() for item in only if item.strip()}
        unknown = sorted(wanted - set(phones))
        if unknown:
            raise SystemExit(f"--only 里的号码不在清单中：{unknown}")
        selected = [phone for phone in phones if phone in wanted]
    if not resume:
        return selected
    return [phone for phone in selected if not has_succeeded_artifact(batch_dir, phone)]


@dataclass
class PhoneResult:
    """一个号码的批次内结论。"""

    phone: str
    status: str
    run_id: str | None = None
    duration_ms: int = 0
    wall_ms: int = 0
    error: str | None = None
    failed_nodes: list[dict[str, Any]] = field(default_factory=list)
    bytes_written: int = 0
    export_summary: dict[str, Any] = field(default_factory=dict)
    exported: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "phone": self.phone,
            "status": self.status,
            "run_id": self.run_id,
            "duration_ms": self.duration_ms,
            "wall_ms": self.wall_ms,
            "error": self.error,
            "failed_nodes": self.failed_nodes,
            "bytes_written": self.bytes_written,
            "exported": self.exported,
            "export_summary": self.export_summary,
        }


def _new_run_id() -> str:
    """32 位 hex，与留痕层自造的 run_id 同形。"""
    return uuid.uuid4().hex


async def run_one(
    service: Any,
    phone: str,
    *,
    timeout: float,
    policy: ExportPolicy,
    batch_dir: Path,
    workflow_id: str = ENTRY_WORKFLOW,
) -> PhoneResult:
    """跑一个号码，落盘其产物。任何异常都转成结果记录，不向上抛。"""
    run_id = _new_run_id()
    inputs = {"phone_number": phone, "batch_id": ""}
    started = time.monotonic()
    result = PhoneResult(phone=phone, status="failed", run_id=run_id)
    workflow = service.require_workflow(workflow_id)
    request = RunRequest(
        workflow=workflow, inputs=inputs, business_ref=phone, run_id=run_id
    )

    try:
        outcome = await asyncio.wait_for(
            service.scheduler.run(request), timeout=timeout
        )
    except (asyncio.CancelledError, asyncio.TimeoutError):
        # Python 3.11 起 ``wait_for`` 超时抛内置 ``TimeoutError``（3.10 及以前是
        # ``CancelledError``），两者都按超时记，避免在旧解释器上被误记成 exception。
        result.status = "timeout"
        result.error = f"超过单号码超时 {timeout:.0f}s"
    except Exception as exc:
        result.status = "exception"
        result.error = f"{type(exc).__name__}: {exc}"
    else:
        result.status = outcome.status
        result.duration_ms = outcome.duration_ms
        result.error = outcome.error
        result.failed_nodes = [
            {"node_id": e.node_id, "call_path": getattr(e, "call_path", ""), "error": e.error}
            for e in outcome.node_executions
            if not e.succeeded
        ][:20]
    result.wall_ms = int((time.monotonic() - started) * 1000)

    # 超时/异常也导出：该次运行已被调度器记为终态，半棵树比没有强，且能看到卡在哪。
    try:
        bundle, written = await export_run_tree(
            service.store, run_id, phone_artifact_dir(batch_dir, phone), policy
        )
        result.bytes_written = sum(written.values())
        result.export_summary = bundle.to_summary()
        result.exported = True
    except Exception as exc:
        result.error = (result.error or "") + f" | 导出失败：{type(exc).__name__}: {exc}"
    return result


def _log(log_path: Path, line: str) -> None:
    """进度行：ASCII、带时刻。中文一律进产物文件，不进 stdout/日志。"""
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    text = f"[{stamp}] {line}"
    print(text, flush=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")


def _write_summary_md(path: Path, results: Sequence[PhoneResult], batch_id: str) -> int:
    ok = sum(1 for item in results if item.status == "succeeded")
    lines = [
        f"# 基线批次 {batch_id}",
        "",
        f"- 号码数：{len(results)}，成功 {ok}，其它 {len(results) - ok}",
        "",
        "| 手机号 | 状态 | 耗时(ms) | 墙钟(ms) | 节点 | 出网 | 产物字节 | 说明 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in results:
        summary = item.export_summary or {}
        note = (item.error or "").replace("|", "/").replace("\n", " ")[:120]
        lines.append(
            f"| {item.phone} | {item.status} | {item.duration_ms} | {item.wall_ms} | "
            f"{summary.get('node_count', 0)} | {summary.get('attempt_count', 0)} | "
            f"{item.bytes_written} | {note} |"
        )
    failed = [item for item in results if item.status != "succeeded"]
    if failed:
        lines += ["", "## 待补跑", ""]
        for item in failed:
            lines.append(f"- `{item.phone}`：{item.status}（{item.error or '无详情'}）")
        lines += [
            "",
            "补跑命令：",
            "",
            "```",
            "python scripts/run_baseline.py --batch-id <同批次> --only "
            + ",".join(item.phone for item in failed),
            "```",
        ]
    lines.append("")
    body = "\n".join(lines).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return len(body)


def load_batch_results(batch_dir: Path) -> dict[str, PhoneResult]:
    """从磁盘上的产物重建「这个批次目前有哪些号码、各自什么状态」。

    汇总必须是**整个批次**的视图，而不是本次调用的内存结果。两个理由：

    - 补跑 2 个号码时，先前跑完的 37 个不能从汇总表里消失——那会让「这批跑了什么」只
      取决于最后一次调用，批次记录随之失去意义。
    - 对已完成的批次再跑一次时（例如手滑重跑同一个 `--only`），汇总不能被清空。

    产物目录是唯一事实来源：它在每号码结束时原子落盘，比库更直接反映「这个号码完成了」。
    """
    runs_dir = batch_dir / "runs"
    found: dict[str, PhoneResult] = {}
    if not runs_dir.is_dir():
        return found
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir():
            continue
        result_path = entry / "result.json"
        if not result_path.is_file():
            continue
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        summary = payload.get("tree_summary") or {}
        found[entry.name] = PhoneResult(
            phone=entry.name,
            status=str(payload.get("status") or "unknown"),
            run_id=payload.get("run_id"),
            duration_ms=int(payload.get("duration_ms") or 0),
            error=payload.get("error"),
            bytes_written=sum(
                p.stat().st_size
                for p in entry.iterdir()
                if p.is_file() and p.suffix == ".json"
            ),
            export_summary=summary,
            exported=True,
        )
    return found


def merge_results(
    batch_dir: Path, fresh: Sequence[PhoneResult]
) -> list[PhoneResult]:
    """把本次的结果并进批次全量视图。本次的结果覆盖同号码的旧记录。"""
    merged = load_batch_results(batch_dir)
    for item in fresh:
        merged[item.phone] = item
    return [item for _, item in sorted(merged.items())]


def write_summary(
    batch_dir: Path, results: Sequence[PhoneResult], batch_id: str
) -> dict[str, int]:
    """写机读与人读两份汇总。每号码结束后调用一次，中断时也有据可查。"""
    machine = {
        "batch_id": batch_id,
        "phone_count": len(results),
        "succeeded": sum(1 for item in results if item.status == "succeeded"),
        "results": [item.to_dict() for item in results],
    }
    return {
        "summary.json": dump_json(batch_dir / "summary.json", machine),
        "summary.md": _write_summary_md(batch_dir / "summary.md", results, batch_id),
    }


def load_manifest(path: Path) -> dict[str, Any]:
    """读既有 manifest。不存在或损坏时返回空字典。

    补跑要继承两件东西：批次的**开批时刻**（不该被改成补跑时间）与此前的**入队历史**
    （不该被本轮覆盖掉）。读不出来时按空处理——补跑仍能继续，只是这两项从头开始，
    而不是让整条补跑路径失败。
    """
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def batch_bytes_on_disk(batch_dir: Path) -> int:
    """批次目录当前的总字节数。收尾时记进 manifest。

    按目录累计而不是只算本次写入：补跑时「本次」的字节数不代表批次总量，而 manifest 里
    这个字段的用途是「这批占了多少磁盘」。
    """
    total = 0
    for path in batch_dir.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


async def run_batch(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir).resolve()
    commit = git_commit()
    batch_id = args.batch_id or make_batch_id(datetime.now(timezone.utc), commit)
    batch_dir = out_dir / "batches" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    log_path = batch_dir / "logs" / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    source_list = Path(args.phones).resolve()
    all_phones = read_phone_list(source_list)
    if args.limit:
        all_phones = all_phones[: args.limit]
    only = [item for item in (args.only or "").split(",") if item.strip()]
    queue = pending_phones(all_phones, batch_dir, resume=args.resume, only=only)

    phones_path = batch_dir / "phones.txt"
    if not phones_path.is_file():
        phones_path.write_text("\n".join(all_phones) + "\n", encoding="utf-8", newline="\n")
    list_bytes = phones_path.read_bytes()
    # 以**快照**为准而不是本次读到的清单：批次开批时冻结的那份号码集合才是这批的事实，
    # 清单日后漂移不该让 manifest 里的计数与哈希对不上。
    snapshot_phones = read_phone_list(phones_path)

    policy = ExportPolicy(
        max_inline_bytes=args.max_inline_bytes,
        include_attempt_bodies=not args.no_attempt_bodies,
    )
    settings = build_settings(batch_dir / "trace.db", read_timeout=args.read_timeout)
    transcript_cache = build_transcript_cache(args, out_dir)

    _log(
        log_path,
        f"batch {batch_id}: {len(queue)} of {len(snapshot_phones)} phones queued",
    )
    if not queue:
        # 没有任何号码要跑。**这条路径必须零写入**：对已完成的批次重跑同一个 `--only`
        # 会走到这里，此时覆盖任何文件都会把该批次的既有记录改掉——批次目录是证据。
        # 日志是唯一例外（追加式进度记录，不改既有内容）。
        _log(log_path, "nothing to run (all requested phones already have succeeded artifacts)")
        _log(log_path, "batch files left untouched")
        return 0

    manifest_path = batch_dir / "manifest.json"
    previous = load_manifest(manifest_path)
    manifest: dict[str, Any] = {
        "batch_id": batch_id,
        # 开批时刻只在首次写入时确定；补跑不该把批次的开批时间改成补跑时间。
        "created_at": previous.get("created_at")
        or datetime.now(timezone.utc).isoformat(),
        "code_commit": commit,
        "workflow_id": ENTRY_WORKFLOW,
        "phones_source": str(source_list),
        "phones_count": len(snapshot_phones),
        "phones_sha256": hashlib.sha256(list_bytes).hexdigest(),
        "trace_db": "batch-local",
        "queue_this_run": list(queue),
        # 历次调用的入队记录，累计保留：补跑时把上一轮的号码抹掉，会让
        # 「这批一共跑过什么」只取决于最后一次调用。
        "runs_this_invocation": list(previous.get("runs_this_invocation") or []),
        "concurrency": args.concurrency,
        "per_phone_timeout": args.per_phone_timeout,
        "export_policy": {
            "max_inline_bytes": policy.max_inline_bytes,
            "max_depth": policy.max_depth,
            "include_attempt_bodies": policy.include_attempt_bodies,
        },
        "config": config_snapshot(
            settings, {"max_active_nodes": GATE_FOR_CONCURRENCY}
        ),
        # 转写归档的配置与计数器。命中数持续为 0 是「录音 URL 形态变了、缓存静默失效」
        # 的判据（见 runbook）。
        "transcript_cache": transcript_cache.asdict(),
        "note": "配置项为白名单快照，凭据不进归档",
    }
    dump_json(manifest_path, manifest)

    service = await build_service(
        settings,
        max_active_nodes=GATE_FOR_CONCURRENCY,
        transcript_cache=transcript_cache,
    )
    results: dict[str, PhoneResult] = {}
    try:
        if args.resume:
            # 只清本批次库里的残留：批次库是独立的，不会碰到生产留痕。
            cleaned = await service.store.mark_running_as_interrupted()
            if cleaned:
                _log(log_path, f"marked {cleaned} leftover running run(s) as interrupted")

        semaphore = asyncio.Semaphore(args.concurrency)

        async def worker(phone: str) -> None:
            async with semaphore:
                _log(log_path, f"start {phone}")
                item = await run_one(
                    service,
                    phone,
                    timeout=args.per_phone_timeout,
                    policy=policy,
                    batch_dir=batch_dir,
                )
                results[phone] = item
                _log(
                    log_path,
                    f"done  {phone}: {item.status} wall={item.wall_ms}ms "
                    f"nodes={(item.export_summary or {}).get('node_count', 0)} "
                    f"attempts={(item.export_summary or {}).get('attempt_count', 0)}",
                )
                stats = transcript_cache.stats
                _log(
                    log_path,
                    # 累计值而非本号码增量：4 路并发共享一份缓存，增量会把邻位号码的
                    # 命中算进来，「本号码命中几次」在并发下没有意义。
                    f"transcript cache (cumulative): hit={stats.hits} "
                    f"miss={stats.misses} write={stats.writes}",
                )
                write_summary(batch_dir, merge_results(batch_dir, list(results.values())), batch_id)

        await asyncio.gather(*(worker(phone) for phone in queue))
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()

    ordered = merge_results(batch_dir, list(results.values()))
    written = write_summary(batch_dir, ordered, batch_id)
    leftovers = leftover_temp_files(batch_dir / "runs")
    # 历史保留：`queue_this_run` 是**本次调用**入队的号码，`runs_all` 是累计。
    # 补跑时把上一轮的号码抹掉，会让「这批一共跑过什么」只取决于最后一次调用。
    history = list(manifest.get("runs_this_invocation", [])) + [
        {"at": datetime.now(timezone.utc).isoformat(), "queued": list(queue)}
    ]
    manifest.update(
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "runs_this_invocation": history,
            "phones_completed": sum(
                1 for item in ordered if item.status == "succeeded"
            ),
            "bytes_written": batch_bytes_on_disk(batch_dir),
            "leftover_temp_files": [str(p) for p in leftovers],
            "transcript_cache": transcript_cache.asdict(),
        }
    )
    dump_json(manifest_path, manifest)

    ok = sum(1 for item in ordered if item.status == "succeeded")
    _log(log_path, f"SUMMARY {ok}/{len(ordered)} succeeded in batch (this run: {len(queue)})")
    _log(log_path, f"artifacts under {batch_dir}")
    return 0 if ok == len(ordered) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据基线：批量跑真实工作流并归档留痕")
    parser.add_argument("--batch-id", default=None, help="缺省按 UTC 时刻 + 提交号生成")
    parser.add_argument("--phones", default=str(DEFAULT_PHONES), help="手机号清单文件")
    parser.add_argument("--only", default=None, help="只跑这些号码，逗号分隔")
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
        help=f"并发数（1-{MAX_CONCURRENCY}，缺省 {DEFAULT_CONCURRENCY}）",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument(
        "--per-phone-timeout", type=float, default=DEFAULT_PER_PHONE_TIMEOUT,
        help="单号码超时秒数",
    )
    parser.add_argument(
        "--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT, help="HTTP 读超时秒数"
    )
    parser.add_argument("--resume", dest="resume", action="store_true", default=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    parser.add_argument(
        "--max-inline-bytes", type=int, default=ExportPolicy().max_inline_bytes,
        help="单条节点输出超过此字节数就存摘要与哈希",
    )
    parser.add_argument(
        "--no-attempt-bodies", action="store_true",
        help="出网尝试只存摘要与哈希（缺省存全文）",
    )
    parser.add_argument("--limit", type=int, default=0, help="只取清单前 N 个号码")
    parser.add_argument(
        "--transcript-cache-dir", default=None,
        help=f"录音转写归档目录（缺省 {DEFAULT_TRANSCRIPT_CACHE_DIR}，跨批次共用）",
    )
    parser.add_argument(
        "--no-transcript-cache", action="store_true",
        help="不使用已归档的转写（仍会归档本次的真实识别结果）",
    )
    parser.add_argument(
        "--no-cache-write", action="store_true",
        help="不写入转写归档（与 --no-transcript-cache 合用即完全关闭缓存）",
    )
    args = parser.parse_args(argv)

    if not 1 <= args.concurrency <= MAX_CONCURRENCY:
        raise SystemExit(f"--concurrency 须在 1..{MAX_CONCURRENCY} 之间，实际 {args.concurrency}")
    return asyncio.run(run_batch(args))


if __name__ == "__main__":
    raise SystemExit(main())
