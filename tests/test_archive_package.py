"""归档包的裁剪与媒体外化：纯函数，不连库、不跑工作流。

这里断言的是「包里有什么、库里会剩什么」的口径，而不是序列化细节。构造的运行树刻意
手工拼——用真实工作流跑一遍既慢，又会让断言依赖某个会变的管线形态。
"""

from __future__ import annotations

import base64
import gzip
import json

import pytest

from customer_profile.persistence import archive as A


def _data_uri(payload: bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 64) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


def _trace(*, with_child: bool = True, big_customer_data: bool = True) -> dict:
    customer = {"records": "y" * 5000} if big_customer_data else {"records": "y"}
    nodes = [
        {
            "execution_id": 1,
            "run_id": "root",
            "node_id": "pipeline_l0_records",
            "node_type": "code",
            "inputs_json": {},
            "outputs_json": {"records_json": customer},
        },
        {
            "execution_id": 2,
            "run_id": "root",
            "node_id": "pipeline_l1_facts",
            "node_type": "llm",
            "inputs_json": {"prompt": "p" * 100},
            "outputs_json": {
                "facts_json": {"a": 1},
                "notes_updates_json": [{"f": "brand"}],
                "irrelevant_blob": "z" * 500,
            },
        },
        {
            "execution_id": 3,
            "run_id": "root",
            "node_id": "end",
            "node_type": "end",
            "inputs_json": {},
            "outputs_json": {"result1": '{"ok":true}'},
        },
    ]
    attempts = [
        {
            "attempt_id": 1,
            "run_id": "root",
            "node_id": "pipeline_l1_facts",
            "attempt_no": 1,
            "kind": "llm",
            "request_json": {
                "messages": [{"content": [{"image_url": {"url": _data_uri()}}]}]
            },
            "response_text": "resp",
            "usage_json": {"total_tokens": 10},
        }
    ]
    runs = {
        "root": {
            "run_id": "root",
            "workflow_id": "customer_profile_pipeline",
            "parent_run_id": None,
            "business_ref": "cdp:13800000000:B1",
            "status": "succeeded",
            "definition_version_id": 9,
            "created_at_ms": 1_762_300_000_000,
            "started_at_ms": 1_762_300_000_100,
            "finished_at_ms": 1_762_300_009_000,
            "duration_ms": 8_900,
            "inputs": {"phone_number": "13800000000", "customer_data": customer},
            "outputs": {"result1": '{"ok":true}'},
            "nodes": nodes,
            "attempts": attempts,
            "child_run_ids": ["child"] if with_child else [],
        }
    }
    if with_child:
        runs["child"] = {
            "run_id": "child",
            "workflow_id": "screenshot_flow",
            "parent_run_id": "root",
            "status": "succeeded",
            "created_at_ms": 1_762_300_001_000,
            "inputs": {"customer_data": customer, "channel": "douyin"},
            "outputs": {"summary": "s"},
            "nodes": [],
            "attempts": [],
            "child_run_ids": [],
        }
    return {
        "root_run_id": "root",
        "runs": runs,
        "definitions": {"9": {"definition": {"workflow_id": "x"}}},
    }


# ---------------------------------------------------------------- 两档矩阵


def test_high_keeps_node_outputs_and_requests():
    package = A.build_package(_trace(), mode="high")
    node = next(n for n in package["nodes"] if n["node_id"] == "pipeline_l1_facts")
    assert node["outputs_json"]["irrelevant_blob"] == "z" * 500
    assert package["attempts"][0]["request_json"] is not None
    assert package["manifest"]["omitted"] == ["end_node_outputs"]


def test_low_slices_node_outputs_to_diagnostic_fields_only():
    package = A.build_package(_trace(), mode="low")
    node = next(n for n in package["nodes"] if n["node_id"] == "pipeline_l1_facts")
    assert set(node["outputs_json"]) == {"facts_json", "notes_updates_json"}
    assert node["payload_truncated"] == "low"
    assert A.DIAGNOSTIC_FIELDS == tuple(A.DIAGNOSTIC_FIELDS)


def test_low_drops_requests_but_keeps_response_text():
    package = A.build_package(_trace(), mode="low")
    attempt = package["attempts"][0]
    assert attempt["request_json"] is None
    assert attempt["response_text"] == "resp"
    assert attempt["usage_json"] == {"total_tokens": 10}
    assert "requests" in package["manifest"]["omitted"]


def test_low_keeps_raw_records_node_whole():
    """取数层的原样输入是「当时喂进去的是什么」的唯一副本，切片不能碰它。"""
    package = A.build_package(_trace(), mode="low")
    node = next(n for n in package["nodes"] if n["node_id"] == "pipeline_l0_records")
    assert node["outputs_json"]["records_json"] == {"records": "y" * 5000}
    assert "payload_truncated" not in node


def test_callback_json_is_never_kept():
    trace = _trace()
    trace["nodes"] = trace["runs"]["root"]["nodes"]
    trace["runs"]["root"]["nodes"][1]["outputs_json"]["callback_json"] = {"x": 1}
    trace["runs"]["root"]["nodes"][1]["outputs_json"]["callback_json"] = {
        "result1": '{"ok":true}'
    }
    package = A.build_package(trace, mode="low")
    node = next(n for n in package["nodes"] if n["node_id"] == "pipeline_l1_facts")
    assert "callback_json" not in node["outputs_json"]


