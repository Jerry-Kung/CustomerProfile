"""``llm`` 节点的模板引用必须都能在运行期解析到值。

**这条用例的由来（2026-09-23 真实录音冒烟）。** AUC 服务换云实现后，外呼录音工作流
首次真正走到「单文件录音」这条路径，随即失败：

``变量解析失败："模板变量 '1776135353554.file_content' 未提供；已提供：[]"``

根因是**迁移时漏了输入绑定**：``llm`` 节点的提示词正文里写着
``{{#<node>.file_content#}}``，但节点没有声明对应的 ``inputs``，于是
``execute_llm`` 建出的变量表是空的。扫描发现这是**同一处遗漏的 10 个节点**——
7 个截图类的「多图片内容总结」（共用 ``screenshot_flow`` 的构造器）与
2 个录音类的一对 llm 节点（共用 ``audio_flow`` 的构造器）。

**为什么此前 321 项测试没抓到。** 这不是「代码写错」，而是「路径没被走过」：

- 截图类的视觉路径要真实数据里命中图片才会执行，V0.3 冒烟时该号码多数渠道无图；
- 录音类的单文件路径依赖 AUC 服务，而它在整个 V0.3 期间不可达。

两者都是「有真实输入才走到」的分支，固定响应与微缩图验证不了。

**这条用例把判据从「跑到了才发现」变成「静态可查」**：任何一个 llm 节点的模板里
出现了本图内的 ``{{#node_id.field#}}`` 引用，该引用就必须被该节点的绑定覆盖，
或者由节点自身的入参名提供。将来新增工作流时漏绑，这里立刻红。
"""

from __future__ import annotations

import re

import pytest

from customer_profile.templates import TemplateRepository
from customer_profile.workflows import load_all

REFERENCE = re.compile(r"\{\{#([^#{}]+)#\}\}")


def _template_text(repo: TemplateRepository, name: str) -> str:
    """取模板正文。``repo.load`` 返回 Template 对象，正文在 ``.text``。"""
    template = repo.load(name)
    return template.text if hasattr(template, "text") else str(template)


def _llm_nodes_with_templates():
    repo = TemplateRepository()
    found = []
    for workflow_id, workflow in sorted(load_all().items()):
        for node in workflow.nodes:
            if node.node_type != "llm":
                continue
            name = node.config.get("template")
            if not name:
                continue
            found.append((workflow_id, node, _template_text(repo, name)))
    return found


LLM_NODES = _llm_nodes_with_templates()


def test_the_scan_is_not_vacuous():
    """扫描本身要有意义：必须真的扫到带引用的 llm 节点。

    否则将来模板机制一改（比如正文不再外置），这条用例会「零命中而永远绿」，
    变成一条看着在守、实际没守的用例。
    """
    assert LLM_NODES, "没有扫到任何带模板的 llm 节点，本用例失去意义"
    with_refs = [x for x in LLM_NODES if REFERENCE.findall(x[2])]
    assert with_refs, "没有任何 llm 模板含 {{#...#}} 引用，本用例失去意义"


@pytest.mark.parametrize(
    "workflow_id,node,template_text",
    LLM_NODES,
    ids=[f"{wid}:{n.node_id}" for wid, n, _ in LLM_NODES],
)
def test_every_template_reference_is_bound(workflow_id, node, template_text):
    """模板里的每个 ``{{#node_id.field#}}`` 都必须被本节点的绑定覆盖。"""
    refs = REFERENCE.findall(template_text)
    if not refs:
        pytest.skip("该节点模板没有引用")

    # 渲染期变量表按**来源** ``node_id.field`` 建键（executors_llm 的实现），
    # 因此这里也按来源比对，而不是比对目标参数名。
    bound = {f"{b.source[0]}.{b.source[1]}" for b in node.bindings if not b.is_config}

    missing = [ref for ref in refs if ref not in bound]
    assert not missing, (
        f"{workflow_id} 的 llm 节点 {node.node_id}（{node.title}）模板引用了 "
        f"{missing}，但本节点没有对应绑定。运行期会以「模板变量未提供」失败——"
        "该路径只有在真实输入走到时才会暴露，静态扫不出来。"
        f"已绑定：{sorted(bound)}"
    )
