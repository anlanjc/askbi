"""端到端（mock 模式）：查询 / 归因 / 澄清 / 拒答 / 权限 / QA修订闭环。"""

import app.llm.mock as mock_mod
from app.agents.graph import run_ask


async def test_simple_query(db_path):
    state = await run_ask("上个月的销售额是多少", role="boss")
    a = state["answer"]
    assert a["kind"] == "answer"
    assert a["table"]["rows"] and a["table"]["rows"][0][0] > 0
    assert a["sql"].lower().startswith("select")
    assert "口径" in a["caliber"]


async def test_drill_down_with_qa(db_path):
    state = await run_ask("为什么上月美妆品类毛利率下滑了", role="boss")
    a = state["answer"]
    assert a["kind"] == "answer"
    assert a["narrative"]
    assert a["qa"]["passed"] is True
    assert a["qa"]["numbers_checked"] > 0
    assert any(b["dimension"] for b in state["facts"].get("维度分解", []))


async def test_clarify_flow(db_path):
    state = await run_ask("最近卖得怎么样", role="boss")
    a = state["answer"]
    assert a["kind"] == "clarify"
    assert a["options"]


async def test_refusal_flow(db_path):
    state = await run_ask("上月客服满意度是多少", role="boss")
    assert state["answer"]["kind"] == "refusal"


async def test_row_level_permission(db_path):
    state = await run_ask("上月东北地区销售额", role="south")
    a = state["answer"]
    # south 角色只可见华南：东北 × 华南 = 空结果（权限外数据不可见，而非报错）
    assert a["kind"] == "answer"
    assert a["data_badge"]["row_count"] == 0


async def test_sloppy_narrative_caught_by_qa(db_path):
    """消融自检：模拟 LLM 编数字 → QA 必须打回，最终带失败徽章降级输出。"""
    mock_mod.SLOPPY = True
    try:
        state = await run_ask("为什么上月美妆品类毛利率下滑了", role="boss")
        a = state["answer"]
        assert a["kind"] == "answer"
        assert a["qa"]["passed"] is False
        assert state["revision_count"] >= 1
    finally:
        mock_mod.SLOPPY = False


async def test_light_route_skips_insight(db_path):
    state = await run_ask("上个月的销售额是多少", role="boss")
    assert state["depth"] == "light"
    assert "insight" not in (state.get("usage") or {})


async def test_full_route_uses_insight(db_path):
    state = await run_ask("为什么上月美妆品类毛利率下滑了", role="boss")
    assert state["depth"] == "full"
    assert "insight" in (state.get("usage") or {})
