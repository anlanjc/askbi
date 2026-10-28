"""E1 判卷测试 —— 改编自 askbi/tests/test_compiler.py（import 指向练习 skeleton）。

验收标准：pytest test_practice.py 全绿（10 passed）。
"""

import pytest
from pydantic import ValidationError

from skeleton import CompileError, Filter, QueryDSL, TimeRange, compile_sql, validate_filter_values


def _dsl(**kw) -> QueryDSL:
    base = dict(intent="aggregate", metric="revenue", dimensions=[],
                filters=[], time=TimeRange(mode="all"))
    base.update(kw)
    return QueryDSL(**base)


# ---- 改编自 tests/test_compiler.py 的核心用例 ----


def test_unknown_metric_rejected():
    with pytest.raises(ValidationError):
        _dsl(metric="satisfaction_score")


def test_unknown_dimension_rejected():
    with pytest.raises(ValidationError):
        _dsl(dimensions=["hack_dim"])


def test_unknown_filter_field_rejected():
    with pytest.raises(ValidationError):
        _dsl(filters=[Filter(field="1=1; DROP TABLE orders", op="=", value="x")])


def test_value_quoting_neutralizes_injection():
    dsl = _dsl(filters=[Filter(field="region", op="=", value="华南' OR '1'='1")])
    sql, _ = compile_sql(dsl)
    # 引号被转义成 ''，条件退化为查一个不存在的区域名 → 安全
    assert "''" in sql


def test_role_scope_injected():
    sql, caliber = compile_sql(_dsl(), role="south")
    assert "o.region IN ('华南')" in sql
    assert "华南" in caliber


def test_limit_clamped():
    sql, _ = compile_sql(_dsl(limit=99999))
    assert "LIMIT 200" in sql


def test_time_last_month():
    sql, _ = compile_sql(_dsl(time=TimeRange(mode="last_n_months", n=1)))
    assert "2026-09" in sql


def test_group_by_and_order():
    sql, caliber = compile_sql(
        _dsl(dimensions=["category"], time=TimeRange(mode="last_n_months", n=1))
    )
    assert "GROUP BY p.category" in sql
    assert "ORDER BY value DESC" in sql
    assert "毛利" not in caliber  # revenue 口径


# ---- 扩展用例（依据 L1 讲义的行为规格，等价于源码行为）----


def test_scalar_op_rejects_list_value():
    with pytest.raises(CompileError):
        compile_sql(_dsl(filters=[Filter(field="region", op="=", value=["华南", "华北"])]))


def test_unknown_region_value_flagged():
    problems = validate_filter_values(
        _dsl(filters=[Filter(field="region", op="=", value="火星")])
    )
    assert problems and "火星" in problems[0]
    assert validate_filter_values(
        _dsl(filters=[Filter(field="region", op="=", value="华南")])
    ) == []
