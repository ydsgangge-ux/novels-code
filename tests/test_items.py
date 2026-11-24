"""``app/`` 演示项目（FastAPI）的端到端用例。

注意：fastapi / httpx 是这个演示项目自己的依赖，不是本仓库的依赖。CI 上没装
时必须跳过——以前这里是裸 import，收集阶段直接抛 ModuleNotFoundError，pytest
以 exit code 2 结束，整个测试会话里一个用例都不会跑（CI 只显示 "collection
failure"，看不出真正坏在哪）。
"""

import pytest

pytest.importorskip("fastapi", reason="app/ 是演示项目，fastapi 不在本仓库依赖里")
pytest.importorskip("httpx", reason="app/ 的验收用例需要 httpx")

from httpx import AsyncClient, ASGITransport  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_create_item(client):
    response = await client.post("/api/v1/items?name=test_item&description=test_desc")
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "test_item"
    assert data["description"] == "test_desc"
    assert "id" in data


@pytest.mark.asyncio
async def test_list_items(client):
    response = await client.get("/api/v1/items")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data


@pytest.mark.asyncio
async def test_get_item(client):
    # 先创建
    create_resp = await client.post("/api/v1/items?name=get_test")
    created = create_resp.json()
    item_id = created["id"]

    response = await client.get(f"/api/v1/items/{item_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == item_id


@pytest.mark.asyncio
async def test_get_item_not_found(client):
    response = await client.get("/api/v1/items/99999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_item(client):
    create_resp = await client.post("/api/v1/items?name=old_name")
    created = create_resp.json()
    item_id = created["id"]

    response = await client.put(f"/api/v1/items/{item_id}?name=new_name")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "new_name"


@pytest.mark.asyncio
async def test_delete_item(client):
    create_resp = await client.post("/api/v1/items?name=delete_me")
    created = create_resp.json()
    item_id = created["id"]

    response = await client.delete(f"/api/v1/items/{item_id}")
    assert response.status_code == 204

    # 验证已删除
    get_resp = await client.get(f"/api/v1/items/{item_id}")
    assert get_resp.status_code == 404
