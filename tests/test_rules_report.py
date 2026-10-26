"""规则命中统计：钉住聚合口径，而不是钉住某次输出。

统计脚本最容易出的错不是算错加法，而是**把不该比的东西放在一起比**：

- 早于规则层的批次里根本没有新编号，算成「零命中」就是误报；
- 同一批运行的多个视图（UAT 快照与它的 8 案子集）算两批，零命中连击会虚增；
- 旧裸串归一不到新编号时如果静默计入，命中数会被算高。

因此这里用合成的批次目录构造上述三种情形，断言报告怎么分类。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import rules_report as rr  # noqa: E402

from customer_profile.rules import default as default_registry  # noqa: E402


def write_batch(root: Path, name: str, *, created: str, cases: dict, manifest: dict) -> Path:
    """``cases`` 形如 ``{"手机号": [节点条目]}``。"""
    batch = root / name
    (batch / "runs").mkdir(parents=True)
    (batch / "manifest.json").write_text(
        json.dumps({"created_at": created, **manifest}, ensure_ascii=False),
        encoding="utf-8",
    )
    for phone, entries in cases.items():
        case = batch / "runs" / phone
        case.mkdir()
        (case / "nodes.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
    return batch


def node(node_id: str, outputs: dict, status: str = "succeeded") -> dict:
    return {"node_id": node_id, "status": status, "outputs": outputs}


def trace(rule_id: str, action: str = "downgrade") -> dict:
    return {
        "rule_id": rule_id,
        "stage": "project",
        "attribute": "annual_income",
        "action": action,
        "severity": "repair",
        "reason": "x",
        "source": "code",
    }


# ---------------------------------------------------------------- 基本聚合

def test_counts_new_rule_hits_by_action(tmp_path):
    root = tmp_path
    write_batch(
        root,
        "b1",
        created="2026-10-01T00:00:00",
        cases={
            "138": [
                node("pipeline_l2_project", {"rule_trace_json": json.dumps([trace("HARD-01", "reject")])}),
                node("pipeline_l2_review", {"rule_trace_json": json.dumps([trace("HARD-02")])}),
            ]
        },
        manifest={},
    )
    batches = rr.load_batches(root, registry=default_registry())
    rows = {row["rule"]: row for row in rr.rule_rows(batches, default_registry())}
    assert rows["HARD-01"]["total"] == 1
    assert rows["HARD-01"]["actions"] == {"reject": 1}
    assert rows["HARD-02"]["actions"] == {"downgrade": 1}


def test_failed_node_is_not_counted(tmp_path):
    root = tmp_path
    write_batch(
        root,
        "b1",
        created="2026-10-01T00:00:00",
        cases={
            "138": [
                node(
                    "pipeline_l2_project",
                    {"rule_trace_json": json.dumps([trace("HARD-01")])},
                    status="failed",
                )
            ]
        },
        manifest={},
    )
    batches = rr.load_batches(root, registry=default_registry())
    assert batches[0].total_hits == 0


# ---------------------------------------------------------------- 零命中口径

def test_zero_streak_ignores_batches_without_rule_layer(tmp_path):
    """老批次没有 rule_trace 字段：不参与零命中判定，否则每条规则都误报。"""
    root = tmp_path
    write_batch(
        root,
        "old",
        created="2026-09-01T00:00:00",
        cases={"138": [node("pipeline_l2_project", {"facts_json": "{}"})]},
        manifest={},
    )
    write_batch(
        root,
        "new",
        created="2026-10-01T00:00:00",
        cases={
            "139": [
                node(
                    "pipeline_l2_project",
                    {"rule_trace_json": json.dumps([trace("HARD-01")])},
                )
            ]
        },
        manifest={},
    )
    batches = rr.load_batches(root, registry=default_registry())
    assert [b.rule_layer for b in batches] == [False, True]
    rows = {row["rule"]: row for row in rr.rule_rows(batches, default_registry())}
    # HARD-01 在唯一一个规则层批次里命中过 → 零命中连击为 0
    assert rows["HARD-01"]["zero_streak"] == 0


def test_zero_streak_counts_consecutive_tail_only(tmp_path):
    root = tmp_path
    for index, hits in enumerate(([trace("HARD-02")], [], [])):
        write_batch(
            root,
            f"b{index}",
            created=f"2026-10-0{index + 1}T00:00:00",
            cases={"138": [node("pipeline_l2_project", {"rule_trace_json": json.dumps(hits)})]},
            manifest={},
        )
    batches = rr.load_batches(root, registry=default_registry())
    rows = {row["rule"]: row for row in rr.rule_rows(batches, default_registry())}
    assert rows["HARD-02"]["zero_streak"] == 2
    # 从没命中过的规则，连击等于规则层批次总数
    assert rows["IDENT-01"]["zero_streak"] == 3


# ---------------------------------------------------------------- 同源去重

def test_same_snapshot_batches_are_merged(tmp_path):
    """UAT 快照与其子集是同一次运行，不能算成两批。"""
    root = tmp_path
    sha = "abc123"
    write_batch(
        root,
        "uat-18",
        created="2026-10-26T10:00:00",
        cases={"138": [node("pipeline_l2_project", {"facts_json": "{}"})]},
        manifest={"source_snapshot_sha256": sha},
    )
    write_batch(
        root,
        "baseline-8",
        created="2026-10-26T13:00:00",
        cases={
            "138": [node("pipeline_l2_project", {"facts_json": "{}"})],
            "139": [node("pipeline_l2_project", {"facts_json": "{}"})],
        },
        manifest={"source_snapshot_sha256": sha},
    )
    batches = rr.load_batches(root, registry=default_registry())
    assert len(batches) == 1
    assert batches[0].name == "baseline-8"  # 案件多的那份留下


# ---------------------------------------------------------------- 旧裸串归一

def test_legacy_codes_map_through_registry(tmp_path):
    root = tmp_path
    write_batch(
        root,
        "b1",
        created="2026-10-01T00:00:00",
        cases={
            "138": [
                node(
                    "pipeline_l2_review",
                    {"issue_json": json.dumps([{"rule": "placeholder_removed", "count": 3}])},
                ),
                node(
                    "pipeline_l2_project",
                    {"rejected_json": json.dumps([{"rule": "name_source_whitelist"}])},
                ),
            ]
        },
        manifest={},
    )
    batches = rr.load_batches(root, registry=default_registry())
    rows = {row["code"]: row for row in rr.legacy_rows(batches, default_registry())}
    assert rows["placeholder_removed"]["rule"] == "HARD-01"
    assert rows["placeholder_removed"]["mapped"] is True
    # 未登记的旧裸串单独列出，不并进任何规则
    assert rows["name_source_whitelist"]["mapped"] is False
    assert rows["name_source_whitelist"]["rule"] == ""


def test_unmapped_legacy_does_not_inflate_rule_hits(tmp_path):
    root = tmp_path
    write_batch(
        root,
        "b1",
        created="2026-10-01T00:00:00",
        cases={
            "138": [
                node(
                    "pipeline_l2_project",
                    {"rejected_json": json.dumps([{"rule": "name_source_whitelist"}])},
                )
            ]
        },
        manifest={},
    )
    batches = rr.load_batches(root, registry=default_registry())
    rows = {row["rule"]: row for row in rr.rule_rows(batches, default_registry())}
    assert all(row["total"] == 0 for row in rows.values())


# ---------------------------------------------------------------- 渲染

def test_render_marks_prompt_rules_as_not_zero_hit():
    """prompt 通道的规则没有判据，命中 0 是正常的，不该被标成零命中告警。"""
    registry = default_registry()
    rows = rr.rule_rows([], registry)
    text = rr.render_text(
        {"batches": [], "rules": rows, "legacy": [], "checker": {}, "cases": []},
        zero_batches=3,
    )
    spec_line = next(line for line in text.splitlines() if line.startswith("SPEC-02"))
    assert "prompt 通道无判据" in spec_line


def test_empty_batch_list_does_not_crash():
    text = rr.render_text(
        {"batches": [], "rules": [], "legacy": [], "checker": {}, "cases": []},
        zero_batches=3,
    )
    assert "没有连续" in text or "零命中" in text


# ---------------------------------------------------------------- 真实批次冒烟

def test_real_batches_load_when_present():
    """本机有归档批次时，脚本必须能把它们读进来（不存在的环境跳过）。

    **不再假设「归档批次全部早于规则层」**：规则层上线后新增的批次（例如
    ``uat-18-20261026-replay``）自带 ``rule_trace``。判据改为按批次的
    ``rule_layer`` 分叉——带规则层的批次必须报出命中，不带的一律零命中。
    这样既保住原意（不带规则层的批次不该冒出命中），也不会因为本机多了一个新批次就误报。
    """
    root = rr.BATCH_ROOT
    if not root.is_dir():
        pytest.skip("本机没有归档批次")
    batches = rr.load_batches(root, registry=default_registry())
    if not batches:
        pytest.skip("归档批次为空")
    assert all(batch.cases > 0 for batch in batches)

    rows = rr.rule_rows(batches, default_registry())
    assert rows

    with_layer = [batch.name for batch in batches if batch.rule_layer]
    for row in rows:
        for batch_name, hits in row["per_batch"].items():
            if batch_name in with_layer:
                continue
            # 不带规则层的批次只可能有旧裸串，新编号必须为 0。
            assert hits == 0, f"{batch_name} 没有规则层却报出 {row['rule']} 命中 {hits}"

    if with_layer:
        assert any(row["total"] > 0 for row in rows), (
            f"带规则层的批次 {with_layer} 至少应有一条规则命中"
        )
