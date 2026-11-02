"""``GET /knowledge``：运行台「知识库」页的只读接口。

三条口径，逐条钉住：

1. **只读**：没有写接口，任何 POST/PUT/DELETE 都不存在；
2. **不依赖服务实例**：与 ``/rules``、``/standards`` 同一定位，服务未就绪也能回答；
3. **价格两段并列**：主力价格带与全系包络都要给，否则看页面的人会以为 917 卖 110 万。
"""

from __future__ import annotations

import httpx
import pytest

from customer_profile.api import create_app


@pytest.fixture
async def client():
    """不装配服务：这条接口按设计就不该依赖它。"""
    app = create_app(service_factory=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_knowledge_endpoint_returns_pack(client):
    resp = await client.get("/knowledge")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["pack_id"] == "dfms-mengshi"
    assert body["display_name"]


async def test_knowledge_endpoint_lists_all_models(client):
    body = (await client.get("/knowledge")).json()
    ids = [item["model_id"] for item in body["models"]]
    assert ids == ["dfms-x700", "dfms-m817", "dfms-917"]
    assert body["total_models"] == 3
    assert body["total_configs"] >= 15


async def test_knowledge_endpoint_gives_both_price_bands(client):
    """917 必须同时给出主力带与全系包络——只给包络会误导成百万级车。"""
    body = (await client.get("/knowledge")).json()
    entry = next(m for m in body["models"] if m["model_id"] == "dfms-917")
    price = entry["price"]
    assert price["core_max_wan"] == pytest.approx(69.90)
    assert price["max_wan"] == pytest.approx(109.80)
    assert price["has_extended"] is True
    assert price["note"], "口径说明必须一起给出，否则两个数字无从解释"


async def test_knowledge_endpoint_exposes_configs(client):
    """档位必须可见：同一车型不同档位的价格与动力都不同。"""
    body = (await client.get("/knowledge")).json()
    entry = next(m for m in body["models"] if m["model_id"] == "dfms-x700")
    names = [c["name"] for c in entry["configs"]]
    assert "325KM 后驱 Pro" in names
    assert len(names) == 4


async def test_knowledge_endpoint_has_signature_and_effective_date(client):
    """换知识要能看出来：指纹与生效日是唯一的线索。"""
    body = (await client.get("/knowledge")).json()
    assert body["content_signature"]
    assert body["effective_from"] == "2026-10-29"
    assert isinstance(body["days_since_effective"], int)


async def test_knowledge_endpoint_is_read_only(client):
    """**没有写接口，这是定论**。"""
    for method in ("post", "put", "patch"):
        resp = await getattr(client, method)("/knowledge", json={})
        assert resp.status_code in (404, 405), f"{method.upper()} 不该被接受"
    resp = await client.delete("/knowledge")
    assert resp.status_code in (404, 405), "DELETE 不该被接受"
