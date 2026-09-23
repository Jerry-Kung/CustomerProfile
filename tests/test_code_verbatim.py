"""逐字符比对：迁移后的 code 函数体与 DSL 原文一致。

这是 V0.2 的核心等价证据之一。规划 §6.2 要求「原节点代码逻辑直接提取，不在迁移中
顺手重构」，本测试就是这条要求的机械检查。

比对方式：从 DSL 取出 ``data.code``（YAML 解析后的字符串），抽取迁移后函数的源码，
把 ``def`` 行换成 ``def main(...)`` 后逐字符比较函数体。``ast`` 用来定位函数边界，
避免手写解析出错。
"""

from __future__ import annotations

import ast
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
