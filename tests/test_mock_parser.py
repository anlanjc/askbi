"""mock 规则解析器测试（也是 mock 评测集正确性的前置保证）。"""

from app.llm.mock import _clarifier, _parse_question


def test_parse_simple_aggregate():
    d = _parse_question("上个月的销售额是多少")
    assert d["metric"] == "revenue"
    assert d["time"]["mode"] == "last_n_months" and d["time"]["n"] == 1
    assert d["dimensions"] == []


def test_parse_topn_lowest_margin():
    d = _parse_question("上月哪个品类毛利率最低")
    assert d["metric"] == "gross_margin_rate"
    assert d["dimensions"] == ["category"]
    assert d["order_by"]["direction"] == "asc"


def test_parse_filter_from_value_prefix():
    d = _parse_question("美妆品类近6个月每月毛利额")
    assert {"field": "category", "op": "=", "value": "美妆"} in d["filters"]
    assert "category" not in d["dimensions"]      # 值成为过滤而非分组
    assert d["dimensions"] == ["month"]           # 每月 → 趋势


def test_parse_unknown_region_kept_for_refusal():
    d = _parse_question("上季度火星区域的销售额")
    assert {"field": "region", "op": "=", "value": "火星"} in d["filters"]


def test_parse_member_level_value():
    d = _parse_question("黑卡会员上月客单价")
    assert d["metric"] == "avg_order_value"
    assert {"field": "member_level", "op": "=", "value": "黑卡"} in d["filters"]


def test_parse_limit_top3():
    d = _parse_question("上月销售额前3的城市")
    assert d["limit"] == 3
    assert d["dimensions"] == ["city"]


def test_clarify_vague_metric():
    v = _clarifier({"question": "最近卖得怎么样"})
    assert v["verdict"] == "clarify"


def test_clarify_missing_metric():
    v = _clarifier({"question": "上月各渠道表现如何"})
    assert v["verdict"] == "clarify"


def test_refuse_prediction():
    v = _clarifier({"question": "预测下个月哪个品类销售额最高"})
    assert v["verdict"] == "refuse"


def test_refuse_unknown_metric():
    v = _clarifier({"question": "上月客服满意度是多少"})
    assert v["verdict"] == "refuse"


def test_clear_proceeds():
    v = _clarifier({"question": "上个月的销售额是多少"})
    assert v["verdict"] == "clear"
