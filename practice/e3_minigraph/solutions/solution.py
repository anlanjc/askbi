"""E3 参考实现 —— 与 app/agents/graph.py + nodes.py 的最小图逐行为对应（先自己写，跑不过再看）。"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import get_settings
from app.data.dsl import QueryDSL, compile_sql
from app.llm.base import ChatRequest
from app.llm.factory import get_llm

MAX_SQL_REPAIRS = 2


class GraphState(TypedDict, total=False):
    question: str
    role: str
    depth: str
    route_reason: str
    clarify: dict | None
    refusal: str | None
    dsl: dict
    sql: str
    caliber: str
    sql_error: str | None
    repair_count: int
    result: dict
    answer: dict
    status: str
    events: list[dict]


def _event(node: str, type_: str, data: Any = None) -> dict:
    return {"ts": round(time.time(), 3), "node": node, "type": type_, "data": data}


def _merge_events(state: GraphState, *events: dict) -> list[dict]:
    return list(state.get("events") or []) + list(events)


def _loose_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(t)


async def _call_llm(node: str, payload: dict) -> str:
    req = ChatRequest(
        node=node, system=f"#task={node}", user=json.dumps(payload, ensure_ascii=False)
    )
    resp = await get_llm().chat(req)
    return resp.content


def route_node(state: GraphState) -> dict:
    q = state["question"]
    heavy = bool(re.search(r"(为什么|原因|为何|趋势|走势|对比|vs|各|每个|按)", q))
    depth = "full" if heavy else "light"
    reason = (
        "包含归因/趋势/对比语义，走完整图（含下钻与质检修订）"
        if heavy
        else "简单聚合查询，走轻量链路（跳过下钻叙述）"
    )
    return {
        "depth": depth,
        "route_reason": reason,
        "status": "routed",
        "events": _merge_events(
            state, _event("router", "route", {"depth": depth, "reason": reason})
        ),
    }


async def clarifier_node(state: GraphState) -> dict:
    try:
        verdict = _loose_json(await _call_llm("clarifier", {"question": state["question"]}))
    except Exception:
        verdict = {"verdict": "clear", "reason": "解析失败默认放行"}
    v = verdict.get("verdict", "clear")
    update: dict = {}
    if v == "clarify":
        update.update(
            clarify={
                "question": verdict.get("clarify_question") or "能补充一下你想看的具体指标或时间吗？",
                "options": verdict.get("options") or [],
            },
            status="needs_clarification",
            events=_merge_events(state, _event("clarifier", "clarify", verdict)),
        )
    elif v == "refuse":
        update.update(
            refusal=verdict.get("reason") or "该问题超出数据集能力范围",
            status="refused",
            events=_merge_events(state, _event("clarifier", "refuse", verdict)),
        )
    else:
        update.update(
            status="clarified",
            events=_merge_events(state, _event("clarifier", "clear", {"reason": verdict.get("reason", "")})),
        )
    return update


async def sql_agent_node(state: GraphState) -> dict:
    content = await _call_llm("sql_agent", {"question": state["question"]})
    try:
        raw = _loose_json(content)
    except Exception:
        raw = {"error": "DSL 输出无法解析"}

    if "error" in raw:
        return {
            "refusal": f"无法将问题转为合规查询：{raw['error']}",
            "status": "refused",
            "events": _merge_events(state, _event("sql_agent", "refuse", raw)),
        }
    try:
        dsl = QueryDSL(**raw)
    except Exception as e:  # noqa: BLE001
        return {
            "refusal": f"数据集不支持该查询：{e}",
            "status": "refused",
            "events": _merge_events(state, _event("sql_agent", "refuse", {"error": str(e)})),
        }

    sql, caliber = compile_sql(dsl, role=state.get("role", "boss"))
    return {
        "dsl": dsl.model_dump(),
        "sql": sql,
        "caliber": caliber,
        "sql_error": None,
        "status": "sql_ready",
        "events": _merge_events(
            state,
            _event("sql_agent", "dsl", dsl.model_dump()),
            _event("sql_agent", "sql", {"sql": sql}),
        ),
    }


async def executor_node(state: GraphState) -> dict:
    from app.data.sandbox import Sandbox

    try:
        res = await Sandbox().execute(state["sql"])
    except Exception as e:  # noqa: BLE001
        rc = state.get("repair_count", 0) + 1
        over = rc > MAX_SQL_REPAIRS
        return {
            "sql_error": str(e),
            "repair_count": rc,
            "refusal": f"查询连续执行失败：{e}" if over else None,
            "status": "sql_failed" if not over else "refused",
            "events": _merge_events(state, _event("executor", "error", {"error": str(e), "repair": rc})),
        }
    result = {
        "columns": res.columns,
        "rows": res.rows,
        "row_count": res.row_count,
        "truncated": res.truncated,
        "elapsed_ms": res.elapsed_ms,
    }
    # 聚合函数在空集上返回一行 NULL：规范化为"无数据"（与 nodes.py:259-265 一致）
    if len(res.columns) == 1 and len(res.rows) == 1 and res.rows[0][0] is None:
        result.update(rows=[], row_count=0, note="空结果（可能为权限范围外或无符合条件的数据）")
    return {
        "result": result,
        "sql_error": None,
        "status": "executed",
        "events": _merge_events(
            state,
            _event("executor", "result", {"row_count": res.row_count, "elapsed_ms": res.elapsed_ms}),
        ),
    }


def finalizer_node(state: GraphState) -> dict:
    if state.get("refusal"):
        answer = {"kind": "refusal", "message": state["refusal"]}
    elif state.get("clarify"):
        answer = {
            "kind": "clarify",
            "message": state["clarify"]["question"],
            "options": state["clarify"]["options"],
        }
    else:
        result = state.get("result", {})
        dsl = state.get("dsl", {})
        dims = "×".join(dsl.get("dimensions", [])) or "整体"
        answer = {
            "kind": "answer",
            "summary": f"已按 {dims} 输出明细，共 {result.get('row_count', 0)} 行",
            "table": {"columns": result.get("columns", []), "rows": result.get("rows", [])},
            "sql": state.get("sql", ""),
            "caliber": state.get("caliber", ""),
        }
    return {
        "answer": answer,
        "status": "done",
        "events": _merge_events(
            state, _event("finalizer", "answer_kind", {"kind": answer["kind"]})
        ),
    }


def _after_clarifier(state: GraphState) -> Literal["sql_agent", "finalizer"]:
    if state.get("clarify") or state.get("refusal"):
        return "finalizer"
    return "sql_agent"


def _after_sql_agent(state: GraphState) -> Literal["executor", "finalizer"]:
    if state.get("refusal"):
        return "finalizer"
    return "executor"


def _after_executor(state: GraphState) -> Literal["sql_agent", "finalizer"]:
    if state.get("refusal"):  # 修复超限
        return "finalizer"
    if state.get("sql_error"):
        return "sql_agent"  # SQL 修复回路（加分项）
    return "finalizer"


def build_minigraph():
    g = StateGraph(GraphState)
    g.add_node("router", route_node)
    g.add_node("clarifier", clarifier_node)
    g.add_node("sql_agent", sql_agent_node)
    g.add_node("executor", executor_node)
    g.add_node("finalizer", finalizer_node)

    g.add_edge(START, "router")
    g.add_edge("router", "clarifier")
    g.add_conditional_edges("clarifier", _after_clarifier)
    g.add_conditional_edges("sql_agent", _after_sql_agent)
    g.add_conditional_edges("executor", _after_executor)
    g.add_edge("finalizer", END)
    return g.compile()


async def run_ask(question: str, role: str = "boss") -> GraphState:
    final: dict = {}
    async for chunk in build_minigraph().astream(
        {"question": question, "role": role, "events": []},
        stream_mode="values",
    ):
        final = chunk
    return final
