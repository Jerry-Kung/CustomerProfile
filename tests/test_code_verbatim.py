"""逐字符比对：迁移后的 code 函数体与 DSL 原文一致。

这是 V0.2 的核心等价证据之一。规划 §6.2 要求「原节点代码逻辑直接提取，不在迁移中
顺手重构」，本测试就是这条要求的机械检查。

比对方式：从 DSL 取出 ``data.code``（YAML 解析后的字符串），抽取迁移后函数的源码，
把 ``def`` 行换成 ``def main(...)`` 后逐字符比较函数体。``ast`` 用来定位函数边界，
避免手写解析出错。
"""

from __future__ import annotations

import ast
import io
import textwrap
from pathlib import Path

import pytest

from customer_profile.workflows import human_corrected_info, mengshi_it_system_data

from .conftest import DSL_DIR, requires_dsl

pytestmark = requires_dsl


def _load_dsl_code(dsl_name: str, node_id: str) -> str:
    """从 DSL 取某节点的代码正文。DSL 缺失时由 ``requires_dsl`` 跳过。"""
    import yaml

    # SOURCE_DSL 形如 ``xxx.yml``，已含扩展名
    path = DSL_DIR / (dsl_name if dsl_name.endswith(".yml") else f"{dsl_name}.yml")
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    for node in document["workflow"]["graph"]["nodes"]:
        if str(node["id"]) == node_id:
            return node["data"]["code"]
    raise AssertionError(f"节点 {node_id} 不在 {dsl_name} 中")


def _function_body(source: str, function_name: str) -> str:
    """取函数的**函数体正文**（不含 def 行与缩进），按 ``dedent`` 归一。

    只比函数体，不比函数名——迁移允许把 ``main`` 改成业务化的名字，函数体不得改。
    """
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            body = list(node.body)
            # 跳过 docstring：迁移允许给函数补中文说明，可执行语句不得有任何改动
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                body = body[1:]
            if not body:
                return ""
            lines = source.splitlines()[body[0].lineno - 1 : node.end_lineno]
            return textwrap.dedent("\n".join(lines)).strip()
    raise AssertionError(f"源码中没有函数 {function_name}")


def _import_function(module: object, name: str) -> str:
    import inspect

    return inspect.getsource(getattr(module, name))


def _literals(source: str) -> dict[str, object]:
    """取模块级常量的字面量值，用于比对枚举表。"""
    tree = ast.parse(source)
    values: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return values


# ---------------------------------------------------------------- 猛士


def test_extract_channel_payload_body_is_verbatim():
    dsl_code = _load_dsl_code(mengshi_it_system_data.SOURCE_DSL, "1776328382353")
    migrated = _import_function(mengshi_it_system_data, "extract_channel_payload")
    assert _function_body(migrated, "extract_channel_payload") == _function_body(
        dsl_code, "main"
    )


def test_integrate_customer_data_body_is_verbatim():
    dsl_code = _load_dsl_code(mengshi_it_system_data.SOURCE_DSL, "1778230548931")
    migrated = _import_function(mengshi_it_system_data, "integrate_customer_data")
    assert _function_body(migrated, "integrate_customer_data") == _function_body(
        dsl_code, "main"
    )


def test_mengshi_module_literals_match_dsl():
    """字段表与三个枚举映射必须与 DSL 一模一样——它们是业务规则，不是实现细节。"""
    tree = ast.parse(_load_dsl_code(mengshi_it_system_data.SOURCE_DSL, "1778230548931"))
    dsl_literals = _literals(
        _load_dsl_code(mengshi_it_system_data.SOURCE_DSL, "1778230548931")
    )
    assert dsl_literals["ORIGINAL_FIELDS"] == mengshi_it_system_data.ORIGINAL_FIELDS
    assert (
        dsl_literals["CUSTOMER_CHANNEL_MAP"]
        == mengshi_it_system_data.CUSTOMER_CHANNEL_MAP
    )
    assert dsl_literals["CUSTOMER_STAGE_MAP"] == mengshi_it_system_data.CUSTOMER_STAGE_MAP
    assert (
        dsl_literals["CUSTOMER_LEADS_STATUS_MAP"]
        == mengshi_it_system_data.CUSTOMER_LEADS_STATUS_MAP
    )
    assert tree is not None


def test_mengshi_end_node_output_names_match_dsl():
    import yaml

    path = DSL_DIR / mengshi_it_system_data.SOURCE_DSL
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for node in document["workflow"]["graph"]["nodes"]:
        if str(node["id"]) == "1773748343861":
            declared = [o["variable"] for o in node["data"]["outputs"]]
            break
    else:
        raise AssertionError("DSL 中没有 end 节点 1773748343861")
    assert set(declared) == set(mengshi_it_system_data.WORKFLOW.outputs)
    assert set(declared) == set(mengshi_it_system_data.WORKFLOW.node_map["1773748343861"].outputs)


