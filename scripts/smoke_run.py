"""真实模型冒烟：按真实配置跑指定工作流，产出可复核的报告行。

与 ``record_fixture.py`` 的分工：那个脚本的产物是**回放 fixture**（供日后不出网复跑），
本脚本的产物是**冒烟证据**（这次真实调用到底成不成功、快不快、JSON 合不合法）。

三条硬约束：

1. **不出生产回写。** 无论 ``.env`` 怎么配，这里强制 ``WRITEBACK_ENABLED=false``——
   冒烟验证的是端到端能跑通，不是在生产库上写一次。
2. **不伪造结论。** 端点不通、超时、JSON 不合法都如实记录；未覆盖的样本场景明确标注，
   不因为「其余都通过了」就把它写成已覆盖。
3. **慢节点耐心等。** 录音类节点单次真实调用可达 20–30 分钟（录音文件大），
   因此默认读超时放宽到 ``--read-timeout``（缺省 1800s）。

用法：

    python scripts/smoke_run.py --workflow customer_profile_entry
    python scripts/smoke_run.py --workflow wechat_homepage --phone 13400000087
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile.runner import build_service  # noqa: E402
from customer_profile.settings import Settings  # noqa: E402

PHONE_FILE = REPO_ROOT / "phone_numbers.txt"
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
MASK = "***"


def mask_phone(text: str) -> str:
    """真实手机号不进报告。"""
    return PHONE_PATTERN.sub(MASK, text or "")


def read_first_phone() -> str:
    if not PHONE_FILE.is_file():
        raise SystemExit(f"缺少 {PHONE_FILE}")
    for line in PHONE_FILE.read_text(encoding="utf-8").splitlines():
        candidate = line.strip()
        if candidate and not candidate.startswith("#"):
            return candidate
    raise SystemExit(f"{PHONE_FILE} 中没有可用号码")


def json_validity(outputs: dict[str, Any]) -> tuple[int, int]:
    """统计``expect_json`` 类产出的合法率：字符串里能解析成 JSON 的算合法。

    只看「看起来像 JSON」的字符串（以 ``{`` 或 ``[`` 开头），避免把纯文本误判为非法。
    """
    total = valid = 0
    for value in outputs.values():
        candidates = value if isinstance(value, list) else [value]
        for item in candidates:
            if not isinstance(item, str):
                continue
            stripped = item.strip()
            if not stripped.startswith(("{", "[")):
                continue
            total += 1
            try:
                json.loads(stripped)
            except ValueError:
                continue
            valid += 1
    return valid, total


async def smoke(
    workflow_id: str, inputs: dict[str, Any], read_timeout: float
) -> dict[str, Any]:
    # 冒烟不出生产回写：回写节点只产出「已跳过」的可解释结果
    settings = Settings(writeback_enabled=False, http_read_timeout=read_timeout)
    if settings.replay_mode != "off":
        raise SystemExit(
            f"REPLAY_MODE={settings.replay_mode!r}，冒烟要求 off（否则测的是回放）"
        )
    service = await build_service(settings)
    started = time.monotonic()
    record: dict[str, Any] = {
        "workflow": workflow_id,
        "model": settings.llm_model,
        "writeback_enabled": settings.writeback_enabled,
        "inputs": {k: mask_phone(str(v)) for k, v in inputs.items()},
    }
    try:
        outcome = await service.run(workflow_id, inputs)
    except Exception as exc:  # 服务级异常也要落报告，不能只留个栈
        record.update(
            status="exception",
            error=f"{type(exc).__name__}: {mask_phone(str(exc))}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return record
    finally:
        await service.aclose()
        if service.store is not None:
            service.store.close()

    valid, total = json_validity(outcome.outputs)
    record.update(
        status=outcome.status,
        error=mask_phone(outcome.error or ""),
        duration_ms=outcome.duration_ms,
        node_count=len(outcome.node_executions),
        failed_nodes=[
            {"node_id": e.node_id, "error": mask_phone(e.error or "")}
            for e in outcome.node_executions
            if not e.succeeded
        ][:10],
        sub_run_count=len(outcome.sub_run_ids),
        output_fields=sorted(outcome.outputs),
        output_types={k: type(v).__name__ for k, v in outcome.outputs.items()},
        json_valid=valid,
        json_total=total,
        replayed=bool(service.replay),
    )
    # 只记长度与前 200 字符，避免真实客户文本整段落进报告
    record["output_preview"] = {
        k: mask_phone(str(v))[:200] for k, v in outcome.outputs.items()
    }
    return record


def to_markdown(record: dict[str, Any]) -> str:
    lines = [f"### {record['workflow']}", ""]
    lines.append(f"- 状态：**{record['status']}**（{record.get('duration_ms', 0)} ms）")
    if record.get("error"):
        lines.append(f"- 错误：`{record['error']}`")
    lines.append(
        f"- 模型：`{record['model']}`；回写开关：`{record['writeback_enabled']}`；"
        f"回放：`{record.get('replayed')}`"
    )
    lines.append(f"- 节点执行：{record.get('node_count', 0)} 个；子运行：{record.get('sub_run_count', 0)} 次")
    if record.get("failed_nodes"):
        lines.append("- 失败节点：")
        for item in record["failed_nodes"]:
            lines.append(f"  - `{item['node_id']}`：{item['error']}")
    lines.append(
        f"- JSON 合法率：{record.get('json_valid', 0)}/{record.get('json_total', 0)}"
        "（仅统计以 `{`/`[` 开头的字符串产出）"
    )
    lines.append(f"- 产出字段：{record.get('output_types')}")
    lines.append("")
    for key, value in (record.get("output_preview") or {}).items():
        lines.append(f"  - `{key}`（前 200 字符）：{value}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实模型冒烟")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--phone", default=None)
    parser.add_argument("--input", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--read-timeout", type=float, default=1800.0)
    parser.add_argument("--out", default=None, help="追加写入的 markdown 报告")
    args = parser.parse_args(argv)

    inputs: dict[str, Any] = {}
    for pair in args.input:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"入参格式应为 key=value，实际为 {pair!r}")
        inputs[key.strip()] = value
    inputs.setdefault("phone_number", args.phone or read_first_phone())
    if args.workflow == "customer_profile_entry":
        inputs.setdefault("batch_id", "")

    record = asyncio.run(smoke(args.workflow, inputs, args.read_timeout))
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(to_markdown(record) + "\n")
    return 0 if record["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
