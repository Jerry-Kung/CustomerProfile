"""从 DSL 生成工作流模块的骨架，供人工复核后再定稿。

生成不是「自动迁移」——它只做机械转写：把 DSL 的节点声明翻成 ``make_node(...)`` 的
关键字参数。判断性的部分（``expect_json`` 的逐点声明、合并短链、删除废弃入参）由人工
在生成稿上完成，并由测试（台账交叉核对、code 正文逐字符比对）兜底。

用法：
    python scripts/generate_workflows.py <workflow_slug>   # 打印一个工作流的生成稿
    python scripts/generate_workflows.py --list            # 列出 slug 与显示名
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DSL_DIR = REPO_ROOT / "dify_dsl_data"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from export_templates import SLUGS, slug_for  # noqa: E402

BY_SLUG = {slug: name for name, slug in SLUGS.items()}

# 这些密钥是 DSL 里的明文值，迁移后一律从环境变量注入，定义里不得出现。
REDACT_MARKERS = ("X-API-Key:", "sk-")


def load_graph(display_name: str) -> dict:
    path = DSL_DIR / f"{display_name}.yml"
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)["workflow"]["graph"]


def selector_to_py(selector: object) -> str | None:
    """``['1776', 'text']`` → ``'1776.text'``。"""
    if not isinstance(selector, (list, tuple)) or len(selector) != 2:
        return None
    return f"{selector[0]}.{selector[1]}"


def parse_reference(value: str | None) -> str | None:
    """``'{{#1776.text#}}'`` → ``'1776.text'``；字面量返回 ``None``。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("{{#") and text.endswith("#}}"):
        inner = text[3:-3]
        return inner.replace("#", ".")
    return None


def node_predecessors(graph: dict, node_id: str) -> list[str]:
    return sorted(
        str(e["source"]) for e in graph["edges"] if str(e["target"]) == node_id
    )


def code_body(data: dict) -> str:
    return data.get("code", "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", nargs="?")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = parser.parse_args()

    if args.list or not args.slug:
        for name, slug in SLUGS.items():
            print(f"{slug:32} {name}")
        return 0

    if args.slug not in BY_SLUG:
        raise SystemExit(f"未知 slug {args.slug!r}；用 --list 查看")

    graph = load_graph(BY_SLUG[args.slug])
    nodes = {str(n["id"]): n["data"] for n in graph["nodes"]}

    report: list[dict[str, object]] = []
    for node_id, data in nodes.items():
        entry: dict[str, object] = {
            "node_id": node_id,
            "type": data.get("type"),
            "title": data.get("title"),
            "preds": node_predecessors(graph, node_id),
        }
        node_type = data.get("type")

        if node_type == "code":
            entry["code"] = code_body(data)
        elif node_type == "template-transform":
            entry["template"] = data.get("template")
        elif node_type == "llm":
            entry["vision"] = data.get("vision")
            entry["prompt_parts"] = [
                {"role": p.get("role"), "text": p.get("text")}
                for p in (data.get("prompt_template") or [])
            ]
        elif node_type == "http-request":
            entry["method"] = data.get("method")
            entry["url"] = data.get("url")
            entry["body"] = data.get("body")
            entry["headers"] = data.get("headers")
        elif node_type == "tool":
            entry["tool_name"] = data.get("tool_name")
            entry["params"] = {
                k: (v.get("value") if isinstance(v, dict) else v)
                for k, v in (data.get("tool_parameters") or {}).items()
            }
        elif node_type == "if-else":
            entry["cases"] = data.get("cases")
        elif node_type == "variable-aggregator":
            entry["variables"] = data.get("variables")
            entry["groups"] = (data.get("advanced_settings") or {}).get("groups")
        elif node_type == "iteration":
            entry["iterator"] = data.get("iterator_selector")
            entry["output"] = data.get("output_selector")
            entry["error_handle_mode"] = data.get("error_handle_mode")
            entry["flatten"] = data.get("flatten_output")
            entry["is_parallel"] = data.get("is_parallel")
            entry["parallel_nums"] = data.get("parallel_nums")
        elif node_type == "start":
            entry["variables"] = [
                {"variable": v.get("variable"), "required": v.get("required")}
                for v in (data.get("variables") or [])
            ]
        elif node_type == "end":
            entry["outputs"] = [
                {
                    "variable": o.get("variable"),
                    "selector": selector_to_py(o.get("value_selector")),
                }
                for o in (data.get("outputs") or [])
            ]

        # 变量绑定：variables[] 与 tool_parameters 两种承载方式
        bindings: dict[str, str] = {}
        for item in data.get("variables") or []:
            # DSL 里 variables[] 有两种形态：带 variable/value_selector 的声明，
            # 以及直接写成 [node_id, field] 的裸选择器（iteration 的 item 之类）。
            if isinstance(item, dict):
                name = item.get("variable")
                selector = selector_to_py(item.get("value_selector"))
            else:
                selector = selector_to_py(item)
                name = None
            if name and selector:
                bindings[name] = selector
        entry["bindings"] = bindings
        entry["in_iteration"] = bool(data.get("isInIteration"))
        entry["iteration_id"] = data.get("iteration_id")
        report.append(entry)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"# {BY_SLUG[args.slug]}（{len(report)} 节点）")
        for entry in report:
            print(f"{entry['node_id']:<18} {str(entry['type']):<20} {entry['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
