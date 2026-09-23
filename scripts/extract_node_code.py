"""从本地 DSL 逐字符提取指定节点的代码正文，用于迁移比对。

只读 `dify_dsl_data/`，不修改任何原始文件。输出到标准输出（UTF-8），供人工比对或
写入工作流模块的字符串常量。

用法：
    python scripts/extract_node_code.py <dsl文件名> <节点ID> [<节点ID> ...]
    python scripts/extract_node_code.py --list-code-nodes <dsl文件名>
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DSL_DIR = REPO_ROOT / "dify_dsl_data"


def load_graph(dsl_name: str) -> dict:
    """加载一份 DSL 的 ``workflow.graph``。文件名可不带 ``.yml``。"""
    path = DSL_DIR / dsl_name
    if path.suffix != ".yml":
        path = path.with_suffix(".yml")
    if not path.is_file():
        raise SystemExit(f"DSL 文件不存在：{path}")
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    return document["workflow"]["graph"]


def node_by_id(graph: dict, node_id: str) -> dict:
    for node in graph["nodes"]:
        if str(node["id"]) == str(node_id):
            return node
    raise SystemExit(f"节点 {node_id} 不在图中")


def code_verbatim(graph: dict, node_id: str) -> str:
    """取 code 节点的代码正文。YAML 解析后即为原字符串，不做任何加工。"""
    node = node_by_id(graph, node_id)
    code = node["data"].get("code")
    if code is None:
        raise SystemExit(f"节点 {node_id} 不是 code 节点（type={node['data']['type']}）")
    return code


def list_code_nodes(graph: dict) -> list[tuple[str, str, str, int]]:
    rows = []
    for node in graph["nodes"]:
        data = node["data"]
        if data.get("type") == "code":
            code = data.get("code", "")
            rows.append((str(node["id"]), data.get("title", ""), node_id_hash(code), len(code)))
    return rows


def node_id_hash(code: str) -> str:
    import hashlib

    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2

    if argv[1] == "--list-code-nodes":
        graph = load_graph(argv[2])
        for node_id, title, digest, chars in list_code_nodes(graph):
            print(f"{node_id}\t{digest}\t{chars}\t{title}")
        return 0

    graph = load_graph(argv[1])
    for index, node_id in enumerate(argv[2:]):
        if index:
            print("\n" + "=" * 70 + "\n")
        code = code_verbatim(graph, node_id)
        sys.stdout.write(code)
        if not code.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
