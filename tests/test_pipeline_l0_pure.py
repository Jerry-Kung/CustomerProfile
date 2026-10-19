"""P1 验收：取数侧的「纯识别变体」与原件并存，且管线只消费逐字原文。

**这一层要守住的东西。** `端到端方案设计.md` §4.4 要求关掉取数侧三个「产物无人消费」的
LLM 节点（实测占全批成本 41.1%）。但三个共用件同时被**基线臂**消费：

- ``audio_content_extract``（2 个 LLM 节点）← ``test_drive_audio`` / ``outbound_call_audio``
- ``moments_info`` 的多图总结 ← ``evidence_subagent``
- ``test_drive_audio`` / ``outbound_call_audio`` 自身图内的分析 LLM ← 基线的取数臂

就地删除会让基线臂图结构改变、``definition_hash`` 漂移，AB 可比性判据失效。
因此一律走 **v2 专属变体**：新 ``workflow_id``、与原件并存、模板资源同一批。

本文件的用例都是纯定义层断言，不出网、不依赖批次产物。
"""

from __future__ import annotations

from customer_profile.validation import validate
from customer_profile.workflows import (
    audio_content_extract as ace,
    customer_profile_pipeline as pipe,
    moments_info as mi,
    outbound_call_audio as ob,
    test_drive_audio as td,
)


def llm_nodes(workflow) -> list:
    return [n for n in workflow.nodes if n.node_type == "llm"]


# ---------------------------------------------------------------- 原件逐字未变

def test_original_audio_extract_shape_is_untouched():
    """``audio_content_extract`` 原件仍是迁移前的定义：12 节点、2 个 llm。"""
    assert len(ace.WORKFLOW.nodes) == 12
    assert len(llm_nodes(ace.WORKFLOW)) == 2
    assert ace.WORKFLOW.exits == (ace.END_VALID, ace.END_INVALID)
    assert set(ace.WORKFLOW.outputs) == {"is_valid", "file_content"}


def test_original_screenshot_shape_is_untouched():
    """``moments_info`` 原件仍是 18 节点、3 个 llm（含「多图片内容总结」）。"""
    assert len(mi.WORKFLOW.nodes) == 18
    assert len(llm_nodes(mi.WORKFLOW)) == 3


def test_original_audio_flows_shape_is_untouched():
    """两个录音流程原件仍是 18 节点、2 个 llm。"""
    for workflow in (td.WORKFLOW, ob.WORKFLOW):
        assert len(workflow.nodes) == 18
        assert len(llm_nodes(workflow)) == 2


def test_originals_still_reference_the_original_extract_child():
    """原件的录音抽取节点仍指向 ``audio_content_extract``，没有被迫切到变体。"""
    for workflow in (td.WORKFLOW, ob.WORKFLOW):
        children = {
            n.config.get("@workflow") for n in workflow.nodes if n.node_type == "tool"
        }
        assert children == {ace.WORKFLOW_ID}


# ---------------------------------------------------------------- 变体形状

def test_pure_extract_has_no_llm_nodes():
    """纯识别变体零 llm 节点，是本阶段成本下降的全部来源。"""
    assert len(ace.PURE_WORKFLOW.nodes) == 6
    assert llm_nodes(ace.PURE_WORKFLOW) == []


def test_pure_extract_still_exposes_auc_result():
    """变体必须交出 ``auc_result``——那是管线真正消费的逐字原文。"""
    assert "auc_result" in ace.PURE_WORKFLOW.outputs
    assert "file_content" in ace.PURE_WORKFLOW.outputs


def test_pure_extract_file_content_is_the_verbatim_not_a_summary():
    """``file_content`` 与 ``auc_result`` 同源，都取自「识别结果提取」。

    这是「关掉的是压缩、不是产出」在定义层的凭据：变体没有任何一步能把原文改写成摘要。
    """
    end = ace.PURE_WORKFLOW.node_map[ace.END_PURE]
    sources = {b.target: b.source for b in end.bindings}
    assert sources["file_content"] == (ace.EXTRACT_RESULT, "auc_result")
    assert sources["auc_result"] == (ace.EXTRACT_RESULT, "auc_result")


def test_pure_screenshot_drops_only_the_summary_llm():
    """纯识别朋友圈变体只少「多图片内容总结」一个节点，其余不动。"""
    assert len(mi.PURE_WORKFLOW.nodes) == len(mi.WORKFLOW.nodes) - 1
    titles = {n.title for n in mi.PURE_WORKFLOW.nodes}
    assert "多图片内容总结" not in titles
    # 逐图解析仍在：它才是 L1 要读的原文
    assert "内容解析" in titles


