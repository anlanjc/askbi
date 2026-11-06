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


# ---- ASK-2 回归（评审 2.0）：SSE 三类帧契约曾被内层事件 type 覆盖 ----

def _parse_sse(text: str) -> list[dict]:
    frames = []
    for chunk in text.split("\n\n"):
        if chunk.startswith("data: "):
            import json as _json
            frames.append(_json.loads(chunk[6:]))
    return frames


async def test_sse_frame_contract_types(client):
    """顶层 type 必须落在声明的帧类型集合内；事件帧带 event_type 子字段。"""
    async with client.stream(
        "POST", "/api/ask", json={"question": "上月各品类销售额", "role": "boss"}
    ) as resp:
        text = (await resp.aread()).decode()
    frames = _parse_sse(text)
    assert frames, "SSE 应有帧"
    allowed = {"start", "node_done", "event", "final", "error", "clarify_pending"}
    types = {f["type"] for f in frames}
    assert types <= allowed, f"越界帧类型: {types - allowed}"
    evs = [f for f in frames if f["type"] == "event"]
    assert evs, "应有 event 帧（执行留痕）"
    assert all("event_type" in f for f in evs)
    assert all(f["event_type"] not in allowed for f in evs), \
        "event_type 不得再顶掉顶层契约类型"


# ---- ASK-10 回归（评审 2.0）：payload.depth 曾从未接线 ----

async def test_depth_override_reaches_router(client):
    """显式 depth=full 应覆盖关键词自动路由（router 事件 data.depth=full + explicit 原因）。"""
    async with client.stream(
        "POST", "/api/ask",
        json={"question": "上月销售额", "role": "boss", "depth": "full"},
    ) as resp:
        text = (await resp.aread()).decode()
    frames = _parse_sse(text)
    routes = [f for f in frames if f["type"] == "event" and f.get("node") == "router"]
    assert routes and routes[0]["data"]["depth"] == "full"
    assert "explicit" in routes[0]["data"]["reason"]