# ---------------------------------------------------------------- 人工确认


def test_extract_locked_notes_body_is_verbatim():
    dsl_code = _load_dsl_code(human_corrected_info.SOURCE_DSL, "1776650937435")
    migrated = _import_function(human_corrected_info, "extract_locked_notes")
    assert _function_body(migrated, "extract_locked_notes") == _function_body(
        dsl_code, "main"
    )


def test_helper_functions_are_verbatim():
    dsl_code = _load_dsl_code(human_corrected_info.SOURCE_DSL, "1776650937435")
    for name in ("_strip_code_fence", "_parse_json_string", "_is_locked"):
        migrated = _import_function(human_corrected_info, name)
        assert _function_body(migrated, name) == _function_body(dsl_code, name), name


def test_human_corrected_http_node_keeps_method_and_path():
    """HTTP 节点的服务、方法与路径必须与 DSL 一致；只有凭据来源改为环境变量。"""
    import yaml

    path = DSL_DIR / human_corrected_info.SOURCE_DSL
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for node in document["workflow"]["graph"]["nodes"]:
        if str(node["id"]) == "1776649722195":
            dsl_node = node["data"]
            break
    else:
        raise AssertionError("DSL 中没有 HTTP 节点 1776649722195")

    migrated = human_corrected_info.WORKFLOW.node_map["1776649722195"]
    assert dsl_node["method"].upper() == migrated.config["@method"].upper()
    assert dsl_node["url"].startswith("https://mhero.dfmc.com.cn")
    assert migrated.config["@path"] == "/api/v1/remote/data/profile/"
    assert migrated.config["@service"] == "mhero"


def test_dsl_secret_is_not_in_definition():
    """DSL 的明文密钥不得出现在迁移后的任何定义里（安全要求）。"""
    import yaml

    path = DSL_DIR / human_corrected_info.SOURCE_DSL
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    secrets: list[str] = []
    for node in document["workflow"]["graph"]["nodes"]:
        headers = node["data"].get("headers")
        if isinstance(headers, str):
            for line in headers.splitlines():
                if line.lower().startswith("x-api-key"):
                    secrets.append(line.split(":", 1)[1].strip())
    assert secrets, "预期 DSL 中存在明文 X-API-Key，本次比对失去意义"

    serialised = str(human_corrected_info.WORKFLOW.asdict())
    for secret in secrets:
        assert secret not in serialised, "迁移后的定义中出现了 DSL 的明文密钥"


# ====================================================================
# V0.3：全量比对（19 个工作流 / 49 个 code 节点）
#
# 上面两个工作流是 V0.2 手写的逐节点断言，保留作为「写法样板」；下面这组是通用比对，
# 从定义里读 ``config['function']`` 自动定位迁移后的函数，因此新增工作流时无需再改测试。
# ====================================================================


def _load_workflows() -> dict:
    from customer_profile.workflows import load_all

    return load_all()


def _dsl_code_nodes(dsl_name: str) -> dict[str, str]:
    """取一个 DSL 里全部 ``code`` 节点的正文，``{node_id: code}``。"""
    import yaml

    path = DSL_DIR / (dsl_name if dsl_name.endswith(".yml") else f"{dsl_name}.yml")
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    return {
        str(node["id"]): node["data"]["code"]
        for node in document["workflow"]["graph"]["nodes"]
        if node["data"].get("type") == "code"
    }


def _module_of(function_path: str):
    import importlib

    module_name, _, _ = function_path.partition(":")
    return importlib.import_module(module_name)


def _all_defs(source: str) -> list[str]:
    tree = ast.parse(source)
    return [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]


def _cases():
    """收集全部 (工作流, 节点, DSL 正文, 迁移后函数路径)。"""
    out = []
    for workflow in _load_workflows().values():
        if not workflow.source_dsl:
            continue
        if not (DSL_DIR / workflow.source_dsl).is_file():
            continue
        code_nodes = _dsl_code_nodes(workflow.source_dsl)
        for node in workflow.nodes:
            if node.node_type != "code":
                continue
            function_path = node.config.get("function")
            if not function_path:
                continue
            out.append((workflow.workflow_id, node.node_id, code_nodes.get(node.node_id), function_path))
    return out


CASES = _cases()


MIGRATED_CODE_NODES = 47
"""迁移后的全部 ``code`` 节点数。

台账统计的 49 个包含 ``Gemini（异常输出重试版）`` 子流程里的 2 个
（``1777365069262`` / ``17773660491670``）。该子流程按有意差异 W2 整体归一为
``llm_call()`` 的重试逻辑，不再作为独立工作流存在，因此它的 code 节点也不进入
本比对——**这是有意的，不是漏迁**。47 + 2 = 49 与台账对得上。
"""


