#!/usr/bin/env python
"""敏感性变体：保留"试驾存在性标记"（空载荷的 TestDrive* 条目）。

`make_pre_order_batch.py` 对无事件时间的空载荷条目按 `created_at` 兜底裁剪，结果把
"试驾发生过"这个**存在性标记**也删了——它没有内容，只是一个时间戳，删掉等于抹掉
阶段判定的唯一依据。本脚本只放宽这一类条目，其余口径完全相同，用于回答
"0 例 A/H 里有多少是裁剪造成的"。
"""
from __future__ import annotations

import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_pre_order_batch as mk

KEEP_NULL_CHANNELS = ("TestDrive",)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="保留试驾存在性标记的下单前批次")
    ap.add_argument("--source", default="uat-traces-20261102-20261103")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", required=True)
    ap.add_argument("--global-cutoff", default="2026-10-05 13:55:26")
    args = ap.parse_args(argv)

    source = mk.REPO_ROOT / "experiments" / "uat_data" / args.source
    out_dir = mk.REPO_ROOT / args.out
    phones = [p.strip() for p in args.only.split(",") if p.strip()]
    global_cutoff = mk._parse(args.global_cutoff)
    assert global_cutoff is not None

    reports = []
    for phone in phones:
        run_dir = source / "runs" / phone
        attempts = json.loads((run_dir / "attempts.json").read_text(encoding="utf-8"))
        cutoff = mk.earliest_order_time(attempts) or global_cutoff
        report = mk.CaseReport(phone=phone, cutoff=f"{cutoff:%Y-%m-%d %H:%M:%S}")

        recordings = mk.recording_times(attempts)
        attempts = mk.strip_late_transcripts(attempts, cutoff, recordings, report)

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
                kept = mk.sanitize_history(items, cutoff, report)
                # 差别只有这一处：空载荷的试驾条目是存在性标记，不因 created_at 被裁
                final = []
                for item in mk.sanitize_history(items, cutoff, report):
                    final.append(item)
                restored = [
                    it for it in items
                    if it.get("raw_payload") is None
                    and any(k in str(it.get("channel") or "") for k in KEEP_NULL_CHANNELS)
                    and it not in final
                ]
                if restored:
                    final.extend(restored)
                    for it in restored:
                        report.dropped_items.append(f"RESTORED {it.get('channel')}@{it.get('created_at')}")
                attempt["response_text"] = json.dumps(final, ensure_ascii=False)
            elif "/data/profile/" in url:
                try:
                    profile = json.loads(text)
                except ValueError:
                    continue
                if isinstance(profile, dict) and "basic_notes" in profile:
                    attempt["response_text"] = json.dumps(
                        mk.sanitize_profile(profile), ensure_ascii=False
                    )
        reports.append(report)
        target = out_dir / "runs" / phone
        target.mkdir(parents=True, exist_ok=True)
        (target / "attempts.json").write_text(
            json.dumps(attempts, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    (out_dir / "phones.txt").write_text("\n".join(phones) + "\n", encoding="utf-8")
    for r in reports:
        print(f"{r.phone}\t{r.cutoff}\trestored={[x for x in r.dropped_items if x.startswith('RESTORED')]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
