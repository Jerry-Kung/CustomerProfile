"""``http-request`` 节点执行器。

迁移原则（规划 §6.2、§2.5）：保留方法、路径、业务 body 与必要 headers，**鉴权从环境
变量注入**；DSL 中的明文 ``X-API-Key`` 不再出现在定义里。

``timeout`` 与 ``retry_interval`` 一律不照搬 DSL 原值（Q5 缺省处理），改用
:class:`~customer_profile.settings.Settings` 里的显式配置。
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from ..definitions import NodeDef, parse_selector
from .context import RunContext, coerce_to_text
from .executors import (
    ExecutionOutcome,
    ExecutorRegistry,
    NodeExecutionError,
    NodeRuntime,
    resolve_binding_value,
)
from .http import HttpCallError

DIFY_REF_IN_BODY = re.compile(r"\{\{#([^#{}]+)#\}\}")


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
    - ``@url_field``：URL **整段来自绑定的值**（DSL 里图片直链就是这种写法：
      ``url: '{{#node.image_url#}}'``，没有字面前缀）。指定它时忽略 ``@path``；
    - ``@service``：``auc`` / ``profile`` / ``mhero``，决定 base_url 与凭据；
    - ``@headers``：非敏感 header 字面量；
    - ``@body``：请求体字面量（写请求用）；
    - ``@body_template``：请求体是**含引用的 JSON 文本**（DSL 里 AUC 的两个节点就是这种
      写法，如 ``'{ "file_url": {{#node.file_url#}} }'``）。与 ``@body`` 的区别在于它由
      引用拼成，且引用的值恒为字符串、需要加引号；渲染结果按 JSON 解析后作为请求体；
    - ``@outputs``：响应字段映射，如 ``{"body": "$text", "status": "$status"}``；
    - ``@writeback``：生产回写节点。``WRITEBACK_ENABLED=false``（缺省）时**不发送**，
      只产出「已跳过」的可解释结果；
    - ``@error_strategy``：``fail``（缺省）/ ``default-value``（失败时用兜底值）。
    """
    config = node.config
    method = str(config.get("@method", "get")).upper()
    service = config.get("@service", "profile")
    path = _resolve_path(node, ctx, config)

    headers = _build_headers(node, rt, config, service)
    params = config.get("@params") or None
    json_body = _resolve_body(node, ctx, config)
    retry_enabled = config.get("@retry_enabled")
    retry_max = config.get("@retry_max")
    ssl_verify = config.get("@ssl_verify")

    node_ref = _HttpNodeRef(ctx, node)
    if config.get("@writeback") and not getattr(rt.settings, "writeback_enabled", False):
        # 生产回写默认关闭（§9.3）。不发送请求，但如实产出「未发送」的可解释结果，
        # 而不是伪造一个成功的响应——下游据此知道这次没有任何生产副作用。
        return ExecutionOutcome(
            outputs=_blocked_writeback_outputs(node, config, path),
            meta={"writeback": "skipped", "reason": "WRITEBACK_ENABLED=false"},
        )
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


def _resolve_body(
    node: NodeDef, ctx: RunContext, config: Mapping[str, Any]
) -> Any:
    """还原请求体。

    DSL 的 http 节点把请求体写成「含 ``{{#node.field#}}`` 引用的 JSON 文本」，例如
    ``{ "file_url": {{#1773112560827.file_url#}} }``。它的含义是：把引用替换成**带引号的
    字符串值**，整体作为 JSON 发送。因此这里按引用逐段替换并对值做 JSON 转义，再解析成
    对象；解析失败说明模板本身不合法，直接报错而不是把坏载荷发出去。
    """
    template = config.get("@body_template")
    if template is None:
        json_template = config.get("@body_json_template")
        if json_template is None:
            return config.get("@body")
        template = json_template

    text = str(template)

    def _substitute(source: str, raw: str) -> str:
        """把模板里的 Dify 引用换成值。

        引用有两种位置，Dify 的语义不同，必须分开处理：

        - **不在字符串里**（``{ "file_url": {{#n.f#}} }``）：值是字符串，要**补引号**；
        - **已在字符串里**（``{"value": "{{#n.f#}}"}"``）：直接插入并做 JSON 转义，
          否则会出现 ``""内容""`` 这种坏载荷。

        判断依据就是占位符紧邻的字符是否为 ``"``。
        """
        def _replace(match: "re.Match[str]") -> str:
            # ``ctx.resolve`` 而非 ``ctx.get``：后者是 ``NodeResult`` 的方法，
            # ``RunContext`` 上没有；写错时只在真实调用的那一刻才炸（冒烟时实测到）。
            value = coerce_to_text(ctx.resolve(parse_selector(match.group(1).strip())))
            start, end = match.start(), match.end()
            before = source[start - 1] if start > 0 else ""
            after = source[end] if end < len(source) else ""
            if before == '"' and after == '"':
                # 已在 JSON 字符串内部：只转义，外层引号由模板自带
                return json.dumps(value, ensure_ascii=False)[1:-1]
            return _json_quote(value)

        return DIFY_REF_IN_BODY.sub(_replace, source)

    text = _substitute(text, text)

    for binding in node.bindings:
        if binding.is_config or not binding.target.startswith("@body_"):
            continue
        placeholder = "{{" + binding.target[len("@body_"):] + "}}"
        if placeholder not in text:
            continue
        value = coerce_to_text(resolve_binding_value(ctx, binding))
        text = text.replace(placeholder, _json_quote(value))

    try:
        return json.loads(text)
    except ValueError as exc:
        raise NodeExecutionError(
            f"http-request 节点 {node.node_id} 的 @body_template 渲染后不是合法 JSON：{text!r}"
        ) from exc


