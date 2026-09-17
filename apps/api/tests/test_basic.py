"""基础测试"""
import pytest
from httpx import AsyncClient, ASGITransport
from src.api.main import app


@pytest.fixture
async def client():
    """创建测试客户端"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_check(client):
    """测试健康检查"""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


@pytest.mark.asyncio
async def test_root(client):
    """测试根路径"""
    response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "OntoloHub"
    assert "version" in data


@pytest.mark.asyncio
async def test_api_root(client):
    """测试 API 根路径"""
    response = await client.get("/api")
    assert response.status_code == 200
    data = response.json()
    assert "endpoints" in data
    assert "projects" in data["endpoints"]
