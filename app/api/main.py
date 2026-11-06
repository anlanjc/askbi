"""FastAPI 应用：SSE 流式问答 + 任务回放 + 指标口径 + 成本看板 + 静态前端。"""

from __future__ import annotations

import hmac
import json
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ..agents.graph import get_graph
from ..audit import get_audit_log
from ..cache import get_query_cache
from ..config import get_settings
from ..data.registry import get_spec
from ..data.schema import schema_prompt
from ..export import answer_to_csv, export_filename
from .schemas import AdminConfigRequest, AskRequest, FeedbackRequest
from .task_store import get_task_store

app = FastAPI(title="AskBI 问数引擎", version="0.1.0")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.middleware("http")
async def api_key_guard(request: Request, call_next):
    """可选 API Key：留空不启用（演示友好），配置后所有 /api 请求必须携带。"""
    key = get_settings().api_key
    if (
        key
        and request.url.path.startswith("/api")
        and request.url.path != "/api/health"
        and request.headers.get("X-API-Key") != key
    ):
        return JSONResponse(status_code=401, content={"detail": "invalid or missing X-API-Key"})
    return await call_next(request)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


_SUSPEND_GRAPH = None


class _LRUSaver(InMemorySaver):
    """ASK-8 修复（评审 2.0）：进程级 InMemorySaver 只增不减——每检查点含全量结果行，
    长跑内存随请求线性爬升至 OOM。LRU 子类：超容量淘汰最旧 thread 并删除其检查点
    （容量对齐 context.MAX_SESSIONS 量级）。继承而非鸭子包装——langgraph 1.2.17 在
    types.py:122 对 checkpointer 做实例类型强校验。"""

    def __init__(self, max_threads: int = 256):
        super().__init__()
        self._max = max_threads
        self._order: OrderedDict[str, None] = OrderedDict()

    def _touch(self, thread_id: str) -> None:
        if thread_id in self._order:
            self._order.move_to_end(thread_id)
        else:
            self._order[thread_id] = None
            while len(self._order) > self._max:
                oldest, _ = self._order.popitem(last=False)
                self.delete_thread(oldest)  # 继承实现：清 storage/writes 并释放内存

    def put(self, config, checkpoint, metadata, new_versions):
        self._touch(config["configurable"]["thread_id"])
        return super().put(config, checkpoint, metadata, new_versions)

    async def aput(self, config, checkpoint, metadata, new_versions):
        self._touch(config["configurable"]["thread_id"])
        return await super().aput(config, checkpoint, metadata, new_versions)

    def get_tuple(self, config):
        tid = config["configurable"]["thread_id"]
        if tid in self._order:
            self._order.move_to_end(tid)
        return super().get_tuple(config)

    async def aget_tuple(self, config):
        tid = config["configurable"]["thread_id"]
        if tid in self._order:
            self._order.move_to_end(tid)
        return await super().aget_tuple(config)


def _get_suspend_graph():
    """挂起拓扑专用图（带 checkpointer；与默认单例隔离，flag 关时不构建）。"""
    global _SUSPEND_GRAPH
    if _SUSPEND_GRAPH is None:
        from ..agents.graph import build_graph
        _SUSPEND_GRAPH = build_graph(clarify_suspend=True, checkpointer=_LRUSaver(max_threads=256))
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
    # ASK-7 修复（评审 2.0）：resume 前置条件曾零校验——开关关时 resume=true 被静默
    # 丢弃，澄清答案被当全新问题跑图出数；thread 缺失同样静默错跑。请求期显式拒绝。
    if body.resume:
        if not suspend_on:
            raise HTTPException(status_code=422, detail="澄清挂起未启用，resume 请求无效")
        if not body.thread_id:
            raise HTTPException(status_code=422, detail="resume 请求必须携带 thread_id")
        graph = _get_suspend_graph()
        snap = await graph.aget_state({"configurable": {"thread_id": body.thread_id}})
        if not snap.next:
            raise HTTPException(status_code=409, detail="该 thread 无挂起的澄清可恢复")
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
                # ASK-10 修复（评审 2.0）：payload 曾不含 depth——schemas 声明的显式
                # 覆盖从未接线，评测用 depth=full 实际无效果。
                payload = {"question": question, "role": body.role, "depth": body.depth,
                           "events": [], "usage": {}}
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
                        # ASK-2 修复（评审 2.0）：曾 `{"type":"event", **ev}`——内层事件恒含
                        # "type"，dict 展开后内层胜出，三类帧契约失效、前端执行日志恒空。
                        # 内层事件类型改走 event_type 子字段，顶层 type 契约保住。
                        yield _sse({"type": "event", "ts": ev.get("ts"),
                                    "node": ev.get("node"),
                                    "event_type": ev.get("type"),
                                    "data": ev.get("data")})
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


