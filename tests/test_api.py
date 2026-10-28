"""API 测试：健康检查 / schema / SSE 问答 / 任务持久化。"""

import httpx
import pytest

from app.api.main import app


@pytest.fixture
async def client(db_path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_schema_endpoint(client):
    r = await client.get("/api/schema")
    body = r.json()
    assert "gross_margin_rate" in body["metrics"]
    assert body["dimensions"]["region"]


async def test_ask_sse(client):
    async with client.stream(
        "POST", "/api/ask", json={"question": "上个月的销售额是多少", "role": "boss"}
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        text = (await resp.aread()).decode()
    assert '"type": "final"' in text or '"type":"final"' in text
    assert "node_done" in text


async def test_task_recorded(client):
    await client.post("/api/ask", json={"question": "上月客单价", "role": "boss"})
    r = await client.get("/api/tasks")
    tasks = r.json()
    assert tasks and tasks[0]["status"] == "done"
    detail = await client.get(f"/api/tasks/{tasks[0]['id']}")
    assert detail.json()["answer"]["kind"] == "answer"
