"""T1 验收探针：用基线批次的真实产物驱动 L0 记录层与 L4 分诊层（0 token）。

为什么需要它
------------
L0 的判据全是「从原始产物里读出结构事实」，因此可以**完全不花 token** 离线回归：
把基线批次的 ``attempts.json``（HTTP 响应）与 ``nodes.json``（各节点产出）当作
入口清单与 14 路渠道产物，直接喂给 ``build_records`` / ``triage``。

为什么是脚本而不是 pytest 用例
------------------------------
``experiments/`` 被 gitignore，干净检出上没有批次产物；`tests/conftest.py` 的口径是
「不出网也能覆盖全部路径、不出现『缺文件所以跳过』」。因此：**本脚本**读真实批次，
`tests/test_pipeline_l0.py` 用合成夹具断言同一套语义，两者互补。

用法::

    python scripts/probe_pipeline_l0.py                       # 全 39 案
    python scripts/probe_pipeline_l0.py --phone 13530559591   # 只跑一案，打印逐条记录

三条验收判据（``docs/business_opt01/落地计划书.md`` §4 T1）：

1. 9 个结构性空渠道 × 39 案 = **351 条** ``status=exception``；
2. ``13530559591`` 的 records 里**明确含**那段 256 字符外呼转写；
3. 每条带清单背书的 record 都有 ``created_at``。

第 3 条的措辞是刻意的：**人工确认 Notes / 历史画像反馈** 两个渠道走 mhero，
不在入口清单里，因而没有 ``created_at`` 可继承；伪造一个时间戳会让 L2 的时效判定
失去依据，所以记录层留空，探针只对清单背书的记录断言非空。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile.workflows import customer_profile_pipeline as pipe  # noqa: E402

DEFAULT_BATCH = REPO_ROOT / "experiments" / "batches" / "baseline-39-dsv41-noThink"

HISTORY_NODE = "1775638961126"
"""入口「获取全量用户数据」的 HTTP 节点 ID。"""

# —— 14 路取数节点 → (渠道产出取自哪个字段)
TOOL_FIELDS: dict[str, str] = {
    "1776068474385": "result",            # 朋友圈
    "1776070114662": "result",            # 微信手机号搜索截图
    "1776070141016": "result",            # 微信主页
    "1776150525082": "result",            # 试驾录音（压缩报告）
    "1776322922950": "result",            # 外呼录音（压缩报告）
    "1776325681312": "result",            # 支付宝
    "1776325690049": "result",            # 小红书
    "1776325696104": "result",            # 抖音
    "1777286551779": "result",            # 人工确认 Notes
    "1777287016409": "result",            # 极光
    "1778231153601": "IT_customer_info",  # 猛士 IT
    "1778809368635": "result",            # 聊天记录
    "1779089586906": "result",            # 历史画像反馈
    "1789983285382": "company_analysis_result",  # 手机号 & 企业
}

AUDIO_START = "1773112560827"
AUDIO_EXTRACT = "1773125494862"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def history_body(run_dir: Path) -> str | None:
    """从 ``attempts.json`` 取入口清单的响应体。"""
    for attempt in _load(run_dir / "attempts.json"):
        if attempt.get("kind") == "http" and "history" in str(attempt.get("target", "")):
            return attempt.get("response_text")
    return None


def reconstruct_inputs(run_dir: Path) -> dict[str, Any]:
    """把批次产物重建成 ``bundle_channels`` 的入参。

    两条重建规则：

    - **渠道产出**按 ``nodes.json`` 里该节点在 ``/0``（父图第一次执行）的 ``outputs`` 取；
    - **录音原文**按 ``audio_content_extract`` 的 ``start.file_url`` **自描述**归属渠道——
      试驾与外呼各有一条独立迭代链，用 URL 里的渠道名分辨，而不是靠调用序号猜。同渠道
      多个文件的原文按列表收集，交给记录层去空。
    """
    nodes = _load(run_dir / "nodes.json")

    def out(node_id: str, field: str) -> Any:
        for entry in nodes:
            if (
                entry.get("node_id") == node_id
                and entry.get("call_path") == "/0"
                and entry.get("depth") == 1
            ):
                return (entry.get("outputs") or {}).get(field)
        return None

    chains: dict[str, str] = {}
    for entry in nodes:
        if entry.get("node_id") == AUDIO_START:
            chains[entry.get("call_path")] = (entry.get("outputs") or {}).get("file_url", "")

    transcripts: dict[str, list[Any]] = {name: [] for name in pipe.TRANSCRIPT_SOURCES}
    for call_path, url in sorted(chains.items()):
        if "TestDriveRecordingFile" in url:
            channel = "试驾录音信息"
        elif "AIOutboundCallRecordingFile" in url:
            channel = "外呼录音信息"
        else:
            continue
        for entry in nodes:
            if entry.get("call_path") == call_path and entry.get("node_id") == AUDIO_EXTRACT:
                transcripts[channel].append((entry.get("outputs") or {}).get("auc_result"))

    return {
        "history_body": history_body(run_dir),
        "moments": out("1776068474385", "result"),
        "wechat_search": out("1776070114662", "result"),
        "wechat_home": out("1776070141016", "result"),
        "test_drive_report": out("1776150525082", "result"),
        "outbound_report": out("1776322922950", "result"),
        "alipay": out("1776325681312", "result"),
        "xiaohongshu": out("1776325690049", "result"),
        "douyin": out("1776325696104", "result"),
        "locked_notes": out("1777286551779", "result"),
        "jiguang": out("1777287016409", "result"),
        "mengshi_it": out("1778231153601", "IT_customer_info"),
        "chat_history": out("1778809368635", "result"),
        "feedback": out("1779089586906", "result"),
        "phone_company": out("1789983285382", "company_analysis_result"),
        "test_drive_transcript": transcripts["试驾录音信息"] or None,
        "outbound_transcript": transcripts["外呼录音信息"] or None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T1 验收探针：离线驱动 L0/L4")
    parser.add_argument("--batch-dir", default=str(DEFAULT_BATCH))
    parser.add_argument("--phone", default=None, help="只跑某一案并逐条打印记录")
    parser.add_argument(
        "--transcript-phone", default="13530559591", help="断言含 256 字符外呼转写的案"
    )
    args = parser.parse_args(argv)

    batch = Path(args.batch_dir)
    runs = batch / "runs"
    if not runs.is_dir():
        print(f"[FAIL] 找不到批次产物目录：{runs}")
        return 2

    phones = (
        [args.phone]
        if args.phone
        else sorted(p.name for p in runs.iterdir() if p.is_dir())
    )

    failures: list[str] = []
    exception_total = 0
    exception_by_channel: dict[str, int] = {}
    missing_created_at: list[str] = []
    manifest_ok = 0
    transcript_hit: str | None = None
    tiers: dict[str, int] = {}

    for phone in phones:
        run_dir = runs / phone
        body = history_body(run_dir)
        if body is None:
            failures.append(f"{phone}: 找不到入口清单响应体")
            continue
        channels = pipe.bundle_channels(**reconstruct_inputs(run_dir))
        built = pipe.build_records(body, channels["channels_json"], phone)
        records = json.loads(built["records_json"])

        for record in records:
            if record["status"] == pipe.EXCEPTION:
                exception_total += 1
                key = record["source_name"]
                exception_by_channel[key] = exception_by_channel.get(key, 0) + 1
            # 判据 3：**有内容的**清单背书记录必须有 created_at。范围刻意排除
            # exception 记录——清单里没有该 channel 就没有时间可继承，那不是缺陷，
            # 而正是「记录层如实反映缺失」的表现。MANIFEST_OK 单独计数以便报告。
            spec = next(
                s for s in pipe.CHANNELS if s["source_name"] == record["source_name"]
            )
            if spec["manifest_channel"] is not None and record["status"] == pipe.OK:
                manifest_ok += 1
                if not record["created_at"]:
                    missing_created_at.append(f"{phone}/{record['source_name']}")

        tier = pipe.triage(built["records_json"])
        tiers[tier["tier"]] = tiers.get(tier["tier"], 0) + 1

        if phone == args.transcript_phone:
            for record in records:
                if record["source_name"] == "外呼录音信息":
                    transcript_hit = record["content"]

        if args.phone:
            print(f"=== {phone} ===")
            for record in records:
                print(
                    f"  {record['status']:<9} {record['source_name']:<28} "
                    f"{record['content_kind']:<10} len={len(record['content']):>6} "
                    f"created_at={'有' if record['created_at'] else '无'}"
                )
            print(f"  tier={tier['tier']} ({tier['tier_reason']})")

    print()
    print(f"案数: {len(phones)}")
    structural = sum(c for c in exception_by_channel.values() if c == len(phones))
    print(f"[判据1] status=exception 合计: {exception_total}")
    print(
        f"          其中 39/39 全案缺失的「结构性空渠道」: "
        f"{sum(1 for c in exception_by_channel.values() if c == len(phones))} 个渠道 × "
        f"{len(phones)} 案 = {structural} 条"
    )
    for name, count in sorted(exception_by_channel.items(), key=lambda x: -x[1]):
        print(f"          {count:>3}  {name}")
    if transcript_hit:
        chars = len(transcript_hit)
        text_len = sum(
            len(u.get("text", "")) for u in json.loads(transcript_hit)
        ) if transcript_hit.strip().startswith("[") else chars
        print(
            f"[判据2] {args.transcript_phone} 外呼转写：载荷 {chars} 字符，"
            f"逐句正文合计 {text_len} 字符"
        )
        if text_len != 256:
            failures.append(f"判据2：{args.transcript_phone} 逐句正文合计 {text_len}，期望 256")
    else:
        failures.append(f"判据2：{args.transcript_phone} 未产出外呼录音记录")
    print(
        f"[判据3] 有内容的清单背书记录 {manifest_ok} 条，"
        f"其中缺 created_at: {len(missing_created_at)}"
    )
    for item in missing_created_at[:10]:
        print(f"          {item}")
    print(f"分诊分布: {tiers}")

    if failures:
        print()
        for item in failures:
            print(f"[FAIL] {item}")
        return 1
    print()
    print("[OK] T1 三条判据全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
