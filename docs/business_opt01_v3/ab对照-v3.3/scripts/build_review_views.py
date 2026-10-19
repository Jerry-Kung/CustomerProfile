# -*- coding: utf-8 -*-
"""审核视图：把每案打成一页「可读证据」，专供业务审核阅读。

**为什么不能直接给 raw packet**：单案 packet 200 KB 里，绝大部分是 OCR 原文与
未加工节点产出（``records`` 18 KB、``l2_project`` 17 KB、``fields_1c`` 8 KB……），
而审核要判的是「输入说了什么 → 结论是什么 → 链条怎么走 → 哪里可疑」。直接把 packet
丢给读者，等于让他自己在噪声里找信号，且 39 案 × 200 KB 根本读不完。

本视图因此只留四样：

1. **源证据**：L1 输入包正文（模型真正看到的逐字转写与登记事实）——判「漏没漏」
   必须看这个，而不是看压缩报告；
2. **两臂结论**：v3.3 与基线的十段正文 + Notes 逐字段；
3. **链路摘要**：分诊档位、L1 三层计数、定坐标降级明细、L2 兜底裁决原文、审查问题；
4. **机器预扫命中**：本案的可判定疑点，带样本。

输出 ``review/views/<phone>.md``，一案一文件，篇幅可控。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKETS = HERE.parent / "review" / "packets"
VIEWS = HERE.parent / "review" / "views"
PRESCAN = HERE.parent / "review" / "prescan.json"

SECTIONS = (
    ("profile_summary", "画像摘要"),
    ("inferred_tags", "推断标签"),
    ("usage_scenarios", "用车场景"),
    ("communication_strategies", "沟通策略"),
    ("customer_overview", "客户概览"),
    ("purchase_motivations", "购车动机"),
    ("product_preferences", "产品偏好"),
    ("resistances", "抗性/顾虑"),
    ("emotion_state", "情绪状态"),
)


def _loads(v, default=None):
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return default


def _render(value, depth: int = 0) -> str:
    """把一段卡片内容渲染成紧凑、可读的缩进文本。"""
    pad = "  " * depth
    if value is None:
        return "_（空）_"
    if isinstance(value, str):
        return value.strip() or "_（空）_"
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            if v in (None, "", [], {}):
                continue
            if isinstance(v, (dict, list)):
                out.append("%s- **%s**：\n%s" % (pad, k, _render(v, depth + 1)))
            else:
                out.append("%s- **%s**：%s" % (pad, k, v))
        return "\n".join(out) or "_（空）_"
    if isinstance(value, list):
        out = []
        for i, item in enumerate(value, 1):
            if isinstance(item, dict):
                title = item.get("title") or item.get("strategy_name") or item.get("resistance_name") \
                    or item.get("attribute_key") or item.get("name") or ("第%d项" % i)
                out.append("%s%d. **%s**" % (pad, i, title))
                for k, v in item.items():
                    if k in ("title", "strategy_name", "resistance_name", "attribute_key", "name"):
                        continue
                    if v in (None, "", [], {}):
                        continue
                    if isinstance(v, (dict, list)):
                        out.append("%s   - %s：%s" % (pad, k, _render(v, depth + 2).strip()))
                    else:
                        out.append("%s   - %s：%s" % (pad, k, v))
            else:
                out.append("%s%d. %s" % (pad, i, item))
        return "\n".join(out) or "_（空）_"
    return str(value)


def raw_text(value) -> str:
    """节点产出归一成文本。

    **不要假定 ``result`` 一定是字符串**：LLM 节点的 ``output`` 配置为对象时，
    产出就是 dict。此前直接 ``L.append(node["result"])`` 会往列表里塞进一个 dict，
    最终在 ``"\\n".join(L)`` 处炸成 ``expected str instance, dict found``——
    而这一行正是第 101 项，属于「13 个节点里恰好那一个不是字符串」的偶发形态。
    """
    if value is None:
        return "_（无）_"
    if isinstance(value, str):
        return value.strip() or "_（无）_"
    return json.dumps(value, ensure_ascii=False, indent=1)


def _join(L: list, phone: str) -> str:
    """拼装视图正文，并**指出**任何非字符串项。

    直接 ``"\\n".join(L)`` 在遇到 dict 时只给一句
    ``sequence item 101: expected str instance, dict found``——既不说哪个节点、
    也不说是什么内容，只能靠二分猜。视图里有十几处 ``L.append(...)``，其中
    若干取的是节点产出（可能是对象）。这里把「不是字符串」变成可定位的报错：
    谁、什么类型、前 200 字符是什么。
    """
    bad = [(i, type(x).__name__, repr(x)[:200]) for i, x in enumerate(L) if not isinstance(x, str)]
    if bad:
        detail = "\n".join("  第 %d 项 %s: %s" % item for item in bad[:5])
        raise TypeError("审核视图 %s 有 %d 项不是字符串：\n%s" % (phone, len(bad), detail))
    return "\n".join(L)


def trim_input(text: str, limit: int = 60000) -> str:
    """L1 输入包很长（含契约清单 + 全渠道正文）。保留前 limit 字符并注明截断。"""
    if not text:
        return "_（无）_"
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n_……（原文共 %d 字符，此处截断；完整见 packets/）_" % len(text)


def notes_block(notes: list) -> str:
    if not notes:
        return "_（无 Notes）_"
    out = []
    for n in notes:
        if not isinstance(n, dict):
            continue
        key = n.get("attribute_key")
        val = n.get("attribute_value")
        d = n.get("reasoning_detail") or {}
        conf = d.get("confidence")
        steps = d.get("steps") or []
        ev = d.get("evidence") or []
        line = "- **%s** = `%s`" % (key, json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else val)
        if conf is not None:
            line += "  （置信 %.2f）" % float(conf)
        out.append(line)
        if steps:
            out.append("  - 推理：%s" % " ".join(str(s) for s in steps)[:400])
        if ev:
            out.append("  - 证据：%s" % " ".join(str(e) for e in ev)[:400])
    return "\n".join(out)


def main() -> int:
    VIEWS.mkdir(parents=True, exist_ok=True)
    prescan = {r["phone"]: r for r in json.loads(PRESCAN.read_text(encoding="utf-8"))} \
        if PRESCAN.is_file() else {}

    packets = sorted(p for p in PACKETS.glob("*.json") if p.name != "_index.json")
    for p in packets:
        d = json.loads(p.read_text(encoding="utf-8"))
        phone = d["phone"]
        chain = d.get("chain") or {}
        new = d.get("new") or {}
        base = d.get("base") or {}
        meta = d.get("meta") or {}

        L = []
        L.append("# 审核案 %s" % phone)
        L.append("")
        L.append("读数：v3.3 状态 %s / 耗时 %s ms / LLM %s 次 / token %s；基线状态 %s / 耗时 %s ms"
                 % (meta.get("new_status"), meta.get("new_duration_ms"), meta.get("new_llm"),
                    meta.get("new_tok"), meta.get("base_status"), meta.get("base_duration_ms")))
        L.append("")

        # ---- 机器预扫
        r = prescan.get(phone)
        L.append("## 0. 机器预扫命中")
        if r and r.get("findings"):
            for f in r["findings"]:
                L.append("- **%s** ×%d：`%s`" % (f["rule"], f["count"],
                                                json.dumps(f["detail"], ensure_ascii=False)[:600]))
        else:
            L.append("- 无（可判定判据均未命中）")
        L.append("")

        # ---- 链路摘要
        tri = (chain.get("triage") or {})
        merge = (chain.get("merge") or {})
        loc = (chain.get("localize") or {})
        inj = (chain.get("inject") or {})
        proj = (chain.get("l2_project") or {})
        rev = (chain.get("l2_review") or {})
        l3 = (chain.get("l3") or {})
        L.append("## 1. 链路摘要")
        L.append("- 分诊：`%s`（%s），可用渠道 %s 个" % (
            tri.get("tier"), tri.get("tier_reason"), tri.get("usable_count")))
        L.append("- L1 合并：1a=%s / 1c=%s / 合并=%s；仅 1a 有=%s，1a 覆盖 1c=%s，subject 覆盖=%s"
                 % (merge.get("facts_count"), merge.get("fields_count"), merge.get("merged_count"),
                    merge.get("from_facts_only"), merge.get("overrode"), merge.get("subject_overrides")))
        L.append("- 定坐标：保留 %s / 丢弃 %s / 降级 %s" % (
            loc.get("kept"), loc.get("dropped"), loc.get("downgraded")))
        dd = _loads(loc.get("downgraded_detail"), [])
        if dd:
            reasons = {}
            for item in dd:
                reasons[item.get("reason")] = reasons.get(item.get("reason"), 0) + 1
            L.append("  - 降级原因分布：%s" % json.dumps(reasons, ensure_ascii=False))
        L.append("- 确定性注入：新增 %s / 替换 %s；驾照注入=%s" % (
            inj.get("added"), inj.get("replaced"), inj.get("license_injected")))
        L.append("- L2 投影：facts %s 键，Notes %s 条，空值字段 %s，候选 %s"
                 % (len(_loads(proj.get("facts_json"), {})), proj.get("notes_count"),
                    proj.get("empty_claims"), len(_loads(proj.get("candidates_json"), []))))
        L.append("- L2 审查：问题 %s 条 `%s`" % (rev.get("issue_count"), rev.get("issue_json")))
        L.append("- L3：实到 %s 段，缺段 %s，上提 %s，无出处量疑似 %s `%s`"
                 % (l3.get("section_count"), l3.get("sections_missing"),
                    l3.get("sections_hoisted"), l3.get("new_fact_suspects"),
                    (l3.get("suspects_json") or "")[:400]))
        L.append("")

        # ---- L1 三层原文
        L.append("## 2. L1 事实层（1a）原文")
        L.append("```json")
        L.append(raw_text((chain.get("facts_1a") or {}).get("result")))
        L.append("```")
        L.append("")
        L.append("## 3. L1 人设层（1b）原文（**不进证据位**，仅作一致性参考）")
        L.append("```json")
        L.append(raw_text((chain.get("persona_1b") or {}).get("result")))
        L.append("```")
        L.append("")
        L.append("## 4. L1 分析层（1c）原文")
        L.append("```json")
        L.append(raw_text((chain.get("fields_1c") or {}).get("result")))
        L.append("```")
        L.append("")

        # ---- L2 兜底与 facts
        L.append("## 5. L2 定稿 facts")
        L.append("```json")
        L.append(json.dumps(_loads(proj.get("facts_json"), {}), ensure_ascii=False, indent=1))
        L.append("```")
        L.append("")
        L.append("## 6. L2 兜底裁决原文")
        L.append("```json")
        L.append(raw_text((chain.get("l2_fallback") or {}).get("result")))
        L.append("```")
        L.append("")

        # ---- 两臂产出
        L.append("## 7. v3.3 卡片（九段）")
        new_secs = new.get("sections") or {}
        for key, label in SECTIONS:
            L.append("### %s" % label)
            L.append(_render(new_secs.get(key)))
            L.append("")
        L.append("## 8. 基线卡片（九段）")
        base_secs = base.get("sections") or {}
        for key, label in SECTIONS:
            L.append("### %s" % label)
            L.append(_render(base_secs.get(key)))
            L.append("")

        # ---- 源证据
        L.append("## 9. 源证据：L1 输入包正文（模型实际看到的）")
        L.append("```text")
        L.append(trim_input(d["input"].get("l1_input") or ""))
        L.append("```")
        L.append("")

        # ---- Notes 对照
        L.append("## 10. Notes 两臂对照")
        L.append("### v3.3 Notes")
        L.append(notes_block(new_secs.get("basic_notes_updates") or []))
        L.append("")
        L.append("### 基线 Notes")
        L.append(notes_block(base_secs.get("basic_notes_updates") or []))
        L.append("")

        out = VIEWS / ("%s.md" % phone)
        out.write_text(_join(L, phone), encoding="utf-8")

    total = sum((VIEWS / ("%s.md" % json.loads(p.read_text(encoding="utf-8"))["phone"])).stat().st_size
                for p in packets)
    print("已写出 %d 份审核视图到 %s" % (len(packets), VIEWS))
    print("合计 %s 字节 ≈ %.2f MB" % (format(total, ","), total / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
