"""E4② 澄清挂起/恢复测试：interrupt 语义、checkpointer 续跑、线程隔离、开关零变化。"""
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.graph import build_graph
from app.config import get_settings


@pytest.fixture
def suspended_graph():
    """flag 开启 + 独立 checkpointer 的图（不经 get_graph 单例，避免污染）。"""
    return build_graph(clarify_suspend=True, checkpointer=InMemorySaver())


def _cfg(tid: str = "t1") -> dict:
    # 注意：langgraph 会原地改写 config dict（注入 checkpoint_ns）——必须每次新建，
    # 共享模块级 dict 会导致跨测试污染（本项目实测踩坑）。
    return {"configurable": {"thread_id": tid}}


async def _drain(graph, payload, config):
    """跑一帧流并返回 (是否中断, 中断负载, 最终状态片段)。"""
    interrupted, payload_out, last = False, None, {}
    async for chunk in graph.astream(payload, config=config, stream_mode="updates"):
        if "__interrupt__" in chunk:
            interrupted = True
            intr = chunk["__interrupt__"][0]
            payload_out = intr.value
        last.update(chunk)
    return interrupted, payload_out, last


@pytest.mark.asyncio
async def test_clarify_suspends_without_answer(suspended_graph):
    """澄清问题不再终结出反问，而是挂起等用户。"""
    interrupted, payload, last = await _drain(
        suspended_graph, {"question": "最近卖得怎么样", "role": "boss", "events": [], "usage": {}}, _cfg())
    assert interrupted is True
    assert "question" in (payload or {})          # interrupt 负载带反问
    assert last.get("answer") is None             # 没有产出最终答案


@pytest.mark.asyncio
async def test_resume_completes_with_merged_question(suspended_graph):
    """用户澄清后 Command(resume) → 续跑至出数，问题并入澄清内容。"""
    await _drain(suspended_graph,
                 {"question": "最近卖得怎么样", "role": "boss", "events": [], "usage": {}},
                 _cfg())
    resumed, _payload, last = await _drain(
        suspended_graph, Command(resume="我想看销售额和毛利率"), _cfg())
    assert resumed is False                        # 恢复后一口气跑完
    # 节点键控增量：answer 在 finalizer 的 delta 里（也可用 aget_state 读全量）
    a = (last.get("finalizer") or {}).get("answer") or {}
    assert a.get("kind") == "answer"
    assert a.get("sql", "").lower().startswith("select")
    # 澄清内容并入问题（供下游 DSL 生成使用）
    assert "用户澄清" in (last.get("clarify_suspend") or {}).get("question", "")


@pytest.mark.asyncio
async def test_thread_isolation(tmp_path):
    """两个 thread 各自独立挂起/恢复，互不串线。"""
    g = build_graph(clarify_suspend=True, checkpointer=InMemorySaver())
    cfg1 = {"configurable": {"thread_id": "t1"}}
    cfg2 = {"configurable": {"thread_id": "t2"}}
    i1, _, _ = await _drain(g, {"question": "最近卖得怎么样", "role": "boss",
                                "events": [], "usage": {}}, cfg1)
    i2, _, _ = await _drain(g, {"question": "最近卖得怎么样", "role": "boss",
                                "events": [], "usage": {}}, cfg2)
    assert i1 and i2
    # 恢复 t1 不影响 t2 的挂起状态
    await _drain(g, Command(resume="销售额"), cfg1)
    state2 = await g.aget_state(cfg2)
    assert state2.next                            # t2 仍停在挂起点


@pytest.mark.asyncio
async def test_flag_off_keeps_legacy_clarify(db_path):
    """开关关闭：原行为不变——clarify 直接终结出反问（零变化证明）。"""
    from app.agents.graph import run_ask
    state = await run_ask("最近卖得怎么样", role="boss")
    assert state["answer"]["kind"] == "clarify"


@pytest.mark.asyncio
async def test_api_clarify_pending_and_resume(db_path, monkeypatch):
    """API 端到端：挂起推 clarify_pending → 带 thread_id+resume 恢复出数。"""
    from fastapi.testclient import TestClient

    from app.api.main import app as api_app

    monkeypatch.setattr(get_settings(), "clarify_suspend_enabled", True)
    client = TestClient(api_app)

    # 第一问：挂起
    r1 = client.post("/api/ask", json={"question": "最近卖得怎么样", "thread_id": "t-api"})
    assert r1.status_code == 200
    body1 = r1.text
    assert "clarify_pending" in body1
    assert '"thread_id": "t-api"' in body1.replace('": "', '": "')

    # 恢复：带同 thread_id + resume
    r2 = client.post("/api/ask", json={"question": "我想看销售额", "thread_id": "t-api",
                                       "resume": True})
    assert r2.status_code == 200
    assert '"kind": "answer"' in r2.text or '\\"kind\\": \\"answer\\"' in r2.text
    assert "clarify_pending" not in r2.text.split("final")[0].split("start")[-1]