def _json_quote(value: str) -> str:
    """把值转成 JSON 字符串字面量（含两侧引号）。"""
    return json.dumps(value, ensure_ascii=False)


def _resolve_path(node: NodeDef, ctx: RunContext, config: Mapping[str, Any]) -> str:
    """拼接请求路径。

    路径里的 ``{{#node.field#}}`` 在定义里登记为 ``@path`` 绑定，因此这里直接把
    绑定值拼到字面路径上，不做通用字符串替换。

    ``@url_field`` 声明「URL 整段来自绑定值」，此时不做拼接：DSL 里图片直链就是
    ``url: '{{#node.image_url#}}'``，本身没有字面前缀。
    """
    if config.get("@url_field"):
        for binding in node.bindings:
            if binding.target == "@url_field" and binding.source:
                url = coerce_to_text(resolve_binding_value(ctx, binding))
                if not url:
                    raise NodeExecutionError(
                        f"http-request 节点 {node.node_id} 的 URL 取值为空"
                    )
                return url
        raise NodeExecutionError(
            f"http-request 节点 {node.node_id} 声明了 @url_field 但没有对应绑定"
        )

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
        # 图片/文件直链下载的响应体就是文件内容。Dify 把它包成 files（data URI 列表），
        # vision 节点消费的正是这个字段。这里按同一形态给出。
        "$files": _files_of(attempt),
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


def _files_of(attempt: Any) -> list[str]:
    """把一次下载响应整理成 Dify 的 ``files`` 形态：一个 data URI 列表。

    返回列表而不是单个字符串，因为 vision 节点的 ``variable_selector`` 指向该字段后
    期望拿到列表（多图时逐张追加）。非图片内容按原样包成 ``data:`` URI，让语法保持
    统一；调用方若不认识该内容，会在模型侧报错，而不是在这里被静默吞掉。
    """
    headers = getattr(attempt, "response_headers", None) or {}
    content_type = ""
    for key, value in headers.items():
        if str(key).lower() == "content-type":
            content_type = str(value)
            break
    if not content_type:
        content_type = "image/png"

    # 优先用**原始字节**：用 ``response_text`` 会把二进制按文本解码后再编码，产出与
    # 真实文件不一致的 base64（实测 PNG 头 ``iVBORw0KGgo`` 被改写成
    # ``77+9UE5HDQoaCg``），而模型侧只会报一个语焉不详的下载失败。
    # 「在本地解析为真实图片」这条要求就落在这一行。
    raw = getattr(attempt, "response_bytes", None)
    if raw:
        import base64

        return [f"data:{content_type};base64,{base64.b64encode(raw).decode('ascii')}"]

    text = attempt.response_text
    if not text:
        return []
    # 没有原始字节（回放 fixture 只给了文本）时退回文本，并保持语法统一
    return [f"data:{content_type};base64,{text}"]


def _try_json(text: str | None) -> Any:
    if not text:
        return None
    import json

    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _blocked_writeback_outputs(
    node: NodeDef, config: Mapping[str, Any], path: str
) -> dict[str, Any]:
    """回写被开关拦下时的输出。

    字段与真实响应保持一致（``body`` / ``status``），值明确表达「未发送」，
    避免下游把缺失字段当成空响应。
    """
    outputs: dict[str, Any] = {}
    for declared in node.outputs:
        outputs[declared] = ""
    outputs["body"] = json.dumps(
        {"writeback_skipped": True, "url": path, "reason": "WRITEBACK_ENABLED=false"},
        ensure_ascii=False,
    )
    outputs["status"] = 0
    return outputs


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
