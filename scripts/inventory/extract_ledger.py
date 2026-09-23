#!/usr/bin/env python3
"""V0.1.1 Dify DSL inventory extractor.

Reads dify_dsl_data/*.yml and emits machine-readable baseline ledgers under
docs/specs/ledger/. Output on stdout is deliberately ASCII-only so it stays
readable on a GBK Windows console; the files themselves are written as UTF-8.

The DSL is the only source of truth here. Nothing is hardcoded from the planning
doc: every count is measured, and disagreements are reported, not reconciled.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
DSL_DIR = REPO / "dify_dsl_data"
OUT_DIR = REPO / "docs" / "specs" / "ledger"
# Committed project artifact (D8): prompt bodies are a first-class optimization
# target in later versions, so they are version-controlled under docs/, not in
# the gitignored data/.
PROMPT_DUMP = REPO / "docs" / "specs" / "prompts"

# {{#node_id.field#}} -- node ids are digits (iteration-start nodes carry a
# "<iteration-id>start" suffix, hence the letters in the alternation).
REF_RE = re.compile(r"\{\{#([0-9A-Za-z_]+)\.([A-Za-z0-9_]+)#\}\}")

# Plaintext credential shapes. Kept deliberately narrow to avoid false positives.
API_KEY_RE = re.compile(r"X-API-Key\s*:\s*(\S+)")
SK_RE = re.compile(r"\bsk-[A-Za-z0-9]{16,}")

# tool_name -> local DSL file. Fixed from docs/specs/V0迁移规划.md 1.1; reference
# counts are recomputed from the DSL and cross-checked in cross_check().
TOOL_MAP = {
    "evidence_subagent_production": "（新）SubAgent - 证据线索汇总（生产环境）",
    "profile_features_analysis": "（新）SubAgent - 人设特征判断",
    "customer_profile_production": "（新）SubAgent - 画像内容生成&回写（生产环境）（新）",
    "gemini_retry_2_times": "Gemini（异常输出重试版）",
    "audio_content_extract": "录音文件内容抽取（纯识别，无加工）",
    "moments_info_extract": "（新）SubAgent - 朋友圈信息提取",
    "wechat_search_homepage_info_extract": "（新）SubAgent - 微信手机号搜索截图信息提取",
    "wechat_homepage_info_extract": "（新）SubAgent - 微信主页信息提取",
    "test_drive_audio_analysis": "（新）SubAgent - 试驾录音信息提取",
    "Outbound_call_info_extract": "（新）SubAgent - 外呼录音信息提取",
    "Alipay_homepage_info_extract": "（新）SubAgent - 支付宝个人页信息提取",
    "Xiaohongshu_homepage_info_extract": "（新）SubAgent - 小红书个人页信息提取",
    "Douyin_homepage_info_extract": "（新）SubAgent - 抖音个人页信息提取",
    "human_corrected_info_new": "（新）SubAgent - 人工确认信息提取（生产环境）",
    "jiguang_data_production": "（新）SubAgent - 极光数据（生产环境）",
    "mengshi_it_system_data": "（新）SubAgent - 猛士IT系统数据信息",
    "chat_history_data": "（新）SubAgent - 聊天记录数据信息",
    "subagent_user_feedback_data": "（新）SubAgent - 用户人工Feedback数据提取",
}

# Counts asserted by docs/specs/V0迁移规划.md, checked -- not trusted.
ASSERTED = {
    "workflows": 19,
    "nodes": 293,
    "edges": 324,
    "tool_nodes": 38,
    "vision_nodes": 12,
    "code": 49,
    "template-transform": 46,
    "tool": 38,
    "llm": 35,
    "if-else": 26,
    "variable-aggregator": 23,
    "end": 22,
    "start": 19,
    "http-request": 19,
    "iteration": 8,
    "iteration-start": 8,
}
ASSERTED_GEMINI_REFS = 17


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def refs_in(value) -> list[tuple[str, str]]:
    """Collect every {{#id.field#}} reference inside an arbitrary nested value."""
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.extend(REF_RE.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            found.extend(refs_in(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(refs_in(v))
    return found


def selector_refs(selector) -> list[tuple[str, str]]:
    """A value_selector is [node_id, field]; tolerate the nested group form."""
    if (
        isinstance(selector, list)
        and len(selector) == 2
        and all(isinstance(x, str) for x in selector)
    ):
        return [(selector[0], selector[1])]
    if isinstance(selector, list):
        out: list[tuple[str, str]] = []
        for item in selector:
            out.extend(selector_refs(item))
        return out
    return []


def normalize_bindings(data: dict) -> dict:
    """Collapse each node type's binding shape into {consumer_var: [(node, field)]}.

    Literal (non-reference) bindings go under "literals" so the variable ledger
    can tell data consumption apart from plain configuration.
    """
    bindings: dict[str, list[tuple[str, str]]] = defaultdict(list)
    binding_src: dict[str, str] = {}
    literals: dict[str, object] = {}
    ntype = data.get("type")

    # code / llm / http-request / variable-aggregator: variable -> value_selector
    for var in data.get("variables") or []:
        if not isinstance(var, dict):
            continue
        name = var.get("variable")
        rs = selector_refs(var.get("value_selector"))
        if rs:
            bindings[name].extend(rs)
            binding_src[name] = "variables[]"
        elif name:
            literals[name] = var.get("value")

    # tool: each parameter holds either a reference or a literal
    for name, spec in (data.get("tool_parameters") or {}).items():
        val = spec.get("value") if isinstance(spec, dict) else spec
        rs = refs_in(val)
        if rs:
            bindings[name].extend(rs)
            binding_src[name] = "tool_parameters.%s" % name
        else:
            literals[name] = val

    # if-else: each condition reads a variable
    for case in data.get("cases") or []:
        for cond in case.get("conditions") or []:
            rs = selector_refs(cond.get("variable_selector"))
            if rs:
                key = "@cond:%s" % (cond.get("id") or "?")
                bindings[key].extend(rs)
                binding_src[key] = "cases[].conditions[]"

    # variable-aggregator with group_enabled: one label per output group.
    # Note: groups and group_enabled sit under advanced_settings, not on the
    # node data directly -- see 人设特征判断 node 1776760747042.
    adv = data.get("advanced_settings") or {}
    for group in adv.get("groups") or []:
        label = "@group:%s" % group.get("group_name")
        binding_src[label] = "advanced_settings.groups[]"
        for pair in group.get("variables") or []:
            bindings[label].extend(selector_refs(pair))

    # iteration: the collection iterated over, and the field it yields
    for src, label in (
        ("iterator_selector", "@iterator"),
        ("output_selector", "@output"),
    ):
        if data.get(src):
            bindings[label].extend(selector_refs(data[src]))
            binding_src[label] = src

    # template-transform: references embedded in the template body
    if data.get("template"):
        bindings["@template"].extend(refs_in(data["template"]))
        binding_src["@template"] = "template"

    # llm / code bodies carry their own references
    for key in ("prompt_template", "code"):
        if data.get(key):
            bindings["@%s" % key].extend(refs_in(data[key]))
            binding_src["@%s" % key] = key

    for key, label in (("url", "@url"), ("headers", "@headers")):
        if isinstance(data.get(key), str) and refs_in(data[key]):
            bindings[label].extend(refs_in(data[key]))
            binding_src[label] = key

    return {
        "refs": {k: sorted(set(v)) for k, v in sorted(bindings.items())},
        "sources": binding_src,
        "literals": literals,
    }


def prompt_text(data: dict) -> str | None:
    """The prompt-bearing body of a node, or None if it has none."""
    ntype = data.get("type")
    if ntype == "llm":
        chunks = []
        for msg in data.get("prompt_template") or []:
            if isinstance(msg, dict):
                chunks.append(
                    "%s\n%s" % (msg.get("role", ""), msg.get("text", ""))
                )
            else:
                chunks.append(str(msg))
        return "\n".join(chunks)
    if ntype == "template-transform":
        return data.get("template") or ""
    return None


def mask_secret(token: str) -> str:
    return token[:6] + "..." + token[-4:] if len(token) > 12 else "***"


def scan_secrets(text: str) -> list[dict]:
    """Find credential-shaped strings, recording a mask and never the value."""
    hits = []
    for m in API_KEY_RE.finditer(text):
        hits.append(
            {"kind": "X-API-Key header", "masked": mask_secret(m.group(1))}
        )
    for m in SK_RE.finditer(text):
        hits.append({"kind": "sk- token", "masked": mask_secret(m.group(0))})
    return hits


def load_workflows() -> list[dict]:
    if not DSL_DIR.is_dir():
        sys.exit(
            "ERROR: DSL directory not found: %s\n"
            "The Dify DSL exports are gitignored and must be supplied locally "
            "before this ledger can be regenerated." % DSL_DIR
        )
    files = sorted(DSL_DIR.glob("*.yml"))
    if not files:
        sys.exit("ERROR: no .yml files under %s" % DSL_DIR)
    out = []
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        graph = doc["workflow"]["graph"]
        out.append(
            {
                "file": path.name,
                "stem": path.stem,
                "doc": doc,
                "nodes": graph["nodes"],
                "edges": graph["edges"],
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "display_name": doc.get("app", {}).get("name", path.stem),
            }
        )
    return out


def vision_of(data: dict) -> bool:
    v = data.get("vision")
    return bool(isinstance(v, dict) and v.get("enabled") is True)


def build(wfs: list[dict]) -> dict:
    node_ledger = {"source": "dify_dsl_data", "workflows": []}
    variable_rows: list[dict] = []
    resource_rows: list[dict] = []
    secret_rows: list[dict] = []
    tool_refs: Counter = Counter()
    type_totals: Counter = Counter()
    totals = Counter()
    digest = []

    for wf in wfs:
        nodes, edges = wf["nodes"], wf["edges"]
        by_id = {n["id"]: n["data"] for n in nodes}
        preds: dict[str, list[str]] = defaultdict(list)
        for e in edges:
            preds[e["target"]].append(e["source"])

        wf_nodes = []
        for n in nodes:
            data = n["data"]
            nid = n["id"]
            ntype = data.get("type")
            bind = normalize_bindings(data)
            body = prompt_text(data)

            outputs = data.get("outputs")
            if isinstance(outputs, dict):
                out_fields = sorted(outputs.keys())
            elif isinstance(outputs, list):
                out_fields = sorted(
                    str(o.get("variable")) for o in outputs if isinstance(o, dict)
                )
            else:
                out_fields = []

            wf_nodes.append(
                {
                    "node_id": nid,
                    "title": data.get("title"),
                    "type": ntype,
                    "direct_predecessors": sorted(set(preds.get(nid, []))),
                    "input_bindings": {
                        k: [list(r) for r in v] for k, v in bind["refs"].items()
                    },
                    "binding_sources": bind["sources"],
                    "literal_bindings": bind["literals"],
                    "output_fields": out_fields,
                    "vision_enabled": vision_of(data),
                    "in_iteration": bool(data.get("isInIteration")),
                    "iteration_id": data.get("iteration_id"),
                    "error_strategy": data.get("error_strategy"),
                    "tool_name": data.get("tool_name"),
                    "original_coords": (
                        {"x": n.get("position", {}).get("x"),
                         "y": n.get("position", {}).get("y")}
                        if isinstance(n.get("position"), dict)
                        else None
                    ),
                }
            )

            type_totals[ntype] += 1
            if ntype == "tool":
                totals["tool_nodes"] += 1
                tool_refs[data.get("tool_name")] += 1
            if ntype == "llm" and vision_of(data):
                totals["vision_nodes"] += 1
            if ntype == "variable-aggregator" and (
                data.get("advanced_settings") or {}
            ).get("group_enabled"):
                totals["grouped_aggregators"] += 1

            if body is not None:
                resource_rows.append(
                    {
                        "workflow_file": wf["file"],
                        "node_id": nid,
                        "title": data.get("title"),
                        "node_type": ntype,
                        "chars": len(body),
                        "lines": body.count("\n") + 1,
                        "sha256": sha256_text(body),
                        "variables": sorted("%s.%s" % r for r in set(refs_in(body))),
                        "vision_enabled": vision_of(data),
                    }
                )
                for h in scan_secrets(body):
                    secret_rows.append(dict(h, workflow_file=wf["file"], node_id=nid))

            if ntype == "http-request":
                for key in ("headers", "url", "params"):
                    if not data.get(key):
                        continue
                    for h in scan_secrets(str(data[key])):
                        secret_rows.append(
                            dict(h, workflow_file=wf["file"], node_id=nid,
                                 field=key)
                        )

        for row in wf_nodes:
            for consumer_var, ref_list in row["input_bindings"].items():
                for prod_id, prod_field in ref_list:
                    variable_rows.append(
                        {
                            "workflow_file": wf["file"],
                            "consumer_node": row["node_id"],
                            "consumer_type": row["type"],
                            "consumer_var": consumer_var,
                            "producer_node": prod_id,
                            "producer_field": prod_field,
                            "producer_type": (by_id.get(prod_id) or {}).get("type"),
                            "producer_exists": prod_id in by_id,
                            "producer_is_direct_predecessor": prod_id
                            in row["direct_predecessors"],
                        }
                    )

        consumed_nodes = {
            r["producer_node"]
            for r in variable_rows
            if r["workflow_file"] == wf["file"]
        }
        sequence_only = [
            {
                "source": e["source"],
                "source_type": (by_id.get(e["source"]) or {}).get("type"),
                "target": e["target"],
                "target_type": (by_id.get(e["target"]) or {}).get("type"),
            }
            for e in edges
            if e["source"] in by_id and e["source"] not in consumed_nodes
        ]

        counts = Counter(n["data"].get("type") for n in nodes)
        node_ledger["workflows"].append(
            {
                "file": wf["file"],
                "display_name": wf["display_name"],
                "mode": wf["doc"].get("app", {}).get("mode"),
                "dsl_version": wf["doc"].get("version"),
                "size_bytes": wf["size_bytes"],
                "sha256": wf["sha256"],
                "counts": {
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "by_type": dict(sorted(counts.items())),
                },
                "nodes": wf_nodes,
                "edges": [
                    {
                        "source": e["source"],
                        "target": e["target"],
                        "source_handle": e.get("sourceHandle"),
                        "target_handle": e.get("targetHandle"),
                        "is_in_iteration": bool(
                            (e.get("data") or {}).get("isInIteration")
                        ),
                        "is_in_loop": bool((e.get("data") or {}).get("isInLoop")),
                    }
                    for e in edges
                ],
                "sequence_only_edges": sequence_only,
            }
        )
        digest.append(
            {
                "file": wf["file"],
                "display_name": wf["display_name"],
                "nodes": len(nodes),
                "edges": len(edges),
                "sequence_only_edges": len(sequence_only),
                "by_type": dict(sorted(counts.items())),
            }
        )
        totals["nodes"] += len(nodes)
        totals["edges"] += len(edges)

    node_ledger["totals"] = {
        "workflows": len(wfs),
        "nodes": totals["nodes"],
        "edges": totals["edges"],
        "by_type": dict(sorted(type_totals.items())),
        "tool_nodes": totals["tool_nodes"],
        "vision_nodes": totals["vision_nodes"],
        "grouped_aggregators": totals["grouped_aggregators"],
    }

    entries = []
    for tname, refcount in sorted(tool_refs.items(), key=lambda kv: (-kv[1], kv[0])):
        entries.append(
            {
                "tool_name": tname,
                "dsl_file": TOOL_MAP.get(tname),
                "mapped": tname in TOOL_MAP,
                "static_references": refcount,
            }
        )
    tool_mapping = {
        "note": "tool_name -> local DSL file. Reference counts are measured.",
        "entries": entries,
        "unmapped_tool_names": [e["tool_name"] for e in entries if not e["mapped"]],
        "mapped_but_unreferenced": sorted(set(TOOL_MAP) - set(tool_refs)),
    }

    return {
        "node_ledger": node_ledger,
        "variable_ledger": {
            "note": "Binding entries (a declared binding or an embedded "
                    "{{#node.field#}} read) with their resolved consumer and "
                    "producer. This is NOT the same as the count of "
                    "{{#node.field#}} occurrences in the DSL text: a node's "
                    "variables[] entry or a tool parameter is a binding even "
                    "when its value is not a reference literal. "
                    "producer_is_direct_predecessor=false marks a read that "
                    "crosses no direct edge.",
            "total_binding_entries": len(variable_rows),
            "references": variable_rows,
        },
        "resource_inventory": {
            "note": "Prompt/template fingerprints. Bodies are not stored in "
                    "this file; run with --dump-prompts to write them verbatim "
                    "to docs/specs/prompts/.",
            "total": len(resource_rows),
            "entries": sorted(
                resource_rows, key=lambda r: (r["workflow_file"], r["node_id"])
            ),
        },
        "tool_mapping": tool_mapping,
        "secrets": secret_rows,
        "digest": digest,
    }


def cross_check(result: dict) -> list[str]:
    t = result["node_ledger"]["totals"]
    measured = dict(t["by_type"])
    measured.update(
        workflows=t["workflows"],
        nodes=t["nodes"],
        edges=t["edges"],
        tool_nodes=t["tool_nodes"],
        vision_nodes=t["vision_nodes"],
    )
    diffs = [
        "%s: doc asserts %s, DSL measured %s" % (k, want, measured.get(k))
        for k, want in ASSERTED.items()
        if measured.get(k) != want
    ]
    gemini = next(
        (e["static_references"] for e in result["tool_mapping"]["entries"]
         if e["tool_name"] == "gemini_retry_2_times"),
        None,
    )
    if gemini != ASSERTED_GEMINI_REFS:
        diffs.append(
            "gemini_retry_2_times: doc asserts %s, DSL measured %s"
            % (ASSERTED_GEMINI_REFS, gemini)
        )
    return diffs


def extract_prompts() -> int:
    """Write verbatim prompt bodies to the committed docs/specs/prompts/ tree."""
    PROMPT_DUMP.mkdir(parents=True, exist_ok=True)
    written = 0
    for path in sorted(DSL_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        safe = re.sub(r"[^0-9A-Za-z_.-]", "_", path.stem)[:60]
        # Distinct workflows share long identical prefixes once sanitised, so
        # the truncation alone collides. Suffix a hash of the full stem.
        safe = "%s_%s" % (safe, hashlib.sha256(path.stem.encode("utf-8")).hexdigest()[:8])
        for n in doc["workflow"]["graph"]["nodes"]:
            body = prompt_text(n["data"])
            if body is None:
                continue
            dest = PROMPT_DUMP / (
                "%s__%s__%s.txt" % (safe, n["id"], n["data"].get("type"))
            )
            dest.write_text(body, encoding="utf-8")
            written += 1
    return written


def main() -> int:
    unknown = [a for a in sys.argv[1:] if a != "--dump-prompts"]
    if unknown:
        sys.exit("unknown argument(s): %s (supported: --dump-prompts)"
                 % " ".join(unknown))

    result = build(load_workflows())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("node_ledger.json", result["node_ledger"]),
        ("variable_ledger.json", result["variable_ledger"]),
        ("resource_inventory.json", result["resource_inventory"]),
        ("tool_mapping.json", result["tool_mapping"]),
        ("workflow_digest.json", {"workflows": result["digest"]}),
    ):
        (OUT_DIR / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    t = result["node_ledger"]["totals"]
    print("workflows        : %d" % t["workflows"])
    print("nodes            : %d" % t["nodes"])
    print("edges            : %d" % t["edges"])
    print("tool nodes       : %d (distinct tool_name: %d)"
          % (t["tool_nodes"], len(result["tool_mapping"]["entries"])))
    print("vision nodes     : %d" % t["vision_nodes"])
    print("grouped aggrs    : %d" % t["grouped_aggregators"])
    vl = result["variable_ledger"]
    raw_refs = sum(
        len(REF_RE.findall(pathlib.Path(DSL_DIR, w["file"]).read_text(encoding="utf-8")))
        for w in result["node_ledger"]["workflows"]
    )
    print("binding entries  : %d" % vl["total_binding_entries"])
    print("{{#..#}} in text : %d" % raw_refs)
    print("prompt/templates : %d" % result["resource_inventory"]["total"])
    for k in sorted(t["by_type"]):
        print("  type %-20s %d" % (k, t["by_type"][k]))

    if result["secrets"]:
        print("")
        print("credential-shaped strings found (masked, not written to repo):")
        seen = set()
        for h in result["secrets"]:
            key = (h["workflow_file"], h["node_id"], h["kind"])
            if key in seen:
                continue
            seen.add(key)
            print("  %s node=%s %s %s"
                  % (h["workflow_file"], h["node_id"], h["kind"], h["masked"]))

    diffs = cross_check(result)
    print("\ncross-check vs planning doc: %s"
          % ("all assertions match" if not diffs
             else "%d mismatch(es)" % len(diffs)))
    for d in diffs:
        print("  MISMATCH %s" % d)

    if "--dump-prompts" in sys.argv:
        print("\nprompt bodies dumped (committed): %d files" % extract_prompts())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
