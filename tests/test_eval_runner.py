"""AB-HR4 评测驱动修复的回归：judge_item 判分类型安全 + router 归因语义。

背景（eval/dataset_v2_140.json 扩容期抓出）：
1. drill 判分裸 and 链——light 链路的空叙述答案使 ok 短路成 ""（str），
   聚合处 int+str 崩溃（此前冻结集未触发属侥幸：116 题恰无 light 路由的 drill）；
2. router heavy 正则漏「归因」——route_reason 文案宣称归因语义走 full，
   「归因一下…」却误入 light（冻结 v2-089 靠「各」字侥幸 heavy）。
"""

from app.agents.nodes import route_node
from app.eval.runner import judge_item

# ---- judge_item：返回类型必须恒为 (bool, str) ----

def test_judge_drill_empty_narrative_is_false_not_str():
    """light 链路答案：kind=answer 但 narrative=""、qa 缺失——曾短路成 "" 并崩聚合。"""
    ok, detail = judge_item("drill", {"kind": "answer", "narrative": "", "qa": None}, None)
    assert ok is False
    assert isinstance(ok, bool)
    assert "归因链路" in detail


def test_judge_drill_healthy_answer_passes():
    ans = {"kind": "answer", "narrative": "客单价为 128.5 元（口径）。", "qa": {"passed": True}}
    ok, detail = judge_item("drill", ans, None)
    assert ok is True and detail == ""


def test_judge_drill_qa_failed_is_false():
    ans = {"kind": "answer", "narrative": "叙述", "qa": {"passed": False}}
    ok, _ = judge_item("drill", ans, None)
    assert ok is False


def test_judge_refusal_and_clarify_kind_match():
    ok_r, _ = judge_item("refusal", {"kind": "refusal", "message": "x"}, None)
    ok_c, detail_c = judge_item("clarify", {"kind": "refusal", "message": "x"}, None)
    assert ok_r is True
    assert ok_c is False and "期望澄清" in detail_c


# ---- router：「归因」语义必须路由 full ----

def test_router_attribution_word_routes_full():
    """「归因一下…」曾漏判 light（正则只有「原因/为何/为什么」），跳过叙述与质检。"""
    st = route_node({"question": "归因一下上月美妆品类的客单价变化"})
    assert st["depth"] == "full"


def test_router_simple_aggregate_routes_light():
    st = route_node({"question": "上月销售额是多少"})
    assert st["depth"] == "light"
