"""执行期上下文：一次运行的可变状态与变量解析。

每个 run 有独立上下文，禁止跨客户共享可变中间结果（`Dify迁移任务说明.md` §5.2）。
节点结果按类型保存——JSON 字符串与 JSON 对象不互换（§5.1）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..definitions import Selector


@dataclass(slots=True)
class NodeResult:
    """一个节点执行完成后的结果。"""

    node_id: str
    outputs: dict[str, Any] = field(default_factory=dict)
    status: str = "succeeded"
    error: str | None = None

    def get(self, field_name: str) -> Any:
        """取输出字段。缺失时报错而不是返回 ``None``——静默的 ``None`` 会把错误往后推。"""
        if field_name not in self.outputs:
            raise KeyError(
                f"节点 {self.node_id} 未产出字段 {field_name}；"
                f"实际产出：{sorted(self.outputs)}"
            )
        return self.outputs[field_name]


class MissingValue(KeyError):
    """引用了尚未产出或已失败的节点的输出。"""


@dataclass(slots=True)
class RunContext:
    """一次工作流运行的独立上下文。

    ``outputs`` 以 ``node_id → NodeResult`` 保存。之所以不扁平化成
    ``(node_id, field)`` 单层字典，是因为子流程与迭代会复用同一静态节点 ID，
    扁平化会掩盖「同一 ID 出现在多个动态实例」的事实。
    """

    run_id: str
    workflow_id: str
    inputs: dict[str, Any] = field(default_factory=dict)
    call_path: str = ""
    parent_run_id: str | None = None
    parent_node_id: str | None = None

    _outputs: dict[str, NodeResult] = field(default_factory=dict, repr=False)
    _started_at: float = field(default_factory=time.monotonic, repr=False)

    # ------------------------------------------------------------ 读写

    def record(self, result: NodeResult) -> None:
        self._outputs[result.node_id] = result

    def result_of(self, node_id: str) -> NodeResult:
        try:
            return self._outputs[node_id]
        except KeyError as exc:
            raise MissingValue(f"节点 {node_id} 尚未产出结果") from exc

    def has(self, node_id: str) -> bool:
        return node_id in self._outputs

    def outputs_snapshot(self) -> dict[str, dict[str, Any]]:
        """当前已完成节点的输出快照，用于留痕。"""
        return {
            node_id: dict(res.outputs)
            for node_id, res in self._outputs.items()
            if res.status == "succeeded"
        }

    # ------------------------------------------------------------ 解析

    def resolve(self, selector: Selector | str) -> Any:
        """解析一个 ``{{#node_id.field#}}`` 引用。

        支持 ``'node.field'`` 字符串与 ``(node, field)`` 二元组。
        """
        if isinstance(selector, str):
            node_id, _, field_name = selector.partition(".")
        else:
            node_id, field_name = selector
        if not node_id or not field_name:
            raise MissingValue(f"变量引用格式非法：{selector!r}")
        return self.result_of(node_id).get(field_name)

    def resolve_optional(self, selector: Selector | str, default: Any = None) -> Any:
        """解析引用；来源缺失时返回 ``default``。

        仅用于确实允许缺省的场合（如 httpx 的 ``params``）。默认路径应使用
        :meth:`resolve`，让缺失显式失败。
        """
        try:
            return self.resolve(selector)
        except (MissingValue, KeyError):
            return default

    def bind_inputs(self, bindings: Mapping[str, Selector] | Any) -> dict[str, Any]:
        """按绑定表解析出执行函数的关键字实参。

        接受 :class:`~customer_profile.definitions.Binding` 序列或
        ``{参数名: 选择器}`` 映射。
        """
        resolved: dict[str, Any] = {}
        bindings = list(bindings)
        for binding in bindings:
            target = getattr(binding, "target", None)
            if target is None:
                raise TypeError(f"绑定项缺少 target 属性：{binding!r}")
            source = getattr(binding, "source", binding)
            if getattr(binding, "is_config", False):
                # 配置项（@url / @code 等）由节点执行器自行处理，不进函数实参
                continue
            resolved[target] = self.resolve(source)
        return resolved

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started_at) * 1000)


class MissingVariable(MissingValue):
    """模板渲染时变量缺失。作为独立类型以便渲染层给出更精确的错误。"""


def coerce_to_text(value: Any) -> str:
    """把值转成文本，用于模板渲染。

    ``None`` 归一为空串：Dify 模板对缺失变量渲染为空。``dict`` / ``list``
    转 JSON 文本而非 Python ``repr``，避免下游拿到单引号的非 JSON。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False)
    return str(value)


def is_empty(value: Any) -> bool:
    """Dify 口径的空值判定：``None``、空串、纯空白为空；``0`` 与 ``False`` 不为空。"""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False
