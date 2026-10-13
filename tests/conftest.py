"""测试夹具。

两条口径：

1. **不触达外网。** 所有测试用固定响应（``ReplaySource``）或纯确定性代码节点。
   DB 用临时文件，测试之间互不干扰。
2. **不出网也能覆盖全部路径。** 需要真实模型或外部服务的用例一律走回放固定响应，
   不依赖任何本地外部文件；测试在干净检出上应当全绿，不出现「缺文件所以跳过」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from customer_profile.replay import ReplaySource
from customer_profile.runner import build_service
from customer_profile.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "replay"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    """构造一份测试用配置：临时数据库、回放模式、不出网。"""
    defaults = {
        "llm_base_url": "http://127.0.0.1:1/v1",
        "llm_api_key": "test-key",
        "llm_model": "test-model",
        "database_url": f"sqlite:///{tmp_path / 'test.db'}",
        "data_dir": tmp_path,
        "replay_mode": "fixture",
        "replay_strict": True,
        "interrupt_on_start": False,
        "max_concurrent_requests": 4,
        # 显式钉定为 None：测试不得从开发者的 .env 取值，
        # 否则本地配置会悄悄改变请求体形状并让用例忽红忽绿。
        "llm_enable_thinking": None,
        "llm_max_tokens": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
async def service(tmp_path):
    """一个启用留痕、走固定响应的服务。所用 replay 由各测试自行传入。"""
    built: list = []

    async def factory(replay: ReplaySource | None = None, **kw):
        settings = make_settings(tmp_path, **kw)
        svc = await build_service(settings, replay=replay, **kw.pop("build_kw", {}))
        built.append(svc)
        return svc

    yield factory
    for svc in built:
        await svc.aclose()
        if svc.store is not None:
            svc.store.close()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
