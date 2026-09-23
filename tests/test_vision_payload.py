"""vision 请求体：图片形态与 ``detail`` 透传。

12 个 vision 节点全部位于 6 个截图类工作流（每流 2 个：单张与迭代内各一），它们的
图片输入都来自 http 节点下载到的 ``files`` 字段——Dify 把下载结果包成
``data:image/...;base64,...`` 的列表。这里把「下载 → 组装 → 下发」这条链的契约钉住：

1. http 的 ``files`` 是 **data URI 列表**，不是单个字符串；
2. 组装出的请求体用多模态的 ``content`` 数组，图片走 ``image_url``；
3. ``detail`` 原样透传（实测 DSL 的 ``vision.configs.detail`` 为 ``high``）；
4. 没有图片时不构造 ``content`` 数组——不给模型发空图片列表。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from customer_profile.definitions import WorkflowDef, make_node
from customer_profile.execution.executors import NodeRuntime, build_default_registry
from customer_profile.execution.http import HttpClient
from customer_profile.execution.llm import LlmClient
from customer_profile.execution.scheduler import RunRequest, Scheduler
from customer_profile.replay import ReplaySource
from customer_profile.settings import Settings

from .conftest import make_settings

START = "v_start"
BUILD_URL = "v_build_url"
FETCH = "v_fetch"
VISION = "v_vision"
END = "v_end"

# 一张 1×1 PNG 的 base64（不含 data: 前缀）——http 下载回来的是这种裸 base64
PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAF"
    "AAH/q842iQAAAABJRU5ErkJggg=="
)


class _RecordingTransport(httpx.AsyncBaseTransport):
    """记录发往模型的请求体，并返回一个最小的合法响应。"""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": "解析结果"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


def _runtime(settings: Settings, transport, http_replay) -> NodeRuntime:
    limiter = asyncio.Semaphore(4)
    return NodeRuntime(
        settings=settings,
        request_limiter=limiter,
        # LLM 走录制传输：测试要断言的是**发出去的载荷**，回放反而看不到它
        llm=LlmClient(settings, transport=transport, request_limiter=limiter),
        # http 走固定响应：这里不关心真实下载，只关心下载结果如何被包装
        http=HttpClient(settings, replay=http_replay, request_limiter=limiter),
        extra={},
    )


def _workflow(*, with_images: bool) -> WorkflowDef:
    vision_kwargs = (
        {"images_selector": (FETCH, "files"), "image_detail": "high"}
        if with_images
        else {}
    )
    return WorkflowDef(
        workflow_id="vision_probe",
        display_name="vision 载荷验证图",
        source_dsl=None,
        entries=(START,),
        exits=(END,),
        outputs={"text": "text"},
        nodes=(
            make_node(START, "输入", "start", variables=("host", "path", "question")),
            # 拼出图片直链。真实的截图流程里这一步由上游 code 节点完成（取 channel 的
            # ``media_urls``）；对「图片形态」这条链来说，它只是提供一段**绝对 URL**，
            # 用模板节点最省事，也不必让本用例依赖业务代码。
            make_node(
                BUILD_URL,
                "拼图片直链",
                "template-transform",
                after=(START,),
                inputs={"host": (START, "host"), "path": (START, "path")},
                outputs=("output",),
                template_text="{{ host }}{{ path }}",
            ),
            make_node(
                FETCH,
                "获取图片",
                "http-request",
                after=(BUILD_URL,),
                # 实测 19 个 http 节点里图片类的写法都相同：``@url`` 是**字面量配置**
                # （图片流程里为空串），真正的地址由 ``@url`` **绑定**提供，执行器把
                # 字面量与绑定值首尾相接。这里照抄 moments_info 的「获取图片」。
                inputs={"@url": (BUILD_URL, "output")},
                outputs=("files", "body", "status"),
                **{
                    "@method": "get",
                    "@url": "",
                    "@auth": False,
                    "@ssl_verify": False,
                    "@outputs": {
                        "files": "$files",
                        "body": "$text",
                        "status": "$status",
                    },
                },
            ),
            make_node(
                VISION,
                "内容解析",
                "llm",
                after=(FETCH,),
                inputs={"prompt": (START, "question")},
                outputs=("text",),
                **vision_kwargs,
            ),
            make_node(
                END,
                "输出",
                "end",
                after=(VISION,),
                inputs={"text": (VISION, "text")},
                outputs=("text",),
            ),
        ),
    )


def _http_replay() -> ReplaySource:
    return ReplaySource(
        http={
            "GET https://example.invalid/a.png": [
                {
                    "status_code": 200,
                    "text": PNG_BASE64,
                    "headers": {"content-type": "image/png"},
                }
            ]
        },
        strict=True,
    )


async def _run(workflow: WorkflowDef, inputs: dict, tmp_path):
    settings = make_settings(tmp_path)
    transport = _RecordingTransport()
    registry = build_default_registry()
    scheduler = Scheduler(
        registry, _runtime(settings, transport, _http_replay()), max_active_nodes=8
    )
    outcome = await scheduler.run(RunRequest(workflow=workflow, inputs=inputs))
    return outcome, transport


# ---------------------------------------------------------------- 与真实定义的形状一致性


def test_real_image_http_nodes_use_literal_url_plus_binding():
    """真实迁移里图片 http 节点用的是「字面量 ``@url`` + ``@url`` 绑定」，不是 ``@url_field``。

    执行器文档提到过 ``@url_field``（URL 整段来自绑定值），但 19 个真实 http 节点
    **一个都没用它**——连图片直链也是 ``@url: ""`` 加绑定值拼接。本用例把这个事实钉住，
    免得将来有人照文档去改定义。
    """
    from customer_profile.workflows import load_all

    image_nodes = [
        n
        for w in load_all().values()
        for n in w.nodes
        if n.node_type == "http-request" and any(b.target == "@url" for b in n.bindings)
    ]
    assert image_nodes, "没有找到任何图片类 http 节点，本断言失去意义"
    for node in image_nodes:
        assert "@url" in node.config, f"{node.node_id} 缺少字面量 @url"
        assert not node.config.get("@url_field"), (
            f"{node.node_id} 使用了 @url_field；实测无一节点使用，需复核执行器契约"
        )


# ---------------------------------------------------------------- 契约（无 I/O）


def test_build_messages_uses_content_array_for_images():
    """有图片时用多模态 content 数组：先文本，后逐张 image_url。"""
    client = LlmClient(_stub_settings())
    messages = client.build_messages(
        "看图说话", system="你是助手", images=["data:image/png;base64,AAA"], detail="high"
    )
    assert messages[0] == {"role": "system", "content": "你是助手"}
    content = messages[1]["content"]
    assert isinstance(content, list), "有图片时必须用 content 数组，不能是纯字符串"
    assert content[0] == {"type": "text", "text": "看图说话"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"] == {"url": "data:image/png;base64,AAA", "detail": "high"}


def test_build_messages_omits_content_array_without_images():
    """没有图片时退回纯字符串 content——不给模型发空图片列表。"""
    client = LlmClient(_stub_settings())
    messages = client.build_messages("纯文本问题")
    assert messages == [{"role": "user", "content": "纯文本问题"}]


def test_build_messages_omits_detail_when_unset():
    """未声明 detail 时不下发该字段，把缺省行为留给模型侧。"""
    client = LlmClient(_stub_settings())
    messages = client.build_messages("看图", images=["data:image/png;base64,AAA"])
    assert "detail" not in messages[0]["content"][1]["image_url"]


def _stub_settings() -> Settings:
    return Settings(
        llm_base_url="http://127.0.0.1:1/v1",
        llm_api_key="k",
        llm_model="m",
        replay_mode="fixture",
        interrupt_on_start=False,
    )


# ---------------------------------------------------------------- 端到端（走调度）


async def test_http_files_field_is_a_data_uri_list(tmp_path):
    """http 下载节点的 ``files`` 是 data URI **列表**，content-type 取自响应头。"""
    outcome, _ = await _run(
        _workflow(with_images=False),
        {"host": "https://example.invalid", "path": "/a.png", "question": "问题"},
        tmp_path,
    )
    assert outcome.succeeded, outcome.error
    fetch = outcome.node(FETCH)
    assert fetch.outputs["files"] == [f"data:image/png;base64,{PNG_BASE64}"], (
        f"files 应是 data URI 列表，实际 {fetch.outputs['files']!r}"
    )


async def test_vision_payload_carries_image_and_detail(tmp_path):
    """整条链：下载 → 组装 → 下发，请求体里图片与 detail 都要在。"""
    outcome, transport = await _run(
        _workflow(with_images=True),
        {"host": "https://example.invalid", "path": "/a.png", "question": "这是什么"},
        tmp_path,
    )
    assert outcome.succeeded, outcome.error
    assert transport.payloads, "没有发出模型请求，断言失去意义"

    payload = transport.payloads[0]
    content = payload["messages"][-1]["content"]
    assert isinstance(content, list), (
        f"vision 调用必须用 content 数组，实际 {type(content).__name__}"
    )
    image_parts = [c for c in content if c.get("type") == "image_url"]
    assert len(image_parts) == 1, f"应带 1 张图片，实际 {len(image_parts)} 张"
    assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert image_parts[0]["image_url"]["detail"] == "high", (
        "detail 必须原样透传（DSL 实测为 high）"
    )


async def test_non_vision_call_sends_plain_string_content(tmp_path):
    """未声明图片来源时不得出现 content 数组——否则是一次多余的 vision 调用。"""
    outcome, transport = await _run(
        _workflow(with_images=False),
        {"host": "https://example.invalid", "path": "/a.png", "question": "问题"},
        tmp_path,
    )
    assert outcome.succeeded, outcome.error
    content = transport.payloads[0]["messages"][-1]["content"]
    assert isinstance(content, str)


# ---------------------------------------------------------------- 二进制路径


def test_files_encodes_raw_bytes_not_text():
    """图片下载必须用**原始字节**编码，不能拿 ``response_text``。

    ``response.text`` 会把二进制按文本编码解码，再编码成 base64，结果与真实文件不一致：
    实测 PNG 头 ``iVBORw0KGgo`` 变成 ``77+9UE5HDQoaCg``。模型侧拿到坏图只会报一个
    语焉不详的下载失败，查起来非常费劲——因此这条断言按字节比对，而不是比对字符串。
    """
    import base64

    from customer_profile.execution.executors_http import _files_of

    # 一张真实 PNG 的头几个字节；用真实二进制而不是 base64 字符串，
    # 因为只有前者能暴露「文本解码」这条错误路径。
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452")

    class _Attempt:
        response_text = png.decode("latin-1")
        response_bytes = png
        response_headers = {"content-type": "image/png"}

    uri = _files_of(_Attempt())[0]
    assert uri.startswith("data:image/png;base64,"), uri[:40]
    decoded = base64.b64decode(uri.split(",", 1)[1])
    assert decoded == png, (
        "data URI 解出的字节与原始响应不一致——说明编码走的是文本而非字节"
    )


def test_files_falls_back_to_text_without_raw_bytes():
    """回放 fixture 只给文本时退回文本形态，语法保持一致。"""
    from customer_profile.execution.executors_http import _files_of

    class _Attempt:
        response_text = "AAAA"
        response_bytes = None
        response_headers = {"content-type": "image/png"}

    assert _files_of(_Attempt()) == ["data:image/png;base64,AAAA"]
