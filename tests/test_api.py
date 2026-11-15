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


# ---- ASKBI-1 回归（评审 4.0）：SSE 断连/早退任务永久滞留 running 绕过内存上限 ----


async def test_sse_client_disconnect_marks_task_terminal(db_path):
    """客户端中途断连：任务必须落终态（曾永久滞留 running，绕过 ASK-9 内存上限）。"""
    import asyncio
    import json as _json

    from app.api.main import app
    from app.api.task_store import get_task_store

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/api/ask", "raw_path": b"/api/ask", "query_string": b"",
        "root_path": "", "headers": [(b"content-type", b"application/json")],
        "client": ("testclient", 123), "server": ("testserver", 80),
    }
    payload = _json.dumps({"question": "上个月的销售额是多少", "role": "boss"}).encode()
    messages: list[dict] = []
    start_seen = asyncio.Event()
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await start_seen.wait()          # start 帧送达后模拟客户端断连
        return {"type": "http.disconnect"}

    async def send(msg):
        messages.append(msg)
        if msg["type"] == "http.response.body" and b'"start"' in (msg.get("body") or b""):
            start_seen.set()

    app_task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(start_seen.wait(), timeout=30)
    start_frame = next(m for m in messages
                       if m["type"] == "http.response.body" and b'"start"' in (m.get("body") or b""))
    task_id = _json.loads(start_frame["body"].decode().split("data: ", 1)[1])["task_id"]
    assert get_task_store().get(task_id)["status"] == "running"

    await asyncio.wait_for(app_task, timeout=30)   # 断连 → 取消流式生成器 → 兜底落终态
    final = get_task_store().get(task_id)
    assert final["status"] != "running", f"断连后任务仍滞留 {final['status']}"
    assert final["kind"] == "aborted"


async def test_validation_early_exit_leaves_no_orphan_task(client, db_path):
    """校验失败的早退（resume 422 等）不得留下 running 孤儿任务（曾 create 先于校验）。"""
    from app.api.task_store import get_task_store

    store = get_task_store()
    before_running = sum(1 for t in store._tasks.values() if t["status"] == "running")
    r = await client.post("/api/ask", json={"question": "上月销售额", "resume": True})
    assert r.status_code == 422
    after_running = sum(1 for t in store._tasks.values() if t["status"] == "running")
    assert after_running == before_running, "校验失败的早退不得新增 running 孤儿任务"


def test_load_marks_stale_running_as_aborted_and_evicts(db_path, tmp_path):
    """重启 _load：JSONL 里滞留的 running 僵尸载入时即标 aborted（重启后不可能仍在跑），
    且 _load 末尾执行上限淘汰——历史僵尸不得把 200 上限永久顶满。"""
    import json as _json

    from app.api.task_store import _MAX_IN_MEMORY, TaskStore

    path = tmp_path / "tasks.jsonl"
    n_stale = _MAX_IN_MEMORY + 30
    with path.open("w", encoding="utf-8") as f:
        for i in range(n_stale):
            f.write(_json.dumps({
                "id": f"zombie{i:04d}", "question": f"历史僵尸{i}", "role": "boss",
                "status": "running", "kind": None,
                "created_at": f"2026-01-01T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}",
                "elapsed_ms": None, "answer": None, "usage": {}, "events": [],
            }, ensure_ascii=False) + "\n")

    store = TaskStore(path=path)
    statuses = {t["status"] for t in store._tasks.values()}
    assert statuses == {"done"}, f"载入的僵尸必须全部落终态，实得 {statuses}"
    assert len(store._tasks) <= _MAX_IN_MEMORY
    # 淘汰保新：最新（字典序最大 created_at）的僵尸必须仍在内存里
    newest_id = f"zombie{n_stale - 1:04d}"
    assert store.get(newest_id) is not None
    # JSONL 追加日志保持原样（可回放），载入标记只影响内存态
    assert sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) == n_stale
