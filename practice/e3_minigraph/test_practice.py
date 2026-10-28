"""E3 判卷测试 —— 改编自 askbi/tests/test_graph.py（import 指向练习 skeleton）。

验收标准：pytest test_practice.py 全绿（6 passed）。
"""

import pytest

from skeleton import run_ask


async def test_simple_query(db_path):
    state = await run_ask("上个月的销售额是多少", role="boss")
    a = state["answer"]
    assert a["kind"] == "answer"
    assert a["table"]["rows"] and a["table"]["rows"][0][0] > 0
    assert a["sql"].lower().startswith("select")
    assert "口径" in a["caliber"]


async def test_clarify_flow(db_path):
    state = await run_ask("最近卖得怎么样", role="boss")
    a = state["answer"]
    assert a["kind"] == "clarify"
    assert a["options"]


async def test_refusal_flow(db_path):
    state = await run_ask("上月客服满意度是多少", role="boss")
    assert state["answer"]["kind"] == "refusal"


async def test_refusal_shortcut_skips_executor(db_path):
    """拒答捷径：clarifier 拒答后直接 finalizer，不得经过 sql_agent / executor。"""
    state = await run_ask("上月客服满意度是多少", role="boss")
    nodes = [e["node"] for e in state["events"]]
    assert nodes[0] == "router"
    assert nodes[-1] == "finalizer"
    assert "sql_agent" not in nodes
    assert "executor" not in nodes


async def test_answer_path_visits_all_nodes(db_path):
    """answer 路径必须按序经过 router → clarifier → sql_agent → executor → finalizer。"""
    state = await run_ask("上个月的销售额是多少", role="boss")
    nodes = [e["node"] for e in state["events"]]
    assert nodes[0] == "router" and nodes[-1] == "finalizer"
    for n in ("clarifier", "sql_agent", "executor"):
        assert n in nodes


async def test_router_depth():
    """预算路由：归因/趋势语义 → full，简单聚合 → light（纯代码，不调 LLM）。"""
    from skeleton import route_node

    assert route_node({"question": "为什么上月美妆品类毛利率下滑了", "events": []})["depth"] == "full"
    assert route_node({"question": "上个月的销售额是多少", "events": []})["depth"] == "light"
