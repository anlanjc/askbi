"""LangGraph 图装配。

拓扑（含三条回路：SQL 修复循环 / QA 修订循环；两条捷径：轻量链路、拒答直出）：

START → router → clarifier ─clarify/refuse→ finalizer → END
                        └clear→ sql_agent ─refuse→ finalizer
                                  └→ executor ─error(未超限)→ sql_agent
                                        └ok→ (light? finalizer : insight)
                                  insight → qa ─pass→ finalizer
                                        └fail(未超限)→ insight（带整改反馈）
"""

from __future__ import annotations

from typing import Literal

from langgraph.graph import END, START, StateGraph

from ..config import get_settings
from .nodes import (
    clarifier_node,
    clarify_suspend_node,
    executor_node,
    finalizer_node,
    insight_node,
    qa_node,
    route_node,
    sql_agent_node,
)
from .state import GraphState


def _after_sql_agent(state: GraphState) -> Literal["executor", "finalizer"]:
    if state.get("refusal"):
        return "finalizer"
    return "executor"


def _after_executor(state: GraphState) -> Literal["sql_agent", "insight", "finalizer"]:
    if state.get("refusal"):          # 修复超限
        return "finalizer"
    if state.get("sql_error"):
        return "sql_agent"            # SQL 修复循环
    if state.get("depth") == "light":  # 预算路由：轻量链路跳过下钻与叙述
        return "finalizer"
    return "insight"


def _after_qa(state: GraphState) -> Literal["insight", "finalizer"]:
    if state.get("qa_report", {}).get("passed", False):
        return "finalizer"
    if state.get("revision_count", 0) > get_settings().max_qa_revisions:
        return "finalizer"            # 超限：带失败徽章出结果（降级而非报错）
    return "insight"                  # QA 修订循环


def build_graph(clarify_suspend: bool | None = None, checkpointer=None):
    """装配图。clarify_suspend=True 时启用澄清挂起拓扑（需配 checkpointer）。

    拓扑差异（E4②）：
    clarifier ─clarify→ clarify_suspend（interrupt 挂起，恢复后）→ sql_agent
    默认（False）：clarifier ─clarify→ finalizer（原行为，零变化）。
    """
    flag = get_settings().clarify_suspend_enabled if clarify_suspend is None else clarify_suspend

    def _after_clarifier(state: GraphState):
        # E4②：flag 开启时 clarify 路由到挂起节点；关闭时保持原行为（终结+反问）
        if state.get("refusal"):
            return "finalizer"
        if state.get("clarify"):
            return "clarify_suspend" if flag else "finalizer"
        return "sql_agent"

    g = StateGraph(GraphState)
    g.add_node("router", route_node)
    g.add_node("clarifier", clarifier_node)
    g.add_node("sql_agent", sql_agent_node)
    g.add_node("executor", executor_node)
    g.add_node("insight", insight_node)
    g.add_node("qa", qa_node)
    g.add_node("finalizer", finalizer_node)

    g.add_edge(START, "router")
    g.add_edge("router", "clarifier")
    if flag:
        g.add_node("clarify_suspend", clarify_suspend_node)
    # 显式 path_map：flag 关闭时不注册 clarify_suspend 目标（LangGraph 会静态校验分支目标）
    clarifier_map = ({"clarify_suspend": "clarify_suspend", "sql_agent": "sql_agent",
                      "finalizer": "finalizer"} if flag else
                     {"sql_agent": "sql_agent", "finalizer": "finalizer"})
    g.add_conditional_edges("clarifier", _after_clarifier, clarifier_map)
    if flag:
        g.add_edge("clarify_suspend", "sql_agent")
    g.add_conditional_edges("sql_agent", _after_sql_agent)
    g.add_conditional_edges("executor", _after_executor)
    g.add_edge("insight", "qa")
    g.add_conditional_edges("qa", _after_qa)
    g.add_edge("finalizer", END)
    if flag:
        from langgraph.checkpoint.memory import InMemorySaver

        return g.compile(checkpointer=checkpointer or InMemorySaver())  # interrupt 需 checkpointer
    return g.compile()


_GRAPH = None


def get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


async def run_ask(question: str, role: str = "boss") -> GraphState:
    """一次性执行（脚本/评测用）。流式场景用 get_graph().astream。"""
    final: dict = {}
    async for chunk in get_graph().astream(
        {"question": question, "role": role, "events": [], "usage": {}},
        stream_mode="values",
    ):
        final = chunk
    return final