def test_pure_screenshot_aggregate_takes_raw_join_output():
    """多图侧改为直接收「多张图片内容聚合」的拼接结果，不再过 LLM 汇总。"""
    node = mi.PURE_WORKFLOW.node_map["1775811233890"]
    # ``make_node`` 把 ``after`` 收进 ``predecessors``，不在 ``config`` 里。
    assert node.predecessors == ("1775811655657", "1775717096663")
    # 多图侧取「多张图片内容聚合」的 ``output``（逐图拼接），单图侧取 vision 的 ``text``。
    assert tuple(tuple(x) for x in node.config["variables"]) == (
        ("1775811655657", "output"),
        ("1775717096663", "text"),
    )
    # 原件这一处接的是「多图片内容总结」的 text —— 变体正是把它换掉了。
    original = mi.WORKFLOW.node_map["1775811233890"]
    assert tuple(tuple(x) for x in original.config["variables"])[0] == (
        "17760451317570",
        "text",
    )


def test_pure_audio_flows_have_no_llm_nodes():
    """两个录音流程的变体同样零 llm，且原件的分析节点被整段摘除。"""
    for workflow in (td.PURE_WORKFLOW, ob.PURE_WORKFLOW):
        assert llm_nodes(workflow) == []
        assert len(workflow.nodes) == 15


def test_pure_audio_flow_aggregates_raw_sources():
    """纯识别录音流程的最终汇总只接「多文件拼接」与「单文件原文」两路逐字文本。"""
    for workflow in (td.PURE_WORKFLOW, ob.PURE_WORKFLOW):
        node = workflow.node_map["1775811233890"]
        sources = tuple(tuple(x) for x in node.config["variables"])
        fields = {src[1] for src in sources}
        assert fields == {"output", "file_content"}


def test_pure_audio_flows_reference_the_pure_extract_child():
    """变体的两个抽取调用点都指向纯识别子流程。"""
    for workflow in (td.PURE_WORKFLOW, ob.PURE_WORKFLOW):
        children = {
            n.config.get("@workflow") for n in workflow.nodes if n.node_type == "tool"
        }
        assert children == {ace.PURE_WORKFLOW_ID}


def test_pure_variants_are_exported_as_subworkflows():
    """变体经 ``SUBWORKFLOWS`` 导出，``load_all`` 才能注册它们。"""
    assert ace.SUBWORKFLOWS == (ace.PURE_WORKFLOW,)
    assert mi.SUBWORKFLOWS == (mi.PURE_WORKFLOW,)
    assert td.SUBWORKFLOWS == (td.PURE_WORKFLOW,)
    assert ob.SUBWORKFLOWS == (ob.PURE_WORKFLOW,)


def test_pure_variants_are_valid_graphs():
    """四个变体都要过定义校验（零 error 级发现）。"""
    for workflow in (
        ace.PURE_WORKFLOW,
        mi.PURE_WORKFLOW,
        td.PURE_WORKFLOW,
        ob.PURE_WORKFLOW,
    ):
        issues = [i for i in validate(workflow) if getattr(i, "level", "error") == "error"]
        assert not issues, f"{workflow.workflow_id}：{issues}"


def test_pure_variants_reuse_the_original_templates():
    """变体与原件共用同一批模板资源名（不新增正文，避免两套提示词漂移）。"""
    for original, pure in (
        (td.WORKFLOW, td.PURE_WORKFLOW),
        (ob.WORKFLOW, ob.PURE_WORKFLOW),
        (mi.WORKFLOW, mi.PURE_WORKFLOW),
    ):
        names = lambda wf: {  # noqa: E731
            n.config["template"] for n in wf.nodes if n.config.get("template")
        }
        assert names(pure) <= names(original)


# ---------------------------------------------------------------- 管线接线

def test_pipeline_points_at_the_pure_variants():
    """管线（v2 实验臂）的四路子流程指向纯变体。"""
    declared = {
        n.title: n.config.get("@workflow") for n in pipe.WORKFLOW.nodes if n.node_type == "tool"
    }
    assert declared["朋友圈信息提取"] == pipe.MOMENTS_PURE
    assert declared["试驾录音信息提取"] == pipe.TEST_DRIVE_PURE
    assert declared["外呼录音信息提取"] == pipe.OUTBOUND_CALL_PURE
    assert pipe.AUDIO_EXTRACT_WORKFLOW == ace.PURE_WORKFLOW_ID


def test_pipeline_no_longer_references_the_original_shared_children():
    """管线不引用任何原件——引用原件就等于把 41% 的成本又花了回来。"""
    children = {
        n.config.get("@workflow") for n in pipe.WORKFLOW.nodes if n.node_type == "tool"
    }
    for original in (
        ace.WORKFLOW_ID,
        mi.WORKFLOW_ID,
        td.WORKFLOW_ID,
        ob.WORKFLOW_ID,
    ):
        assert original not in children, f"管线仍在引用原件 {original}"
