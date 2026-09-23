"""``llm`` 节点执行器。

原 DSL 里 17 处 ``tool`` 节点调用 ``Gemini（异常输出重试版）`` 子流程、另有若干直连
``llm`` 节点，迁移后**全部**收敛成一次 :meth:`LlmClient.llm_call`（规划 §6.3）。
原节点身份仍保留：调用路径与 ``original_node_id`` 一起写进运行记录。
"""

from __future__ import annotations

from typing import Any

from ..definitions import NodeDef
from .context import RunContext, coerce_to_text
from .executors import (
    ExecutionOutcome,
    ExecutorRegistry,
    NodeExecutionError,
    NodeRuntime,
    resolve_binding_value,
)


def register_all(registry: ExecutorRegistry) -> ExecutorRegistry:
    registry.register("llm", execute_llm)
    return registry


async def execute_llm(
    node: NodeDef, ctx: RunContext, rt: NodeRuntime
) -> ExecutionOutcome:
    """渲染提示词并调用统一 LLM 层。

    节点配置：

    - ``prompt_field``：提示词来自哪个入参（缺省 ``prompt``）；
    - ``expect_json``：是否要求 JSON 输出。**由调用点显式声明**，不继承 DSL 的
      ``output_json`` 入参——实测 17 处全为 ``false``，据它判断会全部判错（§6.4.2）；
    - ``output``：结果写入哪个字段（缺省 ``text``）；
    - ``system_field`` / ``images_field``：可选系统提示与图片入参名。
    """
    inputs = ctx.bind_inputs(node.bindings)

    prompt_field = node.config.get("prompt_field", "prompt")
    if prompt_field not in inputs:
        raise NodeExecutionError(
            f"llm 节点 {node.node_id} 找不到提示词入参 {prompt_field!r}；"
            f"已绑定：{sorted(inputs)}"
        )
    prompt = coerce_to_text(inputs[prompt_field])
    if not prompt.strip():
        raise NodeExecutionError(
            f"llm 节点 {node.node_id} 的提示词为空；空提示词不发起调用"
        )

    system = None
    system_field = node.config.get("system_field")
    if system_field and system_field in inputs:
        system = coerce_to_text(inputs[system_field]) or None

    images = _collect_images(node, ctx, inputs)

    expect_json = bool(node.config.get("expect_json", False))
    output_field = node.config.get("output", "text")

    node_ref = _NodeRef(ctx, node)
    raw = await rt.llm.llm_call(
        prompt,
        system=system,
        images=images,
        expect_json=expect_json,
        node_ref=node_ref,
    )

    if expect_json:
        return ExecutionOutcome(
            outputs={output_field: raw, "parsed_json": raw},
            meta={"expect_json": True},
        )
    return ExecutionOutcome(outputs={output_field: coerce_to_text(raw)})


def _collect_images(
    node: NodeDef, ctx: RunContext, inputs: dict[str, Any]
) -> list[str] | None:
    """收集多模态图片输入。

    V0.2 不覆盖 vision 节点（12 个截图解析节点属 V0.3），因此这里只在配置显式声明
    时才收集，且对非字符串做类型校验，避免把错误类型悄悄送给模型。
    """
    images_field = node.config.get("images_field")
    if not images_field:
        return None
    raw = inputs.get(images_field)
    if raw is None:
        return None

    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise NodeExecutionError(
            f"llm 节点 {node.node_id} 的图片入参 {images_field!r} 类型不受支持：{type(raw)!r}"
        )

    images: list[str] = []
    for item in items:
        if isinstance(item, dict):
            url = item.get("url") or item.get("remote_url")
            if url:
                images.append(str(url))
        elif isinstance(item, str) and item:
            images.append(item)
    return images or None


class _NodeRef:
    """留给 ``llm_call`` 的引用，用于把每次尝试写进「请求尝试」层。"""

    __slots__ = ("run_id", "call_path", "node_id", "node_title", "workflow_id")

    def __init__(self, ctx: RunContext, node: NodeDef) -> None:
        self.run_id = ctx.run_id
        self.call_path = ctx.call_path
        self.node_id = node.node_id
        self.node_title = node.title
        self.workflow_id = ctx.workflow_id

    def asdict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "call_path": self.call_path,
            "node_id": self.node_id,
            "node_title": self.node_title,
        }


def make_direct_llm_executor(
    prompt_field: str, output_field: str = "text", expect_json: bool = False
) -> Any:
    """构造一个固定提示词字段的 llm 执行器，供工作流定义里的短链使用。"""

    async def executor(
        node: NodeDef, ctx: RunContext, rt: NodeRuntime
    ) -> ExecutionOutcome:
        merged = NodeDef(
            node_id=node.node_id,
            title=node.title,
            node_type=node.node_type,
            predecessors=node.predecessors,
            bindings=node.bindings,
            outputs=node.outputs,
            config={
                **node.config,
                "prompt_field": node.config.get("prompt_field", prompt_field),
                "output": node.config.get("output", output_field),
                "expect_json": node.config.get("expect_json", expect_json),
            },
            coords=node.coords,
        )
        return await execute_llm(merged, ctx, rt)

    return executor
