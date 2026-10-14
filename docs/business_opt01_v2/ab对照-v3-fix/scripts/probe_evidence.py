# -*- coding: utf-8 -*-
"""核对证据串形态与审查命中构成（只读，零 token）。

为什么要单独查：README 要写「证据串带出处率」，而这个数字此前两个口径对不上
（抽样看着几乎都带冒号，计数却只有个位数）。此处把证据串的真实字符、前缀分布、
审查命中构成全部摊开，避免照错数上报。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
PAY_V3 = HERE.parent / "data" / "payload.json"
PAY_V21 = HERE.parents[1] / "ab对照-v21" / "data" / "payload.json"


def ev_stats(pay: Path, arm: str) -> dict:
    d = json.loads(pay.read_text(encoding="utf-8"))
    total = 0
    has_colon = 0
    prefix = Counter()
    samples: list[str] = []
    for c in d["cases"]:
        for n in c[arm]["notes"]:
            for s in ((n.get("reasoning_detail") or {}).get("evidence") or []):
                t = str(s)
                total += 1
                if len(samples) < 8:
                    samples.append(t)
                if "：" in t or ":" in t:
                    has_colon += 1
                    prefix[t.split("：")[0].split(":")[0][:12]] += 1
    return {"total": total, "colon": has_colon, "prefix": prefix, "samples": samples}


def review_breakdown(pay: Path) -> tuple[Counter, list[str]]:
    d = json.loads(pay.read_text(encoding="utf-8"))
    rules = Counter()
    details: list[str] = []
    for c in d["cases"]:
        rv = c["v3"].get("review") or {}
        for it in (rv.get("issues") or []):
            rules[str(it.get("rule"))] += int(it.get("count") or 0)
            details.append("%s %s×%s" % (c["phone"], it.get("rule"), it.get("count")))
    return rules, details


def main() -> int:
    print("========== v3 证据串 ==========")
    a = ev_stats(PAY_V3, "v3")
    print("总数 %d  含冒号 %d (%.1f%%)" % (a["total"], a["colon"], a["colon"] / max(a["total"], 1) * 100))
    print("冒号前前缀分布：", dict(a["prefix"].most_common(8)))
    print("样例：")
    for s in a["samples"]:
        print("   ", s[:110])
    print("\n冒号字符码点检查：")
    for s in a["samples"][:3]:
        print("   ", [hex(ord(ch)) for ch in s if not ch.isalnum()][:8])

    print("\n========== v2.1 证据串（同口径对照） ==========")
    if PAY_V21.exists():
        b = ev_stats(PAY_V21, "v2")
        print("总数 %d  含冒号 %d (%.1f%%)" % (b["total"], b["colon"], b["colon"] / max(b["total"], 1) * 100))
        print("冒号前前缀分布：", dict(b["prefix"].most_common(8)))
    else:
        print("无 v2.1 payload")

    print("\n========== v3 审查命中构成 ==========")
    rules, details = review_breakdown(PAY_V3)
    for k, v in rules.items():
        print("  %-28s %d" % (k, v))
    for x in details:
        print("   ", x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
