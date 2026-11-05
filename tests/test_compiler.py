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


# ---- ASK-1 红队回归（评审 2.0 P0）：TimeRange 曾直拼 SQL，可注释行级权限/读任意文件 ----

def test_time_start_injection_rejected():
    """start 夹带 SQL 注入串必须被 YYYY-MM 白名单拒绝（曾可 ' OR 1=1 -- 注释权限行）。"""
    with pytest.raises(ValidationError):
        _dsl(time=TimeRange(mode="between", start="x' OR 1=1 --"))


def test_time_start_file_read_rejected():
    """start 夹带 UNION 读文件载荷必须被拒绝（曾可借表函数读 .env）。"""
    with pytest.raises(ValidationError):
        _dsl(time=TimeRange(mode="between", start="2000-01' UNION ALL SELECT 1 --"))


def test_time_path_traversal_rejected():
    with pytest.raises(ValidationError):
        _dsl(time=TimeRange(mode="between", start="../.env"))


def test_time_non_positive_n_rejected():
    with pytest.raises(ValidationError):
        _dsl(time=TimeRange(mode="last_n_months", n=0))
    with pytest.raises(ValidationError):
        _dsl(time=TimeRange(mode="last_n_months", n=-3))


def test_time_valid_between_compiles_quoted():
    """合法值放行，且经 _q 统一加引号（纵深防御，不裸插）。"""
    sql, _ = compile_sql(_dsl(time=TimeRange(mode="between", start="2026-01", end="2026-03")))
    assert ">= '2026-01'" in sql
    assert "<= '2026-03'" in sql
