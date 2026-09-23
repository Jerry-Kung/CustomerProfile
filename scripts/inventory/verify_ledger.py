#!/usr/bin/env python3
"""V0.1.1 ledger verifier.

Read-only re-check. Re-runs the extraction from the DSL and compares the result
against the committed ledgers under docs/specs/ledger/. Exits non-zero on any
mismatch, so it is safe to wire into a pre-commit hook or CI.

This deliberately recomputes rather than re-reading the ledgers' own summary
fields: comparing a file to itself proves nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_ledger import (  # noqa: E402
    OUT_DIR,
    build,
    cross_check,
    load_workflows,
)

# Fields expected in every node row, so a silent schema change is caught.
NODE_FIELDS = {
    "node_id",
    "title",
    "type",
    "direct_predecessors",
    "input_bindings",
    "binding_sources",
    "literal_bindings",
    "output_fields",
    "vision_enabled",
    "in_iteration",
    "iteration_id",
    "error_strategy",
    "tool_name",
    "original_coords",
}

# Substrings that must never appear in anything committed. Prompt bodies are
# committed verbatim under docs/specs/prompts/ (D8) and are scanned too; the
# ledger JSONs keep hashes only.
FORBIDDEN_SUBSTRINGS = ["X-API-Key", "sk-"]


def fail(msg: str) -> None:
    print("FAIL: %s" % msg)


def main() -> int:
    problems: list[str] = []

    if not OUT_DIR.is_dir():
        print("FAIL: ledger directory missing: %s" % OUT_DIR)
        print("Run: python scripts/inventory/extract_ledger.py")
        return 1

    fresh = build(load_workflows())
    committed = {}
    for name, key in (
        ("node_ledger.json", "node_ledger"),
        ("variable_ledger.json", "variable_ledger"),
        ("resource_inventory.json", "resource_inventory"),
        ("tool_mapping.json", "tool_mapping"),
    ):
        path = OUT_DIR / name
        if not path.is_file():
            problems.append("missing ledger file: %s" % name)
            continue
        committed[key] = json.loads(path.read_text(encoding="utf-8"))

    ft = fresh["node_ledger"]["totals"]

    # 1. headline counts
    if "node_ledger" in committed:
        ct = committed["node_ledger"]["totals"]
        for field in ("workflows", "nodes", "edges", "tool_nodes",
                      "vision_nodes", "grouped_aggregators"):
            if ct.get(field) != ft.get(field):
                problems.append(
                    "node_ledger.totals.%s: committed=%s recomputed=%s"
                    % (field, ct.get(field), ft.get(field))
                )
        if ct.get("by_type") != ft.get("by_type"):
            problems.append("node_ledger.totals.by_type differs")

        # 2. per-workflow structure and node schema
        cw = {w["file"]: w for w in committed["node_ledger"]["workflows"]}
        for w in fresh["node_ledger"]["workflows"]:
            c = cw.get(w["file"])
            if c is None:
                problems.append("workflow missing from ledger: %s" % w["file"])
                continue
            if c["sha256"] != w["sha256"]:
                problems.append(
                    "DSL changed since ledger was built: %s "
                    "(committed=%s recomputed=%s) -- regenerate the ledger"
                    % (w["file"], c["sha256"][:12], w["sha256"][:12])
                )
            for key in ("counts", "edges"):
                if c.get(key) != w.get(key):
                    problems.append("%s: %s differs" % (w["file"], key))
            if len(c.get("nodes", [])) != len(w["nodes"]):
                problems.append("%s: node count differs" % w["file"])
                continue
            for cn, wn in zip(c["nodes"], w["nodes"]):
                if cn["node_id"] != wn["node_id"]:
                    problems.append(
                        "%s: node order differs at %s" % (w["file"], wn["node_id"])
                    )
                    break
                if cn.get("input_bindings") != wn["input_bindings"]:
                    problems.append(
                        "%s node %s: input_bindings differ"
                        % (w["file"], wn["node_id"])
                    )
                if set(cn) != set(wn):
                    problems.append(
                        "%s node %s: schema differs %s"
                        % (w["file"], wn["node_id"],
                           sorted(set(cn) ^ set(wn)))
                    )
                missing = NODE_FIELDS - set(cn)
                if missing:
                    problems.append(
                        "%s node %s: missing fields %s"
                        % (w["file"], wn["node_id"], sorted(missing))
                    )

    # 3. binding entries
    if "variable_ledger" in committed:
        c = committed["variable_ledger"]["total_binding_entries"]
        f = fresh["variable_ledger"]["total_binding_entries"]
        if c != f:
            problems.append(
                "total_binding_entries: committed=%s recomputed=%s" % (c, f)
            )
        if len(committed["variable_ledger"]["references"]) != f:
            problems.append("variable_ledger.references length disagrees with count")

    # 4. tool mapping completeness
    fmap = fresh["tool_mapping"]
    if fmap["unmapped_tool_names"]:
        problems.append(
            "tool_name(s) present in DSL but absent from TOOL_MAP: %s"
            % fmap["unmapped_tool_names"]
        )
    if fmap["mapped_but_unreferenced"]:
        problems.append(
            "TOOL_MAP entries never referenced by any tool node: %s"
            % fmap["mapped_but_unreferenced"]
        )
    if "tool_mapping" in committed:
        if committed["tool_mapping"]["entries"] != fmap["entries"]:
            problems.append("tool_mapping entries differ from recomputation")

    # 5. resource inventory
    if "resource_inventory" in committed:
        if (committed["resource_inventory"]["total"]
                != fresh["resource_inventory"]["total"]):
            problems.append("resource_inventory total differs")

    # 5b. committed prompt bodies carry no credential shapes
    prompt_dir = OUT_DIR.parent / "prompts"
    if prompt_dir.is_dir():
        for f in sorted(prompt_dir.glob("*.txt")):
            body = f.read_text(encoding="utf-8")
            for needle in FORBIDDEN_SUBSTRINGS:
                if needle in body:
                    problems.append(
                        "committed prompt %s contains forbidden substring %r"
                        % (f.name, needle)
                    )

    # 6. nothing sensitive or verbatim in the committed ledgers
    for name in ("node_ledger.json", "variable_ledger.json",
                 "resource_inventory.json", "tool_mapping.json",
                 "workflow_digest.json"):
        path = OUT_DIR / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_SUBSTRINGS:
            if needle in text:
                problems.append("%s contains forbidden substring %r" % (name, needle))

    # 7. planning-doc assertions still hold
    for d in cross_check(fresh):
        problems.append("cross-check: %s" % d)

    if problems:
        for p in problems:
            fail(p)
        print("\n%d problem(s) found." % len(problems))
        return 1

    print("OK: ledgers match the DSL.")
    print("  workflows=%d nodes=%d edges=%d tool_nodes=%d vision_nodes=%d "
          "grouped_aggregators=%d binding_entries=%d prompt_templates=%d"
          % (ft["workflows"], ft["nodes"], ft["edges"], ft["tool_nodes"],
             ft["vision_nodes"], ft["grouped_aggregators"],
             fresh["variable_ledger"]["total_binding_entries"],
             fresh["resource_inventory"]["total"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
