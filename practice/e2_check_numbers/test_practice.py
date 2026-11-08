"""E2 判卷测试 —— 改编自 askbi/tests/test_qa_check.py（import 指向练习 skeleton）+ 豁免规则扩展。

验收标准：pytest test_practice.py 全绿（7 passed）。
"""

from skeleton import check_numbers


def _ctx():
    result = {"rows": [["美妆", 12345.67], ["服饰", 8888.88]], "row_count": 2}
    facts = {"变化率": -0.0523, "当前值": 12345.67, "上期值": 13027.55,
             "维度分解": [{"dimension": "category", "top_movers": [
                 {"member": "美妆", "上期": 100.0, "本期": 90.0, "变化": -10.0}]}]}
    return result, facts


# ---- 改编自 tests/test_qa_check.py 的核心用例 ----


def test_pass_when_numbers_from_result():
    result, facts = _ctx()
    narrative = "美妆当前值 12345.67，上期 13027.55，变化率 -5.2%。"
    issues, checked = check_numbers(narrative, result, facts)
    assert checked >= 3
    assert issues == []


def test_fail_on_invented_number():
    result, facts = _ctx()
    narrative = "美妆销售额高达 999999 元。"
    issues, _checked = check_numbers(narrative, result, facts)
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


# ---- 扩展用例（依据 L3 讲义第 2 节：日期/时长豁免与分域拦截）----


def test_date_and_duration_exempt():
    """时间坐标与量词不参与核验：2026-09 / 近12个月 不应误报。"""
    result, facts = _ctx()
    narrative = "2026-09 近12个月，美妆当前值 12345.67。"
    issues, checked = check_numbers(narrative, result, facts)
    assert issues == []
    assert checked == 1  # 只有 12345.67 被核验


def test_percent_domain_rejects_cross_domain_value():
    """分域：编造的百分比不能借原值域蒙混（0.0523×100 才是合法百分比）。"""
    result, facts = _ctx()
    narrative = "退货率高达 8.88%。"
    issues, _ = check_numbers(narrative, result, facts)
    assert issues and "8.88" in issues[0]


def test_invented_percent_still_caught():
    """百分比域内编造同样拦截。"""
    result, facts = _ctx()
    narrative = "毛利率飙升 42.7%。"
    issues, _ = check_numbers(narrative, result, facts)
    assert issues and "42.7" in issues[0]