@app.get("/api/export")
async def export_csv(task_id: str):
    """把一次问答的答案表导出 CSV（BOM+元信息+SQL 口径+数据表，Excel 直开）。"""
    t = get_task_store().get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    if not t.get("answer"):
        raise HTTPException(status_code=409, detail="task has no answer yet")
    csv_text = answer_to_csv(t["answer"], question=t.get("question") or "", role=t.get("role") or "")
    # 中文文件名走 RFC 5987 filename*（percent-encode 后才可入 latin-1 header），ASCII fallback 兜底
    fname = quote(export_filename(t.get("question") or ""))
    return Response(
        content=csv_text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=\"askbi_export.csv\"; filename*=UTF-8''{fname}"},
    )


@app.get("/api/tasks/{task_id}")
async def task_detail(task_id: str):
    t = get_task_store().get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    return t


@app.post("/api/feedback")
async def feedback(body: FeedbackRequest):
    """thumbs 反馈：👍/👎 附着到任务，随任务落盘（指标看板 /api/metrics 可见汇总）。"""
    t = get_task_store().set_feedback(body.task_id, body.value, body.comment)
    if t is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"task_id": body.task_id, "feedback": t["feedback"]}


# ---------------- 管理面（审计日志见 app/audit.py：谁在何时通过什么改了什么） ----------------

def _require_admin(request: Request) -> None:
    """管理面门禁：admin_token 配置后要求 X-Admin-Token（常量时间比较）；
    留空=开放但全审计（演示友好，与 api_key 中间件同哲学）。"""
    token = get_settings().admin_token
    if token and not hmac.compare_digest(request.headers.get("X-Admin-Token", ""), token):
        raise HTTPException(status_code=401, detail="invalid or missing X-Admin-Token")


def _audit_fn(action: str, target_type: str, target_id: str):
    """生成审计回调（agentgate _audit(db, ...) 的同窗体形态）：由各变更方法在其锁窗口内调用。"""
    def _record(detail: dict) -> None:
        get_audit_log().record("admin", action, target_type, target_id, detail)
    return _record


@app.post("/api/admin/tasks/clear")
async def admin_tasks_clear(request: Request):
    """清除全部任务：JSONL 归档改名（可追溯）+ 内存索引清空，审计同窗口落盘。

    审计写失败（如磁盘满）→ 变更已回滚，向客户端显式 500。
    """
    _require_admin(request)
    try:
        info = get_task_store().clear(audit=_audit_fn("task.clear", "task_store", "*"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"audit write failed; task clear rolled back ({e})")
    return info


@app.post("/api/admin/cache/clear")
async def admin_cache_clear(request: Request):
    """清空查询缓存（跨数据集全部条目），审计记录清空规模与清空前命中率。"""
    _require_admin(request)
    stats_before = get_query_cache().stats()
    try:
        info = get_query_cache().clear(audit=_audit_fn("cache.clear", "query_cache", "*"))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"audit write failed; cache clear rolled back ({e})")
    return {**info, "enabled": stats_before["enabled"], "hit_rate_before": stats_before["hit_rate"]}


@app.post("/api/admin/config")
async def admin_config(body: AdminConfigRequest, request: Request):
    """运行期配置变更（仅三个演示开关）：from→to 全程入审计；审计写失败回滚变更。"""
    _require_admin(request)
    provided = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not provided:
        raise HTTPException(status_code=422, detail="no config fields provided")
    s = get_settings()
    changes = {k: {"from": getattr(s, k), "to": v} for k, v in provided.items()}
    for k, v in provided.items():
        setattr(s, k, v)
    try:
        get_audit_log().record("admin", "config.update", "settings", "*", {"changes": changes})
    except Exception:  # noqa: BLE001  审计失败 → 写回原值（同事务语义，见 app/audit.py）
        for k, old in changes.items():
            setattr(s, k, old["from"])
        raise HTTPException(status_code=500, detail="audit write failed; config change rolled back")
    return {"applied": provided, "changes": changes}


@app.get("/api/admin/audit")
async def admin_audit_list(request: Request, limit: int = Query(default=100, ge=1, le=1000)):
    """审计日志只读查询（最新在前）。"""
    _require_admin(request)
    return get_audit_log().list(limit=limit)


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
