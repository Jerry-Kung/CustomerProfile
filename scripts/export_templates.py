"""把 DSL 里的提示词/模板正文导出为包内资源文件。

用途：V0.3 要求提示词外置为受 Git 管理的资源文件（规划 §6.5）。正文的**唯一运行期
来源**是 ``src/customer_profile/templates/``；``docs/specs/prompts/`` 保留为台账与
评审副本，两边由测试断言逐字符一致。

文件名规则：``<workflow_slug>__<node_id>.txt``。slug 由中文显示名转写，保证稳定且可读。

    python scripts/export_templates.py            # 写入包内资源目录
    python scripts/export_templates.py --check    # 只比对，不写入（供测试/复核用）

只读 ``dify_dsl_data/``，不修改任何原始文件。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DSL_DIR = REPO_ROOT / "dify_dsl_data"
TEMPLATE_DIR = REPO_ROOT / "src" / "customer_profile" / "templates"

# DSL 显示名 → 稳定 slug。与 workflows/ 下的模块名保持一致，便于互相定位。
SLUGS: dict[str, str] = {
    "Gemini（异常输出重试版）": "gemini_retry",
    "录音文件内容抽取（纯识别，无加工）": "audio_content_extract",
    "（新）SubAgent - 人工确认信息提取（生产环境）": "human_corrected_info",
    "（新）SubAgent - 人设特征判断": "profile_features_analysis",
    "（新）SubAgent - 外呼录音信息提取": "outbound_call_audio",
    "（新）SubAgent - 小红书个人页信息提取": "xiaohongshu_homepage",
    "（新）SubAgent - 微信主页信息提取": "wechat_homepage",
    "（新）SubAgent - 微信手机号搜索截图信息提取": "wechat_search_homepage",
    "（新）SubAgent - 抖音个人页信息提取": "douyin_homepage",
    "（新）SubAgent - 支付宝个人页信息提取": "alipay_homepage",
    "（新）SubAgent - 朋友圈信息提取": "moments_info",
    "（新）SubAgent - 极光数据（生产环境）": "jiguang_data",
    "（新）SubAgent - 猛士IT系统数据信息": "mengshi_it_system_data",
    "（新）SubAgent - 用户人工Feedback数据提取": "user_feedback_data",
    "（新）SubAgent - 画像内容生成&回写（生产环境）（新）": "customer_profile_production",
    "（新）SubAgent - 聊天记录数据信息": "chat_history_data",
    "（新）SubAgent - 证据线索汇总（生产环境）": "evidence_subagent",
    "（新）SubAgent - 试驾录音信息提取": "test_drive_audio",
    "（新）客户初始画像（生产环境）": "customer_profile_entry",
}


def slug_for(display_name: str) -> str:
    try:
        return SLUGS[display_name]
    except KeyError as exc:  # 新增 DSL 时必须显式登记，不能悄悄生成一个名字
        raise SystemExit(
            f"显示名 {display_name!r} 未在 SLUGS 中登记；新增工作流时请一并补上"
        ) from exc


def load_workflows() -> list[tuple[str, dict]]:
    if not DSL_DIR.is_dir():
        raise SystemExit(f"缺少 {DSL_DIR}（被 gitignore，需业务方提供）")
    out: list[tuple[str, dict]] = []
    for path in sorted(DSL_DIR.glob("*.yml")):
        with path.open("r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
        out.append((path.stem, document["workflow"]["graph"]))
    return out


def template_texts(graph: dict) -> list[tuple[str, str]]:
    """取出一个图里全部提示词/模板正文，返回 ``[(node_id, text), ...]``。

    ``template-transform`` 取 ``template``；``llm`` 取 ``prompt_template`` 各段的
    ``text``，用换行拼接——Dify 把多段消息分开发送，而迁移后统一由 ``llm_call()``
    承载，system 段通过节点的 ``system_field`` 单独传，正文资源里保留完整文本以便
    逐字符比对。
    """
    found: list[tuple[str, str]] = []
    for node in sorted(graph["nodes"], key=lambda n: str(n["id"])):
        data = node["data"]
        node_id = str(node["id"])
        node_type = data.get("type")
        if node_type == "template-transform":
            template = data.get("template")
            if template is not None:
                found.append((node_id, template))
        elif node_type == "llm":
            parts = data.get("prompt_template") or []
            if parts:
                found.append(
                    (node_id, "\n".join(str(p.get("text", "")) for p in parts))
                )
    return found


def body_of(node_id: str, texts: list[tuple[str, str]]) -> str | None:
    for candidate, text in texts:
        if candidate == node_id:
            return text
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只比对，不写入")
    args = parser.parse_args()

    expected: dict[str, str] = {}
    manifest: list[dict[str, object]] = []

    for display_name, graph in load_workflows():
        slug = slug_for(display_name)
        texts = template_texts(graph)
        for node_id, text in texts:
            name = f"{slug}__{node_id}.txt"
            expected[name] = text
            manifest.append(
                {
                    "file": name,
                    "workflow": display_name,
                    "node_id": node_id,
                    "chars": len(text),
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )

    print(f"DSL 中提示词/模板节点：{len(expected)} 个")

    if args.check:
        missing: list[str] = []
        differing: list[str] = []
        for name, text in expected.items():
            path = TEMPLATE_DIR / name
            if not path.is_file():
                missing.append(name)
                continue
            with path.open("r", encoding="utf-8", newline="") as handle:
                if handle.read() != text:
                    differing.append(name)
        extra = sorted(
            p.name for p in TEMPLATE_DIR.glob("*.txt")
            if p.name not in expected
        )
        if missing:
            print(f"缺少 {len(missing)} 个资源文件：{missing[:5]}")
        if differing:
            print(f"{len(differing)} 个资源文件与 DSL 不一致：{differing[:5]}")
        if extra:
            print(f"资源目录中有 {len(extra)} 个 DSL 里不存在的文件：{extra[:5]}")
        if missing or differing or extra:
            return 1
        print("比对通过：包内资源与 DSL 正文逐字符一致")
        return 0

    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in expected.items():
        # newline="" 保留原始换行，不做 CRLF 归一（正文逐字符等价是提示词效果的前提）
        with (TEMPLATE_DIR / name).open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    stale = [
        p for p in TEMPLATE_DIR.glob("*.txt")
        if p.name not in expected
    ]
    for path in stale:
        path.unlink()

    print(f"已写入 {len(expected)} 个资源文件到 {TEMPLATE_DIR}")
    if stale:
        print(f"清理了 {len(stale)} 个已失效文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
