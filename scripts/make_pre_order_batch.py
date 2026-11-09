#!/usr/bin/env python
"""把归档批次裁剪到「销售录入订单之前」，产出可回放的新批次。

## 为什么改 attempts.json 而不是改代码

回放器（``scripts/replay_from_archive.py``）把 ``runs/<号码>/attempts.json`` 里的
HTTP 响应体原样喂回取数层。因此「时间点拉回下单前」这件事只需在**输入**上做，
不必碰管线代码——模型仍然真实调用，比较的仍是同一套代码。

## 剥离口径

以``订单事件时间``为分界（不是 ``created_at``：本体快照的 ``created_at`` 全是本批
抓取时刻 2026-11-02，按它裁会把所有客户裁空）。分界点取该客户**最早一笔订单的
``order_time``**；无订单记录的客户按统一口径取 ``GLOBAL_CUTOFF``。

- 整渠道剔除：``OntologyOrder``、``OntologyOrderFinance``（成交事实本身）
- 整渠道剔除：``OntologyLeadStage``（``lock_order_time`` / ``delivery_time`` /
  ``is_delivery`` / ``order_no`` 全是下单后才产生的字段）
- 改写状态：``OntologyLeadInfo.leads_status`` 的 100411（已锁单）/ 100415（已预订）
  → 100410（跟进中）；``stage`` 保留
- 事件时间晚于分界的过程记录逐条剔除：跟进记录、人工外呼、试驾排程、录音文件
- **录音转写单独处理**：回放器从 ``submit_analyze`` / ``query_analyze`` 配对播种转写缓存，
  不读 ``response_text``。因此下单之后那几通电话的转写（例如「我今天都办完了」）
  必须按同一分界把**提交与查询配对整体删除**，否则等于把成交结论直接喂进模型。
- ``/data/profile/`` 响应只保留 ``basic_notes``（去掉 ``planned_purchase_time``、
  ``viewing_progress``），并清空 ``main_summary.deal_level`` 与 ``ai_insight``
  ——后者是我们上一轮自己给出的等级，留着等于把答案喂回去

用法::

    python experiments/scripts/make_pre_order_batch.py --only 18971232821,... \\
        --out experiments/uat_data/pre-order-20261104 --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

DROP_CHANNELS = {
    "OntologyOrder",
    "OntologyOrderFinance",
    "OntologyLeadStage",
}
"""下单之后才存在的渠道。整条剔除，不留空壳（空壳会被渲染成 null，反而提示这里少了东西）。"""

STATUS_REWRITE = {
    100411: 100410,  # 已锁单 → 跟进中
    100415: 100410,  # 已预订 → 跟进中
}

EVENT_TIME_KEYS = ("createtime", "call_time", "start_time", "order_time", "lock_order_time")

STRIP_NOTES = ("planned_purchase_time", "viewing_progress")
"""销售登记的静态意向字段。复盘指出 ``planned_purchase_time`` 误导效应强，一并摘除。"""


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(ts)[: len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    return None


def _event_time(payload: Any) -> datetime | None:
    """取载荷里最早的事件时间（递归，含 ``records`` 数组）。"""
    found: list[datetime] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in EVENT_TIME_KEYS:
                    parsed = _parse(value if isinstance(value, str) else None)
                    if parsed:
                        found.append(parsed)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return min(found) if found else None


@dataclass
class CaseReport:
    phone: str
    cutoff: str
    dropped_channels: list[str] = field(default_factory=list)
    dropped_items: list[str] = field(default_factory=list)
    dropped_transcripts: list[str] = field(default_factory=list)
    rewritten: list[str] = field(default_factory=list)
    kept_content_chars: int = 0


def sanitize_history(items: list[dict], cutoff: datetime, report: CaseReport) -> list[dict]:
    kept: list[dict] = []
    for item in items:
        channel = item.get("channel")
        if channel in DROP_CHANNELS:
            report.dropped_channels.append(channel)
            continue

        payload = item.get("raw_payload")

        # 基础快照（LeadInfo / AppRegistration / Distribution / Assignment）：留，但改状态
        if channel == "OntologyLeadInfo" and isinstance(payload, dict):
            for record in payload.get("records") or []:
                status = record.get("leads_status")
                if isinstance(status, int) and status in STATUS_REWRITE:
                    record["leads_status"] = STATUS_REWRITE[status]
                    report.rewritten.append(f"LeadInfo {status}→{STATUS_REWRITE[status]}")
            kept.append(item)
            continue

        # 过程资料：按事件时间裁
        event = _event_time(payload)
        if event is not None and event >= cutoff:
            report.dropped_items.append(f"{channel}@{event:%Y-%m-%d %H:%M}")
            continue

        kept.append(item)

    # 截图/录音这类本身无时间的条目，用 item.created_at 兜底裁一次
    final: list[dict] = []
    for item in kept:
        payload = item.get("raw_payload")
        if payload is None:
            created = _parse(str(item.get("created_at") or "").replace("T", " "))
            if created is not None and created >= cutoff:
                report.dropped_items.append(f"{item.get('channel')}(空载荷)@{created:%Y-%m-%d}")
                continue
        final.append(item)
    return final


def sanitize_profile(profile: dict) -> dict:
    """只留 basic_notes（去静态意向字段），其余清空。"""
    notes = dict(profile.get("basic_notes") or {})
    for key in STRIP_NOTES:
        notes.pop(key, None)
    return {
        "main_summary": {
            "deal_level": "",
            "overall_tag": (profile.get("main_summary") or {}).get("overall_tag") or "",
            "personality_summary": None,
            "profile_summary": None,
        },
        "basic_notes": notes,
        "profile_version": profile.get("profile_version"),
        "ai_insight": {},
        "purchase_decisions": [],
        "deal_battle": {},
        "decision_psychology": [],
    }


def earliest_order_time(attempts: list[dict]) -> datetime | None:
    times: list[datetime] = []
    for attempt in attempts:
        if attempt.get("kind") == "llm":
            continue
        url = str((attempt.get("request_json") or {}).get("url") or "")
        if "/data/history/" not in url:
            continue
        try:
            items = json.loads(attempt.get("response_text") or "[]")
        except ValueError:
            continue
        for item in items:
            if item.get("channel") not in ("OntologyOrder", "OntologyOrderFinance"):
                continue
            payload = item.get("raw_payload")
            for record in (payload if isinstance(payload, list) else [payload]):
                if isinstance(record, dict):
                    parsed = _parse(record.get("order_time"))
                    if parsed:
                        times.append(parsed)
                elif isinstance(record, dict):
                    pass
    return min(times) if times else None


def recording_times(attempts: list[dict]) -> dict[str, datetime]:
    """``录音 URL → 事件时间``。

    两条来源：
    - 人工外呼：``OntologyManualOutboundCallRecordingFile.call_time``；
    - 试驾：录音条目本身不带时间，用 ``test_drive_id`` 回到
      ``OntologyTestDriveSchedule.source_id`` 取 ``start_time``。

    取不到的录音**不在这里猜**，由调用方回落到条目 ``created_at``。
    """
    schedule: dict[str, datetime] = {}
    recordings: dict[str, datetime] = {}
    pending: dict[str, str] = {}

    for attempt in attempts:
        if attempt.get("kind") == "llm":
            continue
        url = str((attempt.get("request_json") or {}).get("url") or "")
        if "/data/history/" not in url:
            continue
        try:
            items = json.loads(attempt.get("response_text") or "[]")
        except ValueError:
            continue
        if not isinstance(items, list):
            continue

        for item in items:
            payload = item.get("raw_payload")
            for record in payload if isinstance(payload, list) else [payload]:
                if not isinstance(record, dict):
                    continue
                if item.get("channel") == "OntologyTestDriveSchedule":
                    parsed = _parse(record.get("start_time"))
                    if parsed:
                        schedule[str(record.get("source_id"))] = parsed

        for item in items:
            channel = item.get("channel") or ""
            if "RecordingFile" not in channel:
                continue
            payload = item.get("raw_payload")
            for record in payload if isinstance(payload, list) else [payload]:
                if not isinstance(record, dict) or not record.get("recording_url"):
                    continue
                target = str(record["recording_url"])
                parsed = _parse(record.get("call_time"))
                if parsed:
                    recordings[target] = parsed
                else:
                    pending[target] = str(
                        record.get("test_drive_id") or record.get("source_business_id") or ""
                    )

    for target, key in pending.items():
        if key in schedule:
            recordings[target] = schedule[key]
    return recordings


def strip_late_transcripts(
    attempts: list[dict],
    cutoff: datetime,
    recordings: dict[str, datetime],
    report: CaseReport,
) -> list[dict]:
    """删掉下单之后那几通电话的 ``submit`` / ``query`` 配对。

    回放器按 ``task_id`` 配对还原转写缓存，**只删一边会让配对错位**，所以两边一起删。
    """
    submits: dict[str, str] = {}
    for attempt in attempts:
        if attempt.get("kind") == "llm":
            continue
        url = str((attempt.get("request_json") or {}).get("url") or "")
        if "/submit_analyze" not in url:
            continue
        body = (attempt.get("request_json") or {}).get("request_body") or {}
        file_url = str(body.get("file_url") or "")
        try:
            task_id = str(json.loads(attempt.get("response_text") or "{}").get("task_id") or "")
        except ValueError:
            continue
        if task_id and file_url:
            submits[task_id] = file_url

    drop_tasks: set[str] = set()
    for task_id, file_url in submits.items():
        event = recordings.get(file_url)
        if event is not None and event >= cutoff:
            drop_tasks.add(task_id)
            report.dropped_transcripts.append(f"{file_url.split('/')[-1]}@{event:%Y-%m-%d %H:%M}")

    kept: list[dict] = []
    for attempt in attempts:
        if attempt.get("kind") == "llm":
            kept.append(attempt)
            continue
        url = str((attempt.get("request_json") or {}).get("url") or "")
        body = attempt.get("request_json") or {}
        if "/submit_analyze" in url:
            if str((body.get("request_body") or {}).get("file_url") or "") in {
                submits[t] for t in drop_tasks
            }:
                continue
        elif "/query_analyze" in url:
            if str((body.get("request_body") or {}).get("task_id") or "") in drop_tasks:
                continue
        kept.append(attempt)
    return kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="裁出「下单前」的回放批次")
    parser.add_argument("--source", default="uat-traces-20261102-20261103")
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", default="", help="逗号分隔的手机号")
    parser.add_argument(
        "--global-cutoff",
        default="2026-10-05 13:55:26",
        help="无订单记录客户的统一分界点",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印报告，不落盘")
    args = parser.parse_args(argv)

    source = REPO_ROOT / "experiments" / "uat_data" / args.source
    out_dir = REPO_ROOT / args.out
    phones = [p.strip() for p in args.only.split(",") if p.strip()]
    if not phones:
        raise SystemExit("--only 必填")

    global_cutoff = _parse(args.global_cutoff)
    assert global_cutoff is not None

    reports: list[CaseReport] = []
    for phone in phones:
        run_dir = source / "runs" / phone
        if not run_dir.is_dir():
            raise SystemExit(f"缺运行目录：{run_dir}")
        attempts = json.loads((run_dir / "attempts.json").read_text(encoding="utf-8"))

        cutoff = earliest_order_time(attempts) or global_cutoff
        report = CaseReport(phone=phone, cutoff=f"{cutoff:%Y-%m-%d %H:%M:%S}")

        # 先裁转写配对（要读原始 history 里的录音时间，必须在改写 response_text 之前）
        recordings = recording_times(attempts)
        attempts = strip_late_transcripts(attempts, cutoff, recordings, report)

        for attempt in attempts:
            if attempt.get("kind") == "llm":
                continue
            url = str((attempt.get("request_json") or {}).get("url") or "")
            text = attempt.get("response_text") or ""

            if "/data/history/" in url:
                try:
                    items = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(items, list):
                    continue
                kept = sanitize_history(items, cutoff, report)
                attempt["response_text"] = json.dumps(kept, ensure_ascii=False)
                for item in kept:
                    report.kept_content_chars += len(
                        json.dumps(item.get("raw_payload"), ensure_ascii=False)
                    )
            elif "/data/profile/" in url:
                try:
                    profile = json.loads(text)
                except ValueError:
                    continue
                if isinstance(profile, dict) and "basic_notes" in profile:
                    attempt["response_text"] = json.dumps(
                        sanitize_profile(profile), ensure_ascii=False
                    )

        reports.append(report)
        if not args.dry_run:
            target = out_dir / "runs" / phone
            target.mkdir(parents=True, exist_ok=True)
            (target / "attempts.json").write_text(
                json.dumps(attempts, ensure_ascii=False, indent=1), encoding="utf-8"
            )

    if not args.dry_run:
        (out_dir / "phones.txt").write_text("\n".join(phones) + "\n", encoding="utf-8")

    print("phone\tcutoff\tkept_chars\tdropped_channels\trewrites\tdropped_transcripts")
    for r in reports:
        print(
            f"{r.phone}\t{r.cutoff}\t{r.kept_content_chars}\t"
            f"{len(r.dropped_channels)}:{sorted(set(r.dropped_channels))}\t{len(r.rewritten)}\t"
            f"{r.dropped_transcripts}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