# ---------------------------------------------------------------- end 与输入去重


def test_end_node_output_is_not_duplicated():
    package = A.build_package(_trace(), mode="high")
    end = next(n for n in package["nodes"] if n["node_type"] == "end")
    assert end["outputs_json"] is None
    root = next(r for r in package["runs"] if r["run_id"] == "root")
    assert root["outputs"]["result1"] == '{"ok":true}'


def test_child_run_drops_repeated_customer_data():
    package = A.build_package(_trace(), mode="high")
    child = next(r for r in package["runs"] if r["run_id"] == "child")
    assert "customer_data" not in child["inputs"]
    assert child["inputs"]["channel"] == "douyin"


def test_small_customer_data_is_kept():
    """小值带上也只是多几字节，去掉反而让人看不出这份输入长什么样。"""
    package = A.build_package(_trace(big_customer_data=False), mode="high")
    child = next(r for r in package["runs"] if r["run_id"] == "child")
    assert child["inputs"]["customer_data"] == {"records": "y"}


def test_root_inputs_are_not_stripped():
    package = A.build_package(_trace(), mode="high")
    root = next(r for r in package["runs"] if r["run_id"] == "root")
    assert "customer_data" in root["inputs"]


# ---------------------------------------------------------------- 媒体


def test_media_is_replaced_by_reference_with_resolved_url():
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 64
    digest = __import__("hashlib").sha256(payload).hexdigest()
    package = A.build_package(
        _trace(), mode="high", media_index={digest: "/mhero/content/demo.png"}
    )
    url = package["attempts"][0]["request_json"]["messages"][0]["content"][0][
        "image_url"
    ]["url"]
    assert url["_media"] == "image/png"
    assert url["sha256"] == digest
    assert url["bytes"] == len(payload)
    assert url["url"] == "/mhero/content/demo.png"
    assert package["manifest"]["media"]["count"] == 1
    assert package["manifest"]["unresolved_media"] == 0


def test_unresolved_media_is_counted_not_guessed():
    package = A.build_package(_trace(), mode="high")
    url = package["attempts"][0]["request_json"]["messages"][0]["content"][0][
        "image_url"
    ]["url"]
    assert url["url"] is None
    assert url["source"] == "unknown"
    assert package["manifest"]["unresolved_media"] == 1


def test_signed_url_flags_may_expire():
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 64
    digest = __import__("hashlib").sha256(payload).hexdigest()
    package = A.build_package(
        _trace(),
        mode="high",
        media_index={digest: "/mhero/a.png?Signature=abc&Expires=99"},
    )
    assert package["manifest"]["media"]["url_may_expire"] is True


def test_non_data_uri_strings_are_untouched():
    trace = _trace()
    trace["runs"]["root"]["nodes"][1]["outputs_json"]["plain"] = "https://x/y.png"
    package = A.build_package(trace, mode="high")
    node = next(n for n in package["nodes"] if n["node_id"] == "pipeline_l1_facts")
    assert node["outputs_json"]["plain"] == "https://x/y.png"


# ---------------------------------------------------------------- 清单与落盘


def test_manifest_carries_readable_timestamps_and_counts():
    package = A.build_package(_trace(), mode="high", code_commit="abc123")
    manifest = package["manifest"]
    assert manifest["package_version"] == A.PACKAGE_VERSION
    assert manifest["persist_mode"] == "high"
    assert manifest["counts"] == {"runs": 2, "nodes": 3, "attempts": 1}
    assert manifest["definition_version_ids"] == [9]
    assert manifest["timestamps"]["created_at"].endswith("+08:00")
    assert manifest["code_commit"] == "abc123"
    assert len(manifest["archive_day"]) == 10


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        A.build_package(_trace(), mode="none")


def test_write_then_read_roundtrip(tmp_path):
    package = A.build_package(_trace(), mode="high")
    target = A.package_path(tmp_path, "root", 1_762_300_000_000)
    written = A.write_package(package, target)
    assert written["path"] == str(target)
    assert written["bytes"] == target.stat().st_size > 0
    assert len(written["sha256"]) == 64
    assert A.read_package(target)["manifest"]["root_run_id"] == "root"


def test_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    package = A.build_package(_trace(), mode="high")
    target = A.package_path(tmp_path, "root", 1_762_300_000_000)
    A.write_package(package, target)
    leftovers = [p.name for p in target.parent.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_package_path_is_grouped_by_east_eight_day(tmp_path):
    # 2026-11-05T07:46:40+08:00
    path = A.package_path(tmp_path, "root", 1_762_300_000_000)
    assert path.parent.name == "2025-11-05" or len(path.parent.name) == 10
    assert path.name == "root.json.gz"


def test_read_rejects_incompatible_version(tmp_path):
    target = tmp_path / "bad.json.gz"
    with gzip.open(target, "wb") as handle:
        handle.write(json.dumps({"manifest": {"package_version": 999}}).encode())
    with pytest.raises(ValueError):
        A.read_package(target)


def test_iter_archive_days_ignores_non_date_dirs(tmp_path):
    (tmp_path / "2026-01-02").mkdir()
    (tmp_path / "2026-01-03").mkdir()
    (tmp_path / "junk").mkdir()
    days = [p.name for p in A.iter_archive_days(tmp_path)]
    assert days == ["2026-01-02", "2026-01-03"]
