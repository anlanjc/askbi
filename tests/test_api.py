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


async def test_feedback_roundtrip(client):
    """thumbs 反馈：落到任务 → 明细可见 → 指标汇总 → JSONL 回放恢复。"""
    await client.post("/api/ask", json={"question": "上月销售额", "role": "boss"})
    task_id = (await client.get("/api/tasks")).json()[0]["id"]

    r = await client.post("/api/feedback", json={"task_id": task_id, "value": "up", "comment": "口径清楚"})
    assert r.status_code == 200
    assert r.json()["feedback"]["value"] == "up"

    detail = (await client.get(f"/api/tasks/{task_id}")).json()
    assert detail["feedback"]["comment"] == "口径清楚"

    metrics = (await client.get("/api/metrics")).json()
    assert metrics["feedback"]["up"] >= 1

    from app.api.task_store import TaskStore
    replayed = TaskStore().get(task_id)
    assert replayed["feedback"]["value"] == "up"


async def test_feedback_errors(client):
    r = await client.post("/api/feedback", json={"task_id": "nonexistent", "value": "down"})
    assert r.status_code == 404
    r = await client.post("/api/feedback", json={"task_id": "x", "value": "middle"})
    assert r.status_code == 422   # Literal["up","down"] 契约拒绝非法值
