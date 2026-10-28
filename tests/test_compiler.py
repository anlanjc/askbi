"""DSL 编译器测试：白名单、注入转义、权限注入、LIMIT 钳制。"""

import pytest
from pydantic import ValidationError

from app.data.dsl import Filter, QueryDSL, TimeRange, compile_sql


def _dsl(**kw) -> QueryDSL:
    base = dict(intent="aggregate", metric="revenue", dimensions=[], filters=[],
                time=TimeRange(mode="all"))
    base.update(kw)
    return QueryDSL(**base)


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
