"""QA 数值核验（确定性部分）单元测试。"""

from app.agents.nodes import check_numbers


def _ctx():
    result = {"rows": [["美妆", 12345.67], ["服饰", 8888.88]], "row_count": 2}
    facts = {"变化率": -0.0523, "当前值": 12345.67, "上期值": 13027.55,
             "维度分解": [{"dimension": "category", "top_movers": [
                 {"member": "美妆", "上期": 100.0, "本期": 90.0, "变化": -10.0}]}]}
    return result, facts


def test_pass_when_numbers_from_result():
    result, facts = _ctx()
    narrative = "美妆当前值 12345.67，上期 13027.55，变化率 -5.2%。"
    issues, checked = check_numbers(narrative, result, facts)
    assert checked >= 3
    assert issues == []


def test_fail_on_invented_number():
    result, facts = _ctx()
    narrative = "美妆销售额高达 999999 元。"
    issues, checked = check_numbers(narrative, result, facts)
    assert issues and "999999" in issues[0]


def test_percent_conversion_allowed():
    result, facts = _ctx()
    narrative = "变化率为 -5.23%。"
    issues, _ = check_numbers(narrative, result, facts)
    assert issues == []


def test_thousands_separator_allowed():
    result, facts = _ctx()
    narrative = "当前值为 12,345.67。"
    issues, _ = check_numbers(narrative, result, facts)
    assert issues == []
