# -*- coding: utf-8 -*-
"""统计 v3 对照页要写进 README 的读数（只读 payload，零 token）。

独立成文件而非 ``python -c``：Windows 下 ``-c`` 的中文字面量会按 ANSI 代码页解码，
判断「审计词 / 冒号」这类含中文的比较会静默失效，读数失真。用 ``python -X utf8`` 跑本文件。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
PAY = HERE.parent / "data" / "payload.json"

AUDIT = ["归属过滤", "来源分级", "枚举归一", "单多值约束", "契约校验", "未知渠道",
         "社交自呈现", "一手交互", "第三方预测", "公开登记查询", "人工/系统登记",
         "按来源分级投影得到", "断言"]


def main() -> int:
    d = json.loads(PAY.read_text(encoding="utf-8"))
    cs = d["cases"]
    S = {k: 0 for k in (
        "n3", "nb", "secv", "secb", "llm", "llmb", "tok", "tokb",
        "hits", "unknown", "self", "ev", "evr", "facts", "persona", "issues",
        "only_base", "only_v3", "both", "norm", "verbatim",
    )}
    hit_detail: list[str] = []

    for c in cs:
        v, b = c["v3"], c["base"]
        S["n3"] += len(v["notes"]); S["nb"] += len(b["notes"])
        S["llm"] += v["metrics"]["llm_calls"]; S["llmb"] += b["metrics"]["llm_calls"]
        S["tok"] += v["metrics"]["tokens"]; S["tokb"] += b["metrics"]["tokens"]
        S["only_base"] += len(c["notes_diff"]["only_base"])
        S["only_v3"] += len(c["notes_diff"]["only_v3"])
        S["both"] += len(c["notes_diff"]["both"])
        recs = (v.get("facts") or {}).get("records") or []
        S["facts"] += len(recs)
        for r in recs:
            if str(r.get("anchor_kind")) == "normalized":
                S["norm"] += 1
            elif str(r.get("anchor_kind")) == "verbatim":
                S["verbatim"] += 1
        p0 = (v.get("persona") or {}).get("persona") or {}
        S["persona"] += sum(1 for x in p0.values() if isinstance(x, dict) and x.get("value"))
        S["issues"] += int((v.get("review") or {}).get("issue_count") or 0)
        for k in d["sections"]:
            S["secv"] += v["sec_counts"][k]; S["secb"] += b["sec_counts"][k]
        for n in v["notes"]:
            rd = n.get("reasoning_detail") or {}
            blob = str(rd.get("summary") or "") + " " + " ".join(rd.get("steps") or [])
            for w in AUDIT:
                if w in blob:
                    S["hits"] += 1
                    hit_detail.append("%s %s | %s" % (c["phone"], n.get("attribute_key"), w))
            for s in (rd.get("evidence") or []):
                S["ev"] += 1
                t = str(s)
                if "未知渠道" in t:
                    S["unknown"] += 1
                if "：" in t or ":" in t:
                    S["evr"] += 1
                if re.search(r"推断为.*置信", t):
                    S["self"] += 1

    print("Notes        v3=%d  基线=%d  比值=%.2f" % (S["n3"], S["nb"], S["n3"] / max(S["nb"], 1)))
    print("画像卡条目    v3=%d  基线=%d  比值=%.2f" % (S["secv"], S["secb"], S["secv"] / max(S["secb"], 1)))
    print("LLM 调用      v3=%d  基线=%d" % (S["llm"], S["llmb"]))
    print("Token         v3=%d  基线=%d" % (S["tok"], S["tokb"]))
    print("单边字段      仅基线=%d  仅v3=%d  两边都有=%d" % (S["only_base"], S["only_v3"], S["both"]))
    print("事实层        %d 条（逐字锚 %d / 当归 %d）" % (S["facts"], S["verbatim"], S["norm"]))
    print("人设项        %d（8 案）" % S["persona"])
    print("审查命中      %d" % S["issues"])
    print("审计词命中    %d" % S["hits"])
    print("未知渠道占位  %d" % S["unknown"])
    print("证据位自指    %d" % S["self"])
    print("证据条数与带冒号出处  %d / %d（%.1f%%）" % (S["evr"], S["ev"], S["evr"] / max(S["ev"], 1) * 100))
    if hit_detail:
        print("\n审计词命中明细：")
        for x in hit_detail:
            print(" ", x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
