"""测试夹具。

两条口径：

1. **不触达外网。** 所有测试用固定响应（``ReplaySource``）或纯确定性代码节点。
   DB 用临时文件，测试之间互不干扰。
2. **缺 DSL 时跳过而非失败。** ``dify_dsl_data/`` 被 gitignore，业务方提供后才能跑
   逐字符比对。相关测试用 ``requires_dsl`` 标记跳过，并在跳过原因里说明缺什么，
   不伪装成通过。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from customer_profile.replay import ReplaySource
from customer_profile.runner import build_service
from customer_profile.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
DSL_DIR = REPO_ROOT / "dify_dsl_data"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "replay"

requires_dsl = pytest.mark.skipif(
    not DSL_DIR.is_dir(),
    reason=f"缺少 {DSL_DIR}（被 gitignore，需业务方提供）——逐字符比对无法执行",
)


@pytest.fixture(scope="session")
def dsl_dir() -> Path:
    return DSL_DIR


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
