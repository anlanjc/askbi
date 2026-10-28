"""E3 · 用 LangGraph 重搭最小图（对应 app/agents/graph.py + nodes.py，讲义 docs/lectures/L4_状态机.md）。

目标（STUDY_GUIDE 第 6 节 E3 的可判卷版）：
    START → router → clarifier ─clarify/refuse→ finalizer → END（拒答/澄清捷径）
                        └clear→ sql_agent ─refuse→ finalizer
                                  └ok→ executor ─ok→ finalizer
                                                  └error→ (修复回路：加分项，mock 下测不到)

节点分工（讲义 L4 第 2 节的确定性/不确定性分层）：
    router / executor / finalizer = 纯代码
    clarifier / sql_agent = LLM（本练习跑 mock provider，按 req.node 分发规则处理器）

必须遵守的契约（测试按此判卷）：
1. LLM 调用用 `_call_llm(node, payload)`——node 必须是 "clarifier" / "sql_agent"
   （mock 的规则处理器按这两个名字分发）；
2. 每个节点返回一个 update dict，并用 `_merge_events` 追加事件（先带旧 events！）；
3. 各节点写入 state 的键：
   router: depth(light|full) / route_reason
   clarifier: clarify={"question","options"} 或 refusal=str / status
   sql_agent: refusal=str 或 dsl/sql/caliber（sql_agent 还要清 sql_error）/ status
   executor: result={"columns","rows","row_count","truncated","elapsed_ms"} 或 refusal / status
   finalizer: answer={"kind": "clarify"|"refusal"|"answer", ...} / status="done"
4. 条件边函数返回节点名字符串（与 build_minigraph 中注册名一致）。
"""

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

# ---------------- 给定：状态与工具函数（与 app/agents/nodes.py 同构） ----------------

MAX_SQL_REPAIRS = 2  # SQL 修复回路上限（加分项）


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
    """调用 LLM（练习中为 mock 规则处理器），返回 content 字符串（JSON）。"""
    req = ChatRequest(
        node=node, system=f"#task={node}", user=json.dumps(payload, ensure_ascii=False)
    )
    resp = await get_llm().chat(req)
    return resp.content


# ---------------- TODO 1: router（纯代码预算路由） ----------------


def route_node(state: GraphState) -> dict:
    """规则路由：正则命中 归因/趋势/对比 语义 → depth="full"，否则 "light"。

    参考（与 app/agents/nodes.py:73-88 一致的口径）：
    heavy = bool(re.search(r"(为什么|原因|为何|趋势|走势|对比|vs|各|每个|按)", q))
    返回 {"depth", "route_reason", "status": "routed", "events": [...]}。
    """
    raise NotImplementedError


# ---------------- TODO 2: clarifier（LLM 澄清/拒答） ----------------


async def clarifier_node(state: GraphState) -> dict:
    """调 _call_llm("clarifier", {"question": ...})，_loose_json 解析 verdict。

    verdict == "clarify"：clarify={"question":..., "options":...}，status="needs_clarification"
    verdict == "refuse" ：refusal=reason（空则默认"该问题超出数据集能力范围"），status="refused"
    其余（含解析失败默认放行）：status="clarified"
    三种情况都要追加事件（type 分别为 clarify/refuse/clear）。
    """
    raise NotImplementedError


# ---------------- TODO 3: sql_agent（LLM 产 DSL → 编译器产 SQL） ----------------


async def sql_agent_node(state: GraphState) -> dict:
    """调 _call_llm("sql_agent", {"question": ...}) 得到 DSL JSON。

    - 解析失败或含 "error" 键 → refusal="无法将问题转为合规查询：{...}"，status="refused"；
    - QueryDSL(**raw) 校验失败 → refusal="数据集不支持该查询：{e}"，status="refused"；
      （练习简化：不做二次重试，原版的重试逻辑见 nodes.py:189-201）
    - 成功 → compile_sql(dsl, role=state.get("role", "boss")) 得 (sql, caliber)，
      写 dsl/dsl.model_dump()、sql、caliber、sql_error=None，status="sql_ready"。
    """
    raise NotImplementedError


# ---------------- TODO 4: executor（纯代码沙箱执行） ----------------


async def executor_node(state: GraphState) -> dict:
    """res = await Sandbox().execute(state["sql"])（from app.data.sandbox import Sandbox）。

    - 失败：repair_count+1；超过 MAX_SQL_REPAIRS → refusal（status="refused"），
      否则 sql_error=str(e)（status="sql_failed"，供修复回路加分项使用）；事件 type="error"；
    - 成功：result={"columns","rows","row_count","truncated","elapsed_ms"}（从 QueryResult 取），
      sql_error=None，status="executed"；事件 type="result"。
    """
    raise NotImplementedError


# ---------------- TODO 5: finalizer（纯代码组装） ----------------


def finalizer_node(state: GraphState) -> dict:
    """三种 answer：
    - state["refusal"]        → {"kind": "refusal", "message": refusal}
    - state["clarify"]        → {"kind": "clarify", "message": clarify["question"],
                                 "options": clarify["options"]}
    - 否则                     → {"kind": "answer", "summary": "...", "table":
                                 {"columns": [...], "rows": [...]}, "sql": ..., "caliber": ...}
    写 answer、status="done"，事件 type="answer_kind"（data 带 kind）。
    """
    raise NotImplementedError


# ---------------- TODO 6: 条件边 ----------------


def _after_clarifier(state: GraphState) -> Literal["sql_agent", "finalizer"]:
    """clarify 或 refusal → finalizer（捷径）；否则 → sql_agent。"""
    raise NotImplementedError


def _after_sql_agent(state: GraphState) -> Literal["executor", "finalizer"]:
    """refusal → finalizer；否则 → executor。"""
    raise NotImplementedError


def _after_executor(state: GraphState) -> Literal["sql_agent", "finalizer"]:
    """refusal（修复超限）→ finalizer；sql_error 未超限 → sql_agent（修复回路，加分项）；
    否则 → finalizer（最小图无 insight/qa）。"""
    raise NotImplementedError


# ---------------- TODO 7: 装配 ----------------


def build_minigraph():
    """StateGraph(GraphState)：注册 5 节点（router/clarifier/sql_agent/executor/finalizer），
    固定边 START→router→clarifier、finalizer→END，三条条件边挂到对应节点。compile() 返回。"""
    raise NotImplementedError


# ---------------- 给定：一次性执行入口 ----------------


async def run_ask(question: str, role: str = "boss") -> GraphState:
    final: dict = {}
    async for chunk in build_minigraph().astream(
        {"question": question, "role": role, "events": []},
        stream_mode="values",
    ):
        final = chunk
    return final
