"""v3 整改的**离线回放**验收探针（0 token，不出网，只读）。

与 ``probe_v21_replay.py`` 的关系：那个测 v2.1 的三项整改（可读性 / 覆盖 / 保真），
本探针在其上**扩展 v3 的机器判据**，是 v3 施工的唯一读数来源。

    python -I scripts/probe_v3_replay.py
    python -I scripts/probe_v3_replay.py --batch experiments/abexperiments/batches/A-pipeline-L8-v21-20261013

口径：读批次里 ``pipeline_l1_llm.parsed_json``（L1 原始输出）→ 走一遍
``localize_claims`` → ``project_claims``，统计读数。**读原始输出**而非定位层产物，
原因见 v2.1 探针的说明（读定位层只会测出「降级后仍是降级后」）。

五类读数：

1. **整案归零**：逐案 ``result1`` 是否存在（v3 P1 的目标是归零率 = 0）
2. **可读性**：审计词命中、「未知渠道」占位
3. **证据链**：证据串形态（渠道：原文）、**证据位自指**（结论当依据）
4. **无坐标事实**：``basis=fact`` 却指不出原文且非登记档
5. **身份字段来源**：``customer_name`` / ``title`` 的来源白名单命中
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from customer_profile.workflows import customer_profile_pipeline as pipe  # noqa: E402

DEFAULT = REPO / "experiments/abexperiments/batches/A-pipeline-L8-v21-20261013/runs"

AUDIT_WORDS = (
    "归属过滤", "来源分级", "枚举归一", "单多值约束", "契约校验",
    "未知渠道", "社交自呈现", "一手交互", "第三方预测", "公开登记查询", "人工/系统登记",
    "按来源分级投影得到", "断言",
)

# 证据位自指：把「推断结论」本身当成依据写进证据（v2.1 实测 `age` 证据为
# 「推断为40-49岁（置信度0.4642）」）。这是 v3 要治的头号证据链缺陷。
SELF_REFERENCE = re.compile(r"推断[为成]|（置信度\s*0?\.\d+|置信度\s*0?\.\d+")

# 登记档渠道：系统直接给出的事实，不需要原文锚点（与 projection 的 CHANNEL_TIER 一致）。
REGISTRY_CHANNELS = frozenset(
    {
        "销售人员人工确认的客户Notes信息",
        "客户留存在我方销售数据系统中的信息记录",
        "销售人员对当前客户历史画像数据的反馈记录",
    }
)

# 身份字段来源白名单（与 projection.NAME_SOURCE_WHITELIST 同口径）：姓名只认这两档。
NAME_ALLOW_LABELS = ("人工/系统登记", "一手交互")


def jload(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def nodes_index(path: Path) -> dict:
    nodes = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(nodes, dict) and "nodes" in nodes:
        nodes = nodes["nodes"]
    elif isinstance(nodes, dict):
        nodes = list(nodes.values())
    index: dict[str, dict] = {}
    for item in nodes or []:
        if isinstance(item, dict):
            index.setdefault(item.get("node_id") or item.get("id"), item.get("outputs") or {})
    return index


_V31_FALLBACK_STEPS: frozenset[str] = frozenset(
    {
        "由客户原话直接得出该结论",
        "经语义纠错归一并核对后得出该结论",
        "综合上述线索归纳得出该结论",
        "据现有记录归纳",
    }
)
"""``_readable_steps`` 的**退路句**（v3.1 改造一）：产物里没有 ``reasoning_chain`` 时
（离线回放旧批次），``steps`` 落到这几种非复述说明。探针据它区分「真有推导链」与
「退路占位」——否则旧产物的 0% 同集率会被误读成「推导链质量已达标」。"""


def _norm_set(items) -> set[str]:
    """把 ``steps`` / ``evidence`` 归一成可比较的集合（v3.1 改造一：判两者是否雷同）。

    去掉「原始依据：/ 推断依据：」前缀、按分号拆分、逐条 strip——同一条依据在两栏里
    措辞略有包装差异时，若不归一就会漏判雷同，把读数做假。
    """
    out: set[str] = set()
    for item in items or []:
        text = str(item)
        for prefix in ("推断依据：", "推断依据:", "原始依据：", "原始依据:"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        for part in text.replace("；", "|").split("|"):
            part = part.strip()
            if part:
                out.add(part)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v3 整改离线回放探针")
    parser.add_argument("--batch", default=str(DEFAULT))
    parser.add_argument("--tier", default="sufficient")
    args = parser.parse_args(argv)

    root = Path(args.batch)
    if not root.is_dir() and (root / "runs").is_dir():
        root = root / "runs"
    if not root.is_dir():
        print("[FAIL] 找不到批次：%s" % root)
        return 2

    sys.stdout.reconfigure(encoding="utf-8")

    hits: Counter = Counter()
    notes_total = evidence_total = unknown = with_quote = self_ref = 0
    fact_total = coordinate_ok = no_coordinate = 0
    notes_per_case: dict[str, int] = {}
    zero_cases: list[str] = []
    identity_denied: list[str] = []
    identity_rejected = 0
    basis_counter: Counter = Counter()
    # v3.1 四类读数（改造一/二/三/四）
    steps_total = steps_same_as_evidence = 0
    chain_notes = 0
    salesperson_hits: list[str] = []
    noisy_anchors: list[str] = []
    signal_mismatch = 0
    license_injected = phone_injected = 0

    for phone in sorted(p.name for p in root.iterdir() if p.is_dir()):
        index = nodes_index(root / phone / "nodes.json")

        result_node = index.get("pipeline_end") or {}
        has_result = bool(result_node.get("result1"))
        if not has_result:
            zero_cases.append(phone)

        raw_node = index.get("pipeline_l1_llm") or {}
        claims_json = raw_node.get("parsed_json") or raw_node.get("result")
        if not claims_json:
            claims_json = (index.get("pipeline_l1_localize") or {}).get("claims_json")
        if not claims_json:
            notes_per_case[phone] = 0
            continue
        claims = jload(claims_json) or {}
        if isinstance(claims, dict):
            claims = claims.get("fields", claims.get("claims"))
        claims = claims or []

        records = jload((index.get("pipeline_l0_records") or {}).get("records_json")) or []
        relocalized = pipe.localize_claims(
            json.dumps(records, ensure_ascii=False),
            json.dumps({"fields": claims}, ensure_ascii=False),
        )
        # —— v3.1 改造四：确定性注入接在定位层之后（与节点图一致）
        injected = pipe.inject_deterministic(
            json.dumps(records, ensure_ascii=False), relocalized["claims_json"], phone
        )
        if injected.get("license_injected"):
            license_injected += 1
        if any(
            str(x.get("attribute")) == "phone_number"
            for x in (jload(injected["claims_json"]) or {}).get("fields", []) or []
        ):
            phone_injected += 1
        # —— v3.1 改造三：定位层的降级明细（噪音锚点 / 信号缺失）留在这里读
        #     （注入层自己的 output 覆盖了 relocalized，故先取明细再覆写）
        for item in jload(relocalized.get("downgraded_detail")) or []:
            reason = str(item.get("reason") or "")
            if reason == "noisy_anchor":
                noisy_anchors.append("%s %s" % (phone, item.get("attribute")))
            elif reason == "signal_mismatch":
                signal_mismatch += 1
        # —— v3.1 改造二：值形态门拒收（顾问信息混入客户字段）
        for item in jload(
            pipe.project_claims(injected["claims_json"], tier=args.tier).get("rejected_json")
        ) or []:
            if str(item.get("rule")) == "a_salesperson_value":
                salesperson_hits.append("%s %s" % (phone, item.get("attribute")))

        result = pipe.project_claims(injected["claims_json"], tier=args.tier)
        notes = jload(result["notes_updates_json"]) or []
        notes_per_case[phone] = len(notes)

        for entry in notes:
            notes_total += 1
            detail = entry.get("reasoning_detail") or {}
            steps = detail.get("steps") or []
            evidence = detail.get("evidence") or []
            blob = json.dumps(
                {"summary": detail.get("summary"), "steps": steps},
                ensure_ascii=False,
            )
            for word in AUDIT_WORDS:
                if word in blob:
                    hits[word] += 1
            # —— v3.1 改造一：steps 与 evidence 是否同集（雷同率）
            if steps and evidence:
                steps_total += 1
                if _norm_set(steps) == _norm_set(evidence):
                    steps_same_as_evidence += 1
            if not (len(steps) == 1 and steps[0] in _V31_FALLBACK_STEPS):
                chain_notes += 1
            for snippet in evidence:
                evidence_total += 1
                text = str(snippet)
                if "未知渠道" in text:
                    unknown += 1
                if "：" in text:
                    with_quote += 1
                if SELF_REFERENCE.search(text):
                    self_ref += 1

        # 无坐标事实：fact 且无 quote（verbatim）又无 source_quote（normalized），且非登记档。
        for claim in relocalized.get("claims_json") and (jload(relocalized["claims_json"]) or {}).get("fields", []) or []:
            if str(claim.get("basis") or "") != "fact":
                continue
            fact_total += 1
            channel = str(claim.get("record_channel") or "").strip()
            has_quote = bool(str(claim.get("quote") or "").strip())
            has_source = bool(str(claim.get("source_quote") or "").strip())
            if has_quote or has_source or channel in REGISTRY_CHANNELS:
                coordinate_ok += 1
            else:
                no_coordinate += 1

        # 身份字段来源白名单：**读投影后的 Notes**，不是读输入断言。
        # 输入断言里的 `customer_name` 来自社交档是常态——投影层会按白名单拒掉它
        # （v3 接口二）。读输入会把这件「已被拦下」的事误报成「越界」。
        rejected = jload(result.get("rejected_json")) or []
        name_rejected = any(
            isinstance(r, dict) and r.get("rule") == "name_source_whitelist" for r in rejected
        )
        notes_by_key = {n.get("attribute_key"): n for n in notes if isinstance(n, dict)}
        name_note = notes_by_key.get("customer_name")
        if name_note:
            # 仍留在产出里的姓名，其来源档必须在白名单内（一手自述 / 登记）。
            src = ""
            for claim in (jload(relocalized["claims_json"]) or {}).get("fields", []) or []:
                if str(claim.get("attribute") or "") == "customer_name" and str(
                    claim.get("value") or ""
                ) == str(name_note.get("attribute_value") or ""):
                    src = str(claim.get("record_channel") or "").strip()
                    break
            tier = pipe.proj.CHANNEL_TIER.get(src, pipe.proj.SOCIAL)
            if pipe.proj.TIER_LABEL.get(tier, "") not in NAME_ALLOW_LABELS:
                identity_denied.append("%s customer_name<-%s" % (phone, src or "无渠道"))
        elif name_rejected:
            identity_rejected += 1

        for basis in (jload(result["basis_json"]) or {}).values():
            basis_counter[basis] += 1

    print("=== v3 回放验收（L1 断言喂回当前代码） ===")
    print("逐案 Notes 条数:", notes_per_case)
    print("Notes 条数合计:", notes_total)
    print("【整案归零】%d 案：%s" % (len(zero_cases), zero_cases or "无"))
    print("审计词命中:", hits.most_common())
    print("证据串 %d：含「未知渠道」 %d，含「渠道：原文」 %d，**证据位自指 %d**" % (
        evidence_total, unknown, with_quote, self_ref))
    print("【无坐标事实】fact %d，有坐标 %d，**无坐标 %d**" % (fact_total, coordinate_ok, no_coordinate))
    print("【身份字段来源越界】", identity_denied or "无",
          "(白名单拒收 %d 案)" % identity_rejected)
    print("字段级 basis 分布:", basis_counter.most_common())
    print()
    print("=== v3.1 四类读数 ===")
    rate = (steps_same_as_evidence / steps_total * 100) if steps_total else 0.0
    print("【改造一·推导链】有 steps/evidence 的字段 %d，**两者同集 %d（%.1f%%）**，"
          "含真推导链的字段 %d" % (steps_total, steps_same_as_evidence, rate, chain_notes))
    print("【改造二·顾问信息】值形态门拒收 %d 条：%s" % (len(salesperson_hits), salesperson_hits or "无"))
    print("【改造三·噪音锚点】%d 条：%s（信号缺失 %d）" % (
        len(noisy_anchors), noisy_anchors or "无", signal_mismatch))
    print("【改造四·确定性注入】phone_number 覆盖 %d 案，driver_license_status 注入 %d 案" % (
        phone_injected, license_injected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
