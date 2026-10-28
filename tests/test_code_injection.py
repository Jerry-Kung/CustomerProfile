"""code 节点注入键的守卫：注入字典的键必须与节点 ``config['function']`` 逐字一致。

这条守卫来自一次真实缺陷（2026-10-28）：``_checker_functions`` 用**节点 ID**做键
（``...:pipeline_l2_enum_map``），而节点定义里写的是**函数名**（``...:enum_map``）。
查找落空后 ``get_code_function`` 静静回退到 ``module:callable`` 的模块属性，``llm``
于是为 ``None``——六案里映射节点全部报「未注入 LLM 客户端」，而单测全绿，因为单测
直接调函数、不经过注入。

这里只钉一件事：``_checker_functions`` 产出的每个键，都能在某个 code 节点的
``config['function']`` 里找到对应。多出来或少了都打红。
"""

from __future__ import annotations

from customer_profile import runner
from customer_profile.workflows import customer_profile_pipeline as pipe


def _injected_keys() -> set[str]:
    return set(runner._checker_functions(llm=object(), templates=None))


def _code_node_refs() -> set[str]:
    refs: set[str] = set()
    for node in pipe.WORKFLOW.nodes:
        if node.node_type != "code":
            continue
        ref = (node.config or {}).get("function")
        if ref:
            refs.add(str(ref))
    return refs


def test_every_injected_key_matches_a_code_node_reference():
    injected = _injected_keys()
    assert injected, "注入字典为空，说明 _checker_functions 没有登记任何节点"
    missing = injected - _code_node_refs()
    assert missing == set(), f"注入键在节点定义里找不到对应引用：{missing}"


def test_llm_backed_code_nodes_are_injected():
    """凡是签名里带 ``llm`` 的 code 节点都必须被注入，否则运行时拿到 ``None``。"""
    import inspect

    injected = _injected_keys()
    for node in pipe.WORKFLOW.nodes:
        if node.node_type != "code":
            continue
        ref = str((node.config or {}).get("function") or "")
        if not ref:
            continue
        attr = ref.rpartition(":")[2]
        func = getattr(pipe, attr, None)
        if func is None:
            continue
        params = inspect.signature(func).parameters
        if "llm" in params:
            assert ref in injected, f"{node.node_id}（{ref}）需要注入 llm，但注入字典里没有"
