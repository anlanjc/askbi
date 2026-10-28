"""FastAPI 应用：SSE 流式问答 + 任务回放 + 指标口径 + 成本看板 + 静态前端。"""

from __future__ import annotations

import json
from langgraph.types import Command
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..agents.graph import get_graph
from ..config import get_settings
from ..data.registry import get_spec
from ..data.schema import schema_prompt
from .schemas import AskRequest
from .task_store import get_task_store

app = FastAPI(title="AskBI 问数引擎", version="0.1.0")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    """可选 API Key：留空不启用（演示友好），配置后所有 /api 请求必须携带。"""
    key = get_settings().api_key
    if key and request.url.path.startswith("/api") and request.url.path != "/api/health":
        if request.headers.get("X-API-Key") != key:
            return JSONResponse(status_code=401, content={"detail": "invalid or missing X-API-Key"})
    return await call_next(request)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


_SUSPEND_GRAPH = None


def _get_suspend_graph():
    """挂起拓扑专用图（带 checkpointer；与默认单例隔离，flag 关时不构建）。"""
    global _SUSPEND_GRAPH
    if _SUSPEND_GRAPH is None:
        from langgraph.checkpoint.memory import InMemorySaver

        from ..agents.graph import build_graph
        _SUSPEND_GRAPH = build_graph(clarify_suspend=True, checkpointer=InMemorySaver())
    return _SUSPEND_GRAPH


@app.post("/api/ask")
async def ask(body: AskRequest):
    """SSE 流式执行：node_done / event / final 三类消息。"""
    from ..config import get_settings
    from ..context import get_session_context, resolve_followup

    question = body.question
    followup_note = ""
    if get_settings().followup_enabled and body.session_id:
        ctx = get_session_context()
        question, followup_note = resolve_followup(
            body.question, ctx.get(body.session_id),
            enabled=get_settings().followup_enabled)
        ctx.set(body.session_id, question)   # 存改写后的完整问题作下轮上下文
    task = get_task_store().create(question, body.role)
    t0 = time.perf_counter()

    # E4②：澄清挂起拓扑（CLARIFY_SUSPEND_ENABLED=1 时启用挂起图 + thread 配置）
    suspend_on = get_settings().clarify_suspend_enabled
    graph = _get_suspend_graph() if suspend_on else get_graph()
    astream_config = ({"configurable": {"thread_id": body.thread_id or task["id"]}}
                      if suspend_on else None)

    async def gen():
        sent_events = 0
        final_state: dict = {}
        yield _sse({"type": "start", "task_id": task["id"]})
        if followup_note:
            yield _sse({"type": "event", "node": "followup", "message": followup_note})
        try:
            if suspend_on and body.resume:
                payload = Command(resume=question)   # 恢复挂起的澄清（question=用户回答）
            else:
                payload = {"question": question, "role": body.role, "events": [], "usage": {}}
            async for update in graph.astream(payload, config=astream_config,
                                              stream_mode="updates"):
                if "__interrupt__" in update:        # 澄清挂起：通知客户端带 thread_id 回答
                    intr = update["__interrupt__"][0]
                    value = intr.value if isinstance(intr.value, dict) else {}
                    get_task_store().finish(
                        task["id"], {"kind": "clarify_pending", "thread_id": astream_config[
                            "configurable"]["thread_id"], "clarify": value},
                        final_state.get("usage", {}), final_state.get("events", []),
                        (time.perf_counter() - t0) * 1000)
                    yield _sse({"type": "clarify_pending",
                                "thread_id": astream_config["configurable"]["thread_id"],
                                **value})
                    return
                for node, delta in update.items():
                    if not isinstance(delta, dict):
                        continue
                    if delta.get("status"):
                        yield _sse({"type": "node_done", "node": node, "status": delta["status"]})
                    events = delta.get("events") or []
                    for ev in events[sent_events:]:
                        yield _sse({"type": "event", **ev})
                    sent_events = max(sent_events, len(events))
                    final_state.update(delta)
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "error", "message": str(e)})
            get_task_store().finish(
                task["id"], {"kind": "error", "message": str(e)}, final_state.get("usage", {}),
                final_state.get("events", []), (time.perf_counter() - t0) * 1000,
            )
            return

        answer = final_state.get("answer") or {"kind": "error", "message": "no answer produced"}
        usage = final_state.get("usage", {})
        events = final_state.get("events", [])
        elapsed = (time.perf_counter() - t0) * 1000
        get_task_store().finish(task["id"], answer, usage, events, elapsed)
        yield _sse({
            "type": "final", "task_id": task["id"], "answer": answer,
            "usage": usage, "elapsed_ms": round(elapsed, 1),
        })

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/tasks")
async def list_tasks():
    return get_task_store().list()


@app.get("/api/tasks/{task_id}")
async def task_detail(task_id: str):
    t = get_task_store().get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    return t


@app.get("/api/schema")
async def schema():
    spec = get_spec(get_settings().dataset)
    return {
        "schema": schema_prompt(spec),
        "dimensions": {k: spec.dimension_values.get(k) for k in spec.dimensions},
        "metrics": {mid: {"name": m.name, "unit": m.unit, "caliber": m.caliber} for mid, m in spec.metrics.items()},
        "roles": {"boss": "全区域", "south": "仅华南", "north": "华北+东北"},
    }


@app.get("/api/metrics")
async def metrics():
    """成本看板：token 记账聚合（预算路由的效果在这里可观测）。"""
    return get_task_store().usage_stats()


@app.get("/api/health")
async def health():
    s = get_settings()
    db = get_spec(s.dataset).db_abs_path
    db_ok = db.exists()
    return {"status": "ok", "db": str(db), "db_exists": db_ok, "llm": s.llm_provider}


# 静态前端（放最后，避免吞掉 /api 路由）
_web_dir = Path(__file__).resolve().parent.parent.parent / "web"
if _web_dir.exists():
    app.mount("/", StaticFiles(directory=str(_web_dir), html=True), name="web")
