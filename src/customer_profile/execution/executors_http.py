"""``http-request`` 节点执行器。

迁移原则（规划 §6.2、§2.5）：保留方法、路径、业务 body 与必要 headers，**鉴权从环境
变量注入**；DSL 中的明文 ``X-API-Key`` 不再出现在定义里。

``timeout`` 与 ``retry_interval`` 一律不照搬 DSL 原值（Q5 缺省处理），改用
:class:`~customer_profile.settings.Settings` 里的显式配置。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..definitions import NodeDef
from .context import RunContext, coerce_to_text
from .executors import (
    ExecutionOutcome,
    ExecutorRegistry,
    NodeExecutionError,
    NodeRuntime,
    resolve_binding_value,
)
from .http import HttpCallError


def register_all(registry: ExecutorRegistry) -> ExecutorRegistry:
    registry.register("http-request", execute_http_request)
    return registry


async def execute_http_request(
    node: NodeDef, ctx: RunContext, rt: NodeRuntime
) -> ExecutionOutcome:
    """执行一个 HTTP 节点。

    节点配置：

    - ``@method``：HTTP 方法（``get`` / ``post`` …）；
    - ``@path`` 或 ``@url``：路径（相对 ``@service`` 的 base_url）。含 ``{{#...#}}``
      引用时须在绑定的 ``@path`` 里声明来源，由这里拼接；
    - ``@service``：``auc`` / ``profile`` / ``mhero``，决定 base_url 与凭据；
    - ``@headers``：非敏感 header 字面量；
    - ``@body``：请求体字面量（写请求用）；
    - ``@outputs``：响应字段映射，如 ``{"body": "$text", "status": "$status"}``；
    - ``@error_strategy``：``fail``（缺省）/ ``default-value``（失败时用兜底值）。
    """
    config = node.config
    method = str(config.get("@method", "get")).upper()
    service = config.get("@service", "profile")
    path = _resolve_path(node, ctx, config)

    headers = _build_headers(node, rt, config, service)
    params = config.get("@params") or None
    json_body = config.get("@body")
    retry_enabled = config.get("@retry_enabled")
    retry_max = config.get("@retry_max")
    ssl_verify = config.get("@ssl_verify")

    node_ref = _HttpNodeRef(ctx, node)
    try:
        attempt = await rt.http.request(
            method,
            path,
            service=service,
            headers=headers,
            params=params,
            json_body=json_body,
            ssl_verify=ssl_verify,
            retry_enabled=retry_enabled,
            retry_max=retry_max,
            node_ref=node_ref,
        )
    except HttpCallError as exc:
        if config.get("@error_strategy") == "default-value":
            return ExecutionOutcome(outputs=_default_outputs(config, exc))
        raise NodeExecutionError(str(exc)) from exc

    return ExecutionOutcome(outputs=_map_outputs(node, config, attempt))


def _resolve_path(node: NodeDef, ctx: RunContext, config: Mapping[str, Any]) -> str:
    """拼接请求路径。

    路径里的 ``{{#node.field#}}`` 在定义里登记为 ``@path`` 绑定，因此这里直接把
    绑定值拼到字面路径上，不做通用字符串替换。
    """
    literal = config.get("@path") or config.get("@url")
    if literal is None:
        raise NodeExecutionError(f"http-request 节点 {node.node_id} 未声明 @path")

    suffix = ""
    for binding in node.bindings:
        if binding.target in {"@path", "@url"} and binding.source:
            suffix = coerce_to_text(resolve_binding_value(ctx, binding))
            break

    if "{" in str(literal):
        # 字面量里本身带占位符：只替换已绑定的那一个值
        text = str(literal)
        marker = "{{"
        if marker in text and suffix:
            head, _, tail = text.partition(marker)
            _, _, rest = tail.partition("}}")
            text = head + suffix + rest
        return text
    return f"{literal}{suffix}"


def _build_headers(
    node: NodeDef, rt: NodeRuntime, config: Mapping[str, Any], service: str
) -> dict[str, str] | None:
    """组装请求头。凭据从环境注入，永不写进定义。"""
    headers: dict[str, str] = {
        str(k): coerce_to_text(v) for k, v in (config.get("@headers") or {}).items()
    }
    if config.get("@auth", True):
        api_key = rt.settings.api_key_for(service)
        if api_key:
            headers.setdefault("X-API-Key", api_key)
    headers.setdefault("accept", "application/json")
    return headers or None


def _map_outputs(node: NodeDef, config: Mapping[str, Any], attempt: Any) -> dict[str, Any]:
    """把响应映射成节点输出字段。

    缺省同时给出 ``body``（文本）与 ``status``，因为 DSL 下游对 GET 的消费方式就是
    取响应体文本（如人工确认提取节点把 ``body`` 当 JSON 字符串喂给 code 节点）。
    """
    mapping = config.get("@outputs") or {"body": "$text", "status": "$status"}
    available = {
        "$text": attempt.response_text,
        "$status": attempt.status_code,
        "$json": _try_json(attempt.response_text),
    }
    outputs: dict[str, Any] = {}
    for field_name, selector in mapping.items():
        if isinstance(selector, str) and selector.startswith("$"):
            outputs[field_name] = available.get(selector)
        else:
            outputs[field_name] = selector
    for declared in node.outputs:
        outputs.setdefault(declared, available.get("$text"))
    return outputs


def _try_json(text: str | None) -> Any:
    if not text:
        return None
    import json

    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _default_outputs(config: Mapping[str, Any], exc: Exception) -> dict[str, Any]:
    defaults = config.get("@default_outputs") or {}
    return {**{k: v for k, v in defaults.items()}, "_error": str(exc)}


class _HttpNodeRef:
    """供 ``HttpClient`` 写「请求尝试」记录的引用。"""

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
