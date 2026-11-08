"""图状态：单一数据总线，所有节点读写这里（LangGraph StateGraph 语义）。"""

from __future__ import annotations

from typing import TypedDict


class GraphState(TypedDict, total=False):
    # 输入
    question: str
    role: str                 # boss | south | north（行级权限）
    depth: str                # light | full（预算路由）
    route_reason: str

    # 澄清
    clarify: dict | None      # {"question": str, "options": [str]}
    clarify_reason: str

    # DSL / SQL
    dsl: dict
    sql: str
    caliber: str              # 口径说明
    sql_error: str | None
    repair_count: int

    # 执行结果（QueryResult 序列化）
    result: dict

    # 归因与叙述
    facts: dict               # drill-down 事实（代码计算）
    narrative: str
    chart: dict | None

    # 质检
    qa_report: dict           # {passed, deterministic_issues, llm_issues, numbers_checked}
    revision_count: int
    revision_feedback: str

    # 输出
    answer: dict
    refusal: str | None

    # 簿记
    status: str
    events: list[dict]
    usage: dict               # node -> {"prompt_tokens": int, "completion_tokens": int}
