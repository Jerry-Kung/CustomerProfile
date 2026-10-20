"""生成「销售方为 0 的录音」原文文档。

**为什么单独写脚本而不是手工摘录。** 文档要逐句贴原始转写，手工粘贴必有遗漏与串行；
这里从探针结果（``experiments/role_labelling_probe/<时间戳>/``）与转写缓存同时读取，
原文取自缓存、角色取自探针，两者按句序号对齐，任何一处错位都能立刻看出来。

**输出**：``docs/business_opt01_v3/审核报告-销售方为0的录音原文.md``。该文件名命中该目录
``.gitignore`` 里的 ``审核报告-*.md`` 规则，不进 Git——文件里是客户原话。

用法：

    python scripts/render_zero_sales_report.py
    python scripts/render_zero_sales_report.py --probe experiments/role_labelling_probe/20261020-124521
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

DEFAULT_PROBE_ROOT = REPO_ROOT / "experiments" / "role_labelling_probe"
OUT_PATH = REPO_ROOT / "docs" / "business_opt01_v3" / "审核报告-销售方为0的录音原文.md"


def latest_probe_dir(root: Path) -> Path:
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    if not dirs:
        raise SystemExit(f"{root} 下没有探针结果")
    return dirs[-1]


def load_cases(probe_dir: Path) -> list[dict]:
    cases = []
    for path in sorted(probe_dir.glob("*.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        if result["role_counts"].get("销售方", 0) != 0:
            continue
        cases.append(result)
    return cases


def raw_utterances(cache_file: str) -> list[dict]:
    payload = json.loads((REPO_ROOT / cache_file).read_text(encoding="utf-8"))
    if payload.get("silent") or not payload.get("raw_json"):
        return []
    raw = json.loads(payload["raw_json"])
    return raw["result"]["utterances"]


def roles_by_index(labelled: str) -> dict[int, str]:
    roles: dict[int, str] = {}
    for line in labelled.split("\n"):
        if not line.startswith("["):
            continue
        head, _, _ = line.partition("]")
        index_text, _, role = head[1:].partition("|")
        try:
            roles[int(index_text)] = role
        except ValueError:
            continue
    return roles


def channel_label(file_url: str) -> str:
    if "TestDrive" in file_url:
        return "试驾录音"
    if "OutboundCall" in file_url:
        return "外呼录音"
    return "未知渠道"


def phone_of(file_url: str) -> str:
    parts = file_url.rstrip("/").split("/")
    return parts[-3] if len(parts) >= 3 else "未知"


def render_case(number: int, case: dict) -> list[str]:
    utterances = raw_utterances(case["cache_file"])
    roles = roles_by_index(case["labelled"])
    counts = case["role_counts"]
    lines = [
        f"## {number}. {phone_of(case['file_url'])}",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 渠道 | {channel_label(case['file_url'])} |",
        f"| 句数 | {case['utterances']} |",
        f"| 标注结果 | 销售方 {counts.get('销售方', 0)} / 客户方 {counts.get('客户方', 0)} / 其他人员 {counts.get('其他人员', 0)} / 存疑 {counts.get('存疑', 0)} |",
        f"| 录音文件 | `{case['file_url'].rsplit('/', 1)[-1]}` |",
        "",
        "原始转写（`[序号|角色] 原文`，角色来自本次标注实验）：",
        "",
        "```text",
    ]
    for index, item in enumerate(utterances):
        additions = item.get("additions") or {}
        text = str(item.get("text") or "").replace("\n", " ")
        role = roles.get(index, "未标注")
        speaker = additions.get("speaker") or "?"
        lines.append(f"[{index:>4}|{role}] (spk={speaker}) {text}")
    lines.extend(["```", ""])
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", type=Path, default=None)
    args = parser.parse_args()

    probe_dir = args.probe or latest_probe_dir(DEFAULT_PROBE_ROOT)
    cases = load_cases(probe_dir)

    doc = [
        "# 销售方为 0 的录音：原始转写",
        "",
        f"> 数据来源：`{probe_dir.relative_to(REPO_ROOT).as_posix()}`（角色标注探针结果）"
        "与 `experiments/transcript_cache`（ASR 原始转写）。",
        "> 本文件含客户原话，按该目录 `.gitignore` 的 `审核报告-*.md` 规则不进 Git。",
        "",
        "## 范围",
        "",
        f"共 **{len(cases)} 条**录音在本次角色标注实验中判出的「销售方」句数为 0。"
        "逐条如下，每条附原始转写全文。",
        "",
        "| # | 手机号 | 渠道 | 句数 | 角色分布 |",
        "|---|---|---|---|---|",
    ]
    for number, case in enumerate(cases, 1):
        counts = case["role_counts"]
        doc.append(
            f"| {number} | {phone_of(case['file_url'])} | {channel_label(case['file_url'])} "
            f"| {case['utterances']} | 其他人员 {counts.get('其他人员', 0)} / 客户方 {counts.get('客户方', 0)} |"
        )
    doc.append("")

    for number, case in enumerate(cases, 1):
        doc.extend(render_case(number, case))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(doc) + "\n", encoding="utf-8")
    print(f"已写入 {OUT_PATH}（{len(cases)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