def test_every_code_node_is_covered():
    """19 个工作流里 DSL 有的 code 节点，定义里都要有，且都登记了 function。"""
    assert len(CASES) == MIGRATED_CODE_NODES, (
        f"预期 {MIGRATED_CODE_NODES} 个 code 节点，实际 {len(CASES)}"
    )
    assert all(code for _, _, code, _ in CASES), "有节点取不到 DSL 正文"


def test_migrated_plus_normalised_equals_ledger_total():
    """47（迁移）+ 2（Gemini 重试子流程，按 W2 归一）= 台账的 49。"""
    from customer_profile.workflows import load_all

    normalised_away = {"Gemini（异常输出重试版）"}
    migrated_names = {wf.display_name for wf in load_all().values() if wf.source_dsl}
    assert not (migrated_names & normalised_away), (
        "Gemini 重试子流程不应作为独立工作流出现（W2）"
    )
    assert len(CASES) + 2 == 49


def _release(module) -> dict[str, dict]:
    """取模块声明的「节点 → 函数名」映射；没有声明的模块返回空表。"""
    return getattr(module, "CODE_SPECS", {}) or {}


def _renamed_body(dsl_code: str, renames: dict[str, str]) -> str:
    """把 DSL 原文里被改名的函数按 token 位置换回去，再取函数体。

    只动 NAME token，字符串与注释保持不变——否则正文比对会因为改写 docstring 而失真。
    """
    if not renames:
        return dsl_code
    import tokenize

    lines = dsl_code.splitlines(keepends=True)
    offs, pos = [], 0
    for line in lines:
        offs.append(pos)
        pos += len(line)

    def abspos(rc):
        return offs[rc[0] - 1] + rc[1]

    edits = []
    for tok in tokenize.generate_tokens(io.StringIO(dsl_code).readline):
        if tok.type == tokenize.NAME and tok.string in renames:
            edits.append((abspos(tok.start), abspos(tok.end), renames[tok.string]))
    for start, end, replacement in reversed(edits):
        dsl_code = dsl_code[:start] + replacement + dsl_code[end:]
    return dsl_code


@pytest.mark.parametrize(
    "workflow_id,node_id,dsl_code,function_path",
    CASES,
    ids=[f"{w}:{n}" for w, n, _, _ in CASES],
)
def test_code_node_body_is_verbatim(workflow_id, node_id, dsl_code, function_path):
    """``main`` 的函数体必须与 DSL 逐字符一致（除函数名与 docstring）。"""
    _, _, function_name = function_path.partition(":")
    module = _module_of(function_path)
    spec = _release(module).get(node_id) or {}
    if spec:
        assert spec.get("main") == function_name, (
            f"{workflow_id} 节点 {node_id} 的 CODE_SPECS 声明与 function 不一致："
            f"{spec.get('main')!r} vs {function_name!r}"
        )
    dsl_code = _renamed_body(dsl_code, spec.get("renames") or {})
    migrated = _import_function(module, function_name)
    assert _function_body(migrated, function_name) == _function_body(dsl_code, "main"), (
        f"{workflow_id} 节点 {node_id} 的 {function_name} 函数体与 DSL 不一致"
    )


@pytest.mark.parametrize(
    "workflow_id,node_id,dsl_code,function_path",
    CASES,
    ids=[f"{w}:{n}" for w, n, _, _ in CASES],
)
def test_code_node_helpers_are_verbatim(workflow_id, node_id, dsl_code, function_path):
    """同一节点里的辅助函数也必须逐字符一致。

    跨节点重名的辅助函数在迁移时只**改函数名**以避免模块内互相覆盖（函数体一字未动）。
    名字取自模块声明的 ``CODE_SPECS``，不靠猜：早先按 ``hasattr`` 试探的写法会挑中
    另一个节点的同名函数，把「名字不同」误报成「正文不同」。
    """
    module = _module_of(function_path)
    spec = _release(module).get(node_id) or {}
    renames = spec.get("renames") or {}
    if not spec:
        # 未声明 CODE_SPECS 的模块（辅助函数不与别处重名，无需改名）
        renames = {}

    helper_names = {v: k for k, v in renames.items()}
    renamed_code = _renamed_body(dsl_code, renames)

    for name in _all_defs(dsl_code):
        if name == "main":
            continue
        target = renames.get(name, name)
        assert hasattr(module, target), (
            f"{workflow_id} 节点 {node_id} 的辅助函数 {name}（迁移后叫 {target}）在模块里找不到"
        )
        assert helper_names.get(target, name) == name
        migrated = _import_function(module, target)
        assert _function_body(migrated, target) == _function_body(renamed_code, target), (
            f"{workflow_id} 节点 {node_id} 的辅助函数 {name}（迁移后叫 {target}）与 DSL 不一致"
        )
