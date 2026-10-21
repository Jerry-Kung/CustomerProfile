"""角色标注提示词的离线验证：直接用真实转写跑一遍 LLM，产出可人工核读的结果。

**为什么单独一个脚本。** 提示词质量是本轮唯一的不确定项，而接入管线后每次验证都要
经过「取数 → 迭代 → 子流程」才能看到效果。这里绕过工作流，只做三件事：

1. 从 ``experiments/transcript_cache`` 挑若干条真实转写（短 / 中 / 长各取，覆盖面优于随机）；
2. 用与节点**完全相同的模板与分块规则**调用 :class:`LlmClient`（读 ``.env``）；
3. 把模型输出按段展开成逐句文本，落到 ``experiments/role_labelling_probe/<时间戳>/``。

**它测的是提示词，不是工作流。** 分块、先验传递、覆盖校验这些由节点代码承担的逻辑，
在这里用同一套规则先跑一遍——若在这里就出现覆盖缺口或切段明显错误，接线后再改提示词
的代价会高得多。

用法：

    python scripts/probe_role_labelling.py            # 缺省 12 条
    python scripts/probe_role_labelling.py --limit 20 --chunk 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from customer_profile.execution.llm import LlmClient  # noqa: E402
from customer_profile.settings import Settings  # noqa: E402
from customer_profile.templates import TemplateRepository  # noqa: E402

ROLES = ("销售方", "客户方", "未知来源")

TEMPLATE_NAME = "audio_content_extract_roled__roled_llm"

LEGACY_ROLES = {"其他人员": "未知来源", "存疑": "未知来源"}

CHANNEL_ALLOWED = {
    "outbound": ROLES,
    "test_drive": ROLES,
}

CACHE_ROOT = REPO_ROOT / "experiments" / "transcript_cache"
OUT_ROOT = REPO_ROOT / "experiments" / "role_labelling_probe"


@dataclass(frozen=True)
class Probe:
    path: Path
    file_url: str
    utterances: list[dict[str, Any]]

    @property
    def channel(self) -> str:
        """``outbound`` / ``test_drive``。渠道决定角色词表。"""
        return "test_drive" if "TestDrive" in self.file_url else "outbound"


CHANNEL_RULES = {
    "outbound": (
        "## 渠道规则：外呼录音\n\n"
        "这是一通**主叫电话**，参与者**只有销售方和客户方两人**（客户的家人代为接听时也算客户方）。\n\n"
        "因此本通录音**只允许使用 `销售方` 与 `客户方` 两个取值**：\n"
        "- 不得使用 `其他人员`——电话里不存在旁人，判出旁人说明你把某一方听错了；\n"
        "- 不得使用 `存疑`——整通电话只有两个人，听不出是谁就回看上下文与问答结构；确实判不出的极个别句子，"
        "按它所在对话轮次的一方归属。"
    ),
    "test_drive": (
        "## 渠道规则：试驾录音\n\n"
        "这是门店/车内的连续录音，可能录到**多段对话与大量无关声音**。\n\n"
        "可以使用全部四个取值。与购车无关的内容（店员闲聊、交接班、办私事、对外打的其他电话、"
        "车上同行人员的闲聊、背景插话）一律判 `其他人员`，不要堆成 `存疑`。"
    ),
}


def load_probes(limit: int, only: Sequence[str] = ()) -> list[Probe]:
    """按「短 / 中 / 长」分层挑样本，避免全是短录音或全是长录音。

    ``only`` 给出一组缓存文件名片段时，只跑命中这些片段的条目（定向复核用，不分层）。
    """
    entries: list[Probe] = []
    for path in sorted(CACHE_ROOT.glob("*/*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("silent") or not payload.get("raw_json"):
            continue
        try:
            raw = json.loads(payload["raw_json"])
            items = raw["result"]["utterances"]
        except (KeyError, ValueError, TypeError):
            continue
        utterances = [
            {
                "index": i,
                "speaker": str((item.get("additions") or {}).get("speaker") or ""),
                "gender": str((item.get("additions") or {}).get("gender") or ""),
                "text": str(item.get("text") or ""),
            }
            for i, item in enumerate(items)
        ]
        if not utterances:
            continue
        entries.append(Probe(path=path, file_url=payload.get("file_url") or "", utterances=utterances))

    entries.sort(key=lambda p: len(p.utterances))
    if only:
        return [p for p in entries if any(k in p.path.name for k in only)]
    if not entries:
        return []
    # 分三层：最短 1/3 取若干、中间 1/3 取若干、最长 1/3 取若干
    third = max(1, len(entries) // 3)
    buckets = [entries[:third], entries[third : 2 * third], entries[2 * third :]]
    picked: list[Probe] = []
    per_bucket = max(1, limit // 3)
    for bucket in buckets:
        step = max(1, len(bucket) // per_bucket)
        picked.extend(bucket[::step][:per_bucket])
    return picked[:limit]


def chunk_probe(probe: Probe, size: int) -> list[list[dict[str, Any]]]:
    return [probe.utterances[i : i + size] for i in range(0, len(probe.utterances), size)]


def prior_text(prior_role: str | None, total: int, first_index: int) -> str:
    if prior_role is None:
        return "这是通话的开头。"
    return (
        f"这是通话的中间一段，从第 {first_index} 句开始。"
        f"紧邻的上一段最后一句的角色是「{prior_role}」，可作倾向参考，"
        "但若本段内容明确指向另一方，以内容为准。"
    )


def validate(payload: Any, count: int, offset: int, allowed: tuple[str, ...]) -> tuple[list[dict], list[str]]:
    """按节点代码同一口径校验分段：无缝覆盖、不重叠、角色合法。

    ``allowed`` 是该渠道允许的角色集合。三类统一放开：外呼也会混入车载语音等，
    一刀切禁止只会逼模型硬猜。
    """
    issues: list[str] = []
    segments = (payload or {}).get("segments") or []
    if not isinstance(segments, list) or not segments:
        return [], ["segments 缺失或为空"]
    covered: dict[int, str] = {}
    for seg in segments:
        try:
            lo, hi = int(seg["from"]), int(seg["to"])
            role = str(seg["role"])
        except (KeyError, TypeError, ValueError):
            issues.append(f"非法分段：{seg!r}")
            continue
        role = LEGACY_ROLES.get(role, role)
        if role not in ROLES:
            issues.append(f"非法角色 {role!r}，按未知来源处理")
            role = "未知来源"
        elif role not in allowed:
            issues.append(f"渠道不允许角色 {role!r}，按未知来源处理")
            role = "未知来源"
        for i in range(max(lo, offset), min(hi, offset + count - 1) + 1):
            if i in covered:
                issues.append(f"第 {i} 句被重复覆盖，保留先出现的一段")
                continue
            covered[i] = role
    for i in range(offset, offset + count):
        if i not in covered:
            issues.append(f"第 {i} 句未被覆盖，补未知来源")
            covered[i] = "未知来源"
    return [{"index": i, "role": covered[i]} for i in sorted(covered)], issues


def render(probe: Probe, roles: dict[int, str], corrections: list[dict]) -> str:
    fixed = {int(c["i"]): c for c in corrections if isinstance(c, dict) and "i" in c}
    lines: list[str] = []
    for utt in probe.utterances:
        i = utt["index"]
        text = str(fixed[i].get("text") or utt["text"]) if i in fixed else utt["text"]
        note = f"   ← 原识别：{utt['text']}（{fixed[i].get('reason', '')}）" if i in fixed else ""
        lines.append(f"[{i}|{roles.get(i, '未知来源')}] {text}{note}")
    return "\n".join(lines)


async def run_one(client: LlmClient, repo: TemplateRepository, probe: Probe, chunk: int) -> dict:
    started = time.monotonic()
    roles: dict[int, str] = {}
    corrections: list[dict] = []
    issues: list[str] = []
    prior_role: str | None = None
    chunks = chunk_probe(probe, chunk)
    allowed = CHANNEL_ALLOWED[probe.channel]
    for number, part in enumerate(chunks):
        offset = part[0]["index"]
        prompt = repo.render(
            TEMPLATE_NAME,
            {
                "total": len(part),
                "prior": prior_text(prior_role, len(part), offset),
                "channel_rule": CHANNEL_RULES[probe.channel],
                "auc_result": json.dumps(part, ensure_ascii=False),
            },
        )
        payload = await client.llm_call(prompt, system="你是一个有用的AI助手", expect_json=True)
        part_roles, part_issues = validate(payload, len(part), offset, allowed)
        issues.extend(f"块{number}: {msg}" for msg in part_issues)
        for item in part_roles:
            roles[item["index"]] = item["role"]
        for corr in (payload or {}).get("corrections") or []:
            if isinstance(corr, dict) and "i" in corr:
                corrections.append(corr)
        prior_role = part_roles[-1]["role"] if part_roles else prior_role
    elapsed = time.monotonic() - started
    counts = {role: sum(1 for v in roles.values() if v == role) for role in ROLES}
    return {
        "file_url": probe.file_url,
        "cache_file": str(probe.path.relative_to(REPO_ROOT)),
        "utterances": len(probe.utterances),
        "chars": sum(len(u["text"]) for u in probe.utterances),
        "chunks": len(chunks),
        "seconds": round(elapsed, 1),
        "role_counts": counts,
        "corrections": len(corrections),
        "issues": issues,
        "labelled": render(probe, roles, corrections),
    }


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    client = LlmClient(settings)
    repo = TemplateRepository()
    probes = load_probes(args.limit, tuple(args.only))
    if not probes:
        print("转写缓存里没有可用样本")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_ROOT / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        for n, probe in enumerate(probes, 1):
            print(f"[{n}/{len(probes)}] {probe.path.name} 句数={len(probe.utterances)}", flush=True)
            try:
                result = await run_one(client, repo, probe, args.chunk)
            except Exception as exc:  # 单条失败不中断整批
                print(f"    失败：{type(exc).__name__}: {exc}", flush=True)
                continue
            print(
                f"    角色={result['role_counts']} 修正={result['corrections']} "
                f"问题={len(result['issues'])} 耗时={result['seconds']}s",
                flush=True,
            )
            (out_dir / f"{n:02d}-{probe.path.stem[:12]}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    finally:
        await client.aclose()

    print(f"\n结果目录：{out_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--chunk", type=int, default=250)
    parser.add_argument(
        "--only",
        nargs="*",
        default=(),
        help="只跑文件名含这些片段的缓存条目（定向复核）",
    )
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
