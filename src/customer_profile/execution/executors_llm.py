"""``llm`` 节点执行器。

原 DSL 里 17 处 ``tool`` 节点调用 ``Gemini（异常输出重试版）`` 子流程、另有若干直连
``llm`` 节点，迁移后**全部**收敛成一次 :meth:`LlmClient.llm_call`（规划 §6.3）。
原节点身份仍保留：``node_executions.node_id`` 用的就是原 Dify 节点 ID，
因此留痕天然可追溯到 DSL 里的那个节点。
"""

from __future__ import annotations

import json
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
from .executors_misc import _template_repository


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

    # DSL 的 ``llm`` 节点不声明 ``variables``：提示词正文里直接写
    # ``{{#node_id.field#}}`` 引用上游。迁移后正文外置为资源文件，因此这里按
    # 「模板 + 已解析绑定」渲染。判定顺序是先看有没有显式提示词入参，再看模板资源，
    # 两者都没有才报错。
    template_name = node.config.get("template")
    prompt_field = node.config.get("prompt_field", "prompt")
    # 提示词入参名有两种，都来自 DSL、都保留原样：
    # - ``prompt``：迁移后新写的 llm 节点用它；
    # - ``input_prompt``：``gemini_retry_2_times`` 子流程的入参名，17 处调用点原样带过来。
    #   只认 ``prompt`` 的话这 17 个节点会全部以「没有提示词入参」失败——冒烟时正是如此。
    prompt_names = (prompt_field, "input_prompt") if prompt_field != "input_prompt" else (prompt_field,)

    matched = next((name for name in prompt_names if name in inputs), None)
    if matched is not None:
        prompt = coerce_to_text(inputs[matched])
    elif template_name:
        variables = {
            f"{b.source[0]}.{b.source[1]}": resolve_binding_value(ctx, b)
            for b in node.bindings
            if not b.is_config
        }
        for name, value in inputs.items():
            variables.setdefault(name, value)
        prompt = _template_repository(rt).render(
            template_name, variables, strict=node.config.get("strict", True)
        )
    else:
        raise NodeExecutionError(
            f"llm 节点 {node.node_id} 既没有提示词入参 {list(prompt_names)}，"
            f"也未声明 config['template']；已绑定：{sorted(inputs)}"
        )

    if not prompt.strip():
        raise NodeExecutionError(
            f"llm 节点 {node.node_id} 的提示词为空；空提示词不发起调用"
        )

    system = None
    system_field = node.config.get("system_field")
    if system_field and system_field in inputs:
        system = coerce_to_text(inputs[system_field]) or None
    if system is None:
        # 定义层用 ``system_text`` 携带**固定**的系统提示（DSL 里 llm 节点的
        # ``prompt_template`` 中 role=system 那一段，例如
        # ``You are a helpful AI assistant.``）。原先只认 ``system_field``（入参名），
        # 于是这些固定系统提示被静默丢弃，模型收到的提示词与 Dify 不一致。
        static_system = node.config.get("system_text")
        if static_system:
            system = coerce_to_text(static_system) or None

    images = _collect_images(node, ctx, inputs)
    image_detail = node.config.get("image_detail")

    expect_json = bool(node.config.get("expect_json", False))
    output_field = node.config.get("output", "text")

    node_ref = _NodeRef(ctx, node)
    raw = await rt.llm.llm_call(
        prompt,
        system=system,
        images=images,
        detail=image_detail,
        expect_json=expect_json,
        node_ref=node_ref,
    )

    if expect_json:
        # ``text`` 必须是**字符串**，不是解析后的对象。
        #
        # 依据：Dify 的 llm 节点输出一律是文本，下游 code 节点按文本消费。实测两个
        # 消费方（``判断结果提取``、``Notes结果拆分``）都对它做 ``json.loads``——
        # 传入 dict 会抛异常，而两者的 ``except`` 都会吞掉错误并按缺省值返回
        # （``is_valid=False`` / 空列表），**静默给出错误业务结论**。
        #
        # 这个缺陷直到 2026-09-23 才暴露：外呼录音的真实录音首次走到该分支时，
        # 模型正确判出 ``is_valid: true``，而 code 节点输出 ``false``，两者矛盾。
        # 此前「单方录音」样本恰好也应判 false，错误与正确无从区分。
        #
        # 解析后的对象仍保留在 ``parsed_json``，供需要对象的场合使用；序列化用
        # ``json.dumps`` 而非 ``str()``，保证能被 ``json.loads`` 原样读回。
        return ExecutionOutcome(
            outputs={
                output_field: json.dumps(raw, ensure_ascii=False),
                "parsed_json": raw,
            },
            meta={"expect_json": True},
        )
    return ExecutionOutcome(outputs={output_field: coerce_to_text(raw)})


def _collect_images(
    node: NodeDef, ctx: RunContext, inputs: dict[str, Any]
) -> list[str] | None:
    """收集多模态图片输入。

    实测 12 个 vision 节点全部以 http 节点的 ``files`` 字段为输入，其值是**图片文件的
    base64 data URI 列表**（Dify 把下载到的文件包成 ``data:image/...;base64,...``）。
    因此这里接受字符串、列表与带 ``url`` / ``remote_url`` 的对象三种形态。

    来源有两种声明方式：

    - ``config['images_field']``：图片来自**已命名的入参**（定义里显式绑定了名字）；
    - ``config['images_selector']``：图片来自**节点选择器**（``(node_id, field)``）。
      DSL 的 ``llm`` 节点不声明 ``variables``，图片引用写在 ``vision.configs``
      的 ``variable_selector`` 里，迁移后就是这个形态。

    上游没有图片时返回 ``None``，由调用方按「非 vision 调用」处理——不伪造空图片列表。
    """
    images_field = node.config.get("images_field")
    selector = node.config.get("images_selector")

    if images_field and images_field in inputs:
        raw = inputs[images_field]
    elif selector:
        raw = ctx.resolve_optional(tuple(selector), default=None)
    else:
        return None
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
